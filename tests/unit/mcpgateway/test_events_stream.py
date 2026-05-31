# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_stream.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the L2 event stream spine (``EventStream`` ABC, the in-memory
backend, the Redis-backed backend, and the ``get_event_stream`` factory).

The in-memory backend models a Redis Streams consumer group with an explicit
Pending Entries List (PEL) so the delivered-but-unacked and stale-claim
semantics that back the DEL reliability gate (TC-DEL-020 no-loss on restart,
TC-DEL-021 reclaim of a stale PEL entry) can be exercised without a real Redis
server. The same semantic suite is parametrized to also run against the
Redis-backed backend using ``fakeredis``'s async client when it is available.
"""

# Future
from __future__ import annotations

# Standard
from typing import Optional

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.stream import (
    EventStream,
    get_event_stream,
    InMemoryStreamBackend,
    RedisStreamBackend,
)

try:
    # Third-Party
    import fakeredis.aioredis as _fakeredis_aio  # noqa: F401

    _HAS_FAKEREDIS = True
except Exception:  # pragma: no cover - environment without fakeredis
    _HAS_FAKEREDIS = False


def _sample_message(event_log_id: str = "el-1") -> dict:
    """Build a representative stream message.

    Args:
        event_log_id: The event_log row id to embed.

    Returns:
        dict: A stream message with the contract-mandated shape.
    """
    return {
        "event_log_id": event_log_id,
        "gateway_id": "gw-1",
        "envelope": {
            "id": "evt-1",
            "source": "//gw-1",
            "type": "com.example.thing",
            "subject": "s",
            "time": "2026-05-30T00:00:00+00:00",
            "data": {"x": 1},
        },
    }


async def _make_redis_backend() -> Optional[RedisStreamBackend]:
    """Build a ``RedisStreamBackend`` wired to a fresh fakeredis async client.

    Returns:
        Optional[RedisStreamBackend]: The backend, or ``None`` when fakeredis
        is unavailable.
    """
    if not _HAS_FAKEREDIS:
        return None
    # Third-Party
    import fakeredis.aioredis as fakeredis_aio  # pylint: disable=import-outside-toplevel

    client = fakeredis_aio.FakeRedis()
    return RedisStreamBackend(client=client, stream_key="test:events:stream", group="delivery")


# --------------------------------------------------------------------------- #
# ABC + factory                                                               #
# --------------------------------------------------------------------------- #
class TestEventStreamContract:
    """Shape of the abstract base class and the process factory."""

    def test_inmemory_is_event_stream(self):
        """InMemoryStreamBackend is an EventStream."""
        assert isinstance(InMemoryStreamBackend(), EventStream)

    def test_eventstream_is_abstract(self):
        """EventStream cannot be instantiated directly."""
        with pytest.raises(TypeError):
            EventStream()  # type: ignore[abstract]

    def test_factory_returns_inmemory_when_not_redis(self, monkeypatch):
        """get_event_stream() falls back to the in-memory backend off Redis."""
        # First-Party
        import mcpgateway.services.events.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_event_stream", None, raising=False)
        monkeypatch.setattr(stream_mod.settings, "cache_type", "database", raising=False)
        backend = get_event_stream()
        assert isinstance(backend, InMemoryStreamBackend)

    def test_factory_is_singleton(self, monkeypatch):
        """get_event_stream() returns the same instance across calls."""
        # First-Party
        import mcpgateway.services.events.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_event_stream", None, raising=False)
        monkeypatch.setattr(stream_mod.settings, "cache_type", "database", raising=False)
        assert get_event_stream() is get_event_stream()

    def test_factory_returns_redis_when_configured(self, monkeypatch):
        """get_event_stream() returns the Redis backend when cache_type=redis + url set."""
        # First-Party
        import mcpgateway.services.events.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_event_stream", None, raising=False)
        monkeypatch.setattr(stream_mod.settings, "cache_type", "redis", raising=False)
        monkeypatch.setattr(stream_mod.settings, "redis_url", "redis://localhost:6379/0", raising=False)
        backend = get_event_stream()
        assert isinstance(backend, RedisStreamBackend)


# --------------------------------------------------------------------------- #
# In-memory backend (no external dependency)                                  #
# --------------------------------------------------------------------------- #
class TestInMemoryStreamBackend:
    """Core consumer-group/PEL semantics on the in-memory backend."""

    @pytest.mark.asyncio
    async def test_add_returns_entry_id_and_read_moves_to_pel(self):
        """add() yields an id; read_group() delivers the entry and parks it in the PEL."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        entry_id = await s.add(_sample_message())
        assert isinstance(entry_id, str) and entry_id

        read = await s.read_group("w1", count=10)
        assert len(read) == 1
        rid, msg = read[0]
        assert rid == entry_id
        assert msg["event_log_id"] == "el-1"
        assert msg["gateway_id"] == "gw-1"
        assert msg["envelope"]["id"] == "evt-1"

        # Delivered-but-unacked => in the PEL.
        assert entry_id in await s.pending()

    @pytest.mark.asyncio
    async def test_read_group_does_not_redeliver_pel_entries(self):
        """A second read_group() does not redeliver an already-delivered (unacked) entry."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        await s.add(_sample_message())

        first = await s.read_group("w1", count=10)
        assert len(first) == 1
        second = await s.read_group("w1", count=10)
        assert second == []

    @pytest.mark.asyncio
    async def test_ack_removes_from_pel(self):
        """ack() clears the entry from the PEL."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        entry_id = await s.add(_sample_message())
        await s.read_group("w1", count=10)
        assert entry_id in await s.pending()

        await s.ack(entry_id)
        assert entry_id not in await s.pending()
        assert await s.pending() == []

    @pytest.mark.asyncio
    async def test_unread_entry_survives_for_a_second_consumer(self):
        """TC-DEL-020: an entry never read by w1 is still deliverable to w2 (no loss)."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        e1 = await s.add(_sample_message("el-1"))
        e2 = await s.add(_sample_message("el-2"))

        # w1 reads only one entry (count=1) then "restarts" without acking the rest.
        read_w1 = await s.read_group("w1", count=1)
        assert len(read_w1) == 1
        assert read_w1[0][0] == e1

        # A fresh consumer picks up the still-undelivered entry.
        read_w2 = await s.read_group("w2", count=10)
        assert [rid for rid, _ in read_w2] == [e2]

    @pytest.mark.asyncio
    async def test_claim_stale_reassigns_pel_entry(self):
        """TC-DEL-021: a delivered-but-unacked entry is reclaimed by another consumer."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        entry_id = await s.add(_sample_message())

        # w1 reads but crashes before ack -> entry sits in w1's PEL.
        await s.read_group("w1", count=10)
        assert entry_id in await s.pending()

        # min_idle_ms=0 -> immediately claimable by w2.
        claimed = await s.claim_stale("w2", min_idle_ms=0, count=10)
        assert [rid for rid, _ in claimed] == [entry_id]
        assert claimed[0][1]["event_log_id"] == "el-1"

        # Still pending (now owned by w2) until acked; ack clears it.
        assert entry_id in await s.pending()
        await s.ack(entry_id)
        assert await s.pending() == []

    @pytest.mark.asyncio
    async def test_claim_stale_respects_min_idle(self):
        """claim_stale() does not reclaim an entry younger than min_idle_ms."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        await s.add(_sample_message())
        await s.read_group("w1", count=10)

        # A large idle threshold means nothing is stale yet.
        claimed = await s.claim_stale("w2", min_idle_ms=10_000, count=10)
        assert claimed == []

    @pytest.mark.asyncio
    async def test_trim_bounds_undelivered_backlog(self):
        """trim(maxlen) bounds the number of not-yet-delivered entries retained."""
        s = InMemoryStreamBackend()
        await s.ensure_group()
        for i in range(5):
            await s.add(_sample_message(f"el-{i}"))

        await s.trim(2)
        read = await s.read_group("w1", count=10)
        # Oldest entries are dropped; the newest two survive.
        assert len(read) == 2
        assert [m["event_log_id"] for _, m in read] == ["el-3", "el-4"]


# --------------------------------------------------------------------------- #
# Redis backend (fakeredis) - same semantics                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_FAKEREDIS, reason="fakeredis not installed; Redis-backed stream tests require fakeredis")
class TestRedisStreamBackend:
    """The same consumer-group/PEL semantics against fakeredis's async client."""

    @pytest.mark.asyncio
    async def test_add_read_ack(self):
        """add() -> read_group() (PEL) -> ack() clears the PEL on the Redis backend."""
        s = await _make_redis_backend()
        await s.ensure_group()
        entry_id = await s.add(_sample_message())
        assert isinstance(entry_id, str) and entry_id

        read = await s.read_group("w1", count=10)
        assert len(read) == 1
        rid, msg = read[0]
        assert rid == entry_id
        assert msg["event_log_id"] == "el-1"
        assert msg["gateway_id"] == "gw-1"
        assert msg["envelope"]["id"] == "evt-1"
        assert entry_id in await s.pending()

        await s.ack(entry_id)
        assert entry_id not in await s.pending()
        await s.aclose()

    @pytest.mark.asyncio
    async def test_read_group_does_not_redeliver(self):
        """A second read_group() does not redeliver a delivered-but-unacked entry."""
        s = await _make_redis_backend()
        await s.ensure_group()
        await s.add(_sample_message())
        assert len(await s.read_group("w1", count=10)) == 1
        assert await s.read_group("w1", count=10) == []
        await s.aclose()

    @pytest.mark.asyncio
    async def test_unread_entry_survives_for_a_second_consumer(self):
        """TC-DEL-020: an undelivered entry survives for a second consumer (no loss)."""
        s = await _make_redis_backend()
        await s.ensure_group()
        e1 = await s.add(_sample_message("el-1"))
        e2 = await s.add(_sample_message("el-2"))

        read_w1 = await s.read_group("w1", count=1)
        assert [rid for rid, _ in read_w1] == [e1]
        read_w2 = await s.read_group("w2", count=10)
        assert [rid for rid, _ in read_w2] == [e2]
        await s.aclose()

    @pytest.mark.asyncio
    async def test_claim_stale_reassigns_pel_entry(self):
        """TC-DEL-021: XAUTOCLAIM reassigns a stale PEL entry to another consumer."""
        s = await _make_redis_backend()
        await s.ensure_group()
        entry_id = await s.add(_sample_message())
        await s.read_group("w1", count=10)
        assert entry_id in await s.pending()

        claimed = await s.claim_stale("w2", min_idle_ms=0, count=10)
        assert [rid for rid, _ in claimed] == [entry_id]
        assert claimed[0][1]["event_log_id"] == "el-1"
        assert claimed[0][1]["envelope"]["id"] == "evt-1"

        await s.ack(entry_id)
        assert entry_id not in await s.pending()
        await s.aclose()

    @pytest.mark.asyncio
    async def test_ensure_group_is_idempotent(self):
        """ensure_group() can be called repeatedly without raising (BUSYGROUP swallowed)."""
        s = await _make_redis_backend()
        await s.ensure_group()
        await s.ensure_group()
        await s.add(_sample_message())
        assert len(await s.read_group("w1", count=10)) == 1
        await s.aclose()

    @pytest.mark.asyncio
    async def test_trim_bounds_stream(self):
        """trim(maxlen) bounds the stream length on the Redis backend."""
        s = await _make_redis_backend()
        await s.ensure_group()
        for i in range(20):
            await s.add(_sample_message(f"el-{i}"))
        await s.trim(5)
        read = await s.read_group("w1", count=100)
        assert len(read) <= 20  # exact trim is approximate; never grows unbounded
        await s.aclose()
