# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_bus.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the in-process event bus and TTL dedup cache used by the
config-driven event ingress machinery.
"""

# Standard
import asyncio

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.bus import (
    get_event_bus,
    InProcessEventBus,
    TTLDedupCache,
)


class TestInProcessEventBus:
    """Pub/sub behaviour of the in-process event bus."""

    @pytest.mark.asyncio
    async def test_subscriber_receives_published_event(self):
        """A subscriber queue receives a published event."""
        bus = InProcessEventBus()
        queue = bus.subscribe()
        assert isinstance(queue, asyncio.Queue)

        event = {"type": "demo", "data": {"x": 1}}
        await bus.publish(event)

        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received == event

    @pytest.mark.asyncio
    async def test_multiple_subscribers_each_receive_event(self):
        """Every subscriber receives a copy of the published event."""
        bus = InProcessEventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        q3 = bus.subscribe()

        event = {"type": "fanout", "data": {"n": 42}}
        await bus.publish(event)

        for queue in (q1, q2, q3):
            received = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert received == event

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        """After unsubscribe the queue receives no further events."""
        bus = InProcessEventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        bus.unsubscribe(q1)

        event = {"type": "after_unsub"}
        await bus.publish(event)

        # q2 still subscribed -> gets the event.
        received = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert received == event

        # q1 unsubscribed -> nothing queued for it.
        assert q1.empty()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q1.get(), timeout=0.05)

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_queue_is_safe(self):
        """Unsubscribing a queue that was never subscribed does not raise."""
        bus = InProcessEventBus()
        stray: asyncio.Queue = asyncio.Queue()
        # Should be a no-op rather than raising ValueError.
        bus.unsubscribe(stray)


class _FakeClock:
    """Controllable monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestTTLDedupCache:
    """TTL-based dedup cache semantics."""

    def test_first_seen_false_then_true(self):
        """First observation is novel; immediate repeat is a duplicate."""
        cache = TTLDedupCache(ttl_seconds=60)
        assert cache.seen("k") is False
        assert cache.seen("k") is True

    def test_distinct_keys_are_independent(self):
        """Distinct keys do not collide."""
        cache = TTLDedupCache(ttl_seconds=60)
        assert cache.seen("a") is False
        assert cache.seen("b") is False
        assert cache.seen("a") is True
        assert cache.seen("b") is True

    def test_entry_expires_after_ttl(self):
        """After the TTL elapses the same key is considered novel again."""
        clock = _FakeClock()
        cache = TTLDedupCache(ttl_seconds=10, clock=clock)

        assert cache.seen("k") is False
        assert cache.seen("k") is True

        # Still within TTL window.
        clock.advance(5)
        assert cache.seen("k") is True

        # Past the TTL window -> novel again.
        clock.advance(20)
        assert cache.seen("k") is False
        # And recorded once more.
        assert cache.seen("k") is True

    def test_tiny_ttl_expires_with_real_clock(self):
        """A very small TTL expires using the default monotonic clock."""
        # Standard
        import time

        cache = TTLDedupCache(ttl_seconds=0.01)
        assert cache.seen("k") is False
        time.sleep(0.05)
        assert cache.seen("k") is False


class TestGetEventBus:
    """Process-wide singleton accessor."""

    def test_returns_singleton(self):
        """Repeated calls return the same bus instance."""
        bus_a = get_event_bus()
        bus_b = get_event_bus()
        assert bus_a is bus_b
        assert isinstance(bus_a, InProcessEventBus)
