# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/ingress_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Config-driven webhook ingress for MCP server-initiated events.

This service is the heart of the single generic ingress route
``POST /webhooks/{conn-id}`` (FRD §6.2/§6.7). It composes the four leaf modules
of the events package - descriptors, verify, envelope, bus - into one ordered
pipeline that turns an inbound provider POST into a verified, normalized,
deduplicated, persisted, and published event.

The pipeline order is security-load-bearing and follows the FRD §6.7/§6.7.1 and
§8.2 reconciled order, plus the M1 scenario gate (SC-SEC-008/009/010,
SC-ING-016/017/020):

1. **Flag gate** - if the events master switch is off, return ``404`` so the
   route is indistinguishable from an unmounted path.
2. **Resolve the connection** - look up the :class:`Gateway` by ``conn-id`` and
   read ``capabilities.events.ingress.descriptor_ref``. An unknown connection,
   a connection that does not declare an ingress descriptor, or an unknown
   descriptor all collapse to the SAME ``401`` an attacker would see for a bad
   signature (no existence oracle, SC-SEC-010 / FR-7a).
3. **Verify the signature FIRST** - over the exact raw bytes, using the secret
   decrypted from the connection's dedicated ``webhook_signing_secret`` column
   (current secret plus an optional rotation ``secret_prev``). A ``none``
   recipe is only accepted when the descriptor explicitly opts in via
   ``allow_unsigned`` (SC-SEC-001). Any failure returns ``401`` with no
   side effect (SC-SEC-008).
4. **Parse the body** - only after a valid signature; malformed JSON yields
   ``400`` (never before verify, so a parse error cannot leak existence -
   SC-ING-044).
5. **Handshake** - a Slack-style ``url_verification`` challenge is echoed with
   ``200`` only on an already-verified, attributed request (SC-SEC-009 /
   SC-ING-016). It is never reachable before verify, so it is not an
   enumeration oracle.
6. **No-op** - a provider liveness probe (e.g. GitHub ``ping``) is acknowledged
   with ``202`` and emits no domain event (SC-ING-017).
7. **Normalize + dedup + publish** - build the canonical event envelope, drop
   duplicates by ``event.id`` via the in-process TTL cache backed by the
   ``event_log`` ``(evt_source, evt_id)`` unique constraint, persist the row,
   and fan the event out onto the in-process bus. Duplicates are dropped
   silently with ``202`` (SC-ING-020 / FR-23).

The connection-scoped event source is ``"//<conn-id>"`` so the same provider
``event.id`` delivered to two different connections is not treated as a
duplicate (FR-23).
"""

# Future
from __future__ import annotations

# Standard
from dataclasses import dataclass
import json
import time
from typing import Any, List, Mapping, Optional

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Gateway as DbGateway
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events.bus import TTLDedupCache
from mcpgateway.services.events.descriptors import get_descriptor, ProviderDescriptor
from mcpgateway.services.events.emit import publish_normalized_event
from mcpgateway.services.events.envelope import build_envelope, resolve
from mcpgateway.services.events.verify import verify_signature
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.services_auth import decode_auth

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = ["IngressResult", "IngressService"]


@dataclass
class IngressResult:
    """Outcome of an ingress attempt, mapped to an HTTP response by the router.

    Attributes:
        status: The HTTP status code to return (``404``/``401``/``400``/``200``/
            ``202``).
        body: The response body. ``None`` for an empty body; a raw string for a
            handshake echo (e.g. the Slack ``challenge``); otherwise router-shaped.
        deduped: ``True`` when the event was recognized as a duplicate and
            dropped without publishing (still a ``202``).
        envelope: The normalized event envelope on the accept path, else ``None``
            (handshake/no-op/rejected requests carry no envelope).
    """

    status: int
    body: Any = None
    deduped: bool = False
    envelope: Optional[EventEnvelope] = None


# A single shared dedup cache for the process. The TTL mirrors the configured
# dedup window; the DB unique constraint is the durable backstop.
_DEDUP_CACHE: Optional[TTLDedupCache] = None


def _dedup_cache() -> TTLDedupCache:
    """Return the process-wide TTL dedup cache, created on first use.

    Returns:
        TTLDedupCache: The shared dedup cache sized to the configured window.
    """
    global _DEDUP_CACHE  # pylint: disable=global-statement
    if _DEDUP_CACHE is None:
        _DEDUP_CACHE = TTLDedupCache(ttl_seconds=settings.mcpgateway_events_dedup_ttl_seconds)
    return _DEDUP_CACHE


class IngressService:
    """Verify, normalize, dedup, persist, and publish inbound webhook events."""

    def __init__(self) -> None:
        """Initialize the service (stateless; shares the process dedup cache + bus)."""

    @staticmethod
    def _descriptor_ref(gateway: DbGateway) -> Optional[str]:
        """Read the ingress descriptor reference from a gateway's capabilities.

        Args:
            gateway: The resolved :class:`Gateway` row.

        Returns:
            Optional[str]: The descriptor reference, or ``None`` when the
            connection does not declare an events ingress descriptor.
        """
        caps = getattr(gateway, "capabilities", None) or {}
        if not isinstance(caps, Mapping):
            return None
        events = caps.get("events")
        if not isinstance(events, Mapping):
            return None
        ingress = events.get("ingress")
        if not isinstance(ingress, Mapping):
            return None
        ref = ingress.get("descriptor_ref")
        return str(ref) if ref else None

    @staticmethod
    def _secrets(gateway: DbGateway) -> List[str]:
        """Decrypt the connection's signing secret(s), current first.

        The secret material is stored AES-GCM-encrypted in the dedicated
        ``webhook_signing_secret`` column as ``{"secret": ..., "secret_prev":
        ...}`` (the optional ``secret_prev`` supports a rotation window). Both
        are returned so the verifier can accept either (current first).

        Args:
            gateway: The resolved :class:`Gateway` row.

        Returns:
            List[str]: Non-empty signing secrets, current first then previous.
        """
        token = getattr(gateway, "webhook_signing_secret", None)
        if not token:
            return []
        data = decode_auth(token) or {}
        secrets: List[str] = []
        current = data.get("secret")
        previous = data.get("secret_prev")
        if current:
            secrets.append(str(current))
        if previous:
            secrets.append(str(previous))
        return secrets

    async def ingest(
        self,
        *,
        conn_id: str,
        raw_body: bytes,
        headers: Mapping[str, str],
        query_params: Mapping[str, str],  # pylint: disable=unused-argument
        db: Any,
        now_epoch: Optional[int] = None,
    ) -> IngressResult:
        """Run the full ingress pipeline for one inbound webhook POST.

        Args:
            conn_id: The opaque connection id from ``POST /webhooks/{conn-id}``.
            raw_body: The exact raw request bytes (captured before any parse).
            headers: Request headers (case-insensitive lookups downstream).
            query_params: Request query parameters (reserved for plugin recipes).
            db: An active SQLAlchemy session.
            now_epoch: Override for the current epoch seconds (testing/replay).

        Returns:
            IngressResult: The outcome with the HTTP status and any body/envelope.
        """
        # 1) Master flag gate: opaque 404 when events are disabled.
        if not settings.mcpgateway_events_enabled:
            return IngressResult(status=404)

        now = int(now_epoch) if now_epoch is not None else int(time.time())

        # 2) Resolve the connection + descriptor. An unknown connection, a
        #    connection with no ingress descriptor, or an unknown descriptor all
        #    fail closed with the SAME 401 a bad signature returns (no oracle).
        gateway = db.get(DbGateway, conn_id) if conn_id else None
        if gateway is None:
            return IngressResult(status=401)

        descriptor_ref = self._descriptor_ref(gateway)
        if not descriptor_ref:
            return IngressResult(status=401)

        descriptor: Optional[ProviderDescriptor] = get_descriptor(descriptor_ref)
        if descriptor is None:
            return IngressResult(status=401)

        recipe = dict(descriptor.verify or {})
        secrets = self._secrets(gateway)

        # 3) Verify the signature FIRST, over the exact raw bytes.
        verdict = verify_signature(
            recipe=recipe,
            secrets=secrets,
            raw_body=raw_body,
            headers=headers,
            now_epoch=now,
            tolerance_seconds=settings.mcpgateway_events_signature_tolerance_seconds,
        )
        if not verdict.ok:
            return IngressResult(status=401)

        # An unsigned ("none") recipe is only honored when the descriptor opts
        # in explicitly; otherwise unsigned traffic is refused (SC-SEC-001).
        if recipe.get("strategy") == "none" and not recipe.get("allow_unsigned", False):
            return IngressResult(status=401)

        # 4) Parse the body - only after a valid signature (SC-ING-044).
        try:
            parsed = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (ValueError, UnicodeDecodeError):
            return IngressResult(status=400)

        # 5) Handshake (e.g. Slack url_verification) - echoed only post-verify.
        handshake_echo = self._handshake_echo(descriptor, parsed=parsed, headers=headers)
        if handshake_echo is not None:
            return IngressResult(status=200, body=handshake_echo)

        # 6) No-op liveness probe (e.g. GitHub ping) - ack with no domain event.
        if self._is_noop(descriptor, parsed=parsed, headers=headers):
            return IngressResult(status=202)

        # 7) Normalize -> dedup -> persist -> publish.
        source = f"//{conn_id}"
        envelope = build_envelope(
            descriptor=descriptor,
            raw_body=raw_body,
            parsed=parsed,
            headers=headers,
            source=source,
        )

        # Dedup -> persist -> publish via the shared emission tail. This is the
        # same connection-scoped dedup (in-process TTL cache + the event_log
        # (evt_source, evt_id) unique-constraint backstop), durable persist,
        # L1 bus fan-out, and L2 stream XADD that the MCP-native ingress path
        # reuses (emit.publish_normalized_event). A duplicate (cache hit or
        # constraint violation) is dropped silently with 202 and no publish.
        published, _event_log_id = await publish_normalized_event(
            db,
            gateway=gateway,
            envelope=envelope,
            raw_headers=headers,
            provider_id=descriptor.id,
        )
        if not published:
            return IngressResult(status=202, deduped=True, envelope=envelope)

        return IngressResult(status=202, envelope=envelope)

    @staticmethod
    def _handshake_echo(descriptor: ProviderDescriptor, *, parsed: Any, headers: Mapping[str, str]) -> Optional[str]:
        """Return the handshake echo value when the request matches the descriptor's handshake.

        The handshake block names a ``match`` (a ``{ref, equals}`` spec resolved
        from the parsed body) and an ``echo`` (a ``{ref}`` spec). When the match
        holds, the echoed value is returned verbatim so the router can answer the
        provider's challenge (Slack ``url_verification``).

        Args:
            descriptor: The resolved provider descriptor.
            parsed: The parsed JSON body.
            headers: Request headers.

        Returns:
            Optional[str]: The echo value when the handshake matches, else None.
        """
        handshake = getattr(descriptor, "handshake", None)
        if not handshake:
            return None
        match = handshake.get("match") or {}
        ref = match.get("ref")
        equals = match.get("equals")
        if not ref:
            return None
        actual = resolve({"from": "jsonpath", "ref": ref}, parsed=parsed, headers=headers)
        if actual != equals:
            return None
        echo = handshake.get("echo") or {}
        echo_ref = echo.get("ref")
        if not echo_ref:
            return None
        return resolve({"from": "jsonpath", "ref": echo_ref}, parsed=parsed, headers=headers)

    @staticmethod
    def _is_noop(descriptor: ProviderDescriptor, *, parsed: Any, headers: Mapping[str, str]) -> bool:
        """Report whether the request is a no-op liveness probe (e.g. GitHub ping).

        Args:
            descriptor: The resolved provider descriptor.
            parsed: The parsed JSON body.
            headers: Request headers.

        Returns:
            bool: ``True`` when the descriptor's ``noop`` block matches and the
            request should be acknowledged with no domain event.
        """
        noop = getattr(descriptor, "noop", None)
        if not noop:
            return False
        value = resolve({"from": noop.get("from", "header"), "ref": noop.get("ref")}, parsed=parsed, headers=headers)
        return value is not None and value in (noop.get("values") or [])
