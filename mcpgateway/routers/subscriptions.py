# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/routers/subscriptions.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

REST API for event subscriptions - the ``(filter -> target)`` binding (FRD section 7.7).

This router exposes CRUD over :class:`~mcpgateway.db.EventSubscription` rows,
delegating every validating-admission, provisioning, and tenant-isolation
concern to :class:`~mcpgateway.services.events.subscription_service.SubscriptionService`.
The router itself owns three things only:

* **The master-flag gate.** Every endpoint returns an opaque ``404`` when
  :data:`settings.mcpgateway_events_enabled` is off, so the surface is
  indistinguishable from an unmounted router (mirroring the webhooks ingress
  route). The router is *always* mounted; the gate lives in the handlers.
* **AuthN/Z + tenant derivation.** Each route is guarded by
  :func:`~mcpgateway.middleware.rbac.require_permission` reusing the
  ``gateways.*`` permission family (FR-35: a subscription binds a filter/target
  onto a connector, so it inherits the connector's access scope). The owning
  tenant is derived from the *authenticated* user (never trusted from the
  request body) via
  :meth:`~mcpgateway.services.team_management_service.TeamManagementService.verify_team_for_user`.
* **Service-exception -> HTTP mapping.** ``NotFoundError`` -> ``404`` (a
  cross-tenant row is reported as missing, BOLA/IDOR hardening),
  ``ForbiddenError`` -> ``403``, ``SubscriptionValidationError`` -> ``422``.
"""

# Standard
import asyncio
from datetime import datetime
import json
from typing import Any, AsyncIterator, List, Optional

# Third-Party
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import EventSubscription, get_db
from mcpgateway.middleware.rbac import get_current_user_with_permissions, require_permission
from mcpgateway.schemas import SubscriberRef, SubscriptionCreate, SubscriptionListResponse, SubscriptionRead, SubscriptionUpdate, TargetRef
from mcpgateway.services.events.bus import get_event_bus
from mcpgateway.services.events.egress.streaming import subscribe_stream
from mcpgateway.services.events.subscription_service import (
    ForbiddenError,
    NotFoundError,
    SubscriptionService,
    SubscriptionValidationError,
)
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.services.team_management_service import TeamManagementService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


def _ensure_enabled() -> None:
    """Gate every endpoint on the events master switch.

    Raises:
        HTTPException: ``404`` when :data:`settings.mcpgateway_events_enabled` is
            off, so the router is indistinguishable from an unmounted path.
    """
    if not settings.mcpgateway_events_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


async def _resolve_team_id(db: Session, user_email: str) -> Optional[str]:
    """Resolve the authenticated caller's tenant.

    The tenant is always derived from the authenticated user, never trusted from
    the request body, so a caller cannot create or read subscriptions in another
    tenant (FR-35 / SC-SEC-029).

    Args:
        db: The active session.
        user_email: The authenticated caller's email.

    Returns:
        Optional[str]: The caller's personal-team id (or ``None`` if unresolved).
    """
    team_id = await TeamManagementService(db).verify_team_for_user(user_email)
    # verify_team_for_user returns [] on lookup failure; normalize to None.
    return team_id if isinstance(team_id, str) else None


def _to_read(sub: EventSubscription) -> SubscriptionRead:
    """Map an :class:`EventSubscription` ORM row to a :class:`SubscriptionRead`.

    The persisted row stores the subscriber/target as flat columns; the wire
    schema nests them, so the mapping is explicit rather than ``from_attributes``.

    Args:
        sub: The persisted subscription row.

    Returns:
        SubscriptionRead: The serialized view of the subscription.
    """
    target = TargetRef(**sub.target) if isinstance(sub.target, dict) else None
    return SubscriptionRead(
        id=sub.id,
        gateway_id=sub.gateway_id,
        subscriber=SubscriberRef(kind=sub.subscriber_kind, callback_url=sub.callback_url, target_ref=sub.subscriber_target_ref),
        target=target,
        source=sub.source,
        event_types=list(sub.event_types or []),
        filter=sub.filter_expr,
        mode=sub.mode,
        correlation_key=sub.correlation_key,
        correlation_value=sub.correlation_value,
        active=bool(sub.active),
        expires_at=sub.expires_at,
        created_at=sub.created_at,
    )


@router.post("", response_model=SubscriptionRead, status_code=status.HTTP_201_CREATED)
@require_permission("gateways.create")
async def create_subscription(
    request: SubscriptionCreate,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> SubscriptionRead:
    """Create and provision a new event subscription (FRD section 7.5/7.7.1).

    Args:
        request: The validated create payload.
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Returns:
        SubscriptionRead: The created subscription.

    Raises:
        HTTPException: ``404`` when events are disabled; ``403`` when the caller
            may not subscribe against the referenced connector; ``422`` on a
            validating-admission failure (empty event_types, bad CEL, unknown /
            non-events connector, SSRF-rejected callback_url).
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    try:
        sub = await service.create(db, request, user_email=current_user["email"], team_id=team_id)
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except SubscriptionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_read(sub)


@router.get("", response_model=SubscriptionListResponse)
@require_permission("gateways.read")
async def list_subscriptions(
    limit: int = 50,
    offset: int = 0,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> SubscriptionListResponse:
    """List the caller's subscriptions, paginated (FRD section 7.7, TC-SUB-003).

    Args:
        limit: Maximum number of subscriptions to return.
        offset: Number of subscriptions to skip.
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Returns:
        SubscriptionListResponse: The tenant-scoped page plus the total count.

    Raises:
        HTTPException: ``404`` when events are disabled.
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    rows, total = await service.list(db, team_id=team_id, limit=limit, offset=offset)
    return SubscriptionListResponse(subscriptions=[_to_read(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{sub_id}", response_model=SubscriptionRead)
@require_permission("gateways.read")
async def get_subscription(
    sub_id: str,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> SubscriptionRead:
    """Fetch one subscription, scoped to the caller's tenant.

    Args:
        sub_id: The subscription id.
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Returns:
        SubscriptionRead: The owned subscription.

    Raises:
        HTTPException: ``404`` when events are disabled, or when the id is
            unknown / owned by another tenant (BOLA/IDOR, TC-SUB-028).
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    try:
        sub = await service.get(db, sub_id, team_id=team_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _to_read(sub)


def _json_default(obj: Any) -> str:
    """Serialize non-JSON-native values (datetimes) for the SSE ``data`` field.

    Args:
        obj: The value SSE serialization could not encode natively.

    Returns:
        str: An ISO-8601 string for a :class:`datetime`.

    Raises:
        TypeError: For any other unserializable type.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type not serializable: {type(obj)!r}")


async def _subscription_event_generator(consumer: Any, sub_id: str, request: Request) -> AsyncIterator[dict]:
    """Yield SSE events for one subscription's deliveries off the fan-out bus.

    Mirrors :meth:`mcpgateway.transports.sse_transport.SSETransport.create_sse_response`:
    an initial keepalive primes the connection, then the loop pulls envelopes
    from ``consumer`` with a keepalive-interval timeout. **Only** envelopes
    whose ``subscription.id`` equals ``sub_id`` are forwarded as ``message``
    events - the fan-out bus broadcasts every streamed delivery, so the scope is
    re-enforced here in addition to the per-session ``_target_ref`` routing. The
    loop terminates when the client disconnects (``request.is_disconnected()``)
    or is cancelled; in every case the ``finally`` closes the consumer so the
    underlying bus queue is always unsubscribed (no leak).

    Args:
        consumer: A :class:`~mcpgateway.services.events.egress.streaming.StreamConsumer`
            (or any object exposing ``async get()`` / ``close()``).
        sub_id: The subscription whose deliveries this stream is scoped to.
        request: The client request, polled for disconnect-driven teardown.

    Yields:
        dict: ``sse_starlette`` event dicts (``message``/``keepalive``/``error``).
    """
    # Prime the connection with an immediate keepalive (if enabled).
    if settings.sse_keepalive_enabled:
        yield {"event": "keepalive", "data": "{}", "retry": settings.sse_retry_timeout}

    try:
        while not await request.is_disconnected():
            try:
                timeout = settings.sse_keepalive_interval if settings.sse_keepalive_enabled else None
                envelope = await asyncio.wait_for(consumer.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if settings.sse_keepalive_enabled:
                    yield {"event": "keepalive", "data": "{}", "retry": settings.sse_retry_timeout}
                continue

            # The bus fans every streamed delivery to every consumer; forward
            # only the envelopes that belong to *this* subscription.
            try:
                envelope_sub_id = envelope.get("subscription", {}).get("id")
            except AttributeError:
                envelope_sub_id = None
            if envelope_sub_id != sub_id:
                continue

            yield {"event": "message", "data": json.dumps(envelope, default=_json_default), "retry": settings.sse_retry_timeout}
    except asyncio.CancelledError:
        logger.info("Subscription SSE generator cancelled: %s", sub_id)
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Subscription SSE generator error for %s: %s", sub_id, exc)
        yield {"event": "error", "data": json.dumps({"error": str(exc)}), "retry": settings.sse_retry_timeout}
    finally:
        # Always detach from the bus so the subscriber queue is never leaked.
        consumer.close()
        logger.info("Subscription SSE generator completed: %s", sub_id)


@router.get("/{sub_id}/events")
@require_permission("gateways.read")
async def stream_subscription_events(
    sub_id: str,
    request: Request,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> EventSourceResponse:
    """Stream this subscription's deliveries as Server-Sent Events (FR-30).

    Opens a best-effort live SSE consumer off the in-process fan-out bus the
    :class:`~mcpgateway.services.events.egress.streaming.StreamingEgressAdapter`
    publishes onto. Tenancy is enforced exactly as ``GET /{sub_id}`` -
    :meth:`SubscriptionService.get` reports a cross-tenant or unknown id as
    ``NotFoundError`` -> ``404`` (BOLA/IDOR, TC-SEC-028) before any stream is
    attached - and the stream itself only forwards envelopes whose
    ``subscription.id`` matches ``sub_id``.

    Args:
        sub_id: The subscription id whose deliveries to stream.
        request: The client request (drives disconnect-based teardown).
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Returns:
        EventSourceResponse: A ``text/event-stream`` of this subscription's
        §9.1a delivery envelopes.

    Raises:
        HTTPException: ``404`` when events are disabled, or when the id is
            unknown / owned by another tenant.
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    try:
        sub = await service.get(db, sub_id, team_id=team_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    # Attach a live, per-session consumer to the fan-out bus, filtered by the
    # subscription's streaming target ref so per-session SSE streams do not
    # cross-talk; the generator additionally enforces subscription.id scoping.
    consumer = subscribe_stream(bus=get_event_bus(), target_ref=getattr(sub, "subscriber_target_ref", None))
    return EventSourceResponse(
        _subscription_event_generator(consumer, sub_id, request),
        status_code=200,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-MCP-SSE": "true",
        },
    )


@router.patch("/{sub_id}", response_model=SubscriptionRead)
@require_permission("gateways.update")
async def update_subscription(
    sub_id: str,
    request: SubscriptionUpdate,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> SubscriptionRead:
    """Apply a partial update to a subscription (object-level authz, FRD 7.7).

    Args:
        sub_id: The subscription id.
        request: The partial update (only present keys are applied).
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Returns:
        SubscriptionRead: The updated subscription.

    Raises:
        HTTPException: ``404`` when events are disabled or the id is unknown /
            owned by another tenant; ``422`` when a supplied CEL ``filter`` fails
            to compile (atomic cut-over leaves the prior filter intact).
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    # Only the keys the client actually sent are applied (PATCH semantics); a
    # nested target is flattened to its dict form for the service.
    patch = request.model_dump(exclude_unset=True)
    if "target" in patch and request.target is not None:
        patch["target"] = request.target.model_dump()
    try:
        sub = await service.update(db, sub_id, patch, team_id=team_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except SubscriptionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_read(sub)


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("gateways.delete")
async def delete_subscription(
    sub_id: str,
    current_user=Depends(get_current_user_with_permissions),
    db: Session = Depends(get_db),
) -> None:
    """Delete a subscription and release its upstream hook reference (FRD 7.7.2).

    Idempotent: deleting an unknown / already-removed id returns ``204`` without
    error (TC-SUB-006/007). A row owned by another tenant is reported as ``404``.

    Args:
        sub_id: The subscription id.
        current_user: The authenticated user context (RBAC dependency).
        db: The active database session.

    Raises:
        HTTPException: ``404`` when events are disabled or the id is owned by
            another tenant (BOLA/IDOR).
    """
    _ensure_enabled()
    team_id = await _resolve_team_id(db, current_user["email"])
    service = SubscriptionService(db)
    try:
        await service.delete(db, sub_id, team_id=team_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


__all__: List[Any] = ["router"]
