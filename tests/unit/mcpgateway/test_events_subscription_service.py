# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_subscription_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.subscription_service**.

Exercises the :class:`SubscriptionService` CRUD surface against a real
(temporary, in-memory) database. Coverage maps to the M2 SUB gating subset:

* TC-SUB-001 — create persists a tenant-scoped row with a server-generated id.
* TC-SUB-016 / SC-SEC-029 — cross-tenant get/update/delete is a ``NotFoundError``
  (BOLA: a sub of another tenant is indistinguishable from a missing one).
* TC-SUB-003 — list is tenant-filtered and paginated.
* TC-SUB-019 / TC-SUB-020 / TC-SUB-021 — refcount provisioning via a counting
  provisioner: two subs on the same connector register the hook once; deleting
  the last deregisters once; deleting one of two retains the hook.
* TC-SUB-022 — a provider register failure fails the create atomically (no row
  persisted, no dangling refcount).
* TC-SUB-028 — a malformed CEL filter is rejected at create
  (``SubscriptionValidationError`` -> 422); no row persisted.
* SSRF — an ``http_callback`` subscriber whose ``callback_url`` resolves to a
  link-local/metadata address is rejected at create (gated on
  ``ssrf_protection_enabled``).
* TC-SUB-006 / TC-SUB-007 — double delete is idempotent; identical re-create
  policy (two distinct ids; one upstream hook shared).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_subscription_service.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EventSubscription, Gateway
from mcpgateway.schemas import SubscriberRef, SubscriptionCreate
from mcpgateway.services.events.provisioner import UpstreamHookProvisioner
from mcpgateway.services.events.subscription_service import (
    ForbiddenError,
    NotFoundError,
    SubscriptionService,
    SubscriptionValidationError,
)

TEAM_A = "team-a"
TEAM_B = "team-b"
USER_A = "a@example.com"
USER_B = "b@example.com"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session():
    """Create a fresh in-memory database session with all tables built."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_gateway(db, *, team_id=TEAM_A, webhooks_supported=True) -> Gateway:
    """Persist a minimal events-capable Gateway row to subscribe against."""
    caps = {"events": {"webhooksSupported": webhooks_supported}} if webhooks_supported is not None else {}
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities=caps,
        team_id=team_id,
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


_UNSET = object()


def _sse_create(gateway_id=None, *, event_types=_UNSET, filter_expr=None, source=None) -> SubscriptionCreate:
    """Build a minimal SSE SubscriptionCreate (no callback_url required)."""
    if event_types is _UNSET:
        event_types = ["com.github.push"]
    return SubscriptionCreate(
        gateway_id=gateway_id,
        subscriber=SubscriberRef(kind="sse"),
        event_types=event_types,
        filter=filter_expr,
        source=source,
    )


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #


class MockProvisioner(UpstreamHookProvisioner):
    """A provisioner that COUNTS register/deregister calls for assertions."""

    def __init__(self, *, fail_register: bool = False) -> None:
        """Initialize the counting/failing provisioner.

        Args:
            fail_register: When ``True``, :meth:`register` raises to simulate a
                provider API failure.
        """
        self.register_calls: list[tuple[str, tuple[str, ...]]] = []
        self.deregister_calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_register = fail_register

    async def register(self, gateway, event_types):  # type: ignore[override]
        """Record a register call and return a synthetic hook ref (or fail).

        Args:
            gateway: The connection row.
            event_types: Provider event types covered by the hook.

        Returns:
            dict: A synthetic hook ref.

        Raises:
            RuntimeError: When ``fail_register`` is set.
        """
        self.register_calls.append((gateway.id, tuple(event_types)))
        if self.fail_register:
            raise RuntimeError("provider 500")
        return {"external_hook_id": f"hook-{uuid.uuid4().hex[:8]}", "scopes_granted": ["admin:repo_hook"]}

    async def deregister(self, gateway, event_types, hook_ref):  # type: ignore[override]
        """Record a deregister call.

        Args:
            gateway: The connection row.
            event_types: Provider event types the hook covered.
            hook_ref: The stored hook entry.
        """
        self.deregister_calls.append((gateway.id, tuple(event_types)))


def _svc(session) -> SubscriptionService:
    """Construct the service under test bound to *session*."""
    return SubscriptionService(session)


# --------------------------------------------------------------------------- #
# TC-SUB-001: create persists a tenant-scoped row with a server-generated id    #
# --------------------------------------------------------------------------- #


def test_create_persists_tenant_scoped_row(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)

    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    assert sub.id  # server-generated id
    assert sub.team_id == TEAM_A
    assert sub.owner_email == USER_A
    assert sub.gateway_id == gw.id
    assert sub.event_types == ["com.github.push"]
    assert sub.active is True

    persisted = session.get(EventSubscription, sub.id)
    assert persisted is not None
    assert persisted.team_id == TEAM_A


# --------------------------------------------------------------------------- #
# TC-SUB-016 / SC-SEC-029: cross-tenant access -> NotFoundError (BOLA)           #
# --------------------------------------------------------------------------- #


def test_get_other_tenant_is_not_found(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    # Owner can read it.
    assert asyncio.run(svc.get(session, sub.id, team_id=TEAM_A)).id == sub.id

    # Another tenant cannot distinguish it from a missing row.
    with pytest.raises(NotFoundError):
        asyncio.run(svc.get(session, sub.id, team_id=TEAM_B))


def test_update_other_tenant_is_not_found(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    with pytest.raises(NotFoundError):
        asyncio.run(svc.update(session, sub.id, {"active": False}, team_id=TEAM_B))


def test_delete_other_tenant_is_not_found(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    with pytest.raises(NotFoundError):
        asyncio.run(svc.delete(session, sub.id, team_id=TEAM_B, provisioner=MockProvisioner()))

    # Still present for the real owner.
    assert session.get(EventSubscription, sub.id) is not None


# --------------------------------------------------------------------------- #
# TC-SUB-003: list is tenant-filtered + paginated                              #
# --------------------------------------------------------------------------- #


def test_list_is_tenant_filtered_and_paginated(session):
    gw_a = _make_gateway(session, team_id=TEAM_A)
    gw_b = _make_gateway(session, team_id=TEAM_B)
    svc = _svc(session)

    for _ in range(15):
        asyncio.run(svc.create(session, _sse_create(gw_a.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))
    for _ in range(5):
        asyncio.run(svc.create(session, _sse_create(gw_b.id), user_email=USER_B, team_id=TEAM_B, provisioner=MockProvisioner()))

    items, total = asyncio.run(svc.list(session, team_id=TEAM_A, limit=10, offset=0))
    assert total == 15  # only tenant A's subs counted
    assert len(items) == 10
    assert all(s.team_id == TEAM_A for s in items)

    page2, total2 = asyncio.run(svc.list(session, team_id=TEAM_A, limit=10, offset=10))
    assert total2 == 15
    assert len(page2) == 5

    # Tenant B sees only its own.
    items_b, total_b = asyncio.run(svc.list(session, team_id=TEAM_B, limit=50, offset=0))
    assert total_b == 5
    assert all(s.team_id == TEAM_B for s in items_b)


# --------------------------------------------------------------------------- #
# TC-SUB-019: two subs same connector -> refcount=2, ONE register total         #
# --------------------------------------------------------------------------- #


def test_two_subs_same_connector_register_once(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner()

    asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))
    asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 2
    assert len(prov.register_calls) == 1


# --------------------------------------------------------------------------- #
# TC-SUB-020: delete the last sub -> refcount 0, ONE deregister                 #
# --------------------------------------------------------------------------- #


def test_delete_last_sub_deregisters_once(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner()

    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))
    asyncio.run(svc.delete(session, sub.id, team_id=TEAM_A, provisioner=prov))

    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})
    assert len(prov.deregister_calls) == 1
    assert session.get(EventSubscription, sub.id) is None


# --------------------------------------------------------------------------- #
# TC-SUB-021: delete one of two -> hook retained, refcount=1, no deregister      #
# --------------------------------------------------------------------------- #


def test_delete_one_of_two_retains_hook(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner()

    sub1 = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))
    asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))

    asyncio.run(svc.delete(session, sub1.id, team_id=TEAM_A, provisioner=prov))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    assert len(prov.deregister_calls) == 0
    assert len(prov.register_calls) == 1


# --------------------------------------------------------------------------- #
# TC-SUB-022: provider register fails -> create fails atomically                #
# --------------------------------------------------------------------------- #


def test_register_failure_fails_create_atomically(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner(fail_register=True)

    with pytest.raises(RuntimeError):
        asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))

    # No subscription row persisted and no dangling refcount.
    assert session.query(EventSubscription).count() == 0
    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})


# --------------------------------------------------------------------------- #
# TC-SUB-028: malformed CEL filter at create -> 422, no row persisted           #
# --------------------------------------------------------------------------- #


def test_malformed_cel_rejected_at_create(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(
            svc.create(
                session,
                _sse_create(gw.id, filter_expr="data.amount >"),
                user_email=USER_A,
                team_id=TEAM_A,
                provisioner=MockProvisioner(),
            )
        )

    assert session.query(EventSubscription).count() == 0


def test_valid_cel_filter_persisted(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)

    sub = asyncio.run(
        svc.create(
            session,
            _sse_create(gw.id, filter_expr='data.ref == "refs/heads/main"'),
            user_email=USER_A,
            team_id=TEAM_A,
            provisioner=MockProvisioner(),
        )
    )
    assert sub.filter_expr == 'data.ref == "refs/heads/main"'


# --------------------------------------------------------------------------- #
# event_types validation                                                        #
# --------------------------------------------------------------------------- #


def test_empty_event_types_rejected(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(
            svc.create(
                session,
                _sse_create(gw.id, event_types=[]),
                user_email=USER_A,
                team_id=TEAM_A,
                provisioner=MockProvisioner(),
            )
        )
    assert session.query(EventSubscription).count() == 0


# --------------------------------------------------------------------------- #
# webhooksSupported negotiation                                                 #
# --------------------------------------------------------------------------- #


def test_gateway_without_webhooks_support_rejected(session):
    gw = _make_gateway(session, team_id=TEAM_A, webhooks_supported=False)
    svc = _svc(session)

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))
    assert session.query(EventSubscription).count() == 0


def test_missing_gateway_rejected(session):
    svc = _svc(session)

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(svc.create(session, _sse_create("does-not-exist"), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))


def test_cross_tenant_gateway_rejected(session):
    """A user must not subscribe against a connector owned by another tenant."""
    gw = _make_gateway(session, team_id=TEAM_B)
    svc = _svc(session)

    with pytest.raises((SubscriptionValidationError, ForbiddenError)):
        asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))


# --------------------------------------------------------------------------- #
# SSRF: callback_url to a metadata/link-local address is rejected at create     #
# --------------------------------------------------------------------------- #


def test_callback_url_ssrf_rejected_at_create(session, monkeypatch):
    monkeypatch.setattr(settings, "ssrf_protection_enabled", True)
    svc = _svc(session)
    create = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="http://169.254.169.254/latest/meta-data/"),
        source="https://github.com/acme/*",
        event_types=["com.github.push"],
    )

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(svc.create(session, create, user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))
    assert session.query(EventSubscription).count() == 0


def test_callback_url_ssrf_skipped_when_disabled(session, monkeypatch):
    monkeypatch.setattr(settings, "ssrf_protection_enabled", False)
    svc = _svc(session)
    create = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="http://169.254.169.254/latest/meta-data/"),
        source="https://github.com/acme/*",
        event_types=["com.github.push"],
    )

    sub = asyncio.run(svc.create(session, create, user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))
    assert sub.callback_url == "http://169.254.169.254/latest/meta-data/"


# --------------------------------------------------------------------------- #
# TC-SUB-006: double delete is idempotent                                       #
# --------------------------------------------------------------------------- #


def test_double_delete_is_idempotent(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner()

    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))

    asyncio.run(svc.delete(session, sub.id, team_id=TEAM_A, provisioner=prov))
    # Deleting again is a no-op, not an error; refcount not double-decremented.
    asyncio.run(svc.delete(session, sub.id, team_id=TEAM_A, provisioner=prov))

    assert len(prov.deregister_calls) == 1
    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})


# --------------------------------------------------------------------------- #
# TC-SUB-007: identical re-create -> distinct id, single upstream hook           #
# --------------------------------------------------------------------------- #


def test_identical_recreate_distinct_id_single_hook(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    prov = MockProvisioner()

    sub1 = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))
    sub2 = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=prov))

    assert sub1.id != sub2.id
    assert len(prov.register_calls) == 1  # single upstream hook shared
    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 2


# --------------------------------------------------------------------------- #
# update: atomic filter cut-over (recompile + in-place) per chosen policy        #
# --------------------------------------------------------------------------- #


def test_update_filter_atomic_cutover(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    updated = asyncio.run(svc.update(session, sub.id, {"filter": 'data.ref == "refs/heads/dev"'}, team_id=TEAM_A))
    assert updated.filter_expr == 'data.ref == "refs/heads/dev"'


def test_update_invalid_filter_rejected_no_halfstate(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(
        svc.create(
            session,
            _sse_create(gw.id, filter_expr='data.ref == "refs/heads/main"'),
            user_email=USER_A,
            team_id=TEAM_A,
            provisioner=MockProvisioner(),
        )
    )

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(svc.update(session, sub.id, {"filter": "data.amount >"}, team_id=TEAM_A))

    # No half-state: the original filter is intact.
    session.refresh(sub)
    assert sub.filter_expr == 'data.ref == "refs/heads/main"'


def test_update_active_toggle(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    svc = _svc(session)
    sub = asyncio.run(svc.create(session, _sse_create(gw.id), user_email=USER_A, team_id=TEAM_A, provisioner=MockProvisioner()))

    paused = asyncio.run(svc.update(session, sub.id, {"active": False}, team_id=TEAM_A))
    assert paused.active is False


def test_get_missing_is_not_found(session):
    svc = _svc(session)
    with pytest.raises(NotFoundError):
        asyncio.run(svc.get(session, "no-such-id", team_id=TEAM_A))


# --- WS1: create-time callback allow-list bypass -------------------------------
# A subscription whose http_callback points at an in-cluster ClusterIP (private,
# http) is rejected by the shared internal-URL guard unless the operator opts the
# host into ``mcpgateway_events_egress_allow_hosts`` (the same list the egress
# adapter honors at send time). These lock that bypass to an EXACT host match.
from mcpgateway.services.events import subscription_service as _ss  # noqa: E402


def test_allow_listed_internal_callback_bypasses_create_check(monkeypatch):
    """An allow-listed private callback host passes create-time validation."""
    monkeypatch.setattr(_ss.settings, "ssrf_protection_enabled", True, raising=False)
    monkeypatch.setattr(_ss.settings, "mcpgateway_events_egress_allow_hosts", ["10.43.0.9"], raising=False)
    # No raise: the allow-listed host short-circuits the internal-URL denial.
    _ss._validate_callback_url("http://10.43.0.9:3015/v1/events")


def test_non_listed_internal_callback_rejected_at_create(monkeypatch):
    """A private callback host that is NOT allow-listed is still rejected."""
    monkeypatch.setattr(_ss.settings, "ssrf_protection_enabled", True, raising=False)
    monkeypatch.setattr(_ss.settings, "mcpgateway_events_egress_allow_hosts", [], raising=False)
    with pytest.raises(_ss.SubscriptionValidationError):
        _ss._validate_callback_url("http://10.43.0.9:3015/v1/events")


def test_create_allow_list_is_exact_host_not_suffix(monkeypatch):
    """The allow-list matches the exact host, not a prefix/suffix lookalike."""
    monkeypatch.setattr(_ss.settings, "ssrf_protection_enabled", True, raising=False)
    monkeypatch.setattr(_ss.settings, "mcpgateway_events_egress_allow_hosts", ["10.43.0.9"], raising=False)
    with pytest.raises(_ss.SubscriptionValidationError):
        _ss._validate_callback_url("http://10.43.0.99:3015/v1/events")
