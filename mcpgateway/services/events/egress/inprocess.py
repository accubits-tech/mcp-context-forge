# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/inprocess.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

In-process egress adapter: a configurable fake subscriber for tests.

:class:`InProcessEgressAdapter` is the M2b workhorse for the delivery-pipeline
reliability tests. It implements the :class:`EgressAdapter` seam without any
network I/O:

* it **records every received delivery** - the full delivery envelope, the
  resolved target ``callback_url``, and the ``Idempotency-Key`` taken from the
  envelope's ``idempotency_key`` - so tests can assert the stable-key invariant
  across retries (TC-DEL-001) and no-double-delivery across workers
  (TC-DEL-061/062/063); and
* it returns a **programmable** :class:`DeliveryOutcome` per target URL via
  :meth:`set_outcomes`, so a single target can be driven through a scripted
  sequence (e.g. 500, 500, 200) to exercise backoff / dead-letter
  (TC-DEL-009/017/024/028), 410-auto-disable, 429+retry_after, 4xx-permanent,
  and timeout paths. When a URL has no (remaining) programmed outcome it falls
  back to the default: record and return ``ok``.

:func:`get_egress_adapter` is the factory the delivery worker calls. In M2b it
returns a shared in-process adapter for **every** subscriber kind; M3 overrides
``http_callback`` with the real signed-POST adapter behind the same seam.

Examples:
    >>> import asyncio
    >>> adapter = InProcessEgressAdapter()
    >>> env = {"event": {"id": "e1"}, "idempotency_key": "e1",
    ...        "subscription": {"target": {"callback_url": "https://x/cb"}}}
    >>> outcome = asyncio.run(adapter.deliver(delivery_envelope=env, subscription=None))
    >>> outcome.ok
    True
    >>> adapter.received[0].idempotency_key
    'e1'
"""

# Future
from __future__ import annotations

# Standard
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# First-Party
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter

__all__ = ["RecordedDelivery", "InProcessEgressAdapter", "get_egress_adapter"]


@dataclass
class RecordedDelivery:
    """One delivery observed by :class:`InProcessEgressAdapter`.

    Attributes:
        callback_url: The resolved target URL the delivery was addressed to.
        idempotency_key: The ``Idempotency-Key`` carried by the envelope.
        delivery_envelope: The full §9.1a delivery envelope as received.
    """

    callback_url: Optional[str]
    idempotency_key: Optional[str]
    delivery_envelope: dict = field(default_factory=dict)


def _resolve_callback_url(delivery_envelope: dict, subscription: Any) -> Optional[str]:
    """Best-effort extraction of the target callback URL.

    Args:
        delivery_envelope: The §9.1a delivery envelope.
        subscription: The resolved subscription record (may be ``None``).

    Returns:
        The target ``callback_url`` if discoverable, else ``None``.
    """
    sub_block = delivery_envelope.get("subscription") or {}
    target = sub_block.get("target") or {}
    url = target.get("callback_url") or sub_block.get("callback_url") or delivery_envelope.get("callback_url")
    if url is None and subscription is not None:
        url = getattr(subscription, "callback_url", None)
    return url


class InProcessEgressAdapter(EgressAdapter):
    """Configurable fake subscriber that records deliveries and returns scripted outcomes.

    Default behaviour for any target is to record the delivery and return
    ``DeliveryOutcome(ok=True, http_status=200)``. Use :meth:`set_outcomes` to
    program a per-URL sequence of outcomes that are consumed in order; once a
    URL's programmed sequence is exhausted it falls back to the default.
    """

    def __init__(self) -> None:
        """Initialize with no recorded deliveries and no programmed outcomes."""
        self.received: List[RecordedDelivery] = []
        self._programmed: Dict[str, Deque[DeliveryOutcome]] = {}

    def set_outcomes(self, callback_url: str, outcomes: List[DeliveryOutcome]) -> None:
        """Program the sequence of outcomes returned for a target URL.

        Args:
            callback_url: The target URL to program.
            outcomes: Outcomes returned in order on successive deliveries to that
                URL. After the sequence is exhausted the adapter falls back to
                the default ``ok`` outcome.
        """
        self._programmed[callback_url] = deque(outcomes)

    def reset(self) -> None:
        """Clear all recorded deliveries and programmed outcomes."""
        self.received.clear()
        self._programmed.clear()

    def received_for(self, callback_url: str) -> List[RecordedDelivery]:
        """Return recorded deliveries addressed to a given URL.

        Args:
            callback_url: The target URL to filter recorded deliveries by.

        Returns:
            The recorded deliveries whose target matches ``callback_url``, in
            arrival order.
        """
        return [r for r in self.received if r.callback_url == callback_url]

    async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
        """Record the delivery and return the next programmed (or default) outcome.

        Args:
            delivery_envelope: The §9.1a delivery envelope.
            subscription: The resolved subscription record (may be ``None`` in
                pure-unit tests).

        Returns:
            The next programmed :class:`DeliveryOutcome` for the target URL, or
            ``DeliveryOutcome(ok=True, http_status=200)`` when none is queued.
        """
        callback_url = _resolve_callback_url(delivery_envelope, subscription)
        idempotency_key = delivery_envelope.get("idempotency_key")
        if idempotency_key is None:
            event = delivery_envelope.get("event") or {}
            idempotency_key = event.get("id")

        self.received.append(
            RecordedDelivery(
                callback_url=callback_url,
                idempotency_key=idempotency_key,
                delivery_envelope=delivery_envelope,
            )
        )

        queue = self._programmed.get(callback_url)
        if queue:
            return queue.popleft()
        return DeliveryOutcome(ok=True, http_status=200)


# Process-wide shared adapters so repeated lookups for one kind reuse a single
# instance within a process (mirrors the prior in-process singleton seam).
_http_callback_adapter: Optional[EgressAdapter] = None
_streaming_adapter: Optional[EgressAdapter] = None

# Subscriber kinds served by the best-effort SSE/WS streaming adapter (no DLQ).
_STREAMING_KINDS = ("sse", "ws")


def get_egress_adapter(subscriber_kind: str) -> EgressAdapter:
    """Return the production egress adapter for a subscriber kind (M3).

    The delivery worker falls through to this factory only when no adapter was
    constructor-injected (the test seam stays available via
    ``DeliveryWorker(egress=...)``). The mapping is the two-adapter set of
    FRD §9.2:

    * ``http_callback`` (and any unknown kind, fail-safe to the single push
      adapter) -> the real
      :class:`~mcpgateway.services.events.egress.http_callback.HttpCallbackEgressAdapter`
      (signed, SSRF-guarded, TLS-verified POST with the gateway's at-least-once
      + dead-letter contract).
    * ``sse`` / ``ws`` -> the best-effort
      :class:`~mcpgateway.services.events.egress.streaming.StreamingEgressAdapter`
      (live fan-out, no retry, no DLQ - §2.5 / §9.2.2).

    Each kind reuses a process-wide instance so repeated lookups are cheap.

    Args:
        subscriber_kind: The subscription kind (``http_callback`` / ``sse`` /
            ``ws`` / ...).

    Returns:
        EgressAdapter: The adapter to use for this subscriber kind.
    """
    global _http_callback_adapter, _streaming_adapter  # pylint: disable=global-statement

    if subscriber_kind in _STREAMING_KINDS:
        if _streaming_adapter is None:
            # First-Party
            from mcpgateway.services.events.egress.streaming import StreamingEgressAdapter  # pylint: disable=import-outside-toplevel

            _streaming_adapter = StreamingEgressAdapter()
        return _streaming_adapter

    if _http_callback_adapter is None:
        # First-Party
        from mcpgateway.config import settings  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter  # pylint: disable=import-outside-toplevel

        allow_hosts = set(getattr(settings, "mcpgateway_events_egress_allow_hosts", None) or [])
        _http_callback_adapter = HttpCallbackEgressAdapter(allow_hosts=allow_hosts or None)
    return _http_callback_adapter
