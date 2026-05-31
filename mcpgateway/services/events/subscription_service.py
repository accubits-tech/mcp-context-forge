# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/subscription_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

CRUD service for event subscriptions (the ``(filter -> target)`` binding).

A subscription is a first-class, standalone row (FRD section 7.1) that binds a
*match* (``event_types`` reverse-DNS globs + an optional CEL ``filter``) onto a
*target* (an HTTP callback, an SSE/WS stream, or an agent ref) and, when it
references a connector (``gateway_id``), provisions the connector's upstream
webhook so events actually flow (the SUBSCRIBE flow, FRD section 7.5).

This service owns:

* **Tenant scoping + object-level authz (BOLA).** Every row carries
  ``team_id``/``owner_email`` (FR-35); :meth:`SubscriptionService.get`,
  :meth:`~SubscriptionService.update`, and :meth:`~SubscriptionService.delete`
  treat a row belonging to another tenant as *missing* — a
  :class:`NotFoundError` (``404``), never a ``403`` that would leak existence.
* **Validating admission at create.** ``event_types`` must be non-empty; a CEL
  ``filter`` (if present) must compile (FR-18 / section 7.4); a referenced
  ``gateway_id`` must exist, belong to the caller's tenant, and advertise
  ``capabilities.events.webhooksSupported`` (capability negotiation, section
  5.7); an ``http_callback`` subscriber's ``callback_url`` is SSRF-validated
  (gated on ``ssrf_protection_enabled``, mirroring the gateway/tool/a2a service
  create paths).
* **Refcounted upstream provisioning.** When ``gateway_id`` is present,
  :func:`~mcpgateway.services.events.provisioner.ensure_hooks` is awaited so the
  provider webhook is registered once and shared across subscriptions; a
  provider failure aborts the create atomically with no persisted row and no
  dangling refcount (TC-SUB-022). :meth:`~SubscriptionService.delete` releases
  the reference (TC-SUB-020/021) and is idempotent (TC-SUB-006).

Filter-update policy (TC-SUB-004): **atomic cut-over (mutable).** A ``PATCH``
that changes ``filter`` recompiles the new expression first and only then writes
it in place, so a malformed update is rejected (``422``) with the prior filter
left intact (no half-state). This matches FRD section 7.7 which lists
``filter`` among the PATCH-able fields rather than declaring it immutable.

The service takes a synchronous SQLAlchemy :class:`~sqlalchemy.orm.Session` in
its constructor and is instantiated per-request by the ``/subscriptions``
router (mirroring :class:`~mcpgateway.services.token_catalog_service.TokenCatalogService`).
Its methods are ``async`` because the provisioning hooks are async.
"""

# Future
from __future__ import annotations

# Standard
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Third-Party
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import EventSubscription, Gateway
from mcpgateway.schemas import SubscriptionCreate
from mcpgateway.services.events.cel_filter import compile_filter, FilterCompileError
from mcpgateway.services.events.provisioner import ensure_hooks, NoopProvisioner, release_hooks, UpstreamHookProvisioner
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.services_auth import encode_auth
from mcpgateway.utils.url_validation import validate_url_not_internal

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = [
    "NotFoundError",
    "ForbiddenError",
    "SubscriptionValidationError",
    "SubscriptionService",
]


class NotFoundError(Exception):
    """Raised when a subscription does not exist or belongs to another tenant.

    Cross-tenant access is deliberately indistinguishable from a missing row
    (BOLA hardening, SC-SEC-029 / TC-SUB-016): the API maps this to ``404``.
    """


class ForbiddenError(Exception):
    """Raised when the caller is authenticated but not permitted on a resource.

    The API maps this to ``403``.
    """


class SubscriptionValidationError(Exception):
    """Raised when a create/update payload fails validating admission.

    Covers an empty ``event_types`` list, a CEL ``filter`` that fails to
    compile, an unknown / non-events-capable ``gateway_id``, or an
    SSRF-rejected ``callback_url``. The API maps this to ``422``.
    """


def _events_capability(gateway: Gateway) -> Dict[str, Any]:
    """Return the ``events`` capability sub-block of a connector, defensively.

    Args:
        gateway: The connector row.

    Returns:
        Dict[str, Any]: The ``capabilities.events`` map, or an empty dict.
    """
    caps = getattr(gateway, "capabilities", None) or {}
    if not isinstance(caps, dict):
        return {}
    events = caps.get("events") or {}
    return events if isinstance(events, dict) else {}


def _webhooks_supported(gateway: Gateway) -> bool:
    """Return whether a connector advertises events/webhooks support.

    Accepts both the camelCase wire key (``webhooksSupported``) and the
    snake_case form (capability negotiation, FRD section 5.7).

    Args:
        gateway: The connector row.

    Returns:
        bool: ``True`` if the connector declares it supports webhooks/events.
    """
    events = _events_capability(gateway)
    return bool(events.get("webhooksSupported") or events.get("webhooks_supported"))


def _validate_callback_url(callback_url: Optional[str]) -> None:
    """SSRF-validate an HTTP callback URL at create time, when enabled.

    Mirrors the gateway/tool/a2a service create paths: gated on
    ``settings.ssrf_protection_enabled`` and delegating to the shared
    :func:`~mcpgateway.utils.url_validation.validate_url_not_internal`.

    Args:
        callback_url: The subscriber callback URL (may be ``None``).

    Raises:
        SubscriptionValidationError: If the URL resolves to an internal /
            private / link-local / reserved address or uses a bad scheme.
    """
    if not callback_url:
        return
    if not getattr(settings, "ssrf_protection_enabled", True):
        return
    # In-cluster receivers (e.g. a ClusterIP like bud-budprompt) are private by
    # design; an operator opts them in via mcpgateway_events_egress_allow_hosts,
    # which the delivery-time egress guard (validate_and_pin) also honors. Skip
    # the internal-URL denial for an exact (case-insensitive) allow-listed host
    # so a subscription to such a receiver can be created; the egress adapter
    # still re-validates and IP-pins immediately before each send.
    allow_hosts = getattr(settings, "mcpgateway_events_egress_allow_hosts", None) or []
    if allow_hosts:
        host = (urlparse(callback_url).hostname or "").lower()
        if host and host in {h.lower() for h in allow_hosts}:
            return
    try:
        validate_url_not_internal(callback_url)
    except ValueError as exc:
        raise SubscriptionValidationError(f"callback_url rejected: {exc}") from exc


# Sensitive ``delivery.auth`` fields that must never be persisted in plaintext,
# mapped to the encrypted-at-rest sentinel key they are rewritten into. The
# ciphertext is produced by :func:`~mcpgateway.utils.services_auth.encode_auth`
# (AES-GCM, key from ``AUTH_ENCRYPTION_SECRET``) over ``{"v": <plaintext>}`` and
# decoded back the same way at delivery time (FRD section 10.1, SC-SEC-015/039).
_DELIVERY_SECRET_FIELDS = {"secret": "secret_encrypted", "token": "token_encrypted"}


def _encrypt_delivery_secrets(delivery: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Encrypt the per-subscription egress credential before it is persisted.

    The per-subscription outbound signing/bearer credential lives in
    ``delivery["auth"]`` (an ``hmac`` ``secret`` or a ``bearer`` ``token``). To
    close the at-rest plaintext gap (SC-SEC-015 / SC-SEC-039), any plaintext
    ``secret`` / ``token`` is moved into its ``*_encrypted`` sentinel field
    (``encode_auth({"v": <plaintext>})``) and the plaintext key is dropped. The
    HTTP-callback adapter decrypts a throwaway in-memory copy at delivery time;
    the decrypted value is never persisted.

    The rewrite is **idempotent**: when the plaintext key is absent (the block
    is already encrypted, carrying only ``*_encrypted``) it is left untouched, so
    re-persisting an already-stored delivery does not double-encrypt. A returned
    dict is always a fresh copy, so the caller's input is never mutated.

    Args:
        delivery: The raw ``delivery`` configuration (or ``None``).

    Returns:
        Optional[Dict[str, Any]]: A copy of ``delivery`` with any plaintext
        ``auth.secret`` / ``auth.token`` rewritten to ``*_encrypted``, or the
        input unchanged when there is nothing to encrypt / it is ``None``.
    """
    if not isinstance(delivery, dict):
        return delivery
    auth = delivery.get("auth")
    if not isinstance(auth, dict):
        return delivery
    if not any(field in auth for field in _DELIVERY_SECRET_FIELDS):
        # Already encrypted (or no credential present): nothing to rewrite.
        return delivery

    # Copy so an already-persisted / caller-owned dict is never mutated in place.
    new_delivery = dict(delivery)
    new_auth = dict(auth)
    for plain_field, enc_field in _DELIVERY_SECRET_FIELDS.items():
        if plain_field in new_auth:
            value = new_auth.pop(plain_field)
            # Drop an empty/falsy credential entirely rather than store a sentinel.
            if value:
                new_auth[enc_field] = encode_auth({"v": value})
    new_delivery["auth"] = new_auth
    return new_delivery


class SubscriptionService:
    """CRUD + provisioning for :class:`~mcpgateway.db.EventSubscription` rows.

    The service is tenant-aware: every read/update/delete is scoped to a
    ``team_id`` and a row owned by another tenant is reported as missing.
    """

    def __init__(self, db: Session) -> None:
        """Bind the service to a synchronous SQLAlchemy session.

        Args:
            db: The active request-scoped session.
        """
        self.db = db

    # ------------------------------------------------------------------ #
    # Create                                                              #
    # ------------------------------------------------------------------ #

    async def create(
        self,
        db: Session,
        data: SubscriptionCreate,
        *,
        user_email: str,
        team_id: Optional[str],
        provisioner: Optional[UpstreamHookProvisioner] = None,
    ) -> EventSubscription:
        """Validate, provision, and persist a new subscription.

        Args:
            db: The active session (passed explicitly to mirror the contract;
                expected to be the same session bound at construction).
            data: The validated create payload.
            user_email: The owner email stamped onto the row.
            team_id: The owning tenant stamped onto the row.
            provisioner: Upstream provisioner; defaults to
                :class:`~mcpgateway.services.events.provisioner.NoopProvisioner`.

        Returns:
            EventSubscription: The persisted, provisioned subscription row.

        Raises:
            SubscriptionValidationError: On any validating-admission failure
                (empty event_types, bad CEL, unknown/non-events connector, SSRF).
            ForbiddenError: If the referenced connector belongs to another tenant.
            Exception: Re-raises any provider ``register`` error after the create
                is rolled back atomically (no row persisted, no dangling refcount).
        """
        prov = provisioner or NoopProvisioner()

        # --- validating admission (raise SubscriptionValidationError -> 422) ---
        if not data.event_types:
            raise SubscriptionValidationError("event_types must be non-empty")

        if data.filter:
            try:
                compile_filter(data.filter)
            except FilterCompileError as exc:
                raise SubscriptionValidationError(f"invalid CEL filter: {exc}") from exc

        # SSRF-validate an http_callback target at create time (PG-FR-A arm).
        _validate_callback_url(data.subscriber.callback_url)

        gateway: Optional[Gateway] = None
        if data.gateway_id:
            gateway = db.get(Gateway, data.gateway_id)
            if gateway is None:
                raise SubscriptionValidationError(f"gateway not found: {data.gateway_id}")
            # A user must not subscribe against a connector they cannot access.
            if gateway.team_id != team_id:
                raise ForbiddenError("not permitted to subscribe against this connector")
            if not _webhooks_supported(gateway):
                raise SubscriptionValidationError("connector does not support events (capabilities.events.webhooksSupported is false)")

        # --- provision upstream BEFORE persisting so a provider failure aborts ---
        # the create atomically with no orphaned row (TC-SUB-022).
        if gateway is not None:
            await ensure_hooks(db, gateway, list(data.event_types), prov)

        try:
            sub = EventSubscription(
                gateway_id=data.gateway_id,
                team_id=team_id,
                owner_email=user_email,
                subscriber_kind=data.subscriber.kind,
                callback_url=data.subscriber.callback_url,
                subscriber_target_ref=data.subscriber.target_ref,
                target=data.target.model_dump() if data.target is not None else None,
                source=data.source,
                event_types=list(data.event_types),
                filter_expr=data.filter,
                mode=data.mode,
                correlation_key=data.correlation_key,
                correlation_value=data.correlation_value,
                # Encrypt the per-subscription egress credential at rest (SC-SEC-015/039).
                delivery=_encrypt_delivery_secrets(data.delivery) or None,
                active=True,
            )
            db.add(sub)
            db.commit()
            db.refresh(sub)
        except Exception:
            # Persistence failed after a successful provision: release the hook
            # reference so no dangling refcount survives, then re-raise.
            db.rollback()
            if gateway is not None:
                try:
                    await release_hooks(db, gateway, list(data.event_types), prov)
                except Exception:  # noqa: BLE001 - best-effort cleanup; surface the original error.
                    logger.warning("Failed to release upstream hooks after subscription persist failure for gateway %s", data.gateway_id)
            raise

        logger.info("Created subscription %s (team=%s, gateway=%s, types=%s)", sub.id, team_id, data.gateway_id, data.event_types)
        return sub

    # ------------------------------------------------------------------ #
    # Read                                                                #
    # ------------------------------------------------------------------ #

    async def get(self, db: Session, sub_id: str, *, team_id: Optional[str]) -> EventSubscription:
        """Fetch one subscription, scoped to the caller's tenant.

        Args:
            db: The active session.
            sub_id: The subscription id.
            team_id: The caller's tenant.

        Returns:
            EventSubscription: The matching row.

        Raises:
            NotFoundError: If no row exists with that id in the caller's tenant
                (a cross-tenant row is reported as missing — BOLA, TC-SUB-016).
        """
        return self._get_owned(db, sub_id, team_id)

    async def list(self, db: Session, *, team_id: Optional[str], limit: int, offset: int) -> Tuple[List[EventSubscription], int]:
        """List subscriptions for a tenant, paginated.

        Args:
            db: The active session.
            team_id: The caller's tenant.
            limit: Page size.
            offset: Page offset.

        Returns:
            Tuple[List[EventSubscription], int]: The page of rows and the total
            count for the tenant (TC-SUB-003).
        """
        base = db.query(EventSubscription).filter(EventSubscription.team_id == team_id)
        total = base.count()
        rows = base.order_by(EventSubscription.created_at.desc(), EventSubscription.id).limit(limit).offset(offset).all()
        return rows, total

    # ------------------------------------------------------------------ #
    # Update                                                              #
    # ------------------------------------------------------------------ #

    async def update(
        self,
        db: Session,
        sub_id: str,
        patch: Dict[str, Any],
        *,
        team_id: Optional[str],
        provisioner: Optional[UpstreamHookProvisioner] = None,  # noqa: ARG002 - reserved for re-provision on event_types/source change
    ) -> EventSubscription:
        """Apply a partial update to a subscription (object-level authz).

        Filter-update policy is **atomic cut-over**: a new ``filter`` is
        recompiled first and only written on success, so a malformed update is
        rejected with the prior filter intact (no half-state, TC-SUB-004).

        Args:
            db: The active session.
            sub_id: The subscription id.
            patch: The partial update; supported keys: ``filter``,
                ``callback_url``, ``delivery``, ``active``, ``correlation_value``,
                ``target``.
            team_id: The caller's tenant.
            provisioner: Reserved for re-provisioning when ``event_types`` /
                ``source`` change (not yet mutated here).

        Returns:
            EventSubscription: The updated row.

        Raises:
            NotFoundError: If the row is missing or belongs to another tenant.
            SubscriptionValidationError: If a supplied ``filter`` fails to
                compile, or a changed ``callback_url`` is SSRF-rejected
                (PG-FR-A / TC-SEC-055 update arm: rejected at update time before
                any write, so a private/obfuscated target cannot be persisted).
        """
        sub = self._get_owned(db, sub_id, team_id)

        if "filter" in patch:
            new_filter = patch["filter"]
            if new_filter:
                try:
                    compile_filter(new_filter)
                except FilterCompileError as exc:
                    raise SubscriptionValidationError(f"invalid CEL filter: {exc}") from exc
            sub.filter_expr = new_filter

        # SSRF-validate a CHANGED callback_url before writing it (TOCTOU/PG-FR-A
        # update arm). Validation runs first so a rejected target leaves the row
        # intact (no half-state), mirroring the create-time guard.
        if "callback_url" in patch:
            new_callback = patch["callback_url"]
            if new_callback != sub.callback_url:
                _validate_callback_url(new_callback)
            sub.callback_url = new_callback

        if "active" in patch:
            sub.active = bool(patch["active"])
        if "delivery" in patch:
            # Re-encrypt any plaintext credential before persisting (idempotent
            # for an already-encrypted block — SC-SEC-015/039).
            sub.delivery = _encrypt_delivery_secrets(patch["delivery"]) or None
        if "correlation_value" in patch:
            sub.correlation_value = patch["correlation_value"]
        if "target" in patch:
            sub.target = patch["target"]

        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub

    # ------------------------------------------------------------------ #
    # Delete                                                              #
    # ------------------------------------------------------------------ #

    async def delete(self, db: Session, sub_id: str, *, team_id: Optional[str], provisioner: Optional[UpstreamHookProvisioner] = None) -> None:
        """Delete a subscription and release its upstream hook reference.

        Idempotent: deleting an already-removed id is a no-op (no error, refcount
        not double-decremented, TC-SUB-006). Releasing the reference decrements
        the connector's refcount and de-registers the provider webhook only on
        the ``1 -> 0`` edge (TC-SUB-020/021).

        Args:
            db: The active session.
            sub_id: The subscription id.
            team_id: The caller's tenant.
            provisioner: Upstream provisioner; defaults to
                :class:`~mcpgateway.services.events.provisioner.NoopProvisioner`.

        Raises:
            NotFoundError: If the row belongs to another tenant (BOLA, TC-SUB-016).
        """
        prov = provisioner or NoopProvisioner()

        sub = db.get(EventSubscription, sub_id)
        if sub is None:
            # Idempotent: deleting an unknown / already-removed id is a no-op.
            return
        if sub.team_id != team_id:
            # Another tenant's row is indistinguishable from a missing one.
            raise NotFoundError(f"subscription not found: {sub_id}")

        gateway_id = sub.gateway_id
        event_types = list(sub.event_types or [])

        db.delete(sub)
        db.commit()

        if gateway_id and event_types:
            gateway = db.get(Gateway, gateway_id)
            if gateway is not None:
                await release_hooks(db, gateway, event_types, prov)

        logger.info("Deleted subscription %s (team=%s, gateway=%s)", sub_id, team_id, gateway_id)

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    def _get_owned(self, db: Session, sub_id: str, team_id: Optional[str]) -> EventSubscription:
        """Return a row only if it exists AND belongs to *team_id*.

        Args:
            db: The active session.
            sub_id: The subscription id.
            team_id: The caller's tenant.

        Returns:
            EventSubscription: The owned row.

        Raises:
            NotFoundError: If missing or owned by another tenant (BOLA).
        """
        sub = db.get(EventSubscription, sub_id)
        if sub is None or sub.team_id != team_id:
            raise NotFoundError(f"subscription not found: {sub_id}")
        return sub
