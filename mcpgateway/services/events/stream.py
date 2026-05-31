# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/stream.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

L2 internal event stream spine for server-initiated events.

This module is the durable, cross-instance buffer between ingress (which
``XADD``s an accepted event after persisting its ``event_log`` row) and the
egress delivery workers (which consume via a consumer group, deliver to each
matched subscription, and ``XACK``). It is the durability/retry/replay
spine - **not** a delivery target (FRD §4.3 L2, §8.5).

Two interchangeable backends sit behind one async interface:

* :class:`InMemoryStreamBackend` - a dependency-free model of a single
  consumer group with an explicit Pending Entries List (PEL). It faithfully
  reproduces the delivered-but-unacked and stale-claim semantics so the
  delivery reliability guarantees (no loss on restart, reclaim of a stale
  worker's PEL) can be exercised without a Redis server. This is the default
  when Redis is not configured and the workhorse for reliability tests.
* :class:`RedisStreamBackend` - a thin wrapper over ``redis.asyncio`` Streams
  (``XADD MAXLEN ~``, ``XGROUP CREATE MKSTREAM``, ``XREADGROUP``, ``XACK``,
  ``XAUTOCLAIM``, ``XPENDING``, ``XTRIM``). The stream key derives from
  :data:`settings.mcpgateway_events_redis_stream_prefix`.

A stream message is a plain ``dict`` with the contract shape::

    {"event_log_id": str, "gateway_id": str | None, "envelope": <event dict>}

where ``envelope`` is the inner event block (``id``/``source``/``type``/
``subject``/``time``/``data``). On the wire (Redis) the message is carried as a
single JSON field so per-subject ordering is established purely by stream-entry
id at consume time (FRD §2.3/§8.5; ordering is a delivery-layer guarantee, not
an envelope field).

:func:`get_event_stream` returns a process-wide singleton: the Redis backend
when ``settings.cache_type == "redis"`` and ``settings.redis_url`` is set, else
the in-memory backend.
"""

# Future
from __future__ import annotations

# Standard
from abc import ABC, abstractmethod
import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

# First-Party
from mcpgateway.config import settings

__all__ = [
    "EventStream",
    "InMemoryStreamBackend",
    "RedisStreamBackend",
    "get_event_stream",
]

# The consumer group name shared by all delivery workers (FRD §8.5: a single
# consumer group, many workers, balanced via XREADGROUP).
_DEFAULT_GROUP = "delivery"

# The JSON field name under which the message dict is serialized on the Redis
# stream entry. A single field keeps ordering and (de)serialization trivial.
_FIELD = "msg"


class EventStream(ABC):
    """Abstract L2 stream backend: a single consumer group with a PEL.

    Concrete backends model one Redis Stream + one consumer group. Workers read
    new entries into the group's Pending Entries List (PEL) via
    :meth:`read_group`, process them, and remove them from the PEL via
    :meth:`ack`. Entries a crashed worker left unacked are reclaimed by another
    worker via :meth:`claim_stale`.
    """

    @abstractmethod
    async def ensure_group(self) -> None:
        """Create the stream and its consumer group if they do not yet exist (idempotent)."""

    @abstractmethod
    async def add(self, message: dict) -> str:
        """Append a message to the stream.

        Args:
            message: The stream message dict (``event_log_id``/``gateway_id``/
                ``envelope``).

        Returns:
            str: The stream entry id assigned to the appended message.
        """

    @abstractmethod
    async def read_group(self, consumer: str, count: int = 10, block_ms: int = 0) -> List[Tuple[str, dict]]:
        """Read up to ``count`` new entries into ``consumer``'s PEL.

        Args:
            consumer: The consumer (worker) name within the group.
            count: Maximum number of new entries to deliver.
            block_ms: Block for up to this many milliseconds for new entries
                (``0`` returns immediately).

        Returns:
            List[Tuple[str, dict]]: ``(entry_id, message)`` pairs newly moved
            into this consumer's PEL.
        """

    @abstractmethod
    async def ack(self, entry_id: str) -> None:
        """Acknowledge an entry, removing it from the PEL.

        Args:
            entry_id: The stream entry id to acknowledge.
        """

    @abstractmethod
    async def claim_stale(self, consumer: str, min_idle_ms: int, count: int = 10) -> List[Tuple[str, dict]]:
        """Reassign PEL entries idle for at least ``min_idle_ms`` to ``consumer``.

        Args:
            consumer: The consumer that should take ownership of the entries.
            min_idle_ms: Minimum idle time (milliseconds) before an entry is
                eligible for reclaim.
            count: Maximum number of entries to claim.

        Returns:
            List[Tuple[str, dict]]: The ``(entry_id, message)`` pairs claimed.
        """

    @abstractmethod
    async def pending(self) -> List[str]:
        """Return the entry ids currently in the consumer group's PEL.

        Returns:
            List[str]: Delivered-but-unacked stream entry ids.
        """

    @abstractmethod
    async def trim(self, maxlen: int) -> None:
        """Bound the stream to approximately ``maxlen`` entries.

        Args:
            maxlen: Approximate maximum number of entries to retain.
        """


class _MemEntry:
    """A single in-memory stream entry plus its PEL bookkeeping.

    Attributes:
        entry_id: The stream entry id (monotonic, Redis-like ``<ms>-<seq>``).
        message: The stored message dict (a copy of the appended dict).
        delivered: Whether the entry has been delivered to some consumer.
        consumer: The consumer that currently owns the entry (PEL owner).
        delivered_at: Monotonic timestamp of last delivery/claim, for idle calc.
    """

    __slots__ = ("entry_id", "message", "delivered", "consumer", "delivered_at")

    def __init__(self, entry_id: str, message: dict) -> None:
        """Initialize an entry.

        Args:
            entry_id: The assigned stream entry id.
            message: The message dict to store (stored by reference copy).
        """
        self.entry_id: str = entry_id
        self.message: dict = message
        self.delivered: bool = False
        self.consumer: Optional[str] = None
        self.delivered_at: float = 0.0


class InMemoryStreamBackend(EventStream):
    """Dependency-free model of one Redis Stream + one consumer group.

    Maintains an ordered list of entries and an explicit PEL (the set of
    delivered-but-unacked entries). New reads draw from the undelivered tail;
    :meth:`claim_stale` re-owns idle PEL entries; :meth:`ack` removes an entry
    from the PEL. This makes the delivered-but-unacked and stale-claim flows
    observable without a real Redis (FRD §8.5; TC-DEL-020/021).

    Examples:
        >>> import asyncio
        >>> s = InMemoryStreamBackend()
        >>> async def demo():
        ...     await s.ensure_group()
        ...     eid = await s.add({"event_log_id": "el-1", "gateway_id": "g", "envelope": {}})
        ...     read = await s.read_group("w1")
        ...     pel = await s.pending()
        ...     await s.ack(eid)
        ...     after = await s.pending()
        ...     return read[0][0] == eid, pel == [eid], after == []
        >>> asyncio.run(demo())
        (True, True, True)
    """

    def __init__(self) -> None:
        """Initialize an empty stream with no delivered entries."""
        self._entries: List[_MemEntry] = []
        self._seq: int = 0
        self._lock = asyncio.Lock()

    def _next_id(self) -> str:
        """Allocate a monotonic, Redis-like ``<ms>-<seq>`` entry id.

        Returns:
            str: A unique, lexicographically increasing entry id.
        """
        self._seq += 1
        return f"{int(time.time() * 1000)}-{self._seq}"

    async def ensure_group(self) -> None:
        """No-op for the in-memory backend (the group always exists)."""

    async def add(self, message: dict) -> str:
        """Append a message and return its entry id.

        Args:
            message: The stream message dict.

        Returns:
            str: The assigned entry id.
        """
        async with self._lock:
            entry_id = self._next_id()
            self._entries.append(_MemEntry(entry_id, dict(message)))
            return entry_id

    async def read_group(self, consumer: str, count: int = 10, block_ms: int = 0) -> List[Tuple[str, dict]]:  # noqa: ARG002 - block_ms unused for the in-memory backend
        """Deliver up to ``count`` undelivered entries into ``consumer``'s PEL.

        Args:
            consumer: The consumer (worker) name.
            count: Maximum number of new entries to deliver.
            block_ms: Ignored for the in-memory backend (returns immediately).

        Returns:
            List[Tuple[str, dict]]: Newly delivered ``(entry_id, message)`` pairs.
        """
        out: List[Tuple[str, dict]] = []
        async with self._lock:
            now = time.monotonic()
            for entry in self._entries:
                if len(out) >= count:
                    break
                if not entry.delivered:
                    entry.delivered = True
                    entry.consumer = consumer
                    entry.delivered_at = now
                    out.append((entry.entry_id, dict(entry.message)))
        return out

    async def ack(self, entry_id: str) -> None:
        """Remove an entry from the stream/PEL.

        Args:
            entry_id: The stream entry id to acknowledge.
        """
        async with self._lock:
            self._entries = [e for e in self._entries if e.entry_id != entry_id]

    async def claim_stale(self, consumer: str, min_idle_ms: int, count: int = 10) -> List[Tuple[str, dict]]:
        """Reassign idle PEL entries to ``consumer``.

        Args:
            consumer: The consumer taking ownership.
            min_idle_ms: Minimum idle time (ms) before an entry is claimable.
            count: Maximum number of entries to claim.

        Returns:
            List[Tuple[str, dict]]: Claimed ``(entry_id, message)`` pairs.
        """
        out: List[Tuple[str, dict]] = []
        async with self._lock:
            now = time.monotonic()
            min_idle = min_idle_ms / 1000.0
            for entry in self._entries:
                if len(out) >= count:
                    break
                if entry.delivered and (now - entry.delivered_at) >= min_idle:
                    entry.consumer = consumer
                    entry.delivered_at = now
                    out.append((entry.entry_id, dict(entry.message)))
        return out

    async def pending(self) -> List[str]:
        """Return entry ids currently delivered-but-unacked.

        Returns:
            List[str]: PEL entry ids in stream order.
        """
        async with self._lock:
            return [e.entry_id for e in self._entries if e.delivered]

    async def trim(self, maxlen: int) -> None:
        """Drop the oldest entries so at most ``maxlen`` remain.

        Args:
            maxlen: Maximum number of entries to retain.
        """
        async with self._lock:
            if maxlen < 0:
                maxlen = 0
            if len(self._entries) > maxlen:
                self._entries = self._entries[len(self._entries) - maxlen :]


class RedisStreamBackend(EventStream):
    """Redis Streams backend over a ``redis.asyncio``-compatible client.

    Carries each message as a single JSON field (:data:`_FIELD`) on a stream
    entry so ordering is purely entry-id order. ``XADD`` uses ``MAXLEN ~`` for
    cheap approximate trimming; the consumer group is created with ``MKSTREAM``
    and a re-create is tolerated (``BUSYGROUP``). New entries are read with
    ``XREADGROUP``/``>`` into the calling consumer's PEL; stale PEL entries are
    re-owned with ``XAUTOCLAIM``; PEL depth is read via ``XPENDING``.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        stream_key: Optional[str] = None,
        group: str = _DEFAULT_GROUP,
        maxlen: int = 100_000,
    ) -> None:
        """Initialize the backend.

        Args:
            client: A ``redis.asyncio``-compatible client. When ``None``, a
                client is lazily created from :data:`settings.redis_url` on
                first use.
            stream_key: The Redis stream key. Defaults to
                :data:`settings.mcpgateway_events_redis_stream_prefix`.
            group: The consumer group name shared by all delivery workers.
            maxlen: Approximate ``XADD MAXLEN ~`` cap for the stream.
        """
        self._client = client
        self._stream_key: str = stream_key or settings.mcpgateway_events_redis_stream_prefix
        self._group: str = group
        self._maxlen: int = maxlen

    def _redis(self) -> Any:
        """Return the Redis client, creating one from settings on first use.

        Returns:
            Any: The ``redis.asyncio`` client.
        """
        if self._client is None:
            # Third-Party
            import redis.asyncio as redis_asyncio  # pylint: disable=import-outside-toplevel

            self._client = redis_asyncio.from_url(settings.redis_url)
        return self._client

    @staticmethod
    def _to_str(value: Any) -> str:
        """Decode a possibly-bytes Redis value to ``str``.

        Args:
            value: A ``str`` or ``bytes`` value from the client.

        Returns:
            str: The decoded string (UTF-8 for bytes).
        """
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    @classmethod
    def _decode_fields(cls, fields: Dict[Any, Any]) -> dict:
        """Decode a Redis entry's field map back into the message dict.

        Args:
            fields: The raw field map from the client (keys/values may be bytes).

        Returns:
            dict: The deserialized message dict (empty when absent/malformed).
        """
        for raw_key, raw_val in fields.items():
            if cls._to_str(raw_key) == _FIELD:
                try:
                    return json.loads(cls._to_str(raw_val))
                except (ValueError, TypeError):
                    return {}
        return {}

    async def ensure_group(self) -> None:
        """Create the stream + consumer group, tolerating an existing group."""
        client = self._redis()
        try:
            await client.xgroup_create(name=self._stream_key, groupname=self._group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - redis raises ResponseError 'BUSYGROUP ...'
            if "BUSYGROUP" not in str(exc):
                raise

    async def add(self, message: dict) -> str:
        """``XADD`` a message with approximate ``MAXLEN`` trimming.

        Args:
            message: The stream message dict.

        Returns:
            str: The assigned stream entry id.
        """
        client = self._redis()
        entry_id = await client.xadd(
            self._stream_key,
            {_FIELD: json.dumps(message)},
            maxlen=self._maxlen,
            approximate=True,
        )
        return self._to_str(entry_id)

    async def read_group(self, consumer: str, count: int = 10, block_ms: int = 0) -> List[Tuple[str, dict]]:
        """``XREADGROUP`` new entries (``>``) into ``consumer``'s PEL.

        Args:
            consumer: The consumer (worker) name.
            count: Maximum number of new entries to deliver.
            block_ms: Block for up to this many milliseconds (``0`` = no block).

        Returns:
            List[Tuple[str, dict]]: Newly delivered ``(entry_id, message)`` pairs.
        """
        client = self._redis()
        result = await client.xreadgroup(
            groupname=self._group,
            consumername=consumer,
            streams={self._stream_key: ">"},
            count=count,
            block=block_ms or None,
        )
        return self._flatten(result)

    async def ack(self, entry_id: str) -> None:
        """``XACK`` an entry, removing it from the PEL.

        Args:
            entry_id: The stream entry id to acknowledge.
        """
        client = self._redis()
        await client.xack(self._stream_key, self._group, entry_id)

    async def claim_stale(self, consumer: str, min_idle_ms: int, count: int = 10) -> List[Tuple[str, dict]]:
        """``XAUTOCLAIM`` stale PEL entries to ``consumer``.

        Args:
            consumer: The consumer taking ownership.
            min_idle_ms: Minimum idle time (ms) before an entry is claimable.
            count: Maximum number of entries to claim.

        Returns:
            List[Tuple[str, dict]]: Claimed ``(entry_id, message)`` pairs.
        """
        client = self._redis()
        result = await client.xautoclaim(
            name=self._stream_key,
            groupname=self._group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # XAUTOCLAIM returns [next_cursor, [(entry_id, fields), ...], [deleted_ids]].
        claimed = result[1] if isinstance(result, (list, tuple)) and len(result) >= 2 else []
        out: List[Tuple[str, dict]] = []
        for entry_id, fields in claimed:
            out.append((self._to_str(entry_id), self._decode_fields(fields)))
        return out

    async def pending(self) -> List[str]:
        """Return PEL entry ids via ``XPENDING`` range scan.

        Returns:
            List[str]: Delivered-but-unacked stream entry ids.
        """
        client = self._redis()
        summary = await client.xpending(self._stream_key, self._group)
        depth = summary.get("pending", 0) if isinstance(summary, dict) else 0
        if not depth:
            return []
        details = await client.xpending_range(
            name=self._stream_key,
            groupname=self._group,
            min="-",
            max="+",
            count=depth,
        )
        ids: List[str] = []
        for item in details:
            if isinstance(item, dict):
                ids.append(self._to_str(item.get("message_id")))
            elif isinstance(item, (list, tuple)) and item:
                ids.append(self._to_str(item[0]))
        return ids

    async def trim(self, maxlen: int) -> None:
        """``XTRIM`` the stream to approximately ``maxlen`` entries.

        Args:
            maxlen: Approximate maximum number of entries to retain.
        """
        client = self._redis()
        await client.xtrim(self._stream_key, maxlen=maxlen, approximate=True)

    async def aclose(self) -> None:
        """Close the underlying Redis client if one was created."""
        client = self._client
        if client is not None:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - older redis clients
                await client.close()

    @classmethod
    def _flatten(cls, result: Any) -> List[Tuple[str, dict]]:
        """Flatten an ``XREADGROUP`` result into ``(entry_id, message)`` pairs.

        Args:
            result: The raw client result
                (``[[stream, [(entry_id, fields), ...]], ...]``) or ``None``.

        Returns:
            List[Tuple[str, dict]]: The decoded entries across all streams.
        """
        out: List[Tuple[str, dict]] = []
        if not result:
            return out
        for _stream, entries in result:
            for entry_id, fields in entries:
                out.append((cls._to_str(entry_id), cls._decode_fields(fields)))
        return out


_event_stream: Optional[EventStream] = None


def get_event_stream() -> EventStream:
    """Return the process-wide :class:`EventStream` singleton.

    Selects the Redis-backed backend when ``settings.cache_type == "redis"`` and
    ``settings.redis_url`` is set; otherwise the in-memory backend.

    Returns:
        EventStream: The shared stream backend, created on first access.
    """
    global _event_stream  # pylint: disable=global-statement
    if _event_stream is None:
        if getattr(settings, "cache_type", None) == "redis" and getattr(settings, "redis_url", None):
            _event_stream = RedisStreamBackend()
        else:
            _event_stream = InMemoryStreamBackend()
    return _event_stream
