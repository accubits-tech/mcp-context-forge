# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_523_webhooks.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

M7 #523 ``tools/call`` per-call ``webhooks[]`` hook + flag-gated TTL sweeper.

These tests cover the two remaining M7 deliverables (additive + flag-gated, all
network mocked):

* :func:`~mcpgateway.services.events.task_webhook.process_tool_call_webhooks` -
  the #523 service hook: when a ``tools/call`` response is a *task handle*
  (:func:`~mcpgateway.services.events.tasks.is_task_result`) **and** the call
  carried a ``webhooks[]`` list, it opens an ephemeral correlate waiter per
  webhook entry (target/callback drawn from the webhook spec, TC-COR-025
  async-switch) and kicks off the poller->deliver flow. A non-task response (or
  an empty/absent ``webhooks[]``) registers nothing. The hook is a no-op when
  ``settings.mcpgateway_events_enabled`` is off.
* The flag-gated TTL sweeper
  (:meth:`~mcpgateway.services.events.delivery_worker.DeliveryWorker.sweep_expired_correlations`)
  removes expired correlate waiters (TC-COR-008) when enabled, and is a no-op
  when the sweep flag is off.

A final smoke-import of :mod:`mcpgateway.main` guards the additive wiring.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_523_webhooks.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timedelta, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EventSubscription, Gateway
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.services.events import task_webhook

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

TEAM_ID = "team-523-aaaaaaaaaaaaaaaaaaaaaaaaaa"
TASK_ID = "task-523-001"
WEBHOOK_URL = "https://consumer.example/webhook"
CORR_KEY = "data.taskId"


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory():
    """Yield a sessionmaker bound to a fresh shared in-memory database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield maker
    finally:
        engine.dispose()


@pytest.fixture
def db(session_factory):
    """A single session over the shared in-memory database (for setup/asserts)."""
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _events_enabled(monkeypatch):
    """Enable the events master switch for the hook by default."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)
    yield


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """Reset process-wide bus/stream/dedup singletons (the poller publishes)."""
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


def _make_gateway(db, *, team_id: str = TEAM_ID) -> Gateway:
    """Persist a minimal connection (Gateway) carrying *team_id*."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        team_id=team_id,
        capabilities={},
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _correlate_subs(db) -> list:
    """Return all live correlate waiters in the database."""
    return list(db.execute(select(EventSubscription).where(EventSubscription.mode == "correlate")).scalars().all())


# --------------------------------------------------------------------------- #
# process_tool_call_webhooks: task-handle response + webhooks[] -> waiter      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_registers_resolvable_waiter(session_factory, db, monkeypatch):
    """TC-COR-025: a task-handle response + webhooks[] opens a resolvable correlate waiter."""
    gw = _make_gateway(db)

    # The async tool returned a task handle (taskId), not a final result.
    response = {"taskId": TASK_ID, "status": "working"}
    webhooks = [{"url": WEBHOOK_URL, "auth": {"type": "bearer", "token": "s3cr3t"}}]

    # The poller->deliver flow is kicked but must be mockable (no network/polling).
    kicked = {"called": 0, "subs": []}

    async def fake_poll_and_deliver(*, sub, gateway, send_task_get, **kw):  # noqa: ANN001
        kicked["called"] += 1
        kicked["subs"].append(sub)
        return True

    monkeypatch.setattr(task_webhook, "poll_and_deliver", fake_poll_and_deliver)

    async def fake_send_task_get(task_id: str) -> dict:
        return {"taskId": task_id, "status": "completed"}

    opened = await task_webhook.process_tool_call_webhooks(
        db,
        response=response,
        webhooks=webhooks,
        gateway=gw,
        team_id=gw.team_id,
        send_task_get=fake_send_task_get,
    )
    # The poller is kicked fire-and-forget (non-blocking); yield so the spawned
    # background task gets a turn before we assert it ran.
    await _drain_tasks()

    # Exactly one ephemeral correlate waiter was opened, keyed on the task id,
    # carrying the webhook's callback url (the async-switch resume target).
    assert len(opened) == 1
    waiter = opened[0]
    assert waiter.mode == "correlate"
    assert waiter.correlation_value == TASK_ID
    assert waiter.callback_url == WEBHOOK_URL
    assert waiter.team_id == gw.team_id

    # It is resolvable by a same-tenant completion carrying that task id.
    resolved = correlate_mod.resolve_correlation(
        db,
        envelope={"id": "e", "source": f"//{gw.id}", "type": "com.mcp.task.completed", "subject": TASK_ID, "data": {"taskId": TASK_ID, "status": "completed"}},
        gateway=gw,
    )
    assert resolved is not None
    assert resolved.id == waiter.id

    # The poller->deliver flow was kicked for the opened waiter.
    assert kicked["called"] == 1
    assert kicked["subs"][0].id == waiter.id


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_tolerant_id_and_callback_aliases(session_factory, db, monkeypatch):
    """Tolerant parsing: ``id`` aliases ``taskId`` and ``callback_url`` aliases ``url``."""
    gw = _make_gateway(db)
    response = {"id": TASK_ID, "state": "working"}  # tolerant: id + state aliases
    webhooks = [{"callback_url": "https://b.example/hook"}]

    monkeypatch.setattr(task_webhook, "poll_and_deliver", _noop_poll_and_deliver)

    opened = await task_webhook.process_tool_call_webhooks(
        db,
        response=response,
        webhooks=webhooks,
        gateway=gw,
        team_id=gw.team_id,
        send_task_get=_noop_send_task_get,
    )
    assert len(opened) == 1
    assert opened[0].correlation_value == TASK_ID
    assert opened[0].callback_url == "https://b.example/hook"


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_duplicate_task_fail_closed(session_factory, db, monkeypatch):
    """Two webhook entries for the SAME task open exactly one waiter (collision fail-closed).

    Correlation keys are unique per live waiter ``(team_id, correlation_value)``,
    so a second webhook entry for the same task id does not open a second waiter
    (TC-COR-012); the first wins and the duplicate is skipped fail-closed.
    """
    gw = _make_gateway(db)
    response = {"taskId": TASK_ID, "status": "working"}
    webhooks = [
        {"url": "https://a.example/hook"},
        {"url": "https://b.example/hook"},
    ]

    monkeypatch.setattr(task_webhook, "poll_and_deliver", _noop_poll_and_deliver)

    opened = await task_webhook.process_tool_call_webhooks(
        db,
        response=response,
        webhooks=webhooks,
        gateway=gw,
        team_id=gw.team_id,
        send_task_get=_noop_send_task_get,
    )
    assert len(opened) == 1
    assert opened[0].callback_url == "https://a.example/hook"
    assert len(_correlate_subs(db)) == 1


# --------------------------------------------------------------------------- #
# process_tool_call_webhooks: nothing registered for non-task / empty webhooks #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_non_task_registers_nothing(session_factory, db, monkeypatch):
    """A non-task (final) tool result registers no waiter and kicks no poll."""
    gw = _make_gateway(db)
    response = {"content": [{"type": "text", "text": "done"}]}  # ordinary result, no taskId
    webhooks = [{"url": WEBHOOK_URL}]

    kicked = {"called": 0}

    async def fake_poll_and_deliver(**kw):  # noqa: ANN003
        kicked["called"] += 1
        return True

    monkeypatch.setattr(task_webhook, "poll_and_deliver", fake_poll_and_deliver)

    opened = await task_webhook.process_tool_call_webhooks(
        db,
        response=response,
        webhooks=webhooks,
        gateway=gw,
        team_id=gw.team_id,
        send_task_get=_noop_send_task_get,
    )
    assert opened == []
    assert _correlate_subs(db) == []
    assert kicked["called"] == 0


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_no_webhooks_registers_nothing(session_factory, db, monkeypatch):
    """A task-handle response WITHOUT any webhooks[] registers nothing."""
    gw = _make_gateway(db)
    response = {"taskId": TASK_ID, "status": "working"}

    monkeypatch.setattr(task_webhook, "poll_and_deliver", _noop_poll_and_deliver)

    for webhooks in (None, [], ()):
        opened = await task_webhook.process_tool_call_webhooks(
            db,
            response=response,
            webhooks=webhooks,
            gateway=gw,
            team_id=gw.team_id,
            send_task_get=_noop_send_task_get,
        )
        assert opened == []
    assert _correlate_subs(db) == []


@pytest.mark.asyncio
async def test_process_tool_call_webhooks_noop_when_flag_disabled(session_factory, db, monkeypatch):
    """The hook is a no-op when the events master switch is off (default-off safety)."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False, raising=False)
    gw = _make_gateway(db)
    response = {"taskId": TASK_ID, "status": "working"}
    webhooks = [{"url": WEBHOOK_URL}]

    kicked = {"called": 0}

    async def fake_poll_and_deliver(**kw):  # noqa: ANN003
        kicked["called"] += 1
        return True

    monkeypatch.setattr(task_webhook, "poll_and_deliver", fake_poll_and_deliver)

    opened = await task_webhook.process_tool_call_webhooks(
        db,
        response=response,
        webhooks=webhooks,
        gateway=gw,
        team_id=gw.team_id,
        send_task_get=_noop_send_task_get,
    )
    assert opened == []
    assert _correlate_subs(db) == []
    assert kicked["called"] == 0


# --------------------------------------------------------------------------- #
# Flag-gated TTL sweeper (TC-COR-008)                                          #
# --------------------------------------------------------------------------- #


def _make_expired_waiter(db, gw) -> EventSubscription:
    """Persist an already-expired correlate waiter."""
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=gw.team_id,
        owner_email=None,
        subscriber_kind="http_callback",
        callback_url=WEBHOOK_URL,
        target=None,
        event_types=[],
        mode="correlate",
        correlation_key=CORR_KEY,
        correlation_value=TASK_ID,
        active=True,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


@pytest.mark.asyncio
async def test_worker_ttl_sweeper_removes_expired_when_enabled(session_factory, db, monkeypatch):
    """TC-COR-008: the flag-gated worker sweeper removes expired correlate waiters."""
    # First-Party
    from mcpgateway.services.events.delivery_worker import DeliveryWorker

    monkeypatch.setattr(settings, "mcpgateway_events_correlation_sweep_enabled", True, raising=False)

    gw = _make_gateway(db)
    expired = _make_expired_waiter(db, gw)
    expired_id = expired.id

    worker = DeliveryWorker(stream=stream_mod.get_event_stream(), egress=None, session_factory=session_factory)
    swept = await worker.sweep_expired_correlations()

    assert swept == 1
    db.expire_all()
    assert db.get(EventSubscription, expired_id) is None


@pytest.mark.asyncio
async def test_worker_ttl_sweeper_noop_when_disabled(session_factory, db, monkeypatch):
    """The sweeper is a no-op (leaves expired waiters) when the sweep flag is off."""
    # First-Party
    from mcpgateway.services.events.delivery_worker import DeliveryWorker

    monkeypatch.setattr(settings, "mcpgateway_events_correlation_sweep_enabled", False, raising=False)

    gw = _make_gateway(db)
    expired = _make_expired_waiter(db, gw)
    expired_id = expired.id

    worker = DeliveryWorker(stream=stream_mod.get_event_stream(), egress=None, session_factory=session_factory)
    swept = await worker.sweep_expired_correlations()

    assert swept == 0
    db.expire_all()
    assert db.get(EventSubscription, expired_id) is not None


# --------------------------------------------------------------------------- #
# Smoke import                                                                 #
# --------------------------------------------------------------------------- #


def test_main_smoke_import():
    """The additive wiring must not break importing the FastAPI app."""
    # First-Party
    import mcpgateway.main as main_mod  # noqa: F401

    assert main_mod is not None


# --------------------------------------------------------------------------- #
# Shared no-op test doubles                                                    #
# --------------------------------------------------------------------------- #


async def _noop_poll_and_deliver(**kw):  # noqa: ANN003
    """A poll_and_deliver double that does nothing (network mocked)."""
    return True


async def _noop_send_task_get(task_id: str) -> dict:
    """A send_task_get double returning a terminal carrier (never actually called)."""
    return {"taskId": task_id, "status": "completed"}


async def _drain_tasks() -> None:
    """Yield repeatedly so fire-and-forget background tasks get to run."""
    for _ in range(5):
        await asyncio.sleep(0)
