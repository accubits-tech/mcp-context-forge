# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_subscriptions_router.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

End-to-end router tests for the ``/subscriptions`` REST API (M2a).

These tests mount a minimal FastAPI app with just the subscriptions router and
drive it through a :class:`~fastapi.testclient.TestClient`, overriding the
RBAC/current-user dependency so an authenticated tenant context is supplied
without a real JWT. They assert the full create/read/list/update/delete surface
plus the security-load-bearing behaviours:

* tenant isolation at the object level (BOLA / IDOR -> 404, TC-SUB-028),
* tenant-only paginated listing (TC-SUB-003),
* the master-flag gate (``mcpgateway_events_enabled`` off -> 404 everywhere),
* validating-admission failures (malformed CEL, SSRF-rejected callback) -> 422,
* unauthenticated access -> 401,
* idempotent delete (TC-SUB-006/007).
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timezone

# Third-Party
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import EventSubscription
from mcpgateway.middleware.rbac import get_current_user_with_permissions

TENANT_A = "team-aaa"
TENANT_B = "team-bbb"
USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _make_sub(sub_id: str, team_id: str, *, owner: str = USER_A) -> EventSubscription:
    """Build a detached :class:`EventSubscription` row for service-layer stubs.

    Args:
        sub_id: The subscription id.
        team_id: The owning tenant.
        owner: The owner email.

    Returns:
        EventSubscription: A populated, detached ORM instance.
    """
    return EventSubscription(
        id=sub_id,
        gateway_id=None,
        team_id=team_id,
        owner_email=owner,
        subscriber_kind="sse",
        callback_url=None,
        subscriber_target_ref=None,
        target=None,
        source="https://github.com/acme/repo",
        event_types=["com.github.push"],
        filter_expr=None,
        mode="fanout",
        correlation_key=None,
        correlation_value=None,
        delivery=None,
        active=True,
        expires_at=None,
        created_at=datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def enable_events(monkeypatch):
    """Turn the events master switch on for the duration of a test."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)
    yield


class _AlwaysGrantPermissionService:
    """Stand-in for ``PermissionService`` that grants every permission.

    The real ``@require_permission`` wrapper instantiates
    ``PermissionService(user_context["db"])`` directly, so neutering RBAC for a
    router unit test means patching that class (not a FastAPI dependency).
    """

    def __init__(self, db):  # noqa: ANN001
        self.db = db

    async def check_permission(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return True


@pytest.fixture
def app_factory(monkeypatch):
    """Return a builder that mounts the subscriptions router with a stub service.

    The builder installs (1) a current-user dependency override producing an
    authenticated tenant context, (2) an always-grant permission service so the
    ``@require_permission`` guard passes, and (3) a monkeypatched service double
    whose behaviour the individual tests control. The unauthenticated test opts
    out of (1) so the real auth dependency rejects the credential-less request.
    """
    # First-Party
    from mcpgateway.routers import subscriptions as subs_module

    # Neuter the RBAC permission check (the wrapper builds PermissionService directly).
    monkeypatch.setattr("mcpgateway.middleware.rbac.PermissionService", _AlwaysGrantPermissionService, raising=True)
    # Disable the plugin-manager permission path so the standard RBAC check runs
    # (the wrapper imports get_plugin_manager lazily from the plugins framework).
    monkeypatch.setattr("mcpgateway.plugins.framework.get_plugin_manager", lambda: None, raising=False)

    def build(*, user_email: str = USER_A, team_id: str = TENANT_A, service=None, authed: bool = True):
        app = FastAPI()
        app.include_router(subs_module.router)

        async def _fake_user():
            return {"email": user_email, "full_name": "U", "is_admin": False, "db": object(), "permissions": ["*"]}

        if authed:
            app.dependency_overrides[get_current_user_with_permissions] = _fake_user

        # Resolve the tenant deterministically (no real team tables in this unit test).
        async def _fake_verify(self, email, team_id=None):  # noqa: ANN001
            return team_id_for[email]

        team_id_for = {user_email: team_id}
        monkeypatch.setattr("mcpgateway.routers.subscriptions.TeamManagementService.verify_team_for_user", _fake_verify, raising=True)

        # get_db just needs to yield *something*; the service double ignores it.
        # First-Party
        from mcpgateway.db import get_db  # pylint: disable=import-outside-toplevel

        def _fake_db():
            yield object()

        app.dependency_overrides[get_db] = _fake_db

        if service is not None:
            monkeypatch.setattr("mcpgateway.routers.subscriptions.SubscriptionService", lambda db: service, raising=True)

        return app

    return build


class _StubService:
    """Configurable stand-in for :class:`SubscriptionService`."""

    def __init__(self):
        self.created = None
        self.create_result = None
        self.create_exc = None
        self.get_result = None
        self.get_exc = None
        self.list_result = ([], 0)
        self.update_result = None
        self.update_exc = None
        self.delete_exc = None
        self.delete_calls = []

    async def create(self, db, data, *, user_email, team_id, provisioner=None):  # noqa: ANN001
        self.created = {"data": data, "user_email": user_email, "team_id": team_id}
        if self.create_exc:
            raise self.create_exc
        return self.create_result

    async def get(self, db, sub_id, *, team_id):  # noqa: ANN001
        if self.get_exc:
            raise self.get_exc
        return self.get_result

    async def list(self, db, *, team_id, limit, offset):  # noqa: ANN001
        return self.list_result

    async def update(self, db, sub_id, patch, *, team_id, provisioner=None):  # noqa: ANN001
        if self.update_exc:
            raise self.update_exc
        return self.update_result

    async def delete(self, db, sub_id, *, team_id, provisioner=None):  # noqa: ANN001
        self.delete_calls.append(sub_id)
        if self.delete_exc:
            raise self.delete_exc
        return None


# --------------------------------------------------------------------------- #
# Master-flag gate                                                            #
# --------------------------------------------------------------------------- #


def test_flag_off_returns_404_on_every_endpoint(app_factory, monkeypatch):
    """When events are disabled every method returns an opaque 404."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False, raising=False)
    svc = _StubService()
    svc.get_result = _make_sub("s1", TENANT_A)
    app = app_factory(service=svc)
    client = TestClient(app)

    assert client.post("/subscriptions", json={"subscriber": {"kind": "sse"}, "event_types": ["com.github.push"]}).status_code == 404
    assert client.get("/subscriptions/s1").status_code == 404
    assert client.get("/subscriptions").status_code == 404
    assert client.patch("/subscriptions/s1", json={"active": False}).status_code == 404
    assert client.delete("/subscriptions/s1").status_code == 404


# --------------------------------------------------------------------------- #
# Create                                                                      #
# --------------------------------------------------------------------------- #


def test_create_valid_returns_201_and_body(app_factory, enable_events):
    """TC-SUB-001: a valid POST creates and returns the subscription (201)."""
    svc = _StubService()
    svc.create_result = _make_sub("new-sub-1", TENANT_A)
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.post(
        "/subscriptions",
        json={"subscriber": {"kind": "sse"}, "event_types": ["com.github.push"], "source": "https://github.com/acme/repo"},
    )
    assert resp.status_code == 201
    body = resp.json()
    # SubscriptionRead extends BaseModelWithConfigDict, so FastAPI serializes
    # field names as camelCase (MCP spec compliance): eventTypes, gatewayId, ...
    assert body["id"] == "new-sub-1"
    assert body["eventTypes"] == ["com.github.push"]
    assert body["active"] is True
    # Tenant stamped from the authed user, not the client payload.
    assert svc.created["team_id"] == TENANT_A
    assert svc.created["user_email"] == USER_A


def test_create_malformed_cel_returns_422(app_factory, enable_events):
    """A CEL filter that fails to compile is surfaced as 422."""
    # First-Party
    from mcpgateway.services.events.subscription_service import SubscriptionValidationError

    svc = _StubService()
    svc.create_exc = SubscriptionValidationError("invalid CEL filter: boom")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.post(
        "/subscriptions",
        json={"subscriber": {"kind": "sse"}, "event_types": ["com.github.push"], "filter": "this is not cel"},
    )
    assert resp.status_code == 422


def test_create_ssrf_callback_returns_422(app_factory, enable_events):
    """An SSRF-rejected http_callback URL is surfaced as 422."""
    # First-Party
    from mcpgateway.services.events.subscription_service import SubscriptionValidationError

    svc = _StubService()
    svc.create_exc = SubscriptionValidationError("callback_url rejected: points at a private address")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.post(
        "/subscriptions",
        json={
            "subscriber": {"kind": "http_callback", "callback_url": "http://169.254.169.254/latest/meta-data"},
            "event_types": ["com.github.push"],
        },
    )
    assert resp.status_code == 422


def test_create_forbidden_connector_returns_403(app_factory, enable_events):
    """Subscribing against another tenant's connector is 403 (ForbiddenError)."""
    # First-Party
    from mcpgateway.services.events.subscription_service import ForbiddenError

    svc = _StubService()
    svc.create_exc = ForbiddenError("not permitted to subscribe against this connector")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.post(
        "/subscriptions",
        json={"subscriber": {"kind": "sse"}, "event_types": ["com.github.push"], "gateway_id": "gw-other"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Read / list                                                                 #
# --------------------------------------------------------------------------- #


def test_get_returns_200(app_factory, enable_events):
    """GET /{id} returns the owned subscription."""
    svc = _StubService()
    svc.get_result = _make_sub("s1", TENANT_A)
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.get("/subscriptions/s1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "s1"


def test_get_other_tenant_returns_404(app_factory, enable_events):
    """TC-SUB-028: another tenant's id is reported as 404 (BOLA/IDOR)."""
    # First-Party
    from mcpgateway.services.events.subscription_service import NotFoundError

    svc = _StubService()
    svc.get_exc = NotFoundError("subscription not found: s-other")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.get("/subscriptions/s-other")
    assert resp.status_code == 404


def test_list_is_tenant_only_and_paginated(app_factory, enable_events):
    """TC-SUB-003: list returns the tenant's page plus total/limit/offset."""
    svc = _StubService()
    rows = [_make_sub("s1", TENANT_A), _make_sub("s2", TENANT_A)]
    svc.list_result = (rows, 5)
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.get("/subscriptions?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [s["id"] for s in body["subscriptions"]] == ["s1", "s2"]


# --------------------------------------------------------------------------- #
# Update                                                                      #
# --------------------------------------------------------------------------- #


def test_patch_returns_200(app_factory, enable_events):
    """PATCH /{id} applies the partial update and returns the row."""
    svc = _StubService()
    updated = _make_sub("s1", TENANT_A)
    updated.active = False
    svc.update_result = updated
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.patch("/subscriptions/s1", json={"active": False})
    assert resp.status_code == 200
    assert resp.json()["active"] is False


def test_patch_other_tenant_returns_404(app_factory, enable_events):
    """PATCH against another tenant's id is 404 (object-level authz)."""
    # First-Party
    from mcpgateway.services.events.subscription_service import NotFoundError

    svc = _StubService()
    svc.update_exc = NotFoundError("subscription not found: s-other")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.patch("/subscriptions/s-other", json={"active": False})
    assert resp.status_code == 404


def test_patch_malformed_cel_returns_422(app_factory, enable_events):
    """A PATCH that supplies an uncompilable CEL filter is 422."""
    # First-Party
    from mcpgateway.services.events.subscription_service import SubscriptionValidationError

    svc = _StubService()
    svc.update_exc = SubscriptionValidationError("invalid CEL filter: boom")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.patch("/subscriptions/s1", json={"filter": "garbage("})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Delete                                                                      #
# --------------------------------------------------------------------------- #


def test_delete_returns_204(app_factory, enable_events):
    """DELETE /{id} removes the subscription and returns 204."""
    svc = _StubService()
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.delete("/subscriptions/s1")
    assert resp.status_code == 204
    assert svc.delete_calls == ["s1"]


def test_delete_is_idempotent(app_factory, enable_events):
    """TC-SUB-006/007: deleting an unknown id is a no-op 204 (idempotent)."""
    svc = _StubService()  # delete never raises -> service treats unknown id as no-op
    app = app_factory(service=svc)
    client = TestClient(app)

    first = client.delete("/subscriptions/ghost")
    second = client.delete("/subscriptions/ghost")
    assert first.status_code == 204
    assert second.status_code == 204


def test_delete_other_tenant_returns_404(app_factory, enable_events):
    """DELETE against another tenant's id is 404 (BOLA/IDOR)."""
    # First-Party
    from mcpgateway.services.events.subscription_service import NotFoundError

    svc = _StubService()
    svc.delete_exc = NotFoundError("subscription not found: s-other")
    app = app_factory(service=svc)
    client = TestClient(app)

    resp = client.delete("/subscriptions/s-other")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #


def test_unauthenticated_request_is_rejected(app_factory, enable_events):
    """A request with no credentials is rejected with 401 (or 403)."""
    svc = _StubService()
    svc.list_result = ([], 0)
    # authed=False -> do NOT override the current-user dependency, so the real
    # JWT/bearer stack runs and rejects the credential-less request.
    app = app_factory(service=svc, authed=False)
    client = TestClient(app)

    resp = client.get("/subscriptions")
    assert resp.status_code in (401, 403)
