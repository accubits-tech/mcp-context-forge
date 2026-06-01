# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/mcp_native.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

MCP-native ingress adapter - the persistent upstream session manager.

MCP is "just another provider" (FRD §4.7): instead of a config-driven webhook,
the gateway holds a **persistent** upstream ``ClientSession`` open, issues
``resources/subscribe``, and reads the server-initiated
``notifications/resources/updated`` it pushes (FRD §4.6 / §6 / §8.2 / FR-32).
Today federation opens that session with a **one-shot** ``async with``
(``gateway_service.connect_to_sse_server`` / ``connect_to_streamablehttp_server``)
that tears the session down right after ``initialize()`` + ``list_*()`` - so
those notifications are silently **dropped**. :class:`McpNativeSessionManager`
is the fix: it keeps the session open with a ``message_handler`` and re-emits
each received notification into the **same** normalize -> dedup -> persist ->
publish tail the webhook ingress uses
(:func:`mcpgateway.services.events.emit.publish_normalized_event`).

The flow for a ``notifications/resources/updated`` (FRD §4.6, §5.2, FR-11a):

1. Allocate a deterministic per-``(source, subject)`` relay sequence ``seq`` (or
   use a transport event-id when one is available).
2. ``evt_id = synthesize_mcp_event_id(gateway_id, source, type, subject, seq)``.
   A replayed notification reuses its ``seq`` -> same ``evt_id`` -> deduped.
3. ``resources/read`` the ``uri`` to fetch the current content for ``data``.
4. Build an :class:`~mcpgateway.schemas.EventEnvelope` with the canonical
   reverse-DNS type ``com.mcp.resource.updated`` and ``subject = uri``.
5. ``await publish_normalized_event(db, gateway=..., envelope=...)`` over a fresh
   ``SessionLocal`` unit-of-work.

``notifications/*/list_changed`` re-fetches the affected primitive list (the
durable store of record is the DB rows, not an in-process list cache) and, for
resources, clears the content cache; it emits **no** domain event.
``notifications/message`` is routed to logging only.

Subscriptions are **refcounted** in ``gateway.hook_state`` (mirroring the
provisioner's edge-triggered semantics): the upstream ``resources/subscribe`` is
issued only on the ``0 -> 1`` transition and ``resources/unsubscribe`` only on
``1 -> 0``. :meth:`reconnect` re-opens the session on a ``404`` / stream break
and **re-issues** ``resources/subscribe`` for every active uri so no live update
is silently lost (TC-MCP-004). When the upstream does not advertise
``capabilities.resources.subscribe`` the manager tracks the uri but does not
call ``resources/subscribe`` (capability fallback - TC-MCP-007/008, best-effort
for M6).

The transport (``sse`` vs ``streamable_http``) and auth are selected exactly as
federation does it; for testability a ``client_factory`` and ``session_factory``
are injectable so the unit tests drive the manager without a real transport.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# Third-Party
import mcp.types as mcp_types
from pydantic import AnyUrl

# First-Party
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events.emit import publish_normalized_event, synthesize_mcp_event_id
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = ["McpNativeSessionManager", "detect_mcp_native_capability"]

# The canonical reverse-DNS envelope type for an MCP resources/updated relay
# (FRD §4.6 table / §5.2). Singular "resource" - matches emit.py and the FRD.
RESOURCE_UPDATED_TYPE = "com.mcp.resource.updated"


def detect_mcp_native_capability(capabilities: Any) -> dict:
    """Mark a connector MCP-native when the upstream advertises live updates.

    The low-risk, **pure** capability-detection helper (M6, FRD §4.7 / FR-32).
    It inspects the upstream-negotiated ``capabilities`` dict and, when the
    server advertises either ``resources.subscribe`` (it can push
    ``notifications/resources/updated``) or ``tools.webhooksSupported``, returns
    a **copy** of the capabilities with the ``events`` block marked so the
    lifespan startup recognizes the connector::

        capabilities["events"]["ingress_mode"]      = "mcp_native"
        capabilities["events"]["webhooksSupported"] = True

    This is intentionally additive and side-effect-free: the input dict is never
    mutated in place and any pre-existing ``events`` sub-keys are preserved, so a
    caller can fold it into the gateway-init path without disturbing existing
    capability negotiation. When the upstream advertises neither signal, the
    input is returned unchanged (a no-op), so the gateway-init hot path keeps its
    current behavior for non-MCP-native servers.

    Args:
        capabilities: The upstream-negotiated capabilities mapping (the value
            stored on ``gateway.capabilities``). A ``None`` / non-dict value is
            tolerated and yields an empty dict.

    Returns:
        dict: A capabilities dict with the ``events`` marker added when the
        connector is MCP-native, else the (copied) input unchanged.

    Examples:
        >>> detect_mcp_native_capability({"resources": {"subscribe": True}})["events"]["ingress_mode"]
        'mcp_native'
        >>> detect_mcp_native_capability({"resources": {"subscribe": False}})
        {'resources': {'subscribe': False}}
        >>> detect_mcp_native_capability(None)
        {}
    """
    if not isinstance(capabilities, dict):
        return {}

    resources = capabilities.get("resources")
    tools = capabilities.get("tools")
    supports_subscribe = isinstance(resources, dict) and bool(resources.get("subscribe"))
    supports_webhooks = isinstance(tools, dict) and bool(tools.get("webhooksSupported"))

    if not (supports_subscribe or supports_webhooks):
        # Return a shallow copy so callers may treat the result as freely
        # owned without ever mutating the input.
        return dict(capabilities)

    # Build a copy with the events marker added; preserve any existing events
    # sub-keys (do not mutate the input or its nested events dict).
    updated = dict(capabilities)
    existing_events = updated.get("events")
    events = dict(existing_events) if isinstance(existing_events, dict) else {}
    events["ingress_mode"] = "mcp_native"
    events["webhooksSupported"] = True
    updated["events"] = events
    return updated


class McpNativeSessionManager:
    """Hold a persistent upstream MCP session and relay its notifications as events.

    One manager instance owns one upstream connector (:class:`~mcpgateway.db.Gateway`)
    session. It is driven by :meth:`start` / :meth:`stop` (lifespan-wired,
    flag-gated) and exposes :meth:`subscribe_resource` / :meth:`unsubscribe_resource`
    for the subscription lifecycle. The ``message_handler`` (:meth:`_on_message`)
    is the seam every received ``ServerNotification`` flows through.

    Attributes:
        gateway: The connector row whose upstream session is held open.
    """

    def __init__(
        self,
        *,
        gateway: Any,
        session_factory: Optional[Callable[[], Any]] = None,
        client_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """Initialize the manager.

        Args:
            gateway: The connector (:class:`~mcpgateway.db.Gateway`) row. Its
                ``id`` scopes the synthesized event source/dedup and its
                ``transport``/auth drive the default upstream connection.
            session_factory: Zero-arg factory returning a fresh SQLAlchemy
                ``Session`` per unit-of-work. Defaults to
                :data:`mcpgateway.db.SessionLocal` (resolved lazily so importing
                this module never requires a configured DB).
            client_factory: Async factory ``async (gateway) -> ClientSession``
                that opens a *persistent* upstream session. Injectable for tests;
                defaults to :meth:`_default_client_factory` (the SSE /
                streamable-HTTP federation transport with ``message_handler``).
        """
        self.gateway = gateway
        self._session_factory = session_factory
        self._client_factory = client_factory or self._default_client_factory

        self._client: Any = None
        self._running: bool = False

        # Refcounts per resource uri (mirrors provisioner edge-triggering). The
        # uri set is the source of truth for what reconnect() must re-subscribe.
        self._refcounts: Dict[str, int] = {}
        # Per-(source, subject) monotonic relay sequence for synthesized ids.
        self._seqs: Dict[str, int] = {}
        # Whether the upstream advertised resources.subscribe at initialize().
        self._supports_subscribe: bool = True
        self._lock = asyncio.Lock()
        # The transport+session exit stack the default factory opens (so the
        # persistent transport is unwound on stop/reconnect, not leaked).
        self._exit_stack: Any = None

    # ------------------------------------------------------------------ #
    # Identity helpers                                                    #
    # ------------------------------------------------------------------ #

    @property
    def _gateway_id(self) -> str:
        """Return the owning gateway id.

        Returns:
            str: The connector id.
        """
        return getattr(self.gateway, "id", None)

    @property
    def _source(self) -> str:
        """Return the connection-scoped event source (``"//<conn-id>"``).

        The same convention the webhook ingress uses (FRD §6.7 / FR-23) so dedup
        scoping is identical across both ingress paths.

        Returns:
            str: The connection-scoped source.
        """
        return f"//{self._gateway_id}"

    def _session(self) -> Any:
        """Open a fresh SQLAlchemy session for one unit-of-work.

        Returns:
            Any: A new ``Session`` from the injected factory or ``SessionLocal``.
        """
        factory = self._session_factory
        if factory is None:
            # First-Party
            from mcpgateway.db import SessionLocal  # pylint: disable=import-outside-toplevel

            factory = SessionLocal
        return factory()

    # ------------------------------------------------------------------ #
    # Introspection (used by lifespan wiring + tests)                     #
    # ------------------------------------------------------------------ #

    def is_running(self) -> bool:
        """Report whether a persistent upstream session is currently held open.

        Returns:
            bool: ``True`` between :meth:`start` and :meth:`stop`.
        """
        return self._running and self._client is not None

    def active_uris(self) -> List[str]:
        """Return the resource uris with a live (refcount > 0) subscription.

        Returns:
            List[str]: The active uris (the set reconnect() re-subscribes).
        """
        return [uri for uri, count in self._refcounts.items() if count > 0]

    def supports_subscribe(self) -> bool:
        """Report whether the upstream advertised ``resources.subscribe``.

        Returns:
            bool: ``True`` when the upstream capability is present; ``False``
            triggers the listChanged/polling fallback (no ``resources/subscribe``
            is sent - TC-MCP-007/008).
        """
        return self._supports_subscribe

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Open the persistent upstream session, initialize, and (re)subscribe.

        Opens the session via the (injectable) client factory with
        ``message_handler=self._on_message`` (so notifications are NOT dropped -
        the regression this milestone fixes, FR-32 / TC-MCP-025), reads the
        upstream capabilities to decide whether ``resources/subscribe`` is
        supported, lists the primitives once to reconcile the durable store, and
        re-issues ``resources/subscribe`` for every already-active uri (so a
        restart does not orphan subscriptions - TC-MCP-022). The session is then
        **held open** (not torn down).
        """
        if self._running:
            return
        self._client = await self._client_factory(self.gateway)
        await self._initialize_and_list()
        # Re-subscribe every active uri on the (possibly fresh) session so a
        # start after a stop/loss re-establishes upstream subscriptions.
        for uri in list(self.active_uris()):
            await self._upstream_subscribe(uri)
        self._running = True

    async def stop(self) -> None:
        """Tear down the persistent upstream session (retaining the uri set).

        The active-uri / refcount bookkeeping is intentionally **kept** so a
        later :meth:`start` (or a worker that picks the connector back up)
        re-subscribes rather than orphaning the subscriptions (TC-MCP-022).
        """
        client = self._client
        self._client = None
        self._running = False
        if client is not None:
            await self._close_client(client)

    async def reconnect(self) -> None:
        """Re-open the session on a ``404`` / stream break and re-subscribe all uris.

        Closes the broken session, opens a fresh one, re-initializes, re-lists to
        reconcile, and re-issues ``resources/subscribe`` for **every** active uri
        so no live update is silently dropped (TC-MCP-004 / FR-32). Idempotent
        with respect to the active-uri set.
        """
        old = self._client
        self._client = None
        if old is not None:
            await self._close_client(old)

        self._client = await self._client_factory(self.gateway)
        await self._initialize_and_list()
        for uri in list(self.active_uris()):
            await self._upstream_subscribe(uri)
        self._running = True

    async def _initialize_and_list(self) -> None:
        """Initialize the session, detect subscribe capability, and reconcile lists.

        Reads the upstream ``capabilities.resources.subscribe`` to set the
        fallback flag, then re-lists resources/tools/prompts so the durable DB
        rows are reconciled (best-effort; a list error never aborts the session).
        """
        result = await self._client.initialize()
        self._supports_subscribe = self._read_subscribe_capability(result)
        await self._relist_resources()
        await self._relist_tools()
        await self._relist_prompts()

    @staticmethod
    def _read_subscribe_capability(init_result: Any) -> bool:
        """Extract ``capabilities.resources.subscribe`` from an initialize result.

        Args:
            init_result: The upstream ``InitializeResult`` (or any object/dict
                shaped like one).

        Returns:
            bool: ``True`` when the upstream advertises ``resources.subscribe``;
            defaults to ``True`` when the field is absent so a server that simply
            omits the (non-required) flag is still subscribed to (the upstream
            rejects the call if it truly cannot honor it).
        """
        caps = getattr(init_result, "capabilities", None)
        resources = getattr(caps, "resources", None) if caps is not None else None
        if resources is None:
            return True
        subscribe = getattr(resources, "subscribe", None)
        return True if subscribe is None else bool(subscribe)

    # ------------------------------------------------------------------ #
    # Subscription lifecycle (refcounted)                                 #
    # ------------------------------------------------------------------ #

    async def subscribe_resource(self, uri: str) -> None:
        """Add a refcounted subscription to *uri*, subscribing upstream on 0->1.

        Args:
            uri: The resource uri to subscribe to.
        """
        uri = str(uri)
        async with self._lock:
            count = self._refcounts.get(uri, 0)
            self._refcounts[uri] = count + 1
            first_ref = count == 0
        if first_ref:
            await self._upstream_subscribe(uri)

    async def unsubscribe_resource(self, uri: str) -> None:
        """Release one reference to *uri*, unsubscribing upstream on 1->0.

        Idempotent: releasing an unknown / already-zero uri is a no-op and never
        drives the refcount negative.

        Args:
            uri: The resource uri to release.
        """
        uri = str(uri)
        async with self._lock:
            count = self._refcounts.get(uri, 0)
            if count <= 0:
                self._refcounts.pop(uri, None)
                return
            if count == 1:
                self._refcounts.pop(uri, None)
                last_ref = True
            else:
                self._refcounts[uri] = count - 1
                last_ref = False
        if last_ref:
            await self._upstream_unsubscribe(uri)

    async def _upstream_subscribe(self, uri: str) -> None:
        """Issue the upstream ``resources/subscribe`` (guarded by capability).

        Args:
            uri: The resource uri to subscribe to upstream.
        """
        if not self._supports_subscribe:
            # Capability fallback: no live updates via subscribe; rely on
            # listChanged / polling. The uri stays tracked (TC-MCP-007/008).
            logger.debug("Upstream %s lacks resources.subscribe; skipping subscribe for %s", self._gateway_id, uri)
            return
        client = self._client
        if client is None:
            return
        await client.subscribe_resource(AnyUrl(uri))

    async def _upstream_unsubscribe(self, uri: str) -> None:
        """Issue the upstream ``resources/unsubscribe`` (guarded by capability).

        Args:
            uri: The resource uri to unsubscribe from upstream.
        """
        if not self._supports_subscribe:
            return
        client = self._client
        if client is None:
            return
        await client.unsubscribe_resource(AnyUrl(uri))

    # ------------------------------------------------------------------ #
    # The message handler                                                 #
    # ------------------------------------------------------------------ #

    async def _on_message(self, message: Any, *, session: Any = None, seq: Optional[int] = None) -> None:
        """Dispatch one inbound ``ClientSession`` message.

        This is the ``message_handler`` passed positionally to the upstream
        ``ClientSession``. The SDK delivers one of three arms (verified against
        ``mcp.client.session``): an :class:`Exception` (transport/decode error),
        a server-initiated ``RequestResponder`` (ignored on the events path), or
        a :class:`mcp.types.ServerNotification` whose ``.root`` is the concrete
        notification. ``resources/updated`` is normalized + published; the
        ``*/list_changed`` family re-fetches the affected list; ``message`` is
        logged.

        Args:
            message: The inbound message (``ServerNotification`` | ``Exception`` |
                ``RequestResponder`` | other).
            session: Optional upstream session override for ``resources/read``
                (used by tests / the E2E to read over a live link). Defaults to
                the held client.
            seq: Optional explicit relay sequence for the synthesized id (a
                replayed notification reuses its seq to collapse). When ``None``,
                a per-``(source, subject)`` monotonic counter is allocated.
        """
        # Arm 1: a transport / decode error - recover by reconnecting (no drop).
        if isinstance(message, Exception):
            logger.warning("MCP-native session error for %s: %s; reconnecting", self._gateway_id, message)
            try:
                await self.reconnect()
            except Exception:  # noqa: BLE001 - reconnect failure must not crash the handler.
                logger.exception("MCP-native reconnect failed for %s", self._gateway_id)
            return

        # Arm 2 + 3: only a ServerNotification carries an inner relayable event.
        if not isinstance(message, mcp_types.ServerNotification):
            # A server-initiated request (RequestResponder) or any other object:
            # not part of the events ingress path - ignore.
            return

        inner = message.root

        if isinstance(inner, mcp_types.ResourceUpdatedNotification):
            await self._handle_resource_updated(inner, session=session, seq=seq)
            return

        if isinstance(inner, mcp_types.ResourceListChangedNotification):
            await self._handle_resources_list_changed()
            return

        if isinstance(inner, mcp_types.ToolListChangedNotification):
            await self._relist_tools()
            return

        if isinstance(inner, mcp_types.PromptListChangedNotification):
            await self._relist_prompts()
            return

        if isinstance(inner, mcp_types.LoggingMessageNotification):
            # Route to logging only; emits no domain event.
            params = getattr(inner, "params", None)
            logger.debug("MCP-native log from %s: %s", self._gateway_id, getattr(params, "data", None))
            return

        # CancelledNotification / ProgressNotification and anything else are not
        # part of the events ingress taxonomy - ignore.

    async def _handle_resource_updated(self, inner: Any, *, session: Any, seq: Optional[int]) -> None:
        """Normalize + publish one ``notifications/resources/updated``.

        Args:
            inner: The :class:`mcp.types.ResourceUpdatedNotification`.
            session: Optional upstream session override for ``resources/read``.
            seq: Optional explicit relay sequence (else allocate monotonically).
        """
        uri = str(inner.params.uri)
        source = self._source
        relay_seq = seq if seq is not None else self._next_seq(uri)
        evt_id = synthesize_mcp_event_id(
            gateway_id=self._gateway_id,
            source=source,
            type=RESOURCE_UPDATED_TYPE,
            subject=uri,
            seq=relay_seq,
        )

        data = await self._read_resource(uri, session=session)

        envelope = EventEnvelope(
            id=evt_id,
            source=source,
            type=RESOURCE_UPDATED_TYPE,
            subject=uri,
            time=datetime.now(timezone.utc),
            data=data,
        )

        db = self._session()
        try:
            await publish_normalized_event(db, gateway=self.gateway, envelope=envelope)
        finally:
            db.close()

    def _next_seq(self, subject: str) -> int:
        """Allocate the next per-``(source, subject)`` monotonic relay sequence.

        Args:
            subject: The resource uri (subject).

        Returns:
            int: The next sequence number for this subject (starts at 1).
        """
        key = f"{self._source}\x00{subject}"
        nxt = self._seqs.get(key, 0) + 1
        self._seqs[key] = nxt
        return nxt

    async def _read_resource(self, uri: str, *, session: Any) -> Any:
        """Fetch the resource content for the envelope ``data`` (best-effort).

        Args:
            uri: The resource uri to read.
            session: Optional session override; defaults to the held client.

        Returns:
            Any: A JSON-serializable dict of the read result, or ``None`` when no
            session is available or the read fails (the event still publishes -
            ``content_hash`` empty per FRD §4.5.1).
        """
        client = session if session is not None else self._client
        if client is None:
            return None
        try:
            result = await client.read_resource(AnyUrl(uri))
        except Exception:  # noqa: BLE001 - a read failure must not drop the event.
            logger.exception("MCP-native resources/read failed for %s (%s)", uri, self._gateway_id)
            return None
        return self._dump(result)

    @staticmethod
    def _dump(result: Any) -> Any:
        """Render an MCP result object as a JSON-serializable dict.

        Args:
            result: A pydantic model (e.g. ``ReadResourceResult``) or a plain
                value.

        Returns:
            Any: ``result.model_dump(mode="json")`` for a pydantic model, else
            the value unchanged.
        """
        dump = getattr(result, "model_dump", None)
        if callable(dump):
            return dump(mode="json", exclude_none=True)
        return result

    # ------------------------------------------------------------------ #
    # list_changed reconciliation                                         #
    # ------------------------------------------------------------------ #

    async def _handle_resources_list_changed(self) -> None:
        """Reconcile on ``resources/list_changed``: clear content cache + re-list.

        There is no in-process per-gateway resource *list* cache to invalidate -
        the durable store of record is the DB ``resources`` rows - so the
        reconciliation is a re-list. The global resource *content* cache (keyed
        ``"resource_list"`` + per-resource ids) is cleared so the next read
        repopulates. No domain event is emitted.
        """
        await self._invalidate_resource_cache()
        await self._relist_resources()

    async def _invalidate_resource_cache(self) -> None:
        """Clear the global resource content cache (best-effort).

        Imported lazily + guarded so this module never hard-depends on ``main``
        (which would create an import cycle) and a missing/failed cache call
        never aborts reconciliation.
        """
        try:
            # First-Party
            from mcpgateway.main import invalidate_resource_cache  # pylint: disable=import-outside-toplevel,cyclic-import

            await invalidate_resource_cache()
        except Exception:  # noqa: BLE001 - cache invalidation is best-effort.
            logger.debug("MCP-native resource cache invalidation skipped for %s", self._gateway_id)

    async def _relist_resources(self) -> None:
        """Re-fetch the upstream resource list (best-effort reconcile)."""
        await self._safe_list("list_resources")

    async def _relist_tools(self) -> None:
        """Re-fetch the upstream tool list (best-effort reconcile)."""
        await self._safe_list("list_tools")

    async def _relist_prompts(self) -> None:
        """Re-fetch the upstream prompt list (best-effort reconcile)."""
        await self._safe_list("list_prompts")

    async def _safe_list(self, method: str) -> None:
        """Call an upstream ``list_*`` method, swallowing transport errors.

        Args:
            method: The ``ClientSession`` method name to call (no-arg overload).
        """
        client = self._client
        if client is None:
            return
        fn = getattr(client, method, None)
        if fn is None:
            return
        try:
            await fn()
        except Exception:  # noqa: BLE001 - a list error must not abort the session.
            logger.debug("MCP-native %s failed for %s", method, self._gateway_id)

    # ------------------------------------------------------------------ #
    # Transport (federation-parity) connection factory                   #
    # ------------------------------------------------------------------ #

    async def _close_client(self, client: Any) -> None:
        """Close an upstream client session, tolerating either close API.

        When the default factory opened a transport+session :class:`AsyncExitStack`,
        unwinding it closes BOTH the session and the underlying transport (so the
        persistent connection is not leaked on stop/reconnect). Otherwise an
        injected session is closed via its own ``aclose``/``close``/``__aexit__``.

        Args:
            client: The session to close.
        """
        stack = self._exit_stack
        if stack is not None:
            self._exit_stack = None
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - a transport close error must not crash teardown.
                logger.debug("MCP-native exit-stack close failed for %s", self._gateway_id)
            return

        for attr in ("aclose", "close", "__aexit__"):
            fn = getattr(client, attr, None)
            if fn is None:
                continue
            try:
                if attr == "__aexit__":
                    await fn(None, None, None)
                else:
                    await fn()
            except Exception:  # noqa: BLE001 - a close error must not crash teardown.
                logger.debug("MCP-native session close via %s failed for %s", attr, self._gateway_id)
            return

    async def _default_client_factory(self, gateway: Any) -> Any:  # pragma: no cover - exercised via lifespan/integration, not unit tests
        """Open a persistent upstream ``ClientSession`` (federation parity).

        Selects the transport (``sse`` vs ``streamable_http``) from
        ``gateway.transport`` and assembles auth headers exactly as
        ``GatewayService`` does, then constructs a long-lived ``ClientSession``
        wired to ``message_handler=self._on_message`` and returns it after
        ``initialize()`` (the caller in :meth:`start`/:meth:`reconnect` performs
        initialize + list). The underlying transport context managers are kept
        alive on the manager so the session stays open (NOT one-shot).

        Args:
            gateway: The connector row to connect to.

        Returns:
            Any: A connected, persistent ``ClientSession``.

        Raises:
            RuntimeError: When the upstream transport could not be opened.
        """
        # Third-Party
        from mcp import ClientSession  # pylint: disable=import-outside-toplevel
        from mcp.client.sse import sse_client  # pylint: disable=import-outside-toplevel
        from mcp.client.streamable_http import streamablehttp_client  # pylint: disable=import-outside-toplevel

        # First-Party
        from mcpgateway.utils.services_auth import decode_auth  # pylint: disable=import-outside-toplevel

        url = getattr(gateway, "url", None)
        transport = (getattr(gateway, "transport", None) or "SSE").lower()

        headers: Dict[str, str] = {}
        auth_value = getattr(gateway, "auth_value", None)
        if isinstance(auth_value, str) and auth_value:
            headers = {str(k): str(v) for k, v in (decode_auth(auth_value) or {}).items()}
        auth_headers = getattr(gateway, "auth_headers", None)
        if auth_headers:
            headers.update({h["key"]: h["value"] for h in auth_headers if h.get("key")})

        # Keep the transport + session context managers alive on the manager so
        # the session is persistent (the federation one-shot drops them here).
        # Standard
        from contextlib import AsyncExitStack  # pylint: disable=import-outside-toplevel

        stack = AsyncExitStack()
        if transport == "sse":
            streams = await stack.enter_async_context(sse_client(url=url, headers=headers))
            read_stream, write_stream = streams[0], streams[1]
        else:
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(streamablehttp_client(url=url, headers=headers))

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream, message_handler=self._on_message))
        # Remember the stack so stop()/reconnect() can unwind the transport.
        self._exit_stack = stack
        return session
