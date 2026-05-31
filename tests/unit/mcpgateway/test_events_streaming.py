# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_streaming.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the best-effort SSE/WS streaming egress adapter (M3, FR-30).

The streaming adapter (``subscriber_kind`` in {``sse``, ``ws``}) is a *live
convenience consumer* served off the L1 in-process fan-out: it publishes the
delivery envelope to :func:`get_event_bus` and always reports success
(best-effort, never dead-lettered - SC-DEL-076 / TC-DEL-037). A
subscription-scoped consumer receives the envelope through a small consume
helper that filters by ``subscriber_target_ref``.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from types import SimpleNamespace

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.bus import InProcessEventBus
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter
from mcpgateway.services.events.egress.streaming import (
    StreamConsumer,
    StreamingEgressAdapter,
    subscribe_stream,
)


def _sub(kind: str = "sse", target_ref: str | None = None):
    """Build a minimal subscription stand-in."""
    return SimpleNamespace(id="sub-1", subscriber_kind=kind, subscriber_target_ref=target_ref, callback_url=None)


def _envelope(event_id: str = "evt-1") -> dict:
    """Build a minimal §9.1a delivery envelope."""
    return {
        "event": {"id": event_id, "type": "demo.event", "source": "demo"},
        "idempotency_key": event_id,
        "subscription": {"id": "sub-1", "mode": "fanout"},
    }


def test_adapter_is_egress_adapter():
    """The streaming adapter implements the shared EgressAdapter seam."""
    assert issubclass(StreamingEgressAdapter, EgressAdapter)
    assert isinstance(StreamingEgressAdapter(), EgressAdapter)


def test_deliver_publishes_and_returns_ok():
    """deliver publishes to the bus and a subscriber receives the envelope (TC-DEL-037)."""

    async def _run() -> None:
        bus = InProcessEventBus()
        adapter = StreamingEgressAdapter(bus=bus)
        consumer = subscribe_stream(bus=bus)

        env = _envelope("evt-1")
        outcome = await adapter.deliver(delivery_envelope=env, subscription=_sub())

        assert isinstance(outcome, DeliveryOutcome)
        assert outcome.ok is True
        # NEVER dead-lettered: best-effort path is never permanent.
        assert outcome.permanent is False

        received = await asyncio.wait_for(consumer.get(), timeout=1.0)
        assert received == env

        consumer.close()

    asyncio.run(_run())


def test_deliver_returns_ok_with_no_consumer():
    """Best-effort: deliver succeeds even when nobody is listening."""

    async def _run() -> None:
        bus = InProcessEventBus()
        adapter = StreamingEgressAdapter(bus=bus)

        outcome = await adapter.deliver(delivery_envelope=_envelope("evt-2"), subscription=_sub())
        assert outcome.ok is True
        assert outcome.permanent is False

    asyncio.run(_run())


def test_target_ref_keying_routes_to_right_consumer():
    """A target_ref-scoped consumer only receives envelopes for its own target_ref."""

    async def _run() -> None:
        bus = InProcessEventBus()
        adapter = StreamingEgressAdapter(bus=bus)

        consumer_a = subscribe_stream(bus=bus, target_ref="session-A")
        consumer_b = subscribe_stream(bus=bus, target_ref="session-B")

        env_a = _envelope("evt-a")
        await adapter.deliver(delivery_envelope=env_a, subscription=_sub(target_ref="session-A"))

        got_a = await asyncio.wait_for(consumer_a.get(), timeout=1.0)
        assert got_a == env_a

        # The B-scoped consumer must NOT receive the A-targeted envelope.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(consumer_b.get(), timeout=0.1)

        consumer_a.close()
        consumer_b.close()

    asyncio.run(_run())


def test_unkeyed_consumer_receives_all_target_refs():
    """A consumer with no target_ref filter receives every published envelope."""

    async def _run() -> None:
        bus = InProcessEventBus()
        adapter = StreamingEgressAdapter(bus=bus)
        consumer = subscribe_stream(bus=bus)

        env_a = _envelope("evt-a")
        env_b = _envelope("evt-b")
        await adapter.deliver(delivery_envelope=env_a, subscription=_sub(target_ref="session-A"))
        await adapter.deliver(delivery_envelope=env_b, subscription=_sub(target_ref="session-B"))

        got1 = await asyncio.wait_for(consumer.get(), timeout=1.0)
        got2 = await asyncio.wait_for(consumer.get(), timeout=1.0)
        assert {got1["event"]["id"], got2["event"]["id"]} == {"evt-a", "evt-b"}

        consumer.close()

    asyncio.run(_run())


def test_default_bus_is_process_singleton():
    """With no explicit bus, the adapter and consumer share the process-wide bus."""

    async def _run() -> None:
        adapter = StreamingEgressAdapter()
        consumer = subscribe_stream()

        env = _envelope("evt-singleton")
        outcome = await adapter.deliver(delivery_envelope=env, subscription=_sub())
        assert outcome.ok is True

        received = await asyncio.wait_for(consumer.get(), timeout=1.0)
        assert received == env

        consumer.close()

    asyncio.run(_run())


def test_consumer_is_returned_type():
    """subscribe_stream returns a StreamConsumer."""
    consumer = subscribe_stream(bus=InProcessEventBus())
    assert isinstance(consumer, StreamConsumer)
    consumer.close()
