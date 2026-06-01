# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/bus.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

In-process event bus and TTL dedup cache for server-initiated events.

This module provides two lightweight, dependency-free primitives used by the
event ingress machinery:

* :class:`TTLDedupCache` - records keys with a time-to-live so that repeated
  observations of the same event (within the window) can be suppressed. It
  uses a monotonic clock and evicts expired entries lazily.
* :class:`InProcessEventBus` - a fan-out publish/subscribe bus backed by
  :class:`asyncio.Queue` subscribers. It mirrors the
  ``_event_subscribers``/``_publish_event`` pattern used in
  :mod:`mcpgateway.services.tool_service`.

:func:`get_event_bus` returns a process-wide singleton bus.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

__all__ = ["TTLDedupCache", "InProcessEventBus", "get_event_bus"]


class TTLDedupCache:
    """In-memory dedup cache keyed by string with per-entry TTL.

    A key is considered "seen" if it was recorded within the last
    ``ttl_seconds``. Expired entries are evicted lazily on access. A monotonic
    clock is used so the cache is unaffected by wall-clock adjustments.

    Examples:
        >>> cache = TTLDedupCache(ttl_seconds=60)
        >>> cache.seen("evt-1")
        False
        >>> cache.seen("evt-1")
        True
        >>> cache.seen("evt-2")
        False
    """

    def __init__(self, ttl_seconds: float, clock: Optional[Callable[[], float]] = None) -> None:
        """Initialize the cache.

        Args:
            ttl_seconds: Lifetime of each recorded key, in seconds.
            clock: Optional monotonic clock callable returning seconds. Defaults
                to :func:`time.monotonic`. Injectable for deterministic tests.
        """
        self._ttl_seconds: float = ttl_seconds
        self._clock: Callable[[], float] = clock or time.monotonic
        self._expiry: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        """Record a key and report whether it was already present.

        Args:
            key: The dedup key to check and record.

        Returns:
            ``True`` if the key was recorded and is still within its TTL window
            (i.e. a duplicate); ``False`` otherwise. In all cases the key's
            expiry is (re)set to ``now + ttl_seconds``.
        """
        now = self._clock()
        self._evict_expired(now)

        expiry = self._expiry.get(key)
        already_seen = expiry is not None and expiry > now

        # Record / refresh the entry regardless of outcome.
        self._expiry[key] = now + self._ttl_seconds
        return already_seen

    def _evict_expired(self, now: float) -> None:
        """Remove entries whose TTL has elapsed.

        Args:
            now: Current monotonic time, in seconds.
        """
        expired = [key for key, expiry in self._expiry.items() if expiry <= now]
        for key in expired:
            del self._expiry[key]


class InProcessEventBus:
    """Fan-out publish/subscribe bus backed by :class:`asyncio.Queue`.

    Each subscriber owns an :class:`asyncio.Queue`; :meth:`publish` enqueues the
    event onto every active subscriber's queue.

    Examples:
        >>> import asyncio
        >>> bus = InProcessEventBus()
        >>> q = bus.subscribe()
        >>> isinstance(q, asyncio.Queue)
        True
        >>> asyncio.run(bus.publish({"type": "demo"}))
        >>> asyncio.run(q.get())
        {'type': 'demo'}
    """

    def __init__(self) -> None:
        """Initialize the bus with no subscribers."""
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber.

        Returns:
            A fresh :class:`asyncio.Queue` onto which published events will be
            delivered until it is passed to :meth:`unsubscribe`.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber.

        Args:
            queue: A queue previously returned by :meth:`subscribe`. Unknown
                queues are ignored (no error is raised).
        """
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    async def publish(self, event: Dict[str, Any]) -> None:
        """Publish an event to every active subscriber.

        Args:
            event: The event payload to fan out to all subscriber queues.
        """
        # Iterate over a snapshot so concurrent (un)subscribe is safe.
        for queue in list(self._subscribers):
            await queue.put(event)


_event_bus: Optional[InProcessEventBus] = None


def get_event_bus() -> InProcessEventBus:
    """Return the process-wide :class:`InProcessEventBus` singleton.

    Returns:
        The shared event bus instance, created on first access.
    """
    global _event_bus  # pylint: disable=global-statement
    if _event_bus is None:
        _event_bus = InProcessEventBus()
    return _event_bus
