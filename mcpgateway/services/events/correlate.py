# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/correlate.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Correlate / resume: the single-target, request-scoped half of event routing.

Where :mod:`mcpgateway.services.events.matching` implements **fanout** (an event
is broadcast to every matching standing subscription), this module implements
**correlate** (FRD section 7.3 / section 8.9): a short-lived, ephemeral waiter
is opened for a specific in-flight async operation (typically an MCP async
``tools/call`` that returned a *task handle*), and exactly one upstream
completion event resumes exactly one waiting step.

The lifecycle is:

* :func:`open_correlation` - persist an ephemeral ``mode="correlate"``
  :class:`~mcpgateway.db.EventSubscription` keyed on
  ``(team_id, correlation_value)``. It is **fail-closed** on a same-tenant
  collision (TC-COR-012 / SC-COR-011): keys are unique per live waiter, so a
  second open on a value already bound by an active waiter raises
  :class:`CorrelationCollisionError`. The persisted row *is* the
  pending-run <-> task-id mapping, so a restart can re-poll a known task id
  individually (no ``tasks/list``, TC-COR-026).
* :func:`resolve_correlation` - given an inbound completion envelope and the
  connection it arrived on, find the **single** active, non-expired,
  **same-tenant** waiter whose ``correlation_value`` equals the value the
  envelope carries under the waiter's ``correlation_key``. Cross-tenant resume
  is structurally impossible (``sub.team_id == gateway.team_id``, TC-COR-013),
  mirroring the fanout tenant-leading filter (SC-SEC-029). Returns ``None`` when
  nothing waits, so the caller can dead-letter an unmatched completion
  (TC-COR-011).
* :func:`consume_correlation` - the idempotent terminal step: DELETE the
  ephemeral waiter so a replayed/duplicate completion finds nothing and is a
  no-op (TC-COR-010). Deleting also frees the unique ``(team_id,
  correlation_value)`` slot for reuse.
* :func:`expire_correlations` - the TTL sweep: each expired active correlate
  waiter is resolved to *timed-out* (audit) and removed so a late completion
  finds nothing (TC-COR-007 / TC-COR-008).
* :func:`register_task_webhook` - the #523 entry point: open a correlate waiter
  keyed ``correlation_value = task_id`` with the per-call ``webhooks[]`` target,
  so the upstream task's completion resumes the registered webhook.

The correlation carrier is resolved out of the envelope by a restricted dotted
JSONPath (``data.taskId``, ``subject``, ``$.data.taskId`` …) reusing the same
traversal as config-driven ingress (:mod:`mcpgateway.services.events.envelope`),
so the carrier spelling is provider-agnostic and the tolerant-parser philosophy
holds (assert *behavior*, not exact wire names).
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import uuid

# Third-Party
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import EventSubscription
from mcpgateway.services.events.envelope import _traverse  # restricted dotted-JSONPath traversal (ingress-shared)
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = [
    "CorrelationCollisionError",
    "extract_correlation_value",
    "is_correlate_shaped",
    "open_correlation",
    "resolve_correlation",
    "consume_correlation",
    "expire_correlations",
    "register_task_webhook",
]

# The default carrier when none is given (e.g. the #523 task-webhook path):
# the upstream task completion carries the task id under ``data.taskId``. The
# tolerant parser already accepts ``taskId``/``id`` aliases upstream; here we
# fix on the canonical carrier the gateway synthesizes for task completions.
_DEFAULT_TASK_CARRIER = "data.taskId"

# Reverse-DNS suffix that marks a task-completion event regardless of the
# provider DNS root (``com.mcp.task.completed``, ``com.<provider>.task.completed``;
# FRD §2.7 taxonomy). Compared case-insensitively.
_TASK_COMPLETED_SUFFIX = ".task.completed"

# Tolerant task-identifier carrier keys probed under the envelope ``data`` body
# to recognize a task-completion carrier (mirrors the tolerant parser of
# :mod:`mcpgateway.services.events.tasks`: accept ``taskId`` OR ``id``).
_TASK_ID_KEYS = ("taskId", "id", "task_id")


class CorrelationCollisionError(Exception):
    """Raised when a live correlate waiter already binds ``(team_id, value)``.

    Correlation keys are unique per active waiter (one in-flight operation per
    value). A second :func:`open_correlation` on a value already bound by an
    active waiter in the same tenant is rejected fail-closed (TC-COR-012 /
    SC-COR-011) rather than silently fanning a completion to two waiters.
    """


def _envelope_attr(envelope: Any, name: str) -> Any:
    """Read *name* from an :class:`EventEnvelope` object or its dict form.

    Args:
        envelope: An :class:`~mcpgateway.schemas.EventEnvelope` (or any object
            exposing the attribute) or a plain ``dict``.
        name: The envelope field name (e.g. ``data``).

    Returns:
        Any: The field value, or ``None`` if absent.
    """
    if isinstance(envelope, dict):
        return envelope.get(name)
    return getattr(envelope, name, None)


def _envelope_root(envelope: Any) -> dict:
    """Build the dotted-traversal root for a correlation carrier.

    The carrier (``correlation_key``) is resolved against a flat envelope dict
    exposing the top-level scalars (``id`` / ``source`` / ``type`` / ``subject``)
    and the raw provider ``data`` body. This lets a carrier of ``data.taskId``,
    ``subject``, or ``id`` resolve uniformly regardless of whether the inbound
    envelope is an :class:`~mcpgateway.schemas.EventEnvelope` object or its dict
    form (e.g. the worker's rebuilt-from-log block).

    Args:
        envelope: The normalized event envelope (object or dict).

    Returns:
        dict: The traversal root.
    """
    return {
        "id": _envelope_attr(envelope, "id"),
        "source": _envelope_attr(envelope, "source"),
        "type": _envelope_attr(envelope, "type"),
        "subject": _envelope_attr(envelope, "subject"),
        "data": _envelope_attr(envelope, "data"),
    }


def extract_correlation_value(envelope: Any, correlation_key: str) -> Optional[str]:
    """Resolve the correlation carrier out of an event envelope.

    The ``correlation_key`` is a restricted dotted JSONPath (``data.taskId``,
    ``subject``, ``$.data.taskId`` …) traversed over the envelope; the leading
    ``$.`` / ``$`` JSONPath spelling is tolerated. A missing path or a
    non-scalar leaf yields ``None`` (the caller treats a missing carrier as
    "this is not a correlate target").

    Args:
        envelope: The normalized event envelope (object or dict).
        correlation_key: The dotted/jsonpath carrier (e.g. ``data.taskId``).

    Returns:
        Optional[str]: The carrier value as a string, or ``None``.
    """
    if not correlation_key:
        return None
    return _traverse(_envelope_root(envelope), str(correlation_key))


def is_correlate_shaped(envelope: Any) -> bool:
    """Return whether an inbound event looks like an async-task completion.

    This is the **dead-letter gate** for the worker's correlate arm (FRD §8.9 /
    TC-COR-011): when :func:`resolve_correlation` finds no waiting sub, the worker
    must decide whether the event was *meant* to resume a run (so a missing waiter
    is an error worth dead-lettering) or is just an ordinary fanout event (which
    must fall through to the standing-subscription fanout path unchanged).

    An event is correlate-shaped when **either** signal holds — both are
    provider-agnostic so the detector does not hardcode a single DNS root:

    * its envelope ``type`` ends in ``.task.completed`` (the reverse-DNS
      task-completion taxonomy: ``com.mcp.task.completed``,
      ``com.<provider>.task.completed`` — FRD §2.7), **or**
    * its ``data`` body carries a task identifier (``taskId`` / ``id`` /
      ``task_id``), i.e. it is a :func:`~mcpgateway.services.events.tasks.is_task_result`-shaped
      completion carrier.

    Args:
        envelope: The normalized inbound event envelope (object or dict).

    Returns:
        bool: ``True`` when the event is an async-task completion carrier.
    """
    evt_type = _envelope_attr(envelope, "type")
    if isinstance(evt_type, str) and evt_type.lower().endswith(_TASK_COMPLETED_SUFFIX):
        return True

    data = _envelope_attr(envelope, "data")
    if isinstance(data, dict):
        for key in _TASK_ID_KEYS:
            value = data.get(key)
            if value is not None:
                return True
    return False


def _active_waiter_for_value(db: Session, *, team_id: Optional[str], correlation_value: str) -> Optional[EventSubscription]:
    """Return the single active correlate waiter binding ``(team_id, value)``.

    This is the app-level collision/uniqueness check: it filters on
    ``active`` and ``mode == "correlate"`` (the partial-unique index backstops a
    race but does not account for ``active``/expiry).

    Args:
        db: An active synchronous SQLAlchemy session.
        team_id: The owning tenant.
        correlation_value: The bound correlation value.

    Returns:
        Optional[EventSubscription]: The active waiter, or ``None``.
    """
    stmt = (
        select(EventSubscription)
        .where(EventSubscription.team_id == team_id)
        .where(EventSubscription.correlation_value == correlation_value)
        .where(EventSubscription.mode == "correlate")
        .where(EventSubscription.active.is_(True))
    )
    return db.execute(stmt).scalars().first()


def _is_expired(sub: EventSubscription, now: datetime) -> bool:
    """Return whether a waiter has passed its ``expires_at`` deadline.

    Args:
        sub: The waiter under test.
        now: The current timezone-aware instant.

    Returns:
        bool: ``True`` if the waiter has expired.
    """
    expires_at = sub.expires_at
    if expires_at is None:
        return False
    # Tolerate naive timestamps from backends that drop tzinfo (e.g. SQLite).
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


async def open_correlation(
    db: Session,
    *,
    gateway_id: Optional[str],
    team_id: Optional[str],
    correlation_key: str,
    correlation_value: str,
    target: Optional[dict] = None,
    callback_url: Optional[str] = None,
    delivery: Optional[dict] = None,
    ttl_seconds: Optional[int] = None,
) -> EventSubscription:
    """Open an ephemeral correlate waiter, fail-closed on a same-tenant collision.

    Creates an ``active``, ``mode="correlate"``
    :class:`~mcpgateway.db.EventSubscription` keyed on ``(team_id,
    correlation_value)``. When ``ttl_seconds`` is given, ``expires_at`` is set to
    ``now + ttl`` so an abandoned waiter is swept by :func:`expire_correlations`.

    Collision is fail-closed (TC-COR-012 / SC-COR-011): if an active correlate
    waiter already binds the same ``(team_id, correlation_value)``, raise
    :class:`CorrelationCollisionError` rather than create a second. The
    partial-unique index ``uq_event_subs_team_corr_value`` backstops the race:
    an :class:`~sqlalchemy.exc.IntegrityError` on insert is converted to the same
    typed error.

    Args:
        db: An active synchronous SQLAlchemy session.
        gateway_id: The connection the completion is expected to arrive on.
        team_id: The owning tenant (tenant-scopes the waiter and its key).
        correlation_key: The dotted/jsonpath carrier the completion will expose.
        correlation_value: The value to wait for (e.g. a task id).
        target: Optional agent target ref for the resume.
        callback_url: Optional HTTP-callback URL for the resume.
        delivery: Optional delivery configuration for the resume.
        ttl_seconds: Optional time-to-live; ``None`` means the waiter never
            self-expires (it is consumed on completion).

    Returns:
        EventSubscription: The persisted ephemeral waiter.

    Raises:
        CorrelationCollisionError: If an active waiter already binds
            ``(team_id, correlation_value)`` (fail-closed).
    """
    # App-level pre-insert collision check (the primary guard; gives a friendly
    # typed error instead of an IntegrityError, and accounts for active/expiry).
    if _active_waiter_for_value(db, team_id=team_id, correlation_value=correlation_value) is not None:
        raise CorrelationCollisionError(f"correlate waiter already exists for value {correlation_value!r} in tenant {team_id!r}")

    expires_at: Optional[datetime] = None
    if ttl_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))

    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gateway_id,
        team_id=team_id,
        owner_email=None,
        subscriber_kind="http_callback" if callback_url else "agent",
        callback_url=callback_url,
        target=target,
        event_types=[],  # correlate routes on correlation_value, not the type glob.
        mode="correlate",
        correlation_key=correlation_key,
        correlation_value=correlation_value,
        delivery=delivery,
        active=True,
        expires_at=expires_at,
    )
    db.add(sub)
    try:
        db.commit()
    except IntegrityError as exc:
        # Race backstop: the partial-unique index caught a concurrent open.
        db.rollback()
        raise CorrelationCollisionError(f"correlate waiter already exists for value {correlation_value!r} in tenant {team_id!r}") from exc
    db.refresh(sub)
    logger.info("Opened correlate waiter %s (team=%s, value=%s, ttl=%s)", sub.id, team_id, correlation_value, ttl_seconds)
    return sub


def resolve_correlation(db: Session, *, envelope: Any, gateway: Any) -> Optional[EventSubscription]:
    """Resolve the single same-tenant waiter an inbound completion resumes.

    Finds the active, non-expired correlate waiter — scoped to the connection's
    tenant (``sub.team_id == gateway.team_id``, TC-COR-013; cross-tenant resume
    structurally impossible) — whose ``correlation_value`` equals the value the
    *envelope* carries under that waiter's ``correlation_key``. Returns ``None``
    when nothing waits (TC-COR-011: the caller dead-letters an unmatched
    completion).

    Each candidate waiter's own ``correlation_key`` is used to extract the
    carrier from the envelope, so waiters opened with different carriers
    (``data.taskId`` vs ``subject``) all resolve correctly. The exact-match
    lookup is backed by the ``correlation_value`` index.

    Args:
        db: An active synchronous SQLAlchemy session.
        envelope: The inbound completion envelope (object or dict).
        gateway: The connection the completion arrived on; supplies the
            authoritative ``team_id`` for tenant scoping (``None`` tolerated).

    Returns:
        Optional[EventSubscription]: The single matched waiter, or ``None``.
    """
    team_id: Optional[str] = getattr(gateway, "team_id", None)
    now = datetime.now(timezone.utc)
    root = _envelope_root(envelope)

    # Tenant-leading candidate scan over this tenant's live correlate waiters.
    stmt = (
        select(EventSubscription)
        .where(EventSubscription.team_id == team_id)
        .where(EventSubscription.mode == "correlate")
        .where(EventSubscription.active.is_(True))
        .where(EventSubscription.correlation_value.isnot(None))
    )
    for sub in db.execute(stmt).scalars().all():
        if _is_expired(sub, now):
            continue
        carrier = _traverse(root, str(sub.correlation_key or "")) if sub.correlation_key else None
        if carrier is not None and carrier == sub.correlation_value:
            return sub
    return None


async def consume_correlation(db: Session, sub: EventSubscription) -> None:
    """Idempotently consume (DELETE) a resumed waiter.

    The terminal step of a correlate resume: deleting the ephemeral waiter means
    a replayed/duplicate completion resolves to ``None`` and is a no-op
    (TC-COR-010), and frees the unique ``(team_id, correlation_value)`` slot for
    reuse. Tolerant of an already-deleted/detached row (a second consume does
    not raise).

    Args:
        db: An active synchronous SQLAlchemy session.
        sub: The waiter to consume.
    """
    if sub is None:
        return
    live = db.get(EventSubscription, sub.id)
    if live is None:
        # Already consumed (idempotent terminal): nothing to do.
        return
    db.delete(live)
    db.commit()
    logger.info("Consumed correlate waiter %s (team=%s, value=%s)", sub.id, getattr(sub, "team_id", None), getattr(sub, "correlation_value", None))


async def expire_correlations(db: Session, *, now: Optional[datetime] = None) -> int:
    """Sweep expired correlate waiters to timed-out + delete.

    For each active correlate waiter past its ``expires_at`` deadline, resolve it
    to *timed-out* (audit log) and delete it so a late completion finds nothing
    (TC-COR-007 / TC-COR-008). Fanout rows and correlate waiters without a TTL
    are untouched.

    Args:
        db: An active synchronous SQLAlchemy session.
        now: The instant to compare against (defaults to ``utcnow``); injectable
            for deterministic tests.

    Returns:
        int: The number of waiters swept.
    """
    now = now or datetime.now(timezone.utc)
    stmt = select(EventSubscription).where(EventSubscription.mode == "correlate").where(EventSubscription.active.is_(True)).where(EventSubscription.expires_at.isnot(None))
    swept = 0
    for sub in db.execute(stmt).scalars().all():
        if not _is_expired(sub, now):
            continue
        logger.info("Correlate waiter %s timed out (team=%s, value=%s)", sub.id, sub.team_id, sub.correlation_value)
        db.delete(sub)
        swept += 1
    if swept:
        db.commit()
    return swept


async def register_task_webhook(db: Session, *, gateway: Any, team_id: Optional[str], task_id: str, webhook: dict) -> EventSubscription:
    """Open a correlate waiter for an upstream task's completion (#523).

    The #523 per-call ``webhooks[]`` entry point: given a task id and a webhook
    spec (``{"url": ..., "auth": {...}}``), open an ephemeral correlate waiter
    keyed ``correlation_value = task_id`` so the upstream task's completion
    resumes the registered webhook. Collision is fail-closed via
    :func:`open_correlation`.

    Args:
        db: An active synchronous SQLAlchemy session.
        gateway: The connection the task lives behind (supplies ``id`` and the
            tenant if ``team_id`` is not given explicitly).
        team_id: The owning tenant; falls back to ``gateway.team_id``.
        task_id: The upstream task id to wait for.
        webhook: The per-call webhook spec: ``{"url": ..., "auth": {...},
            "ttl_seconds": ...}`` (extra fields ignored — tolerant parser).

    Returns:
        EventSubscription: The persisted ephemeral waiter.

    Raises:
        CorrelationCollisionError: If a waiter already binds this task id in the
            tenant (fail-closed).
    """
    spec = webhook or {}
    resolved_team = team_id if team_id is not None else getattr(gateway, "team_id", None)
    gateway_id = getattr(gateway, "id", None)
    callback_url = spec.get("url") or spec.get("callback_url")
    delivery = {"auth": spec["auth"]} if isinstance(spec.get("auth"), dict) else None
    ttl_seconds = spec.get("ttl_seconds")

    return await open_correlation(
        db,
        gateway_id=gateway_id,
        team_id=resolved_team,
        correlation_key=_DEFAULT_TASK_CARRIER,
        correlation_value=str(task_id),
        target=spec.get("target"),
        callback_url=callback_url,
        delivery=delivery,
        ttl_seconds=ttl_seconds,
    )
