# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/streaming.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Best-effort SSE/WS streaming egress adapter (FR-30).

The streaming adapter serves ``subscriber_kind`` in {``sse``, ``ws``} - the one
non-callback egress kind, used solely for browser/streaming clients that cannot
receive an inbound HTTP callback. Unlike the durable HTTP-callback adapter it is
a **live convenience consumer** served off the L1 in-process fan-out: it is
**best-effort with no DLQ** (FRD §2.5 / §9.1 / §9.2.2). A dropped live stream is
recovered by re-reading the durable L2 stream / ``event_log``, never by
retry/dead-lettering.

Consequently :meth:`StreamingEgressAdapter.deliver` publishes the delivery
envelope to the process-wide :func:`~mcpgateway.services.events.bus.get_event_bus`
fan-out and **always reports** ``DeliveryOutcome(ok=True)`` - even when no live
client is attached. It never returns a ``permanent`` failure, so the delivery
worker never dead-letters a stream delivery (SC-DEL-076 / TC-DEL-037).

To keep the existing fan-out bus (which broadcasts to every subscriber) usable
for subscription-scoped streams, each published item is wrapped in a small
routing envelope carrying the subscription's ``subscriber_target_ref``. A
subscription-scoped SSE/WS consumer is built with :func:`subscribe_stream`,
which returns a :class:`StreamConsumer` that filters incoming items by
``target_ref`` (an SSE session id / WS connection ref). A consumer created
without a ``target_ref`` receives every streamed envelope (e.g. a tenant-wide
Admin UI view). The SSE route mounting itself is decided in the wiring phase;
this module is only the adapter plus the consume helper.

Examples:
    >>> import asyncio
    >>> from mcpgateway.services.events.bus import InProcessEventBus
    >>> bus = InProcessEventBus()
    >>> adapter = StreamingEgressAdapter(bus=bus)
    >>> consumer = subscribe_stream(bus=bus)
    >>> env = {"event": {"id": "e1"}, "idempotency_key": "e1"}
    >>> async def _demo():
    ...     outcome = await adapter.deliver(delivery_envelope=env, subscription=None)
    ...     got = await consumer.get()
    ...     return outcome.ok, got == env
    >>> asyncio.run(_demo())
    (True, True)
    >>> consumer.close()
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from typing import Any, Optional

# First-Party
from mcpgateway.services.events.bus import get_event_bus, InProcessEventBus
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter

__all__ = ["StreamConsumer", "StreamingEgressAdapter", "subscribe_stream"]

# Internal envelope key under which the routing target_ref is stamped on the
# wrapper item published to the fan-out bus. The leading underscore avoids any
# collision with delivery-envelope fields.
_TARGET_REF_KEY = "_target_ref"
_PAYLOAD_KEY = "_payload"


def _resolve_target_ref(subscription: Any) -> Optional[str]:
    """Best-effort extraction of the streaming target ref from a subscription.

    Args:
        subscription: The resolved subscription record (may be ``None`` in
            pure-unit tests).

    Returns:
        The ``subscriber_target_ref`` (SSE session id / WS connection ref) if
        present, else ``None``.
    """
    if subscription is None:
        return None
    return getattr(subscription, "subscriber_target_ref", None)


class StreamConsumer:
    """A subscription-scoped consumer of streamed delivery envelopes.

    Wraps the underlying fan-out :class:`asyncio.Queue` returned by the bus and
    filters items by ``target_ref``. When ``target_ref`` is ``None`` every
    streamed envelope is delivered; otherwise only envelopes published for a
    matching ``subscriber_target_ref`` are delivered, so per-session SSE/WS
    streams do not cross-talk.

    The unwrapped delivery envelope (the original §9.1a body) is what
    :meth:`get` yields - the internal routing wrapper is stripped.
    """

    def __init__(self, bus: InProcessEventBus, queue: asyncio.Queue, target_ref: Optional[str]) -> None:
        """Initialize the consumer.

        Args:
            bus: The fan-out bus this consumer is attached to (for teardown).
            queue: The raw subscriber queue returned by :meth:`bus.subscribe`.
            target_ref: Optional ``subscriber_target_ref`` filter; ``None`` to
                receive every streamed envelope.
        """
        self._bus = bus
        self._queue = queue
        self._target_ref = target_ref

    async def get(self) -> dict:
        """Return the next delivery envelope matching this consumer's filter.

        Skips wrapper items whose ``target_ref`` does not match (when a filter
        is set), so a per-session consumer only observes its own deliveries.

        Returns:
            The unwrapped §9.1a delivery envelope.
        """
        while True:
            item = await self._queue.get()
            if not isinstance(item, dict) or _PAYLOAD_KEY not in item:
                # Foreign item on a shared bus; pass it through untouched only
                # for the unfiltered consumer, otherwise ignore.
                if self._target_ref is None:
                    return item
                continue
            if self._target_ref is not None and item.get(_TARGET_REF_KEY) != self._target_ref:
                continue
            return item[_PAYLOAD_KEY]

    def close(self) -> None:
        """Detach this consumer from the bus (idempotent)."""
        self._bus.unsubscribe(self._queue)


def subscribe_stream(*, bus: Optional[InProcessEventBus] = None, target_ref: Optional[str] = None) -> StreamConsumer:
    """Build a subscription-scoped consumer of streamed delivery envelopes.

    This is the helper a subscription-scoped SSE/WS handler uses to receive the
    envelopes the :class:`StreamingEgressAdapter` publishes. Filtering by
    ``target_ref`` lets one fan-out bus serve many per-session streams.

    Args:
        bus: The fan-out bus to attach to. Defaults to the process-wide
            :func:`~mcpgateway.services.events.bus.get_event_bus` singleton.
        target_ref: Optional ``subscriber_target_ref`` to filter on (an SSE
            session id / WS connection ref). ``None`` receives every streamed
            envelope.

    Returns:
        A :class:`StreamConsumer`; call :meth:`StreamConsumer.close` when done.
    """
    target_bus = bus if bus is not None else get_event_bus()
    queue = target_bus.subscribe()
    return StreamConsumer(target_bus, queue, target_ref)


class StreamingEgressAdapter(EgressAdapter):
    """Best-effort SSE/WS streaming egress adapter (FR-30).

    Publishes each delivery envelope to the L1 in-process fan-out bus and always
    reports success. There is no retry and no dead-lettering: a dropped live
    stream is recovered from the durable L2 stream / ``event_log``, not by the
    worker (FRD §2.5 / §9.1 / §9.2.2; SC-DEL-076 / TC-DEL-037).
    """

    def __init__(self, *, bus: Optional[InProcessEventBus] = None) -> None:
        """Initialize the adapter.

        Args:
            bus: The fan-out bus to publish onto. Defaults to the process-wide
                :func:`~mcpgateway.services.events.bus.get_event_bus` singleton.
        """
        self._bus = bus

    @property
    def _resolved_bus(self) -> InProcessEventBus:
        """Return the configured bus or the process-wide singleton."""
        return self._bus if self._bus is not None else get_event_bus()

    async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
        """Publish the envelope to the fan-out bus; always succeed (best-effort).

        The envelope is wrapped with the subscription's ``subscriber_target_ref``
        so a :func:`subscribe_stream` consumer can route it to the right live
        client. Publishing is unconditional; with no attached client the publish
        simply fans out to zero subscriber queues and still reports success -
        the stream kind is best-effort and is never dead-lettered.

        Args:
            delivery_envelope: The §9.1a delivery envelope to stream.
            subscription: The resolved subscription record (used only for its
                ``subscriber_target_ref``; may be ``None``).

        Returns:
            ``DeliveryOutcome(ok=True)`` - always, regardless of whether any live
            client is attached (best-effort, never ``permanent``).
        """
        target_ref = _resolve_target_ref(subscription)
        wrapper = {_TARGET_REF_KEY: target_ref, _PAYLOAD_KEY: delivery_envelope}
        await self._resolved_bus.publish(wrapper)
        return DeliveryOutcome(ok=True)
