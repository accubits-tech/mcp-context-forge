# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_sse_route.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the subscription-scoped SSE consumption route (M4).

``GET /subscriptions/{sub_id}/events`` returns a long-lived
``sse_starlette`` ``EventSourceResponse`` that streams *only this
subscription's* delivery envelopes off the in-process fan-out bus. Because the
response is an open stream these tests drive the route's pieces directly rather
than reading an endless socket:

* the module-level async generator is exercised against a fake stream consumer
  and a fake request so we can assert it (a) yields only envelopes whose
  ``subscription.id`` matches, (b) emits a keepalive on a get-timeout, (c)
  tears down (closes/unsubscribes) the consumer on client disconnect /
  cancellation;
* the route handler itself is exercised through a mounted FastAPI app +
  ``TestClient`` for the security-load-bearing guards: cross-tenant id -> 404
  (TC-SEC-028 parity), flag-off -> 404, and the happy-path content-type.

The streaming generator is iterated under ``asyncio`` with tiny timeouts only -
no real sleeps - so the suite stays hermetic and fast.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone

# Third-Party
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import EventSubscription
from mcpgateway.middleware.rbac import get_current_user_with_permissions
from mcpgateway.routers import subscriptions as subs_module

TENANT_A = "team-aaa"
USER_A = "alice@example.com"


def _make_sub(sub_id: str, team_id: str) -> EventSubscription:
    """Build a detached SSE :class:`EventSubscription` row.

    Args:
        sub_id: The subscription id.
        team_id: The owning tenant.

    Returns:
        EventSubscription: A populated, detached ORM instance.
    """
    return EventSubscription(
        id=sub_id,
        gateway_id=None,
        team_id=team_id,
        owner_email=USER_A,
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


def _envelope(sub_id: str, evt_id: str = "e1") -> dict:
    """Build a minimal §9.1a delivery envelope for ``sub_id``.

    Args:
        sub_id: The owning subscription id stamped at ``subscription.id``.
        evt_id: The event id.

    Returns:
        dict: A delivery envelope shaped like the worker's output.
    """
    return {"event": {"id": evt_id, "type": "com.github.push"}, "subscription": {"id": sub_id, "delivery_id": "d1"}, "idempotency_key": evt_id}


class _FakeConsumer:
    """A scriptable stand-in for ``StreamConsumer``.

    ``get`` returns queued items in order; when exhausted it raises
    :class:`asyncio.TimeoutError` (mimicking the keepalive path) for the first
    drained call and thereafter blocks forever (so a real ``wait_for`` would
    time out). ``close`` records that teardown happened.
    """

    def __init__(self, items):  # noqa: ANN001
        self._items = list(items)
        self.closed = False

    async def get(self):
        if self._items:
            return self._items.pop(0)
        # Nothing left: block forever so the caller's wait_for times out.
        await asyncio.Event().wait()

    def close(self):
        self.closed = True


class _FakeRequest:
    """Minimal request exposing ``is_disconnected`` for the teardown loop."""

    def __init__(self, disconnect_after: int = 10_000):  # noqa: ANN001
        self._calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after


async def _drain(gen, *, max_events: int):  # noqa: ANN001
    """Pull up to ``max_events`` SSE dicts from ``gen`` with a hard timeout.

    Args:
        gen: The async generator under test.
        max_events: Stop after this many yielded events.

    Returns:
        list[dict]: The collected SSE event dicts.
    """
    out = []
    try:
        while len(out) < max_events:
            out.append(await asyncio.wait_for(gen.__anext__(), timeout=1.0))
    except (StopAsyncIteration, asyncio.TimeoutError):
        pass
    return out


# --------------------------------------------------------------------------- #
# Generator-level behaviour (stream scoping, keepalive, teardown)             #
# --------------------------------------------------------------------------- #


def test_generator_yields_only_matching_subscription():
    """Only envelopes whose ``subscription.id`` matches are streamed."""
    matching = _envelope("sub-1", "match")
    foreign = _envelope("sub-2", "other")
    consumer = _FakeConsumer([foreign, matching])
    request = _FakeRequest(disconnect_after=5)

    gen = subs_module._subscription_event_generator(consumer, "sub-1", request)
    events = asyncio.run(_drain(gen, max_events=2))

    # Find the delivery (non-keepalive) events.
    deliveries = [e for e in events if e.get("event") == "message"]
    assert len(deliveries) == 1
    # Standard
    import json  # pylint: disable=import-outside-toplevel

    body = json.loads(deliveries[0]["data"])
    assert body["event"]["id"] == "match"
    assert body["subscription"]["id"] == "sub-1"


def test_generator_emits_keepalive_on_timeout(monkeypatch):
    """A drained consumer (get timeout) yields a keepalive when enabled."""
    monkeypatch.setattr(settings, "sse_keepalive_enabled", True, raising=False)
    monkeypatch.setattr(settings, "sse_keepalive_interval", 0, raising=False)
    consumer = _FakeConsumer([])  # immediately exhausted -> wait_for times out
    request = _FakeRequest(disconnect_after=2)

    gen = subs_module._subscription_event_generator(consumer, "sub-1", request)
    events = asyncio.run(_drain(gen, max_events=1))

    assert any(e.get("event") == "keepalive" for e in events)


def test_generator_closes_consumer_on_disconnect():
    """When the client disconnects the loop ends and unsubscribes the queue."""
    consumer = _FakeConsumer([_envelope("sub-1")])
    request = _FakeRequest(disconnect_after=0)  # disconnected from the first check

    gen = subs_module._subscription_event_generator(consumer, "sub-1", request)
    asyncio.run(_drain(gen, max_events=3))

    assert consumer.closed is True


def test_generator_closes_consumer_on_cancel(monkeypatch):
    """Cancellation (client gone mid-await) still tears the consumer down."""
    # Drop the priming keepalive so the first ``__anext__`` advances straight
    # into the blocking ``consumer.get()`` we want to cancel.
    monkeypatch.setattr(settings, "sse_keepalive_enabled", False, raising=False)
    consumer = _FakeConsumer([])  # blocks forever in get
    request = _FakeRequest(disconnect_after=10_000)

    async def _run():
        gen = subs_module._subscription_event_generator(consumer, "sub-1", request)
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)  # let the generator enter its blocking get()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # aclose() drives the finally block that unsubscribes the consumer.
        await gen.aclose()

    asyncio.run(_run())
    assert consumer.closed is True


# --------------------------------------------------------------------------- #
# Route-level guards (RBAC/tenant/flag) via TestClient                        #
# --------------------------------------------------------------------------- #


class _AlwaysGrantPermissionService:
    """Stand-in for ``PermissionService`` that grants every permission."""

    def __init__(self, db):  # noqa: ANN001
        self.db = db

    async def check_permission(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return True


class _StubService:
    """Configurable stand-in for :class:`SubscriptionService`."""

    def __init__(self):
        self.get_result = None
        self.get_exc = None

    async def get(self, db, sub_id, *, team_id):  # noqa: ANN001
        if self.get_exc:
            raise self.get_exc
        return self.get_result


@pytest.fixture
def app_factory(monkeypatch):
    """Mount the subscriptions router with neutered RBAC + a stub service."""
    monkeypatch.setattr("mcpgateway.middleware.rbac.PermissionService", _AlwaysGrantPermissionService, raising=True)
    monkeypatch.setattr("mcpgateway.plugins.framework.get_plugin_manager", lambda: None, raising=False)

    def build(*, service=None, authed: bool = True):
        app = FastAPI()
        app.include_router(subs_module.router)

        async def _fake_user():
            return {"email": USER_A, "full_name": "U", "is_admin": False, "db": object(), "permissions": ["*"]}

        if authed:
            app.dependency_overrides[get_current_user_with_permissions] = _fake_user

        async def _fake_verify(self, email, team_id=None):  # noqa: ANN001
            return TENANT_A

        monkeypatch.setattr("mcpgateway.routers.subscriptions.TeamManagementService.verify_team_for_user", _fake_verify, raising=True)

        # First-Party
        from mcpgateway.db import get_db  # pylint: disable=import-outside-toplevel

        def _fake_db():
            yield object()

        app.dependency_overrides[get_db] = _fake_db

        if service is not None:
            monkeypatch.setattr("mcpgateway.routers.subscriptions.SubscriptionService", lambda db: service, raising=True)

        return app

    return build


@pytest.fixture
def enable_events(monkeypatch):
    """Turn the events master switch on for the duration of a test."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)
    yield


def test_route_flag_off_returns_404(app_factory, monkeypatch):
    """Flag off -> the SSE route is an opaque 404."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False, raising=False)
    svc = _StubService()
    svc.get_result = _make_sub("s1", TENANT_A)
    client = TestClient(app_factory(service=svc))

    resp = client.get("/subscriptions/s1/events")
    assert resp.status_code == 404


def test_route_other_tenant_returns_404(app_factory, enable_events):
    """TC-SEC-028 parity: a cross-tenant id is reported as 404 (BOLA/IDOR)."""
    # First-Party
    from mcpgateway.services.events.subscription_service import NotFoundError

    svc = _StubService()
    svc.get_exc = NotFoundError("subscription not found: s-other")
    client = TestClient(app_factory(service=svc))

    resp = client.get("/subscriptions/s-other/events")
    assert resp.status_code == 404


def test_route_happy_path_returns_event_stream_response(enable_events, monkeypatch):
    """An owned subscription yields an EventSourceResponse scoped to it.

    The handler is invoked directly (not via an open socket) so the test never
    blocks on a long-lived stream: we assert the response shape + that the
    bus consumer is built filtered by the subscription's streaming target ref.
    """
    # Third-Party
    from sse_starlette.sse import EventSourceResponse  # pylint: disable=import-outside-toplevel

    sub = _make_sub("s1", TENANT_A)
    sub.subscriber_target_ref = "sess-xyz"
    svc = _StubService()
    svc.get_result = sub

    captured = {}

    def _fake_subscribe_stream(*, bus, target_ref=None):  # noqa: ANN001
        captured["target_ref"] = target_ref
        return _FakeConsumer([])

    monkeypatch.setattr("mcpgateway.routers.subscriptions.SubscriptionService", lambda db: svc, raising=True)
    monkeypatch.setattr("mcpgateway.routers.subscriptions.subscribe_stream", _fake_subscribe_stream, raising=True)

    async def _fake_verify(self, email, team_id=None):  # noqa: ANN001
        return TENANT_A

    monkeypatch.setattr("mcpgateway.routers.subscriptions.TeamManagementService.verify_team_for_user", _fake_verify, raising=True)

    async def _call():
        # require_permission unwraps to the bare coroutine when no plugin manager
        # is configured and PermissionService grants; call the underlying handler.
        return await subs_module.stream_subscription_events.__wrapped__(
            sub_id="s1",
            request=_FakeRequest(disconnect_after=0),
            current_user={"email": USER_A, "db": object()},
            db=object(),
        )

    resp = asyncio.run(_call())
    assert isinstance(resp, EventSourceResponse)
    assert resp.media_type == "text/event-stream"
    # The live consumer is scoped to this subscription's streaming target ref.
    assert captured["target_ref"] == "sess-xyz"


def test_route_unauthenticated_is_rejected(app_factory, enable_events):
    """A credential-less request is rejected (401/403) before streaming."""
    svc = _StubService()
    svc.get_result = _make_sub("s1", TENANT_A)
    client = TestClient(app_factory(service=svc, authed=False))

    resp = client.get("/subscriptions/s1/events")
    assert resp.status_code in (401, 403)
