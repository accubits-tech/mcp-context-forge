# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_delivery_contract.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

M4 delivery-contract tests: the locked egress envelope a thin budprompt/bda
receiver relies on, plus tenant isolation at the delivery hop.

These drive the L3 :class:`~mcpgateway.services.events.delivery_worker.DeliveryWorker`
end-to-end (``run_once``) against a real temporary SQLite database, the in-memory
L2 stream backend (:class:`~mcpgateway.services.events.stream.InMemoryStreamBackend`),
and the recording in-process subscriber
(:class:`~mcpgateway.services.events.egress.inprocess.InProcessEgressAdapter`).
No Redis server and no lifespan task are involved.

The §8 / §9.1a delivery envelope is the cross-repo contract (FRD S11/D3,
``FR-13``/``FR-26``/``FR-28b``): a receiver maps ``subscription.target.agent_id``
into its existing executor without a routing engine, dedupes on
``Idempotency-Key = subscription.delivery_id``, and reads ``correlation_id`` to
decide spawn (fanout, ``null``) vs resume (correlate). The exact shape is::

    {
        "event": {id, source, type, subject, time, data},
        "subscription": {id, delivery_id, mode,
                         target: {agent_id, version, params}, correlation_id},
        "idempotency_key": "<= delivery_id == DeliveryAttempt.id>",
    }

Covered M4 gating subset (test-cases section 8):

* Delivery-shape contract against a budprompt/bda-style receiver - the recorded
  envelope echoes ``target`` verbatim, ``delivery_id`` is the per-attempt
  ``DeliveryAttempt.id``, ``mode`` is correct, and ``correlation_id`` is the
  subscription's ``correlation_value`` (``null`` in fanout).
* TC-SEC-029 - an event ingested on a team-A connection is delivered ONLY to
  team-A subscriptions; a team-B sub with an otherwise-matching filter receives
  nothing (cross-tenant fan-out is structurally impossible, §10.1.7).
* TC-SEC-028 - the delivery hop never crosses tenants: every recorded delivery
  for a team-A event is addressed to a team-A subscription, and team-B's
  callback never appears.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_delivery_contract.py -q
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, DeliveryAttempt, EventLog, EventSubscription, Gateway
from mcpgateway.services.events.delivery_worker import DeliveryWorker
from mcpgateway.services.events.egress.inprocess import InProcessEgressAdapter
from mcpgateway.services.events.stream import InMemoryStreamBackend

# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #

TEAM_A = "team-a"
TEAM_B = "team-b"
# A cross-provider source shared by both tenants' subs so the ONLY thing that
# can keep team-B's sub from matching a team-A event is the tenant scope.
SHARED_SOURCE = "//stripe"
TARGET = {"agent_id": "agent_abc", "version": "1", "params": {"k": "v"}}


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


def _make_gateway(db, *, team_id: str, source: str = SHARED_SOURCE) -> Gateway:
    """Persist a minimal connection (Gateway) carrying *team_id* and *source*."""
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


def _make_event(db, gw: Gateway, *, evt_id: str = "evt-1", evt_type: str = "com.stripe.payment_intent.succeeded", source: str = SHARED_SOURCE) -> EventLog:
    """Persist an EventLog row scoped to *gw* (source defaults to the shared one)."""
    row = EventLog(
        id=uuid.uuid4().hex,
        evt_id=evt_id,
        evt_source=source,
        evt_type=evt_type,
        evt_subject="cus_123",
        evt_time=datetime.now(timezone.utc),
        gateway_id=gw.id,
        provider_id="stripe",
        data={"amount": 4200},
        raw_headers={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_subscription(
    db,
    *,
    team_id: str,
    callback_url: str,
    target=None,
    mode: str = "fanout",
    correlation_value=None,
    source: str = SHARED_SOURCE,
    event_types=None,
    filter_expr=None,
) -> EventSubscription:
    """Persist an active, cross-provider http_callback subscription.

    ``gateway_id`` is ``None`` (cross-provider) so candidate selection turns on
    ``(team_id, source)`` alone - the tenant scope is the only discriminator
    between an otherwise-identical team-A and team-B subscription.
    """
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=None,
        team_id=team_id,
        owner_email="finance@bud.studio",
        subscriber_kind="http_callback",
        callback_url=callback_url,
        target=target,
        source=source,
        event_types=event_types or ["com.stripe.*"],
        filter_expr=filter_expr,
        mode=mode,
        correlation_value=correlation_value,
        active=True,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _stream_message(event: EventLog, gw: Gateway) -> dict:
    """Build the L2 stream message for *event* on *gw* per the contract shape."""
    return {
        "event_log_id": event.id,
        "gateway_id": gw.id,
        "envelope": {
            "id": event.evt_id,
            "source": event.evt_source,
            "type": event.evt_type,
            "subject": event.evt_subject,
            "time": event.evt_time.isoformat() if event.evt_time else None,
            "data": event.data,
        },
    }


def _new_worker(*, stream, egress, session_factory, consumer="w1", **kw) -> DeliveryWorker:
    """Construct a DeliveryWorker over the injected collaborators."""
    return DeliveryWorker(stream=stream, egress=egress, session_factory=session_factory, consumer_name=consumer, jitter=False, **kw)


# --------------------------------------------------------------------------- #
# Delivery-shape contract (budprompt / bda receiver)                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delivery_envelope_matches_locked_contract(session_factory, db):
    """End-to-end: the recorded envelope matches the §9.1a contract exactly.

    A budprompt/bda receiver relies on this exact shape: ``event`` block,
    ``subscription`` block with the verbatim ``target`` it registered, the
    per-attempt ``delivery_id`` to dedupe on, ``mode``, and ``correlation_id``.
    """
    gw = _make_gateway(db, team_id=TEAM_A)
    sub = _make_subscription(db, team_id=TEAM_A, callback_url="https://budprompt.example/v1/events", target=dict(TARGET))
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    handled = await worker.run_once()

    assert handled == 1
    assert len(egress.received) == 1
    envelope = egress.received[0].delivery_envelope

    # Top-level keys: exactly event + subscription + idempotency_key.
    assert set(envelope.keys()) == {"event", "subscription", "idempotency_key"}

    # event block: exactly id/source/type/subject/time/data, echoed from the log.
    event_block = envelope["event"]
    assert set(event_block.keys()) == {"id", "source", "type", "subject", "time", "data"}
    assert event_block["id"] == evt.evt_id
    assert event_block["source"] == evt.evt_source
    assert event_block["type"] == evt.evt_type
    assert event_block["subject"] == evt.evt_subject
    assert event_block["time"] == evt.evt_time.isoformat()
    assert event_block["data"] == evt.data

    # subscription block: exactly id/delivery_id/mode/target/correlation_id.
    sub_block = envelope["subscription"]
    assert set(sub_block.keys()) == {"id", "delivery_id", "mode", "target", "correlation_id"}
    assert sub_block["id"] == sub.id
    assert sub_block["mode"] == "fanout"

    # target echoed verbatim (agent_id/version/params) - the receiver invokes it.
    assert sub_block["target"] == TARGET
    assert sub_block["target"]["agent_id"] == "agent_abc"
    assert sub_block["target"]["version"] == "1"
    assert sub_block["target"]["params"] == {"k": "v"}

    # delivery_id is the per-attempt DeliveryAttempt.id and the idempotency key.
    attempt = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().one()
    assert sub_block["delivery_id"] == attempt.id
    assert envelope["idempotency_key"] == evt.evt_id
    assert egress.received[0].idempotency_key == evt.evt_id

    # fanout -> correlation_id is null (None).
    assert sub_block["correlation_id"] is None


@pytest.mark.asyncio
async def test_delivery_envelope_correlate_carries_correlation_id(session_factory, db):
    """Correlate mode echoes the bound ``correlation_value`` as ``correlation_id``."""
    gw = _make_gateway(db, team_id=TEAM_A)
    sub = _make_subscription(
        db,
        team_id=TEAM_A,
        callback_url="https://budpipeline.example/workflow-events",
        target=dict(TARGET),
        mode="correlate",
        correlation_value="task-handle-7",
    )
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    sub_block = egress.received[0].delivery_envelope["subscription"]
    assert sub_block["mode"] == "correlate"
    assert sub_block["correlation_id"] == "task-handle-7"
    assert sub_block["target"] == TARGET
    assert sub_block["id"] == sub.id


# --------------------------------------------------------------------------- #
# TC-SEC-029 / TC-SEC-028: tenant isolation at the delivery hop               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tc_sec_029_event_delivered_only_to_same_tenant(session_factory, db):
    """An event on a team-A connection is delivered ONLY to team-A subscriptions.

    A team-B sub with the same source, event-type glob, and (matching) filter
    receives nothing - cross-tenant fan-out is structurally impossible because
    candidate selection is tenant-scoped on ``team_id`` (§10.1.7, SC-SEC-029).
    """
    gw_a = _make_gateway(db, team_id=TEAM_A)

    cb_a = "https://team-a.example/cb"
    cb_b = "https://team-b.example/cb"
    sub_a = _make_subscription(db, team_id=TEAM_A, callback_url=cb_a, target=dict(TARGET))
    # Identical sub except for the tenant; same source/glob, with a filter that
    # WOULD match the event - proving tenant scope (not the filter) is the gate.
    sub_b = _make_subscription(
        db,
        team_id=TEAM_B,
        callback_url=cb_b,
        target=dict(TARGET),
        filter_expr="data.amount == 4200",
    )

    evt = _make_event(db, gw_a)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw_a))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    # Exactly one delivery, addressed to team-A; team-B's callback never fired.
    assert len(egress.received) == 1
    assert egress.received[0].callback_url == cb_a
    assert egress.received_for(cb_b) == []

    # And no team-B attempt row was ever created.
    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert {a.subscription_id for a in attempts} == {sub_a.id}
    assert sub_b.id not in {a.subscription_id for a in attempts}


@pytest.mark.asyncio
async def test_tc_sec_028_delivery_never_crosses_tenants(session_factory, db):
    """Parallel events per tenant: no delivery ever crosses the tenant boundary.

    Each delivery for a team-A event is addressed to a team-A sub and vice
    versa; the envelope's ``subscription.id`` always belongs to the same tenant
    as the connection the event arrived on.
    """
    gw_a = _make_gateway(db, team_id=TEAM_A)
    gw_b = _make_gateway(db, team_id=TEAM_B)

    cb_a = "https://team-a.example/cb"
    cb_b = "https://team-b.example/cb"
    sub_a = _make_subscription(db, team_id=TEAM_A, callback_url=cb_a, target=dict(TARGET))
    sub_b = _make_subscription(db, team_id=TEAM_B, callback_url=cb_b, target=dict(TARGET))

    # One event per tenant, on each tenant's own connection.
    evt_a = _make_event(db, gw_a, evt_id="evt-a")
    evt_b = _make_event(db, gw_b, evt_id="evt-b")

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt_a, gw_a))
    await stream.add(_stream_message(evt_b, gw_b))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()
    await worker.run_once()

    # Two deliveries total, one per tenant, each to its own callback only.
    assert len(egress.received) == 2
    by_cb = {r.callback_url: r for r in egress.received}
    assert set(by_cb) == {cb_a, cb_b}

    # team-A's delivery carries team-A's sub + event; never team-B's.
    rec_a = by_cb[cb_a]
    assert rec_a.delivery_envelope["subscription"]["id"] == sub_a.id
    assert rec_a.delivery_envelope["event"]["id"] == "evt-a"

    rec_b = by_cb[cb_b]
    assert rec_b.delivery_envelope["subscription"]["id"] == sub_b.id
    assert rec_b.delivery_envelope["event"]["id"] == "evt-b"

    # Attempt rows: team-A's event only ever produced a team-A attempt, etc.
    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    by_event = {a.event_id: a.subscription_id for a in attempts}
    assert by_event[evt_a.id] == sub_a.id
    assert by_event[evt_b.id] == sub_b.id
