# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_delivery_worker.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Reliability tests for the L3 delivery worker (M2b).

These drive :class:`~mcpgateway.services.events.delivery_worker.DeliveryWorker`
directly (``run_once`` / ``reclaim_once`` / ``run_due_retries``) against a real
temporary SQLite database, the in-memory L2 stream backend
(:class:`~mcpgateway.services.events.stream.InMemoryStreamBackend`), and the
in-process fake subscriber
(:class:`~mcpgateway.services.events.egress.inprocess.InProcessEgressAdapter`).
No Redis server and no lifespan task are involved - the worker is exercised in
isolation so the at-least-once guarantees are observable in-process.

Covered M2 DEL gating subset (test-cases section 8):

* TC-DEL-001 - stable ``Idempotency-Key`` (= event id) across all retries.
* TC-DEL-004 - two workers race one entry -> exactly one attempt row per
  ``(event, sub, attempt_no)`` (unique constraint / ON CONFLICT).
* TC-DEL-009 - always-failing subscriber + max attempts -> ``dead_letters`` row
  with context, attempt ``failed``, no silent drop.
* TC-DEL-020 - entry XADDed then worker "crashes" before reading -> a fresh
  ``run_once`` consumes and delivers exactly once (no loss).
* TC-DEL-021 - worker reads (entry in PEL) then "crashes" before ack ->
  ``reclaim_once`` reclaims and delivers; PEL drains after ack; no dup row.
* TC-DEL-026 - replay a stored/dead-lettered delivery -> re-dispatched with the
  original idempotency key preserved (subscriber dedups on the same key).
* TC-DEL-027 - scale-down: a consumer disappears mid-flight -> the survivor
  reclaims the dead consumer's PEL; no entry is double-delivered.

Plus: 410 -> subscription auto-disabled; 429 ``retry_after`` honored in
``next_retry_at``; fan-out (2 subs match one event) -> 2 distinct attempt rows.
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timedelta, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, DeadLetter, DeliveryAttempt, EventLog, EventSubscription, Gateway
from mcpgateway.services.events.delivery_worker import DeliveryWorker
from mcpgateway.services.events.egress.base import DeliveryOutcome
from mcpgateway.services.events.egress.inprocess import InProcessEgressAdapter
from mcpgateway.services.events.stream import InMemoryStreamBackend

# --------------------------------------------------------------------------- #
# Helpers / fixtures                                                          #
# --------------------------------------------------------------------------- #

TEAM_ID = "team-1"
CALLBACK = "https://sub.example/cb"


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


def _make_gateway(db) -> Gateway:
    """Persist a minimal connection (Gateway) carrying the worker's tenant."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        team_id=TEAM_ID,
        capabilities={},
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _make_event(db, gw: Gateway, *, evt_id: str = "evt-1", evt_type: str = "com.github.push") -> EventLog:
    """Persist an EventLog row scoped to *gw* and return it."""
    row = EventLog(
        id=uuid.uuid4().hex,
        evt_id=evt_id,
        evt_source=f"//{gw.id}",
        evt_type=evt_type,
        evt_subject="octo/repo",
        evt_time=datetime.now(timezone.utc),
        gateway_id=gw.id,
        provider_id="github",
        data={"ref": "refs/heads/main"},
        raw_headers={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_subscription(db, gw: Gateway, *, callback_url: str = CALLBACK, event_types=None) -> EventSubscription:
    """Persist an active http_callback subscription bound to *gw*."""
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=TEAM_ID,
        owner_email="finance@bud.studio",
        subscriber_kind="http_callback",
        callback_url=callback_url,
        source=f"//{gw.id}",
        event_types=event_types or ["com.github.*"],
        mode="fanout",
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
# Happy path + fan-out                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_happy_path_single_delivery(session_factory, db):
    """A matched event is delivered once and the attempt is marked delivered."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    handled = await worker.run_once()

    assert handled == 1
    assert len(egress.received) == 1
    assert egress.received[0].idempotency_key == evt.evt_id
    assert await stream.pending() == []  # acked

    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert len(attempts) == 1
    assert attempts[0].subscription_id == sub.id
    assert attempts[0].status == "delivered"
    assert attempts[0].idempotency_key == evt.evt_id


@pytest.mark.asyncio
async def test_fanout_two_subs_two_attempts(session_factory, db):
    """Two matching subs for one event yield two distinct attempt rows + 2 deliveries."""
    gw = _make_gateway(db)
    sub_a = _make_subscription(db, gw, callback_url="https://a.example/cb")
    sub_b = _make_subscription(db, gw, callback_url="https://b.example/cb")
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    sub_ids = {a.subscription_id for a in attempts}
    assert sub_ids == {sub_a.id, sub_b.id}
    assert len(attempts) == 2
    assert len(egress.received) == 2


# --------------------------------------------------------------------------- #
# TC-DEL-001: stable Idempotency-Key across retries                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_idempotency_key_stable_across_retries(session_factory, db):
    """A flaky subscriber forcing retries -> every attempt carries the same key."""
    gw = _make_gateway(db)
    _make_subscription(db, gw, callback_url=CALLBACK)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    # 500, 500, then 200 -> three deliveries total.
    egress.set_outcomes(
        CALLBACK,
        [
            DeliveryOutcome(ok=False, http_status=500, error="boom"),
            DeliveryOutcome(ok=False, http_status=500, error="boom"),
            DeliveryOutcome(ok=True, http_status=200),
        ],
    )
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory, backoff_base=0.0)

    await worker.run_once()  # attempt 1 -> 500 (retrying)
    await worker.run_due_retries(now=datetime.now(timezone.utc) + timedelta(seconds=1))  # attempt 2 -> 500
    await worker.run_due_retries(now=datetime.now(timezone.utc) + timedelta(seconds=2))  # attempt 3 -> 200

    assert len(egress.received) == 3
    keys = {r.idempotency_key for r in egress.received}
    assert keys == {evt.evt_id}

    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert {a.idempotency_key for a in attempts} == {evt.evt_id}
    statuses = [a.status for a in attempts]
    assert "delivered" in statuses


# --------------------------------------------------------------------------- #
# TC-DEL-004 / TC-DEL-027 / TC-DEL-061-063: workers racing one entry          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_workers_race_one_attempt_row(session_factory, db):
    """Two workers process the same reclaimed entry -> exactly one attempt row."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    w1 = _new_worker(stream=stream, egress=egress, session_factory=session_factory, consumer="w1")
    w2 = _new_worker(stream=stream, egress=egress, session_factory=session_factory, consumer="w2")

    # w1 reads (entry -> w1 PEL) then "crashes" before ack.
    read = await stream.read_group("w1")
    assert len(read) == 1
    await w1._process_event(db, read[0])  # pylint: disable=protected-access

    # w2 reclaims the stale entry and reprocesses it (idempotent).
    reclaimed = await stream.claim_stale("w2", min_idle_ms=0)
    assert len(reclaimed) == 1
    await w2._process_event(db, reclaimed[0])  # pylint: disable=protected-access

    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    matching = [a for a in attempts if a.subscription_id == sub.id and a.attempt_no == 1]
    assert len(matching) == 1  # unique constraint / ON CONFLICT held


# --------------------------------------------------------------------------- #
# TC-DEL-009: always-failing subscriber -> dead-letter after max attempts     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_max_attempts_dead_letters(session_factory, db):
    """A persistently-5xx subscriber lands in dead_letters; status failed; no drop."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    # Persistent 5xx on every attempt.
    egress.set_outcomes(CALLBACK, [DeliveryOutcome(ok=False, http_status=500, error="boom") for _ in range(10)])
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory, max_attempts=3, backoff_base=0.0)

    await worker.run_once()  # attempt 1 -> 500 (retrying)

    far_future = datetime.now(timezone.utc) + timedelta(hours=1)
    await worker.run_due_retries(now=far_future)  # attempt 2 -> 500 (retrying)
    await worker.run_due_retries(now=far_future)  # attempt 3 -> 500 -> dead-letter

    dls = db.execute(select(DeadLetter).where(DeadLetter.event_id == evt.id)).scalars().all()
    assert len(dls) == 1
    assert dls[0].subscription_id == sub.id
    assert dls[0].attempts >= 3
    assert dls[0].last_error is not None

    # The final attempt for evt/sub is failed (no silent drop).
    final = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.event_id == evt.id, DeliveryAttempt.subscription_id == sub.id)).scalars().all()
    assert any(a.status == "failed" for a in final)
    assert max(a.attempt_no for a in final) == 3


# --------------------------------------------------------------------------- #
# TC-DEL-020: XADD then worker crashes before reading -> no loss              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_durable_before_read_no_loss(session_factory, db):
    """An entry XADDed but never read is delivered by a fresh worker run_once."""
    gw = _make_gateway(db)
    _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    # First "worker" never runs (simulated crash before read). A fresh worker:
    fresh = _new_worker(stream=stream, egress=egress, session_factory=session_factory, consumer="w-fresh")
    handled = await fresh.run_once()

    assert handled == 1
    assert len(egress.received) == 1
    assert await stream.pending() == []


# --------------------------------------------------------------------------- #
# TC-DEL-021: read then crash before ack -> reclaim delivers; PEL drains      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reclaim_after_crash_before_ack(session_factory, db):
    """Worker reads (PEL) then crashes pre-ack -> reclaim_once delivers; PEL drains; no dup row."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    # Simulate read-then-crash: read moves the entry into the PEL, no ack.
    read = await stream.read_group("w-dead")
    assert len(read) == 1
    assert await stream.pending() == [read[0][0]]

    survivor = _new_worker(stream=stream, egress=egress, session_factory=session_factory, consumer="w-survivor")
    reclaimed = await survivor.reclaim_once(min_idle_ms=0)

    assert reclaimed == 1
    assert len(egress.received) == 1
    assert await stream.pending() == []  # acked after reclaim+deliver

    attempts = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().all()
    assert len(attempts) == 1  # no duplicate effect


# --------------------------------------------------------------------------- #
# TC-DEL-026: replay a stored delivery -> same idempotency key                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replay_preserves_idempotency_key(session_factory, db):
    """An operator replay re-dispatches with the original idempotency key."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()

    # First delivery.
    await stream.add(_stream_message(evt, gw))
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    first_key = egress.received[0].idempotency_key

    # Replay the stored delivery (e.g. a manual dead-letter replay from Admin UI).
    replayed = await worker.replay(event_id=evt.id, subscription_id=sub.id)
    assert replayed is True

    assert len(egress.received) == 2
    assert egress.received[1].idempotency_key == first_key == evt.evt_id

    # The replay is a fresh attempt row (attempt_no > 1), not a duplicate of attempt 1.
    attempts = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().all()
    assert {a.attempt_no for a in attempts} == {1, 2}
    assert {a.idempotency_key for a in attempts} == {evt.evt_id}


# --------------------------------------------------------------------------- #
# 410 Gone -> subscription auto-disabled + dead-letter                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_410_gone_auto_disables_subscription(session_factory, db):
    """A 410 permanent failure dead-letters and flips the subscription inactive."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    egress.set_outcomes(CALLBACK, [DeliveryOutcome(ok=False, http_status=410, permanent=True, error="gone")])
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    db.expire_all()
    refreshed = db.get(EventSubscription, sub.id)
    assert refreshed.active is False

    dls = db.execute(select(DeadLetter).where(DeadLetter.subscription_id == sub.id)).scalars().all()
    assert len(dls) == 1


# --------------------------------------------------------------------------- #
# 4xx permanent -> dead-letter, no retry                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_permanent_4xx_dead_letters_no_retry(session_factory, db):
    """A 400/403 permanent rejection dead-letters immediately (status failed)."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    egress.set_outcomes(CALLBACK, [DeliveryOutcome(ok=False, http_status=403, permanent=True, error="forbidden")])
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    attempts = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().all()
    assert len(attempts) == 1
    assert attempts[0].status == "failed"
    assert attempts[0].next_retry_at is None

    dls = db.execute(select(DeadLetter).where(DeadLetter.subscription_id == sub.id)).scalars().all()
    assert len(dls) == 1


# --------------------------------------------------------------------------- #
# 429 Retry-After honored in next_retry_at                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_429_retry_after_honored(session_factory, db):
    """A 429 with retry_after schedules next_retry_at ~= now + retry_after."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    egress.set_outcomes(CALLBACK, [DeliveryOutcome(ok=False, http_status=429, retry_after=30.0, error="rate limited")])
    await stream.add(_stream_message(evt, gw))

    before = datetime.now(timezone.utc)
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    attempt = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().one()
    assert attempt.status == "retrying"
    assert attempt.next_retry_at is not None
    nra = attempt.next_retry_at
    if nra.tzinfo is None:
        nra = nra.replace(tzinfo=timezone.utc)
    delta = (nra - before).total_seconds()
    # ~30s honored (allow slack for execution time); clearly tied to retry_after.
    assert 25.0 <= delta <= 40.0
