# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_e2e.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

True end-to-end pipeline tests for the server-initiated events subsystem.

Where the per-module suites (``test_events_*.py``) each prove one milestone in
isolation, this suite exercises the **whole vertical** across modules and asserts
the milestones *compose* into the locked FRD sequence diagrams:

* **E2E-1 (webhook fanout, FRD §8.8)** - a REAL GitHub-signed push POSTed through
  the FastAPI :class:`~fastapi.testclient.TestClient` to ``POST /webhooks/{conn}``
  is verified (M1), normalized + persisted + published onto L1 *and* the L2
  stream (M2a/emit), then the out-of-band :class:`DeliveryWorker` (M2b) drains the
  stream, matches the standing fanout subscription (M2 matching), and delivers
  exactly ONE §9.1a envelope to the fake subscriber whose ``Idempotency-Key`` is
  the event id (= GitHub delivery GUID) and whose ``subscription.target`` is
  echoed verbatim. A replayed delivery (same ``X-GitHub-Delivery``) is deduped
  end-to-end (no 2nd EventLog / stream entry / delivery).

* **E2E-2 (signed wire, M3)** - the same fanout path but delivered via the real
  :class:`HttpCallbackEgressAdapter` (``allow_loopback=True``) to a threaded
  loopback ``http.server`` on ``127.0.0.1``; the server is asserted to receive a
  POST carrying ``X-MCPGW-Signature`` + ``Idempotency-Key`` that re-verifies
  (recompute HMAC over ``"{ts}.{body}"``). If the hermetic loopback server proves
  flaky, E2E-1 (InProcess) remains the authoritative full-pipeline assertion;
  this arm is best-effort and skips on a server bring-up error.

* **E2E-3 (correlate resume, FRD §8.9)** - a correlate waiter is opened
  (:func:`register_task_webhook`) for a task id, a terminal completion carrying
  that id is published through the emit tail, and the worker resumes the ONE
  waiter (single delivery, ``mode="correlate"``, ``correlation_id`` set), consumes
  it, and a SECOND identical completion is a no-op; an UNKNOWN task id is
  dead-lettered with no delivery.

* **E2E-4 (mcp-native, M6, best-effort)** - driving
  :meth:`McpNativeSessionManager._on_message` with a ``resources/updated`` and a
  mock session whose ``read_resource`` returns canned content yields exactly one
  ``com.mcp.resource.updated`` EventLog row + an L1 stream/bus entry.

All behind the master flag, which is monkeypatched on
(:data:`settings.mcpgateway_events_enabled` = ``True``).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_e2e.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import http.server
import json
import os
import tempfile
import threading
import uuid

# Third-Party
from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, DeadLetter, DeliveryAttempt, EventLog, EventSubscription, Gateway
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.services.events.delivery_worker import DeliveryWorker
from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter
from mcpgateway.services.events.egress.inprocess import InProcessEgressAdapter
from mcpgateway.utils.services_auth import encode_auth

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

SECRET = "e2e-github-signing-secret"
TEAM_ID = "team-e2e-aaaaaaaaaaaaaaaaaaaaaaaa"
FANOUT_CB = "https://agent.example/runs/new"
TARGET = {"agent_id": "agent-7", "version": "2", "params": {"k": "v"}}

# A real GitHub push body. The subject normalizes to ``repository.full_name``
# and ``type`` to ``com.github.{X-GitHub-Event}`` = ``com.github.push``.
GITHUB_PUSH_BODY = b'{"ref":"refs/heads/main","repository":{"full_name":"octo/repo"},"pusher":{"name":"alice"}}'

CORR_KEY = "data.taskId"
TASK_ID = "task-e2e-xyz-789"
TASK_COMPLETED_TYPE = "com.mcp.task.completed"
RESUME_CB = "https://agent.example/resume"


# --------------------------------------------------------------------------- #
# Signing helpers (mirror the GitHub provider recipe)                          #
# --------------------------------------------------------------------------- #


def _github_headers(body: bytes, *, secret: str = SECRET, delivery: str) -> dict:
    """Build valid GitHub webhook headers (sha256 hex, ``sha256=`` prefix)."""
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": "sha256=" + sig,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# App + temp-DB fixture (mirrors the conftest app_with_temp_db, fn-scoped)     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_db():
    """Yield ``(app, SessionLocal)`` wired to a fresh temp SQLite database.

    Function-scoped so each test gets an isolated database and a handle to seed
    rows. Patches both ``mcpgateway.db`` and ``mcpgateway.main`` to point the
    request-scoped ``get_db`` dependency at the temp DB.
    """
    mp = MonkeyPatch()

    fd, path = tempfile.mkstemp(suffix=".db")
    url = f"sqlite:///{path}"
    mp.setattr(settings, "database_url", url, raising=False)

    # First-Party
    import mcpgateway.db as db_mod

    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    mp.setattr(db_mod, "engine", engine, raising=False)
    mp.setattr(db_mod, "SessionLocal", testing_session_local, raising=False)

    # First-Party
    import mcpgateway.main as main_mod

    mp.setattr(main_mod, "SessionLocal", testing_session_local, raising=False)
    mp.setattr(main_mod, "engine", engine, raising=False)

    Base.metadata.create_all(bind=engine)

    # First-Party
    from mcpgateway.main import app

    yield app, testing_session_local

    mp.undo()
    engine.dispose()
    os.close(fd)
    os.unlink(path)


@pytest.fixture(autouse=True)
def _events_enabled(monkeypatch):
    """Enable the events master flag for the duration of each test."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """Reset the process-wide bus, L2 stream, and ingress dedup-cache singletons.

    The webhook ingress publishes onto the process-wide
    :func:`~mcpgateway.services.events.stream.get_event_stream` singleton (the
    in-memory backend by default); resetting it per-test gives a fresh stream the
    worker can drain, and a fresh dedup cache so the idempotency arm is exercised
    against the cache *and* the DB unique-constraint backstop, not stale state.
    """
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


@pytest.fixture
def client(app_db):
    """Return a :class:`TestClient` against the temp-DB-wired app."""
    app, _ = app_db
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Seed helpers                                                                 #
# --------------------------------------------------------------------------- #


def _seed_github_gateway(session_local, *, secret: str = SECRET, team_id: str = TEAM_ID) -> str:
    """Persist a Gateway wired for GitHub events ingress; return its id (conn_id)."""
    db = session_local()
    try:
        gw = Gateway(
            id=uuid.uuid4().hex,
            name=f"gw-github-{uuid.uuid4().hex[:6]}",
            slug=f"gw-{uuid.uuid4().hex[:8]}",
            url="http://example.com",
            team_id=team_id,
            capabilities={"events": {"ingress": {"descriptor_ref": "github"}}},
            events_enabled=True,
            webhook_signing_secret=encode_auth({"secret": secret}),
        )
        db.add(gw)
        db.commit()
        db.refresh(gw)
        return gw.id
    finally:
        db.close()


def _seed_fanout_subscription(
    session_local,
    *,
    conn_id: str,
    callback_url: str = FANOUT_CB,
    team_id: str = TEAM_ID,
    target: dict | None = None,
    event_types=None,
) -> str:
    """Persist a standing fanout http_callback subscription bound to *conn_id*."""
    db = session_local()
    try:
        sub = EventSubscription(
            id=uuid.uuid4().hex,
            gateway_id=conn_id,
            team_id=team_id,
            owner_email="finance@bud.studio",
            subscriber_kind="http_callback",
            callback_url=callback_url,
            source=f"//{conn_id}",
            target=target if target is not None else dict(TARGET),
            event_types=event_types if event_types is not None else ["com.github.*"],
            mode="fanout",
            active=True,
        )
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return sub.id
    finally:
        db.close()


def _new_worker(session_local, *, egress, stream=None, consumer="w1") -> DeliveryWorker:
    """Construct a DeliveryWorker over the injected collaborators (no jitter)."""
    return DeliveryWorker(
        stream=stream if stream is not None else stream_mod.get_event_stream(),
        egress=egress,
        session_factory=session_local,
        consumer_name=consumer,
        jitter=False,
    )


def _count(session_local, model) -> int:
    """Count rows of *model* in a fresh session."""
    db = session_local()
    try:
        return len(db.execute(select(model)).scalars().all())
    finally:
        db.close()


# =========================================================================== #
# E2E-1: webhook fanout (FRD §8.8) - the headline full-pipeline test           #
# =========================================================================== #


@pytest.mark.asyncio
async def test_e2e1_github_push_fanout_full_pipeline(app_db, client):
    """E2E-1: signed GitHub push -> 202 + EventLog + stream entry; worker delivers
    exactly one §9.1a envelope; a replayed delivery is deduped end-to-end."""
    _, session_local = app_db
    conn_id = _seed_github_gateway(session_local)
    sub_id = _seed_fanout_subscription(session_local, conn_id=conn_id)

    stream = stream_mod.get_event_stream()

    # --- HTTP ingress through the real route (M1 verify -> emit) ------------- #
    delivery_guid = "gh-delivery-0001"
    resp = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_PUSH_BODY,
        headers=_github_headers(GITHUB_PUSH_BODY, delivery=delivery_guid),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}

    # One persisted EventLog row with the normalized envelope. The event id is the
    # GitHub delivery GUID (provider dedup id); source is connection-scoped.
    db = session_local()
    try:
        logs = db.execute(select(EventLog)).scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.evt_id == delivery_guid
        assert log.evt_type == "com.github.push"
        assert log.evt_subject == "octo/repo"
        assert log.evt_source == f"//{conn_id}"
        assert log.gateway_id == conn_id
        event_id = log.evt_id
    finally:
        db.close()

    # One entry landed on the durable L2 stream (the worker's input).
    assert len(await stream.pending()) == 0  # nothing read yet
    # The entry is undelivered-but-present; read_group will move it into the PEL.

    # --- Out-of-band delivery worker (M2 match + M2b egress) ----------------- #
    egress = InProcessEgressAdapter()
    worker = _new_worker(session_local, egress=egress, stream=stream)
    handled = await worker.run_once()
    assert handled == 1

    # Exactly ONE delivery, to the subscription's callback_url.
    assert len(egress.received) == 1
    rec = egress.received[0]
    assert rec.callback_url == FANOUT_CB
    # The Idempotency-Key equals the event id (= the GitHub delivery GUID).
    assert rec.idempotency_key == event_id

    # The §9.1a envelope is exactly the locked contract.
    env = rec.delivery_envelope
    assert set(env.keys()) == {"event", "subscription", "idempotency_key"}
    event_block = env["event"]
    assert event_block["id"] == event_id
    assert event_block["source"] == f"//{conn_id}"
    assert event_block["type"] == "com.github.push"
    assert event_block["subject"] == "octo/repo"
    # ``time`` is part of the locked §9.1a shape; its value is provider-dependent
    # (the GitHub descriptor has no time mapping, so it is None here).
    assert "time" in event_block
    assert event_block["data"]["ref"] == "refs/heads/main"
    assert event_block["data"]["repository"]["full_name"] == "octo/repo"

    sub_block = env["subscription"]
    assert sub_block["id"] == sub_id
    assert sub_block["delivery_id"] is not None
    assert sub_block["mode"] == "fanout"
    # The target is echoed verbatim - exactly {agent_id, version, params}.
    assert sub_block["target"] == TARGET
    # A fanout delivery carries a null correlation_id.
    assert sub_block["correlation_id"] is None
    assert env["idempotency_key"] == event_id

    # A DeliveryAttempt ledger row recorded the success.
    db = session_local()
    try:
        attempts = db.execute(select(DeliveryAttempt)).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].subscription_id == sub_id
        assert attempts[0].status == "delivered"
        assert attempts[0].idempotency_key == event_id
    finally:
        db.close()

    # Stream acked after delivery.
    assert await stream.pending() == []

    # --- Idempotency arm: replay the SAME signed delivery -------------------- #
    resp2 = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_PUSH_BODY,
        headers=_github_headers(GITHUB_PUSH_BODY, delivery=delivery_guid),
    )
    # The route still answers 202 (a duplicate is silently accepted), but no
    # second EventLog row, no second stream entry, and so no second delivery.
    assert resp2.status_code == 202
    assert _count(session_local, EventLog) == 1

    handled2 = await worker.run_once()
    assert handled2 == 0  # nothing new on the stream
    assert len(egress.received) == 1  # still exactly one delivery
    assert _count(session_local, DeliveryAttempt) == 1


# =========================================================================== #
# E2E-2: signed wire to a real loopback HTTP server (M3) - best-effort         #
# =========================================================================== #


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """A POST handler that records the request and returns 200."""

    record: dict = {}

    def log_message(self, *args, **kwargs):  # noqa: D401 - silence access log.
        """Suppress the stdlib access log."""

    def do_POST(self):  # noqa: N802 - stdlib handler name.
        """Record the request body + headers, then answer 200."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).record = {"headers": {k: v for k, v in self.headers.items()}, "body": body}
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass


@pytest.mark.asyncio
async def test_e2e2_signed_wire_to_loopback_server(app_db, client):
    """E2E-2 (best-effort): the same fanout path delivered over a REAL loopback
    HTTP server via the production-grade :class:`HttpCallbackEgressAdapter`.

    The receiver verifies the ``X-MCPGW-Signature`` (HMAC over ``"{ts}.{body}"``)
    and ``Idempotency-Key``. The delivery secret rides in the subscription's
    ``delivery.auth`` block. ``https_only=False`` + ``allow_loopback=True`` reach
    the plain-http loopback server; the production default policy is never
    weakened (it is asserted by the M3 suite).
    """
    _, session_local = app_db

    # Bring up a threaded loopback HTTP server; skip the arm if it fails.
    handler_cls = type("_BoundRecordingHandler", (_RecordingHandler,), {"record": {}})
    try:
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    except OSError as exc:  # pragma: no cover - hermetic server bring-up failure.
        pytest.skip(f"loopback server unavailable: {exc}")
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    delivery_secret = "wire-hmac-secret"
    callback_url = f"http://127.0.0.1:{port}/cb"

    try:
        conn_id = _seed_github_gateway(session_local)
        # The subscription carries an hmac delivery.auth block whose secret is
        # encrypted at rest under ``secret_encrypted`` (the persist-side sentinel
        # the adapter decrypts for signing).
        db = session_local()
        try:
            sub = EventSubscription(
                id=uuid.uuid4().hex,
                gateway_id=conn_id,
                team_id=TEAM_ID,
                owner_email="finance@bud.studio",
                subscriber_kind="http_callback",
                callback_url=callback_url,
                source=f"//{conn_id}",
                target=dict(TARGET),
                event_types=["com.github.*"],
                mode="fanout",
                active=True,
                delivery={"auth": {"strategy": "hmac", "secret_encrypted": encode_auth({"v": delivery_secret})}},
            )
            db.add(sub)
            db.commit()
        finally:
            db.close()

        stream = stream_mod.get_event_stream()

        # Ingress the signed push through the HTTP route.
        resp = client.post(
            f"/webhooks/{conn_id}",
            content=GITHUB_PUSH_BODY,
            headers=_github_headers(GITHUB_PUSH_BODY, delivery="gh-wire-0001"),
        )
        assert resp.status_code == 202

        # Drive the worker with the REAL signed-POST adapter to the loopback host.
        adapter = HttpCallbackEgressAdapter(
            https_only=False,
            allow_loopback=True,
            connect_timeout=2.0,
            read_timeout=2.0,
            total_timeout=2.0,
        )
        worker = _new_worker(session_local, egress=adapter, stream=stream)
        handled = await worker.run_once()
        assert handled == 1

        # The loopback server received exactly one POST with the verifiable
        # signature + idempotency key.
        record = handler_cls.record
        assert record, "loopback server did not receive a delivery"
        rec_headers = {k.lower(): v for k, v in record["headers"].items()}
        body = record["body"]

        idem = rec_headers.get("idempotency-key")
        assert idem == "gh-wire-0001"

        ts = rec_headers.get("x-mcpgw-timestamp")
        sig = rec_headers.get("x-mcpgw-signature")
        assert ts is not None and sig is not None
        expected = "sha256=" + hmac.new(delivery_secret.encode("utf-8"), ts.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(sig, expected)

        # The delivered body is the §9.1a envelope carrying the GitHub event.
        env = json.loads(body)
        assert env["event"]["type"] == "com.github.push"
        assert env["event"]["subject"] == "octo/repo"
        assert env["subscription"]["target"] == TARGET
        assert env["idempotency_key"] == "gh-wire-0001"

        # The attempt ledger recorded a delivered outcome.
        db = session_local()
        try:
            attempts = db.execute(select(DeliveryAttempt)).scalars().all()
            assert len(attempts) == 1
            assert attempts[0].status == "delivered"
        finally:
            db.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


# =========================================================================== #
# E2E-3: correlate resume (FRD §8.9)                                           #
# =========================================================================== #


def _seed_plain_gateway(session_local, *, team_id: str = TEAM_ID) -> str:
    """Persist a minimal connection (Gateway) carrying *team_id*; return its id."""
    db = session_local()
    try:
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
        return gw.id
    finally:
        db.close()


async def _publish_completion(session_local, *, conn_id: str, task_id: str, evt_id: str) -> str:
    """Publish a terminal task-completion through the emit tail; return event_log_id.

    Builds an :class:`~mcpgateway.schemas.EventEnvelope` whose ``data`` carries the
    task id under ``taskId`` so the waiter's ``data.taskId`` carrier resolves, and
    whose ``type`` is ``com.mcp.task.completed`` (terminal + correlate-shaped).
    """
    # First-Party
    from mcpgateway.schemas import EventEnvelope
    from mcpgateway.services.events.emit import publish_normalized_event

    db = session_local()
    try:
        gw = db.get(Gateway, conn_id)
        envelope = EventEnvelope(
            id=evt_id,
            source=f"//{conn_id}",
            type=TASK_COMPLETED_TYPE,
            subject=task_id,
            time=datetime.now(timezone.utc),
            data={"taskId": task_id, "status": "completed"},
        )
        published, event_log_id = await publish_normalized_event(db, gateway=gw, envelope=envelope)
        assert published is True
        return event_log_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_e2e3_correlate_resume_full_pipeline(app_db):
    """E2E-3: open a correlate waiter, publish a terminal completion carrying the
    task id, run the worker -> the ONE waiter is resumed + consumed; a duplicate
    is a no-op; an UNKNOWN task id is dead-lettered with no delivery."""
    _, session_local = app_db
    conn_id = _seed_plain_gateway(session_local)

    # Open the correlate waiter (the #523 register_task_webhook entry point).
    db = session_local()
    try:
        gw = db.get(Gateway, conn_id)
        waiter = await correlate_mod.register_task_webhook(
            db,
            gateway=gw,
            team_id=TEAM_ID,
            task_id=TASK_ID,
            webhook={"url": RESUME_CB, "target": dict(TARGET)},
        )
        waiter_id = waiter.id
    finally:
        db.close()

    stream = stream_mod.get_event_stream()
    egress = InProcessEgressAdapter()
    worker = _new_worker(session_local, egress=egress, stream=stream)

    # Publish a terminal completion carrying TASK_ID and drain the worker.
    await _publish_completion(session_local, conn_id=conn_id, task_id=TASK_ID, evt_id="evt-complete-1")
    handled = await worker.run_once()
    assert handled == 1

    # Exactly ONE single-target resume to the waiter's callback_url.
    assert len(egress.received) == 1
    rec = egress.received[0]
    assert rec.callback_url == RESUME_CB
    sub_block = rec.delivery_envelope["subscription"]
    assert sub_block["mode"] == "correlate"
    assert sub_block["correlation_id"] == TASK_ID
    assert sub_block["target"] == TARGET

    # Exactly one attempt row against the waiter; no fanout / dead-letter.
    db = session_local()
    try:
        attempts = db.execute(select(DeliveryAttempt)).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].subscription_id == waiter_id
        # The waiter is consumed (deleted) after the resume.
        assert db.get(EventSubscription, waiter_id) is None
        assert db.execute(select(DeadLetter)).scalars().all() == []
    finally:
        db.close()
    assert await stream.pending() == []

    # --- A SECOND identical completion is an idempotent no-op ---------------- #
    # The waiter is gone, so the replay resolves to nothing; being correlate-
    # shaped + unmatched it is dead-lettered (never a 2nd resume, never fanout).
    await _publish_completion(session_local, conn_id=conn_id, task_id=TASK_ID, evt_id="evt-complete-2")
    handled2 = await worker.run_once()
    assert handled2 == 1
    assert len(egress.received) == 1  # still exactly one resume
    db = session_local()
    try:
        dls = db.execute(select(DeadLetter)).scalars().all()
        assert len(dls) == 1
        assert "correlat" in (dls[0].last_error or "").lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_e2e3_correlate_unknown_task_dead_lettered(app_db):
    """E2E-3 (unmatched arm): a completion for an UNKNOWN task id is dead-lettered
    with no delivery (no waiter, no fanout)."""
    _, session_local = app_db
    conn_id = _seed_plain_gateway(session_local)

    # A waiter exists for TASK_ID, but the completion carries a DIFFERENT id.
    db = session_local()
    try:
        gw = db.get(Gateway, conn_id)
        await correlate_mod.register_task_webhook(
            db, gateway=gw, team_id=TEAM_ID, task_id=TASK_ID, webhook={"url": RESUME_CB, "target": dict(TARGET)}
        )
    finally:
        db.close()

    stream = stream_mod.get_event_stream()
    egress = InProcessEgressAdapter()
    worker = _new_worker(session_local, egress=egress, stream=stream)

    unknown_log_id = await _publish_completion(session_local, conn_id=conn_id, task_id="task-nobody-waits", evt_id="evt-unknown-1")
    handled = await worker.run_once()
    assert handled == 1

    # No delivery, no attempt row; exactly one dead-letter for the unmatched event.
    assert egress.received == []
    db = session_local()
    try:
        assert db.execute(select(DeliveryAttempt)).scalars().all() == []
        dls = db.execute(select(DeadLetter)).scalars().all()
        assert len(dls) == 1
        assert dls[0].event_id == unknown_log_id
        assert dls[0].subscription_id is None
    finally:
        db.close()
    assert await stream.pending() == []


# =========================================================================== #
# E2E-4: mcp-native ingress (M6) - best-effort                                 #
# =========================================================================== #


@pytest.mark.asyncio
async def test_e2e4_mcp_native_resource_updated(app_db):
    """E2E-4 (best-effort): a ``resources/updated`` notification driven into
    :meth:`McpNativeSessionManager._on_message` with a mock session whose
    ``read_resource`` returns canned content yields exactly one
    ``com.mcp.resource.updated`` EventLog row + an L1 bus/stream entry."""
    # Third-Party
    import mcp.types as mcp_types
    from pydantic import AnyUrl

    # First-Party
    from mcpgateway.services.events.emit import synthesize_mcp_event_id
    from mcpgateway.services.events.mcp_native import McpNativeSessionManager

    _, session_local = app_db

    # Seed an MCP-native gateway.
    db = session_local()
    try:
        gw = Gateway(
            id=uuid.uuid4().hex,
            name=f"gw-mcp-{uuid.uuid4().hex[:6]}",
            slug=f"gw-{uuid.uuid4().hex[:8]}",
            url="http://upstream.example.com/mcp",
            transport="STREAMABLEHTTP",
            team_id=TEAM_ID,
            capabilities={"events": {"ingress_mode": "mcp_native"}},
            hook_state={},
        )
        db.add(gw)
        db.commit()
        db.refresh(gw)
        gw_id = gw.id
    finally:
        db.close()

    uri = "res://doc/e2e"
    read_text = "canned-contents-e2e"

    class _FakeSession:
        """Scripted upstream ClientSession: initialize / subscribe / read."""

        def __init__(self):
            self.reads: list[str] = []

        async def initialize(self):
            caps = mcp_types.ServerCapabilities(resources=mcp_types.ResourcesCapability(subscribe=True, listChanged=True))
            return mcp_types.InitializeResult(protocolVersion="2025-06-18", capabilities=caps, serverInfo=mcp_types.Implementation(name="fake", version="0"))

        async def subscribe_resource(self, _uri):
            return mcp_types.EmptyResult()

        async def list_resources(self, *a, **k):
            return mcp_types.ListResourcesResult(resources=[])

        async def list_tools(self, *a, **k):
            return mcp_types.ListToolsResult(tools=[])

        async def list_prompts(self, *a, **k):
            return mcp_types.ListPromptsResult(prompts=[])

        async def read_resource(self, u):
            self.reads.append(str(u))
            return mcp_types.ReadResourceResult(contents=[mcp_types.TextResourceContents(uri=AnyUrl(str(u)), text=read_text)])

        async def aclose(self):
            return None

    fake = _FakeSession()

    async def client_factory(_gateway):
        return fake

    # Reload the gateway in a fresh session to hand to the manager.
    db = session_local()
    try:
        gw_row = db.get(Gateway, gw_id)
        mgr = McpNativeSessionManager(gateway=gw_row, session_factory=session_local, client_factory=client_factory)
    finally:
        db.close()

    queue = bus_mod.get_event_bus().subscribe()
    stream = stream_mod.get_event_stream()

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource(uri)
        await mgr._on_message(mcp_types.ServerNotification(mcp_types.ResourceUpdatedNotification(params=mcp_types.ResourceUpdatedNotificationParams(uri=AnyUrl(uri)))))
        await mgr.stop()

    await scenario()

    # Exactly one normalized com.mcp.resource.updated EventLog row.
    db = session_local()
    try:
        rows = db.execute(select(EventLog)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.evt_type == "com.mcp.resource.updated"
        assert row.evt_subject == uri
        assert row.evt_source == f"//{gw_id}"
        assert row.gateway_id == gw_id
        assert read_text in str(row.data)
        # The id is the synthesized deterministic digest (seq=1 for the first relay).
        expected = synthesize_mcp_event_id(gateway_id=gw_id, source=f"//{gw_id}", type="com.mcp.resource.updated", subject=uri, seq=1)
        assert row.evt_id == expected
    finally:
        db.close()

    # The upstream read was issued to fetch content.
    assert fake.reads == [uri]

    # Exactly one L1 fan-out of the inner event dict.
    published = []
    while not queue.empty():
        published.append(queue.get_nowait())
    assert len(published) == 1
    assert published[0]["type"] == "com.mcp.resource.updated"
    assert published[0]["subject"] == uri

    # One entry landed on the durable L2 stream too (drainable by the worker).
    assert len(await stream.pending()) == 0  # nothing read yet, but present
    egress = InProcessEgressAdapter()
    worker = _new_worker(session_local, egress=egress, stream=stream)
    handled = await worker.run_once()
    assert handled == 1  # the worker consumed the one stream entry
