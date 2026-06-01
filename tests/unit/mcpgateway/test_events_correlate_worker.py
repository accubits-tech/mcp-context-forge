# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_correlate_worker.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

M7 worker correlate-integration + #523 poller->deliver tests.

These drive the *correlate-first* seam wired into
:meth:`~mcpgateway.services.events.delivery_worker.DeliveryWorker._process_event`
(FRD §7.3 / §8.9) and the #523 poller->deliver coordinator
(:func:`~mcpgateway.services.events.task_webhook.poll_and_deliver`) against a
real temporary SQLite database, the in-memory L2 stream backend
(:class:`~mcpgateway.services.events.stream.InMemoryStreamBackend`), and the
in-process fake subscriber
(:class:`~mcpgateway.services.events.egress.inprocess.InProcessEgressAdapter`).

Covered M7 COR gating subset (test-cases section 8):

* TC-COR-001 - a waiting correlate sub + a terminal completion event ->
  exactly one single-target resume to that sub, the waiter is consumed
  (deleted), no fanout, and no new run/extra attempt is created.
* TC-COR-010 - delivering the SAME completion twice -> the first resumes +
  consumes; the second resolves to nothing and is a no-op (idempotent).
* TC-COR-011 - a completion for an UNKNOWN correlation_value that is
  nonetheless correlate-shaped (a task-completion carrier) -> dead-lettered,
  no run, no fanout.
* TC-COR-026 - the persisted correlate sub IS the pending-run<->task_id map,
  so after a simulated "restart" (fresh session) the same task_id is still
  re-resolvable and resumable.
* TC-COR-028 - a notification AND a poll both observe the terminal task ->
  a single resume (the second delivery is the idempotent no-op of TC-COR-010).
* TC-COR-014/015/016 (best-effort) - a stale/older status arriving after a
  terminal consume does NOT re-open or re-resume the consumed correlation.
* TC-COR-021 - a FORGED (bad-signature) task completion is rejected by the M1
  ingress signature verify (HTTP 401) BEFORE the worker is ever reached: it
  never persists an ``event_log`` row, never lands on the L2 stream, and so a
  waiting correlate sub is never resumed. The gating linkage asserted here is
  that the security boundary lives in M1 ingress, upstream of the worker's
  correlate arm - a valid-signature twin proves the negative is not vacuous.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_correlate_worker.py -q
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timezone
import hashlib
import hmac
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, DeadLetter, DeliveryAttempt, EventLog, EventSubscription, Gateway
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.services.events.correlate import open_correlation
from mcpgateway.services.events.delivery_worker import DeliveryWorker
from mcpgateway.services.events.egress.inprocess import InProcessEgressAdapter
from mcpgateway.services.events.ingress_service import IngressService
from mcpgateway.services.events.stream import InMemoryStreamBackend
from mcpgateway.utils.services_auth import encode_auth

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

TEAM_ID = "team-correlate-aaaaaaaaaaaaaaaaaaaa"
OTHER_TEAM = "team-other-bbbbbbbbbbbbbbbbbbbbbb"
RESUME_CB = "https://agent.example/resume"
CORR_KEY = "data.taskId"
TASK_ID = "task-abc-123"
TASK_COMPLETED_TYPE = "com.mcp.task.completed"


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
def _fresh_singletons(monkeypatch):
    """Reset the process-wide bus, stream, and ingress dedup-cache singletons.

    The #523 poller publishes its synthesized completion via
    :func:`~mcpgateway.services.events.emit.publish_normalized_event`, which uses
    the process-wide :func:`~mcpgateway.services.events.stream.get_event_stream`
    singleton; resetting it per-test gives a fresh in-memory stream that the
    worker can then drain.
    """
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


def _waiter_gone(db, sub_id: str) -> bool:
    """Return whether a (possibly cross-session-deleted) waiter is gone.

    The worker consumes (DELETEs) the waiter in its **own** session; the test's
    ``db`` session may still hold the row in its identity map. Expiring the
    identity map forces a re-read so the deletion is observed without raising
    :class:`~sqlalchemy.orm.exc.ObjectDeletedError`.
    """
    db.expire_all()
    return db.get(EventSubscription, sub_id) is None


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


def _make_completion_event(
    db,
    gw: Gateway,
    *,
    task_id: str = TASK_ID,
    status: str = "completed",
    evt_type: str = TASK_COMPLETED_TYPE,
    evt_id: str | None = None,
) -> EventLog:
    """Persist a task-completion EventLog carrying ``data.taskId``."""
    row = EventLog(
        id=uuid.uuid4().hex,
        evt_id=evt_id or ("evt-" + uuid.uuid4().hex[:10]),
        evt_source=f"//{gw.id}",
        evt_type=evt_type,
        evt_subject=task_id,
        evt_time=datetime.now(timezone.utc),
        gateway_id=gw.id,
        provider_id="mcp",
        data={"taskId": task_id, "status": status},
        raw_headers={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _make_plain_event(db, gw: Gateway, *, evt_type: str = "com.github.push") -> EventLog:
    """Persist an ordinary (non-task-shaped) fanout event."""
    row = EventLog(
        id=uuid.uuid4().hex,
        evt_id="evt-" + uuid.uuid4().hex[:10],
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


def _make_fanout_sub(db, gw: Gateway, *, callback_url: str, event_types=None) -> EventSubscription:
    """Persist a standing fanout http_callback subscription bound to *gw*."""
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=gw.team_id,
        owner_email="finance@bud.studio",
        subscriber_kind="http_callback",
        callback_url=callback_url,
        source=f"//{gw.id}",
        event_types=event_types or ["com.mcp.*", "com.github.*"],
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
    """Construct a DeliveryWorker over the injected collaborators (no jitter)."""
    return DeliveryWorker(stream=stream, egress=egress, session_factory=session_factory, consumer_name=consumer, jitter=False, **kw)


async def _open_waiter(db, gw, *, callback_url=RESUME_CB, task_id=TASK_ID, correlation_key=CORR_KEY, ttl_seconds=None):
    """Open an ephemeral correlate waiter on *gw* keyed on *task_id*."""
    return await open_correlation(
        db,
        gateway_id=gw.id,
        team_id=gw.team_id,
        correlation_key=correlation_key,
        correlation_value=task_id,
        target={"agent_id": "agent-1", "version": "1"},
        callback_url=callback_url,
        ttl_seconds=ttl_seconds,
    )


# --------------------------------------------------------------------------- #
# is_correlate_shaped: the dead-letter gate detector                           #
# --------------------------------------------------------------------------- #


def test_is_correlate_shaped_detects_task_completed_type():
    """A ``*.task.completed`` envelope is correlate-shaped (carries a task carrier)."""
    env = EventEnvelope(
        id="e1",
        source="//gw",
        type=TASK_COMPLETED_TYPE,
        subject=TASK_ID,
        time=datetime.now(timezone.utc),
        data={"taskId": TASK_ID, "status": "completed"},
    )
    assert correlate_mod.is_correlate_shaped(env) is True


def test_is_correlate_shaped_false_for_plain_fanout_event():
    """An ordinary fanout event (no task carrier, non-task type) is NOT correlate-shaped."""
    env = EventEnvelope(
        id="e2",
        source="//gw",
        type="com.github.push",
        subject="octo/repo",
        time=datetime.now(timezone.utc),
        data={"ref": "refs/heads/main"},
    )
    assert correlate_mod.is_correlate_shaped(env) is False


# --------------------------------------------------------------------------- #
# TC-COR-001: waiting sub + terminal completion -> single resume, consumed     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_correlate_single_target_resume_no_fanout(session_factory, db):
    """TC-COR-001: a terminal completion resumes the ONE waiting sub, no fanout."""
    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id
    # A standing fanout sub that WOULD match the type glob is present, to prove
    # the correlate path does NOT fan out.
    fanout = _make_fanout_sub(db, gw, callback_url="https://fanout.example/cb")
    fanout_id = fanout.id
    evt = _make_completion_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    handled = await worker.run_once()

    assert handled == 1
    # Exactly one delivery, addressed to the correlate waiter's callback_url.
    assert len(egress.received) == 1
    assert egress.received[0].callback_url == RESUME_CB
    # The delivery envelope echoes correlate mode + the bound correlation_id.
    sub_block = egress.received[0].delivery_envelope["subscription"]
    assert sub_block["mode"] == "correlate"
    assert sub_block["correlation_id"] == TASK_ID

    # Exactly one attempt row, against the waiter (not the fanout sub).
    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert len(attempts) == 1
    assert attempts[0].subscription_id == waiter_id

    # No fanout attempt for the standing sub.
    assert all(a.subscription_id != fanout_id for a in attempts)

    # Waiter consumed (deleted) after the resume.
    assert _waiter_gone(db, waiter_id)
    # No dead-letter.
    assert db.execute(select(DeadLetter)).scalars().all() == []
    # Stream acked.
    assert await stream.pending() == []


# --------------------------------------------------------------------------- #
# TC-COR-010: deliver the same completion twice -> 2nd is a no-op              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_correlate_replay_is_idempotent_no_op(session_factory, db):
    """TC-COR-010: a replayed completion finds no waiter -> no 2nd resume."""
    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id
    evt = _make_completion_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)

    # First delivery: resumes + consumes.
    await stream.add(_stream_message(evt, gw))
    await worker.run_once()
    assert len(egress.received) == 1
    assert _waiter_gone(db, waiter_id)

    # Replay the exact same completion: the waiter is gone, so this is a no-op.
    # A new event_log id is used (the worker would otherwise dedup at ingress,
    # but here we drive the worker directly so the replay reaches it).
    replay_evt = _make_completion_event(db, gw, evt_id="evt-replay")
    await stream.add(_stream_message(replay_evt, gw))
    await worker.run_once()

    # Still exactly one delivery; the replay resumed nothing.
    assert len(egress.received) == 1
    # The replay was correlate-shaped + unmatched -> dead-lettered (TC-COR-011),
    # never fanned out, never a 2nd resume.
    dls = db.execute(select(DeadLetter)).scalars().all()
    assert len(dls) == 1
    assert dls[0].event_id == replay_evt.id


# --------------------------------------------------------------------------- #
# TC-COR-011: unknown correlation_value -> dead-lettered, no run, no fanout    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_correlate_unmatched_completion_dead_lettered(session_factory, db):
    """TC-COR-011: a correlate-shaped completion with no waiter is dead-lettered."""
    gw = _make_gateway(db)
    # A standing fanout sub is present; it must NOT be fanned to for a
    # correlate-shaped completion with no waiter.
    fanout = _make_fanout_sub(db, gw, callback_url="https://fanout.example/cb")
    evt = _make_completion_event(db, gw, task_id="task-nobody-waits")

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    # No delivery (no fanout, no resume).
    assert egress.received == []
    # No delivery attempt rows (no run created).
    assert db.execute(select(DeliveryAttempt)).scalars().all() == []
    # One dead-letter for the unmatched correlate completion.
    dls = db.execute(select(DeadLetter)).scalars().all()
    assert len(dls) == 1
    assert dls[0].event_id == evt.id
    assert dls[0].subscription_id is None
    assert "correlat" in (dls[0].last_error or "").lower()
    # Stream acked (durable dead-letter persisted before ack).
    assert await stream.pending() == []
    # The fanout sub stays active and untouched.
    db.refresh(fanout)
    assert fanout.active is True


@pytest.mark.asyncio
async def test_plain_event_still_fans_out_unchanged(session_factory, db):
    """A non-correlate-shaped event with no waiter falls through to fanout."""
    gw = _make_gateway(db)
    fanout = _make_fanout_sub(db, gw, callback_url="https://fanout.example/cb")
    evt = _make_plain_event(db, gw)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    # Fanned out normally.
    assert len(egress.received) == 1
    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert len(attempts) == 1
    assert attempts[0].subscription_id == fanout.id
    # No dead-letter for an ordinary event.
    assert db.execute(select(DeadLetter)).scalars().all() == []


# --------------------------------------------------------------------------- #
# TC-COR-026: persisted waiter survives "restart" -> re-resolvable by task_id  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_persisted_waiter_survives_restart(session_factory, db):
    """TC-COR-026: the waiter row IS the pending-run<->task_id map; survives restart."""
    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    # Simulate a restart: discard the in-memory worker + session entirely.
    # A fresh worker over a NEW session_factory-backed session re-resolves the
    # persisted waiter by task_id (no tasks/list — the row is the map).
    evt = _make_completion_event(db, gw)
    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw))

    fresh_worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory, consumer="w-after-restart")
    await fresh_worker.run_once()

    assert len(egress.received) == 1
    assert egress.received[0].callback_url == RESUME_CB
    assert _waiter_gone(db, waiter_id)


# --------------------------------------------------------------------------- #
# TC-COR-013: a foreign-team waiter is never resumed by a same-value completion #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_correlate_cross_tenant_not_resumed(session_factory, db):
    """A waiter in another team is never resumed (tenant-scoped); event dead-letters."""
    gw_a = _make_gateway(db, team_id=TEAM_ID)
    gw_b = _make_gateway(db, team_id=OTHER_TEAM)
    # Waiter lives in TEAM_ID, but the completion arrives on gw_b (OTHER_TEAM).
    waiter = await _open_waiter(db, gw_a)
    evt = _make_completion_event(db, gw_b)

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    await stream.add(_stream_message(evt, gw_b))

    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    await worker.run_once()

    # Cross-tenant resume is structurally impossible: no delivery to the waiter.
    assert egress.received == []
    # The waiter in TEAM_ID is untouched (still active).
    db.refresh(waiter)
    assert waiter.active is True
    assert db.get(EventSubscription, waiter.id) is not None
    # The completion (correlate-shaped, no SAME-tenant waiter) is dead-lettered.
    dls = db.execute(select(DeadLetter)).scalars().all()
    assert len(dls) == 1


# --------------------------------------------------------------------------- #
# TC-COR-014/015/016 (best-effort): stale status after consume does not re-open #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_status_after_consume_does_not_reopen(session_factory, db):
    """A later, stale (non-terminal) status after a terminal consume does not re-resume."""
    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    stream = InMemoryStreamBackend()
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)

    # Terminal completion: resume + consume.
    terminal = _make_completion_event(db, gw, status="completed")
    await stream.add(_stream_message(terminal, gw))
    await worker.run_once()
    assert len(egress.received) == 1
    assert _waiter_gone(db, waiter_id)

    # A stale, OLDER "working" status for the same task arrives afterwards.
    stale = _make_completion_event(db, gw, status="working", evt_id="evt-stale")
    stale_id = stale.id
    await stream.add(_stream_message(stale, gw))
    await worker.run_once()

    # No re-resume: the consumed waiter is gone, so the stale status resolves
    # to nothing. It is correlate-shaped + unmatched -> dead-lettered.
    assert len(egress.received) == 1
    dls = db.execute(select(DeadLetter)).scalars().all()
    assert len(dls) == 1
    assert dls[0].event_id == stale_id


# --------------------------------------------------------------------------- #
# TC-COR-028: poller->deliver coordinator + single resume under double observe #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_poll_and_deliver_resumes_waiter(session_factory, db, monkeypatch):
    """The #523 poller->deliver flow polls to terminal then resumes the waiter."""
    # First-Party
    from mcpgateway.services.events import task_webhook

    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    # A fake send_task_get: pending once, then terminal.
    calls = {"n": 0}

    async def fake_send_task_get(task_id: str) -> dict:
        calls["n"] += 1
        if calls["n"] < 2:
            return {"taskId": task_id, "status": "working"}
        return {"taskId": task_id, "status": "completed"}

    # The poller publishes via emit.publish_normalized_event onto the process-wide
    # stream singleton (reset fresh per-test); the worker drains that SAME stream.
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream_mod.get_event_stream(), egress=egress, session_factory=session_factory)

    published = await task_webhook.poll_and_deliver(
        sub=waiter,
        gateway=gw,
        send_task_get=fake_send_task_get,
        session_factory=session_factory,
        poll_interval=0.0,
        jitter=False,
    )
    assert published is True

    # The synthesized completion is on the L2 stream; the worker resumes the waiter.
    drained = await worker.run_once()
    assert drained == 1
    assert len(egress.received) == 1
    assert egress.received[0].callback_url == RESUME_CB
    assert _waiter_gone(db, waiter_id)


@pytest.mark.asyncio
async def test_poll_and_deliver_single_flight_double_observe(session_factory, db):
    """TC-COR-028: a poll + a duplicate completion both observe terminal -> one resume."""
    # First-Party
    from mcpgateway.services.events import task_webhook

    gw = _make_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    async def fake_send_task_get(task_id: str) -> dict:
        return {"taskId": task_id, "status": "completed"}

    stream = stream_mod.get_event_stream()
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)

    # The poller publishes the synthesized completion (one).
    await task_webhook.poll_and_deliver(
        sub=waiter,
        gateway=gw,
        send_task_get=fake_send_task_get,
        session_factory=session_factory,
        poll_interval=0.0,
        jitter=False,
    )
    # A duplicate provider-pushed completion also lands on the stream.
    dup = _make_completion_event(db, gw, evt_id="evt-dup-push")
    await stream.add(_stream_message(dup, gw))

    await worker.run_once()  # drains both entries

    # Exactly one resume: the second (whichever lost the race) found no waiter.
    assert len(egress.received) == 1
    assert _waiter_gone(db, waiter_id)


# --------------------------------------------------------------------------- #
# TC-COR-021: a forged completion is rejected by M1 ingress verify, never the  #
# worker - so a waiting correlate sub is never resumed by an unsigned forgery.  #
# --------------------------------------------------------------------------- #

# A task-completion provider descriptor that signs the raw body with an HMAC
# (same recipe family as GitHub), classifies every POST as a terminal
# ``com.mcp.task.completed`` (doubly correlate-shaped: the ``.task.completed``
# type suffix AND a ``taskId`` carrier in the body) and derives a deterministic
# dedup id from ``taskId``. The normalizer sets the envelope ``data`` to the raw
# parsed body, so a flat ``{"taskId": ...}`` body resolves the waiter's
# ``data.taskId`` carrier (envelope.data.taskId). A forged (bad-signature) POST
# fails the M1 verify before any normalize/persist/publish; a valid one resumes.
_TASKS_DESCRIPTOR_YAML = (
    "display_name: TaskCompletions\n"
    "verify:\n"
    "  strategy: hmac\n"
    "  header: X-Hub-Signature-256\n"
    "  algo: sha256\n"
    "  encoding: hex\n"
    "  prefix: sha256=\n"
    "  signed_payload: '{body}'\n"
    "event_type:\n"
    "  from: const\n"
    "  ref: com.mcp.task.completed\n"
    "  template: com.mcp.task.completed\n"
    "dedup_id:\n"
    "  from: jsonpath\n"
    "  ref: taskId\n"
)

_TASKS_SECRET = "task-completion-signing-key"


def _completion_body(task_id: str = TASK_ID, status: str = "completed") -> bytes:
    """Build a task-completion POST body carrying the task id (correlate-shaped)."""
    # Compact, key-ordered JSON so the signed bytes are stable across the test.
    # The body is flat so the normalized envelope's ``data`` is ``{"taskId": ...,
    # "status": ...}`` and the waiter's ``data.taskId`` carrier resolves.
    return ('{"taskId":"%s","status":"%s"}' % (task_id, status)).encode("utf-8")


def _valid_completion_headers(body: bytes, *, secret: str = _TASKS_SECRET) -> dict:
    """Build headers carrying a VALID HMAC-sha256 signature over *body*."""
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": "sha256=" + sig}


def _make_ingress_gateway(db, *, team_id: str = TEAM_ID, descriptor_ref: str = "tasks") -> Gateway:
    """Persist a Gateway wired for task-completion ingress with an encrypted secret."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-ingress-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        team_id=team_id,
        capabilities={"events": {"ingress": {"descriptor_ref": descriptor_ref}}},
        webhook_signing_secret=encode_auth({"secret": _TASKS_SECRET}),
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


@pytest.fixture
def _tasks_descriptor(tmp_path, monkeypatch):
    """Install a task-completion provider descriptor + enable the events flag."""
    (tmp_path / "tasks.yaml").write_text(_TASKS_DESCRIPTOR_YAML, encoding="utf-8")
    monkeypatch.setenv("MCPGATEWAY_EVENTS_DESCRIPTORS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)
    yield


@pytest.mark.asyncio
async def test_forged_completion_rejected_at_ingress_never_resumes_waiter(session_factory, db, _tasks_descriptor):
    """TC-COR-021: a forged completion is refused by M1 verify (401); the worker
    never sees it and the waiting correlate sub is never resumed."""
    gw = _make_ingress_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    body = _completion_body()
    # Forged: a syntactically-valid sig header that does NOT match the body bytes.
    forged_headers = {"X-Hub-Signature-256": "sha256=" + ("0" * 64)}

    svc = IngressService()
    result = await svc.ingest(conn_id=gw.id, raw_body=body, headers=forged_headers, query_params={}, db=db)

    # 1) M1 ingress refuses with the opaque 401 a bad signature always returns.
    assert result.status == 401
    assert result.envelope is None
    # 2) No domain event was normalized/persisted (the forgery left no event_log).
    assert db.execute(select(EventLog)).scalars().all() == []

    # 3) The worker drains the L2 stream the ingress publishes onto - which is
    #    EMPTY, because the forged completion was rejected upstream of the publish
    #    tail. So the worker resumes nobody and creates no attempt / dead-letter.
    stream = stream_mod.get_event_stream()
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    handled = await worker.run_once()

    assert handled == 0
    assert egress.received == []
    assert db.execute(select(DeliveryAttempt)).scalars().all() == []
    assert db.execute(select(DeadLetter)).scalars().all() == []

    # 4) The correlate waiter is untouched: a forgery cannot resume a run.
    db.expire_all()
    survivor = db.get(EventSubscription, waiter_id)
    assert survivor is not None and survivor.active is True


@pytest.mark.asyncio
async def test_valid_signed_completion_passes_ingress_and_resumes_waiter(session_factory, db, _tasks_descriptor):
    """TC-COR-021 (positive twin): a VALID-signature completion clears M1 verify,
    lands on the L2 stream, and the worker resumes the waiting correlate sub.

    This proves the forged-completion negative is a true security boundary, not a
    vacuous pass: the SAME body, gateway, and waiter resume end-to-end once the
    signature is genuine.
    """
    gw = _make_ingress_gateway(db)
    waiter = await _open_waiter(db, gw)
    waiter_id = waiter.id

    body = _completion_body()
    svc = IngressService()
    result = await svc.ingest(conn_id=gw.id, raw_body=body, headers=_valid_completion_headers(body), query_params={}, db=db)

    # M1 verify passes: the completion is normalized, persisted, and published.
    assert result.status == 202
    assert result.envelope is not None
    assert result.envelope.type == "com.mcp.task.completed"
    rows = db.execute(select(EventLog)).scalars().all()
    assert len(rows) == 1

    # The worker drains the published completion and resumes the ONE waiter.
    stream = stream_mod.get_event_stream()
    egress = InProcessEgressAdapter()
    worker = _new_worker(stream=stream, egress=egress, session_factory=session_factory)
    handled = await worker.run_once()

    assert handled == 1
    assert len(egress.received) == 1
    assert egress.received[0].callback_url == RESUME_CB
    assert _waiter_gone(db, waiter_id)
