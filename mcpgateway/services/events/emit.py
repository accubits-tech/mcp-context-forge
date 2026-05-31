# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/emit.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Shared event-emission tail and MCP-native ``evt_id`` synthesis.

This module factors out the behavior-preserving "Normalize -> dedup -> persist
-> publish" tail that both ingress paths share (FRD §6.7 step 7 / §8.2 / §8.3):

* the config-driven **webhook ingress**
  (:meth:`mcpgateway.services.events.ingress_service.IngressService.ingest`),
  which builds an envelope from a verified provider POST, and
* the **MCP-native ingress** adapter, which builds an envelope from an upstream
  ``notifications/resources/updated`` and reuses the very same tail so dedup,
  persistence, the L2 stream spine, and the L1 bus fan-out behave identically.

The public surface is:

* :func:`synthesize_mcp_event_id` - the deterministic ``evt_id`` for MCP-native
  notifications, which carry **no** provider-supplied id (FRD §5.2). The digest
  is a stable sha256 hex over ``gateway_id ‖ source ‖ type ‖ subject ‖ seq``
  where ``seq`` is the upstream-session relay sequence for that
  ``(source, subject)``. A replayed notification reuses its ``seq`` and so
  collapses to the same id; a genuinely new update gets a new ``seq`` and a new
  id (FR-11a, §5.2, TC-MCP-002).
* :func:`publish_normalized_event` - given a built :class:`EventEnvelope`, run
  the connection-scoped dedup (in-process TTL cache backed by the
  ``event_log`` ``(evt_source, evt_id)`` unique-constraint backstop), persist
  one ``event_log`` row, ``XADD`` the accepted event onto the L2 stream, and
  fan it out once onto the L1 bus, returning ``(published, event_log_id)``.

The dedup is scoped by the envelope ``source`` (the connection-scoped
``"//<conn-id>"`` value) so the same provider/synthesized id delivered to two
different connections is not treated as a duplicate (FR-23).
"""

# Future
from __future__ import annotations

# Standard
import hashlib
from typing import Any, Mapping, Optional, Tuple

# Third-Party
from sqlalchemy.exc import IntegrityError

# First-Party
from mcpgateway.db import EventLog
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events.bus import get_event_bus
from mcpgateway.services.events.stream import get_event_stream
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = ["synthesize_mcp_event_id", "publish_normalized_event"]


def synthesize_mcp_event_id(*, gateway_id: str, source: str, type: str, subject: str, seq: Any) -> str:  # noqa: A002 - "type" mirrors the FRD §5.2 formula field name
    """Synthesize a deterministic ``evt_id`` for an MCP-native notification.

    MCP ``notifications/resources/updated`` carries no provider-supplied id, so
    the dedup-on-``(evt_source, evt_id)`` invariant (§5.4.2) and the per-attempt
    idempotency key (§5.4.3) would otherwise be unsatisfiable for the entire
    MCP-native path. The gateway therefore synthesizes a deterministic id per
    FRD §5.2::

        evt_id = sha256(gateway_id ‖ "|" ‖ source ‖ "|" ‖ type ‖ "|"
                        ‖ subject ‖ "|" ‖ seq)

    where ``seq`` is the upstream-session relay sequence number for that
    ``(source, subject)``. This is deterministic per logical event (a
    relayed-twice notification with the same ``seq`` collapses) yet unique
    across distinct notifications (a new update gets a new ``seq``).

    Args:
        gateway_id: The connector / gateway id that owns the upstream session.
        source: The connection-scoped event source (``"//<conn-id>"``).
        type: The reverse-DNS envelope type (e.g. ``com.mcp.resource.updated``).
        subject: The event subject (the resource ``uri``).
        seq: The per-``(source, subject)`` monotonic relay sequence (any value
            with a stable ``str`` rendering).

    Returns:
        str: A 64-character sha256 hex digest.

    Examples:
        >>> a = synthesize_mcp_event_id(gateway_id="g", source="//c", type="com.mcp.resource.updated", subject="res://x", seq=1)
        >>> b = synthesize_mcp_event_id(gateway_id="g", source="//c", type="com.mcp.resource.updated", subject="res://x", seq=1)
        >>> a == b and len(a) == 64
        True
        >>> a != synthesize_mcp_event_id(gateway_id="g", source="//c", type="com.mcp.resource.updated", subject="res://x", seq=2)
        True
    """
    payload = "|".join([str(gateway_id), str(source), str(type), str(subject), str(seq)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _event_dict(envelope: EventEnvelope) -> dict:
    """Render the envelope as a plain dict for the in-process bus / L2 stream.

    Args:
        envelope: The normalized event envelope.

    Returns:
        dict: The event block with an ISO-8601 ``time`` (or ``None``).
    """
    return {
        "id": envelope.id,
        "source": envelope.source,
        "type": envelope.type,
        "subject": envelope.subject,
        "time": envelope.time.isoformat() if envelope.time else None,
        "data": envelope.data,
    }


async def publish_normalized_event(
    db: Any,
    *,
    gateway: Any,
    envelope: EventEnvelope,
    raw_headers: Optional[Mapping[str, str]] = None,
    provider_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Dedup, persist, and publish a normalized event - the shared ingress tail.

    This is the behavior-preserving "Normalize -> dedup -> persist -> publish"
    tail extracted from
    :meth:`mcpgateway.services.events.ingress_service.IngressService.ingest`
    (FRD §6.7 step 7 / §8.2 / §8.3). The pipeline:

    1. **Fast-path dedup** on the connection-scoped ``(source, id)`` key via the
       shared in-process TTL cache. A hit is a duplicate: return
       ``(False, None)`` with no side effect.
    2. **Persist** one ``event_log`` row. The ``(evt_source, evt_id)`` unique
       constraint is the durable dedup backstop for a cache miss: an
       :class:`~sqlalchemy.exc.IntegrityError` means a true duplicate, so roll
       back and return ``(False, <existing row id>)`` without publishing.
    3. **L1 fan-out** - publish the inner event dict onto the in-process bus for
       live convenience consumers (SSE/WS).
    4. **L2 spine** - ``XADD`` the accepted event onto the durable stream so the
       out-of-band delivery workers can match + deliver it at-least-once. A
       stream hiccup must never fail emission (the row is already persisted), so
       it is best-effort with an exception log.

    Args:
        db: An active SQLAlchemy session.
        gateway: The resolved :class:`~mcpgateway.db.Gateway` row that owns the
            event (its ``id`` is stored on the row and the L2 message).
        envelope: The normalized event envelope (``id``/``source``/``type``/
            ``subject``/``time``/``data``).
        raw_headers: Optional raw request headers to persist on the row (webhook
            parity); ``None`` stores ``None`` (the MCP-native path has no HTTP
            request).
        provider_id: Optional provider/descriptor id to persist on the row.

    Returns:
        Tuple[bool, Optional[str]]: ``(published, event_log_id)`` - ``published``
        is ``True`` and ``event_log_id`` is the new row id on a fresh event;
        ``published`` is ``False`` on a duplicate (``event_log_id`` is the
        existing row id when known via the DB backstop, else ``None``).
    """
    # Imported lazily to share the single process-wide dedup cache that the
    # webhook ingress path owns (and that tests reset via ingress_service).
    # First-Party
    from mcpgateway.services.events.ingress_service import _dedup_cache  # pylint: disable=import-outside-toplevel,cyclic-import

    source = envelope.source

    # 1) Fast-path dedup on the connection-scoped (source, id) key.
    dedup_key = f"{source}\x00{envelope.id}"
    if _dedup_cache().seen(dedup_key):
        return (False, None)

    # 2) Durable persistence + DB-level dedup backstop on (evt_source, evt_id).
    row = EventLog(
        evt_id=envelope.id,
        evt_source=envelope.source,
        evt_type=envelope.type,
        evt_subject=envelope.subject,
        evt_time=envelope.time,
        gateway_id=getattr(gateway, "id", None),
        provider_id=provider_id,
        data=envelope.data,
        raw_headers={str(k): str(v) for k, v in raw_headers.items()} if raw_headers is not None else None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Lost the race / cache miss for a true duplicate: the unique constraint
        # is the durable backstop. Drop silently and do not publish.
        db.rollback()
        existing = db.query(EventLog).filter(EventLog.evt_source == envelope.source, EventLog.evt_id == envelope.id).first()
        return (False, existing.id if existing is not None else None)

    event_log_id = row.id

    # 3) In-process L1 fan-out: live convenience consumers (SSE/WS).
    event_dict = _event_dict(envelope)
    await get_event_bus().publish(event_dict)

    # 4) Durable L2 spine: XADD the accepted event for the delivery workers
    #    (FRD §8.5). Additive to the L1 publish; a stream hiccup must not fail
    #    emission since the row is already durably persisted in event_log.
    try:
        await get_event_stream().add(
            {
                "event_log_id": event_log_id,
                "gateway_id": getattr(gateway, "id", None),
                "envelope": event_dict,
            }
        )
    except Exception:  # noqa: BLE001 - never fail emission on an L2 publish error.
        logger.exception("Failed to XADD accepted event %s to the L2 stream", event_log_id)

    return (True, event_log_id)
