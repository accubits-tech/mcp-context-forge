# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_correlate.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.correlate** (M7 correlate/resume).

These tests drive the correlate lifecycle (FRD section 7.3 / section 8.9)
against a real (temporary, in-memory) database. Two ``Gateway`` connections are
seeded in two different teams, ephemeral ``mode="correlate"``
``EventSubscription`` rows are opened, and the correlate contract is asserted:

* ``extract_correlation_value`` resolves the dotted/jsonpath carrier out of an
  :class:`~mcpgateway.schemas.EventEnvelope` (or its dict form).
* ``open_correlation`` creates a single ephemeral waiter and is **fail-closed**
  on a same-tenant collision (TC-COR-012 / SC-COR-011).
* ``resolve_correlation`` returns the single active, non-expired, **same-tenant**
  waiter whose ``correlation_value`` matches (TC-COR-013); returns ``None`` for
  an unknown value so the caller dead-letters (TC-COR-011); a foreign-team
  task_id is never resolved (TC-COR-013).
* ``consume_correlation`` is an idempotent terminal DELETE: a second identical
  resolve after consume yields ``None`` (TC-COR-010), freeing the unique
  ``(team_id, correlation_value)`` slot for reuse.
* ``expire_correlations`` sweeps expired waiters to timed-out + delete so a
  later completion finds nothing (TC-COR-008 / TC-COR-007).
* ``register_task_webhook`` opens a resolvable waiter keyed on the task id
  (#523).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_correlate.py -q
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timedelta, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, EventSubscription, Gateway
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events.correlate import (
    consume_correlation,
    CorrelationCollisionError,
    expire_correlations,
    extract_correlation_value,
    open_correlation,
    register_task_webhook,
    resolve_correlation,
)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

TEAM_A = "team-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEAM_B = "team-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SOURCE_A = "https://acme.example/api"
CORR_KEY = "data.taskId"
TASK_ID = "task-12345"
TASK_COMPLETED_TYPE = "io.mcp.task.completed"


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


def _make_gateway(db, *, team_id: str, source: str = SOURCE_A) -> Gateway:
    """Persist a connection in *team_id* whose canonical source is *source*."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url=source,
        capabilities={},
        team_id=team_id,
        events_enabled=True,
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _completion_envelope(*, task_id: str = TASK_ID, source: str = SOURCE_A, extra: dict | None = None) -> EventEnvelope:
    """Build a task-completion envelope carrying ``data.taskId``."""
    data = {"taskId": task_id, "status": "completed"}
    if extra:
        data.update(extra)
    return EventEnvelope(
        id="evt-" + uuid.uuid4().hex[:8],
        source=source,
        type=TASK_COMPLETED_TYPE,
        subject=task_id,
        time=datetime(2026, 5, 31, tzinfo=timezone.utc),
        data=data,
    )


# --------------------------------------------------------------------------- #
# extract_correlation_value                                                    #
# --------------------------------------------------------------------------- #


def test_extract_correlation_value_dotted_jsonpath():
    env = _completion_envelope(task_id="abc-1")
    assert extract_correlation_value(env, "data.taskId") == "abc-1"


def test_extract_correlation_value_subject_top_level():
    env = _completion_envelope(task_id="abc-2")
    # Top-level (hoisted) carriers also resolve: subject == task_id here.
    assert extract_correlation_value(env, "subject") == "abc-2"


def test_extract_correlation_value_jsonpath_dollar_prefix():
    env = _completion_envelope(task_id="abc-3")
    assert extract_correlation_value(env, "$.data.taskId") == "abc-3"


def test_extract_correlation_value_missing_returns_none():
    env = _completion_envelope(task_id="abc-4")
    assert extract_correlation_value(env, "data.nope") is None


def test_extract_correlation_value_accepts_dict_envelope():
    env = _completion_envelope(task_id="abc-5")
    as_dict = {"id": env.id, "source": env.source, "type": env.type, "subject": env.subject, "data": env.data}
    assert extract_correlation_value(as_dict, "data.taskId") == "abc-5"


# --------------------------------------------------------------------------- #
# open_correlation + collision (TC-COR-012 / SC-COR-011)                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_open_correlation_creates_ephemeral_waiter(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    sub = await open_correlation(
        session,
        gateway_id=gw.id,
        team_id=TEAM_A,
        correlation_key=CORR_KEY,
        correlation_value=TASK_ID,
        target={"agent_id": "a", "version": "1"},
        callback_url="https://hook.example/cb",
        delivery={"signing": "hmac"},
        ttl_seconds=300,
    )
    assert sub.id is not None
    assert sub.mode == "correlate"
    assert sub.active is True
    assert sub.team_id == TEAM_A
    assert sub.gateway_id == gw.id
    assert sub.correlation_key == CORR_KEY
    assert sub.correlation_value == TASK_ID
    assert sub.callback_url == "https://hook.example/cb"
    assert sub.target == {"agent_id": "a", "version": "1"}
    # TTL set -> expires_at in the future.
    assert sub.expires_at is not None
    expires = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc)
    # Persisted (the row is the pending-run <-> task_id mapping; survives restart).
    assert session.get(EventSubscription, sub.id) is not None


@pytest.mark.asyncio
async def test_open_correlation_no_ttl_has_no_expiry(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    sub = await open_correlation(
        session,
        gateway_id=gw.id,
        team_id=TEAM_A,
        correlation_key=CORR_KEY,
        correlation_value="no-ttl-task",
        target=None,
    )
    assert sub.expires_at is None


@pytest.mark.asyncio
async def test_open_correlation_collision_fail_closed(session):
    """TC-COR-012 / SC-COR-011: a 2nd open on the SAME (team, value) fails closed."""
    gw = _make_gateway(session, team_id=TEAM_A)
    await open_correlation(
        session,
        gateway_id=gw.id,
        team_id=TEAM_A,
        correlation_key=CORR_KEY,
        correlation_value=TASK_ID,
        target=None,
    )
    with pytest.raises(CorrelationCollisionError):
        await open_correlation(
            session,
            gateway_id=gw.id,
            team_id=TEAM_A,
            correlation_key=CORR_KEY,
            correlation_value=TASK_ID,
            target=None,
        )
    # Exactly one waiter persisted (the collision did not create a second).
    rows = session.query(EventSubscription).filter(EventSubscription.correlation_value == TASK_ID).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_open_correlation_same_value_different_team_ok(session):
    """Collision is tenant-scoped: the SAME value in a DIFFERENT team is allowed."""
    gw_a = _make_gateway(session, team_id=TEAM_A)
    gw_b = _make_gateway(session, team_id=TEAM_B)
    await open_correlation(session, gateway_id=gw_a.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    sub_b = await open_correlation(session, gateway_id=gw_b.id, team_id=TEAM_B, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    assert sub_b.team_id == TEAM_B
    rows = session.query(EventSubscription).filter(EventSubscription.correlation_value == TASK_ID).all()
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# resolve_correlation (TC-COR-013 tenant-scope / TC-COR-011 unknown)           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_correlation_matches_same_tenant(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    waiter = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    env = _completion_envelope(task_id=TASK_ID)
    matched = resolve_correlation(session, envelope=env, gateway=gw)
    assert matched is not None
    assert matched.id == waiter.id


@pytest.mark.asyncio
async def test_resolve_correlation_unknown_value_returns_none(session):
    """TC-COR-011: a completion with no waiting sub resolves to None -> caller dead-letters."""
    gw = _make_gateway(session, team_id=TEAM_A)
    await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value="other-task", target=None)
    env = _completion_envelope(task_id="unknown-task")
    assert resolve_correlation(session, envelope=env, gateway=gw) is None


@pytest.mark.asyncio
async def test_resolve_correlation_cross_tenant_not_resolved(session):
    """TC-COR-013: a foreign-team task_id is NOT resolved (cross-tenant impossible)."""
    gw_a = _make_gateway(session, team_id=TEAM_A)
    gw_b = _make_gateway(session, team_id=TEAM_B)
    # Waiter belongs to team B.
    await open_correlation(session, gateway_id=gw_b.id, team_id=TEAM_B, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    env = _completion_envelope(task_id=TASK_ID)
    # The event arrives on team A's connection: the team-B waiter must NOT match.
    assert resolve_correlation(session, envelope=env, gateway=gw_a) is None
    # But on team B's connection it DOES match.
    assert resolve_correlation(session, envelope=env, gateway=gw_b) is not None


@pytest.mark.asyncio
async def test_resolve_correlation_skips_inactive(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    waiter = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    waiter.active = False
    session.add(waiter)
    session.commit()
    env = _completion_envelope(task_id=TASK_ID)
    assert resolve_correlation(session, envelope=env, gateway=gw) is None


@pytest.mark.asyncio
async def test_resolve_correlation_skips_expired(session):
    gw = _make_gateway(session, team_id=TEAM_A)
    waiter = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    waiter.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    session.add(waiter)
    session.commit()
    env = _completion_envelope(task_id=TASK_ID)
    assert resolve_correlation(session, envelope=env, gateway=gw) is None


@pytest.mark.asyncio
async def test_resolve_correlation_skips_fanout_rows(session):
    """A fanout sub (no correlation_value) is never matched by resolve_correlation."""
    gw = _make_gateway(session, team_id=TEAM_A)
    fanout = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=TEAM_A,
        owner_email="finance@bud.studio",
        subscriber_kind="sse",
        source=SOURCE_A,
        event_types=[TASK_COMPLETED_TYPE],
        mode="fanout",
        active=True,
    )
    session.add(fanout)
    session.commit()
    env = _completion_envelope(task_id=TASK_ID)
    assert resolve_correlation(session, envelope=env, gateway=gw) is None


# --------------------------------------------------------------------------- #
# consume_correlation (TC-COR-010 idempotent terminal)                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_then_consume_is_idempotent(session):
    """TC-COR-010: resolve+consume; a 2nd identical resolve after consume -> None."""
    gw = _make_gateway(session, team_id=TEAM_A)
    await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    env = _completion_envelope(task_id=TASK_ID)

    matched = resolve_correlation(session, envelope=env, gateway=gw)
    assert matched is not None
    await consume_correlation(session, matched)

    # The waiter is gone: a replayed completion finds nothing (no-op).
    assert resolve_correlation(session, envelope=env, gateway=gw) is None
    assert session.query(EventSubscription).filter(EventSubscription.correlation_value == TASK_ID).count() == 0


@pytest.mark.asyncio
async def test_consume_correlation_frees_slot_for_reuse(session):
    """Deleting the ephemeral sub frees the unique (team, value) slot."""
    gw = _make_gateway(session, team_id=TEAM_A)
    sub1 = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    await consume_correlation(session, sub1)
    # Re-binding the same value must now succeed (slot freed).
    sub2 = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    assert sub2.id != sub1.id


@pytest.mark.asyncio
async def test_consume_correlation_tolerates_already_deleted(session):
    """Consuming a row that is already gone is a no-op (idempotent terminal)."""
    gw = _make_gateway(session, team_id=TEAM_A)
    sub = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None)
    await consume_correlation(session, sub)
    # Second consume on the same (now-detached) row must not raise.
    await consume_correlation(session, sub)


# --------------------------------------------------------------------------- #
# expire_correlations (TC-COR-008 / TC-COR-007 sweep)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expire_correlations_sweeps_expired(session):
    """TC-COR-008: an expired waiter is timed-out + removed; a later completion finds nothing."""
    gw = _make_gateway(session, team_id=TEAM_A)
    waiter = await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None, ttl_seconds=60)
    # Force it past expiry.
    waiter.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.add(waiter)
    session.commit()

    swept = await expire_correlations(session)
    assert swept == 1
    # Removed: a later completion resolves to nothing.
    env = _completion_envelope(task_id=TASK_ID)
    assert resolve_correlation(session, envelope=env, gateway=gw) is None
    assert session.query(EventSubscription).filter(EventSubscription.correlation_value == TASK_ID).count() == 0


@pytest.mark.asyncio
async def test_expire_correlations_leaves_live_waiters(session):
    """A non-expired waiter is untouched by the sweep."""
    gw = _make_gateway(session, team_id=TEAM_A)
    await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value=TASK_ID, target=None, ttl_seconds=3600)
    swept = await expire_correlations(session)
    assert swept == 0
    env = _completion_envelope(task_id=TASK_ID)
    assert resolve_correlation(session, envelope=env, gateway=gw) is not None


@pytest.mark.asyncio
async def test_expire_correlations_ignores_fanout_and_no_ttl(session):
    """The sweep only times out expired correlate waiters, not fanout/no-ttl rows."""
    gw = _make_gateway(session, team_id=TEAM_A)
    # correlate waiter with no TTL: never expires.
    await open_correlation(session, gateway_id=gw.id, team_id=TEAM_A, correlation_key=CORR_KEY, correlation_value="forever", target=None)
    # fanout row with a past expires_at: not a correlate waiter, ignored by sweep.
    fanout = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=TEAM_A,
        owner_email="finance@bud.studio",
        subscriber_kind="sse",
        source=SOURCE_A,
        event_types=[TASK_COMPLETED_TYPE],
        mode="fanout",
        active=True,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    session.add(fanout)
    session.commit()
    swept = await expire_correlations(session)
    assert swept == 0


# --------------------------------------------------------------------------- #
# register_task_webhook (#523)                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_register_task_webhook_opens_resolvable_waiter(session):
    """#523: register_task_webhook opens a correlate sub keyed on task_id that resolves."""
    gw = _make_gateway(session, team_id=TEAM_A)
    webhook = {"url": "https://hook.example/523", "auth": {"bearer": "tkn"}}
    sub = await register_task_webhook(session, gateway=gw, team_id=TEAM_A, task_id=TASK_ID, webhook=webhook)
    assert sub.mode == "correlate"
    assert sub.correlation_value == TASK_ID
    assert sub.team_id == TEAM_A
    assert sub.callback_url == "https://hook.example/523"

    # The waiter resolves against an upstream task-completion envelope.
    env = _completion_envelope(task_id=TASK_ID)
    matched = resolve_correlation(session, envelope=env, gateway=gw)
    assert matched is not None
    assert matched.id == sub.id


@pytest.mark.asyncio
async def test_register_task_webhook_collision_fail_closed(session):
    """Re-registering the same task_id in the same tenant fails closed."""
    gw = _make_gateway(session, team_id=TEAM_A)
    webhook = {"url": "https://hook.example/523"}
    await register_task_webhook(session, gateway=gw, team_id=TEAM_A, task_id=TASK_ID, webhook=webhook)
    with pytest.raises(CorrelationCollisionError):
        await register_task_webhook(session, gateway=gw, team_id=TEAM_A, task_id=TASK_ID, webhook=webhook)


# --------------------------------------------------------------------------- #
# smoke-import                                                                 #
# --------------------------------------------------------------------------- #


def test_module_smoke_import():
    assert hasattr(correlate_mod, "extract_correlation_value")
    assert hasattr(correlate_mod, "open_correlation")
    assert hasattr(correlate_mod, "resolve_correlation")
    assert hasattr(correlate_mod, "consume_correlation")
    assert hasattr(correlate_mod, "expire_correlations")
    assert hasattr(correlate_mod, "register_task_webhook")
    assert issubclass(correlate_mod.CorrelationCollisionError, Exception)
