# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/matching.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

L2 match stage: candidate selection and per-event subscription matching.

This module implements the gateway-side **match-is-routing** step (FRD section
8.6): given a normalized :class:`~mcpgateway.schemas.EventEnvelope` and the
connection (:class:`~mcpgateway.db.Gateway`) it arrived on, decide which
:class:`~mcpgateway.db.EventSubscription` rows should receive it. There is no
downstream routing engine — the subscription match *is* the routing decision
(FRD section 8.6 / section 7.0).

The match is two-staged and tenant-leading:

* **Candidate query (tenant-leading pre-filter).**
  :func:`find_candidate_subscriptions` runs a cheap, indexed SQL pre-filter
  scoped to the connection's tenant. Cross-tenant fan-out is structurally
  impossible: a subscription is a candidate only when ``sub.team_id`` equals the
  connection's ``team_id`` (FRD section 10.1.7 / SC-SEC-029) **and** it is bound
  to this connection (``sub.gateway_id == gateway.id``) or is a cross-provider
  subscription (``sub.gateway_id is None``) whose ``source`` equals the
  envelope's ``source``. Inactive subscriptions and expired correlate
  subscriptions are excluded.
* **In-app refinement (glob + CEL).** :func:`matches` applies the reverse-DNS
  *segment* glob over ``event_types`` (the cheap pre-filter) and, only for the
  survivors, the optional CEL ``filter`` — both delegated to
  :mod:`mcpgateway.services.events.cel_filter`. CEL evaluation is fail-closed: a
  runtime error yields a no-match, never an exception into the match loop
  (FR-18 / FR-19).

The CEL activation (``ctx``) shape produced by :func:`envelope_to_ctx` is::

    {
        "event":   {<envelope dict>},   # full envelope, e.g. event.data.ref
        "data":    <raw provider body>,  # map; e.g. data.amount
        "type":    <reverse-DNS type>,   # str
        "source":  <event source>,       # str
        "subject": <event subject>,      # str | None
    }
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timezone
from typing import Any, List, Optional

# Third-Party
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import EventSubscription
from mcpgateway.services.events import cel_filter

__all__ = [
    "envelope_to_ctx",
    "matches",
    "find_candidate_subscriptions",
]


def _envelope_attr(envelope: Any, name: str) -> Any:
    """Read *name* from an :class:`EventEnvelope` object or its dict form.

    Args:
        envelope: An :class:`~mcpgateway.schemas.EventEnvelope` (or any object
            exposing the attribute) or a plain ``dict``.
        name: The envelope field name (e.g. ``type``).

    Returns:
        Any: The field value, or ``None`` if absent.
    """
    if isinstance(envelope, dict):
        return envelope.get(name)
    return getattr(envelope, name, None)


def envelope_to_ctx(envelope: Any) -> dict:
    """Build the CEL activation dict from an event envelope.

    Accepts either a :class:`~mcpgateway.schemas.EventEnvelope` or its plain
    ``dict`` form. The full envelope is exposed under ``event`` for dotted CEL
    access (``event.data.ref``), with the most commonly filtered scalars
    (``type`` / ``source`` / ``subject``) and the raw provider ``data`` body
    hoisted to top-level activation keys for the fast path.

    Args:
        envelope: The normalized event envelope (object or dict).

    Returns:
        dict: The activation mapping ``{"event", "data", "type", "source",
        "subject"}`` consumed by :func:`mcpgateway.services.events.cel_filter.evaluate`.

    Examples:
        >>> ctx = envelope_to_ctx({"type": "com.github.push", "source": "s", "subject": None, "data": {"ref": "main"}})
        >>> ctx["type"], ctx["data"]["ref"]
        ('com.github.push', 'main')
        >>> ctx["event"]["type"]
        'com.github.push'
    """
    evt_type = _envelope_attr(envelope, "type")
    evt_source = _envelope_attr(envelope, "source")
    evt_subject = _envelope_attr(envelope, "subject")
    evt_id = _envelope_attr(envelope, "id")
    evt_time = _envelope_attr(envelope, "time")
    data = _envelope_attr(envelope, "data")

    event_block = {
        "id": evt_id,
        "source": evt_source,
        "type": evt_type,
        "subject": evt_subject,
        "time": evt_time.isoformat() if isinstance(evt_time, datetime) else evt_time,
        "data": data,
    }
    return {
        "event": event_block,
        "data": data,
        "type": evt_type,
        "source": evt_source,
        "subject": evt_subject,
    }


def matches(sub: EventSubscription, ctx: dict) -> bool:
    """Return whether a subscription matches an event activation.

    A subscription matches when its reverse-DNS ``event_types`` glob admits the
    event ``type`` (cheap pre-filter, ReDoS-safe segment compare) **and** its
    optional CEL ``filter_expr`` evaluates true (or there is no filter). CEL
    compilation/evaluation is fail-closed: a compile or runtime error yields a
    no-match, never an exception into the match loop (FR-18 / FR-19).

    Args:
        sub: The candidate :class:`~mcpgateway.db.EventSubscription`.
        ctx: The activation dict from :func:`envelope_to_ctx`.

    Returns:
        bool: ``True`` only when both the glob and (if present) the CEL filter
        admit the event.
    """
    if not cel_filter.match_event_type(list(sub.event_types or []), ctx.get("type")):
        return False

    filter_expr = sub.filter_expr
    if not filter_expr:
        return True

    try:
        compiled = cel_filter.compile_filter(filter_expr)
    except Exception:  # noqa: BLE001 - fail-closed: a bad stored filter never matches.
        return False
    return cel_filter.evaluate(compiled, ctx)


def _is_expired(sub: EventSubscription, now: datetime) -> bool:
    """Return whether a subscription has passed its ``expires_at`` deadline.

    Args:
        sub: The subscription under test.
        now: The current timezone-aware instant.

    Returns:
        bool: ``True`` if the subscription has expired.
    """
    expires_at = sub.expires_at
    if expires_at is None:
        return False
    # Tolerate naive timestamps from backends that drop tzinfo (e.g. SQLite).
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def find_candidate_subscriptions(db: Session, *, envelope: Any, gateway: Any) -> List[EventSubscription]:
    """Return the subscriptions that match *envelope* on *gateway*, tenant-scoped.

    Runs the tenant-leading candidate pre-filter (FRD section 8.6 / section
    10.1.7) and then the in-app glob + CEL refinement. A subscription is a
    candidate only when it is active **and** its ``team_id`` equals the
    connection's ``team_id`` (cross-tenant isolation, SC-SEC-029) **and** it is
    either bound to this connection (``sub.gateway_id == gateway.id``) or a
    cross-provider subscription (``sub.gateway_id is None``) whose ``source``
    equals the envelope's ``source``. Expired correlate subscriptions are
    skipped. Survivors must then pass :func:`matches` (event-type glob plus the
    optional, fail-closed CEL filter).

    Args:
        db: An active SQLAlchemy session (synchronous).
        envelope: The normalized :class:`~mcpgateway.schemas.EventEnvelope`
            (object or dict).
        gateway: The connection (:class:`~mcpgateway.db.Gateway`) the event
            arrived on; supplies the authoritative ``team_id`` for tenant
            scoping.

    Returns:
        List[EventSubscription]: The matching subscriptions (possibly empty).
    """
    team_id: Optional[str] = getattr(gateway, "team_id", None)
    gateway_id = getattr(gateway, "id", None)
    evt_source = _envelope_attr(envelope, "source")

    # Tenant-leading candidate query (backed by ix_event_subs_tenant_source_*).
    # Tenant scope is enforced in the WHERE clause so cross-tenant fan-out is
    # structurally impossible, not merely filtered in the application layer.
    stmt = (
        select(EventSubscription)
        .where(EventSubscription.team_id == team_id)
        .where(EventSubscription.active.is_(True))
        .where(
            or_(
                EventSubscription.gateway_id == gateway_id,
                (EventSubscription.gateway_id.is_(None)) & (EventSubscription.source == evt_source),
            )
        )
    )
    rows = db.execute(stmt).scalars().all()

    now = datetime.now(timezone.utc)
    ctx = envelope_to_ctx(envelope)

    candidates: List[EventSubscription] = []
    for sub in rows:
        if _is_expired(sub, now):
            continue
        if matches(sub, ctx):
            candidates.append(sub)
    return candidates
