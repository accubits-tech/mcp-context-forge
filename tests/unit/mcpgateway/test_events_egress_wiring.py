# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_egress_wiring.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

M3 wiring tests: the real egress adapters behind the worker seam.

These cover the M3 "wire it up" surface that sits on top of the already-built
HTTP-callback / streaming adapters and the SSRF/signing helpers:

* :func:`~mcpgateway.services.events.egress.inprocess.get_egress_adapter`
  resolves ``http_callback`` -> :class:`HttpCallbackEgressAdapter` and
  ``sse``/``ws`` -> :class:`StreamingEgressAdapter` (the factory default the
  worker falls through to when no adapter is injected).
* The :class:`~mcpgateway.services.events.delivery_worker.DeliveryWorker`
  selects the adapter per ``sub.subscriber_kind`` (TC-DEL-037 divergence): a
  failing ``http_callback`` sub dead-letters on exhaustion, while a dropped
  ``sse``/``ws`` sub is **best-effort** - never retried and never dead-lettered
  (SC-DEL-076 / TC-DEL-037).
* A per-subscription circuit breaker opens after N consecutive ``http_callback``
  failures and caps dispatch while open (TC-DEL-016 / SC-DEL-042/043/047).
* Updating a subscription's ``callback_url`` to a private/obfuscated target is
  rejected at UPDATE time with ``SubscriptionValidationError`` -> 422
  (TC-SEC-055 update arm), mirroring the create-time SSRF guard.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_egress_wiring.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, DeadLetter, DeliveryAttempt, EventLog, EventSubscription, Gateway
from mcpgateway.schemas import SubscriberRef, SubscriptionCreate
from mcpgateway.services.events.delivery_worker import DeliveryWorker
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter
from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter
from mcpgateway.services.events.egress.inprocess import get_egress_adapter, InProcessEgressAdapter
from mcpgateway.services.events.egress.streaming import StreamingEgressAdapter
from mcpgateway.services.events.stream import InMemoryStreamBackend
from mcpgateway.services.events.subscription_service import (
    SubscriptionService,
    SubscriptionValidationError,
)

TEAM_ID = "team-1"
CALLBACK = "https://sub.example/cb"


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
    """A single session over the shared in-memory database (setup/asserts)."""
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def session():
    """A standalone in-memory DB session (for the subscription-service tests)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_gateway(db) -> Gateway:
    """Persist a minimal, events-capable connection (Gateway) for the tenant."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        team_id=TEAM_ID,
        capabilities={"events": {"webhooksSupported": True}},
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


def _make_subscription(db, gw: Gateway, *, kind: str = "http_callback", callback_url: str = CALLBACK, target_ref: Optional[str] = None) -> EventSubscription:
    """Persist an active subscription of *kind* bound to *gw*."""
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gw.id,
        team_id=TEAM_ID,
        owner_email="finance@bud.studio",
        subscriber_kind=kind,
        callback_url=callback_url if kind == "http_callback" else None,
        subscriber_target_ref=target_ref,
        source=f"//{gw.id}",
        event_types=["com.github.*"],
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


class _ProgrammableAdapter(EgressAdapter):
    """An adapter returning a fixed outcome and counting deliver() calls."""

    def __init__(self, outcome: DeliveryOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
        self.calls += 1
        return self.outcome


def _svc(db) -> SubscriptionService:
    """Build a SubscriptionService bound to *db*."""
    return SubscriptionService(db)


# --------------------------------------------------------------------------- #
# get_egress_adapter: per-kind resolution                                      #
# --------------------------------------------------------------------------- #


def test_factory_returns_http_callback_adapter_for_http_kind():
    """``http_callback`` resolves to the real signed-POST adapter."""
    adapter = get_egress_adapter("http_callback")
    assert isinstance(adapter, HttpCallbackEgressAdapter)


def test_factory_returns_streaming_adapter_for_sse_and_ws():
    """``sse`` and ``ws`` resolve to the best-effort streaming adapter."""
    assert isinstance(get_egress_adapter("sse"), StreamingEgressAdapter)
    assert isinstance(get_egress_adapter("ws"), StreamingEgressAdapter)


def test_factory_unknown_kind_falls_back_to_http_callback():
    """An unknown subscriber kind falls back to the HTTP-callback push adapter."""
    adapter = get_egress_adapter("mystery")
    assert isinstance(adapter, HttpCallbackEgressAdapter)


# --------------------------------------------------------------------------- #
# WS1 - the factory threads the egress allow-list from settings               #
# --------------------------------------------------------------------------- #


def test_factory_threads_allow_hosts_from_settings(monkeypatch):
    """The HTTP-callback adapter is built with ``mcpgateway_events_egress_allow_hosts``.

    The factory caches a single adapter in a module global, so the cache is
    cleared and the setting is patched before resolution to assert the configured
    in-cluster allow-list (e.g. ``bud-budprompt``) reaches the adapter.
    """
    # First-Party
    from mcpgateway.services.events.egress import inprocess  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(settings, "mcpgateway_events_egress_allow_hosts", ["bud-budprompt"], raising=False)
    monkeypatch.setattr(inprocess, "_http_callback_adapter", None, raising=False)

    adapter = get_egress_adapter("http_callback")
    assert isinstance(adapter, HttpCallbackEgressAdapter)
    assert adapter._allow_hosts == {"bud-budprompt"}  # pylint: disable=protected-access


def test_factory_allow_hosts_defaults_to_none_when_unset(monkeypatch):
    """With no configured allow-list the adapter gets ``allow_hosts=None`` (unchanged)."""
    # First-Party
    from mcpgateway.services.events.egress import inprocess  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(settings, "mcpgateway_events_egress_allow_hosts", [], raising=False)
    monkeypatch.setattr(inprocess, "_http_callback_adapter", None, raising=False)

    adapter = get_egress_adapter("http_callback")
    assert adapter._allow_hosts is None  # pylint: disable=protected-access


# --------------------------------------------------------------------------- #
# Worker adapter selection per kind                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_worker_uses_streaming_adapter_for_sse_sub(session_factory, db):
    """An ``sse`` sub is delivered via the streaming adapter (best-effort, ok)."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw, kind="sse", target_ref="sess-1")
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    # egress=None -> the worker falls through to the factory per sub.kind.
    worker = DeliveryWorker(stream=stream, egress=None, session_factory=session_factory, consumer_name="w1", jitter=False)
    await worker.run_once()

    attempt = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id)).scalars().one()
    # Streaming publish always succeeds -> delivered; no dead-letter ever.
    assert attempt.status == "delivered"
    assert db.execute(select(DeadLetter).where(DeadLetter.subscription_id == sub.id)).scalars().all() == []


@pytest.mark.asyncio
async def test_tc_del_037_sse_best_effort_http_dead_letters(session_factory, db):
    """TC-DEL-037: a failing http sub dead-letters; a dropped sse sub does NOT.

    Both subscriptions match one event. The http_callback sub is driven by an
    always-failing adapter and must dead-letter on exhaustion; the sse sub is
    served by the (real) streaming adapter with no live client - it must never
    be retried or dead-lettered.
    """
    gw = _make_gateway(db)
    http_sub = _make_subscription(db, gw, kind="http_callback", callback_url=CALLBACK)
    sse_sub = _make_subscription(db, gw, kind="sse", target_ref="sess-1")
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    # A per-kind egress map: http -> always-fail; sse -> real streaming adapter.
    failing_http = _ProgrammableAdapter(DeliveryOutcome(ok=False, http_status=500, error="boom"))
    streaming = StreamingEgressAdapter()

    class _PerKind(EgressAdapter):
        async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
            kind = getattr(subscription, "subscriber_kind", "http_callback")
            adapter = streaming if kind in ("sse", "ws") else failing_http
            return await adapter.deliver(delivery_envelope=delivery_envelope, subscription=subscription)

    worker = DeliveryWorker(stream=stream, egress=_PerKind(), session_factory=session_factory, consumer_name="w1", max_attempts=2, backoff_base=0.0, jitter=False)

    await worker.run_once()  # http attempt 1 -> 500 (retrying); sse -> delivered (best-effort)
    await worker.run_due_retries(now=datetime.now(timezone.utc) + timedelta(hours=1))  # http attempt 2 -> 500 -> dead-letter

    # http_callback sub: dead-lettered on exhaustion.
    http_dls = db.execute(select(DeadLetter).where(DeadLetter.subscription_id == http_sub.id)).scalars().all()
    assert len(http_dls) == 1

    # sse sub: delivered, never retried, never dead-lettered (best-effort divergence).
    sse_attempts = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sse_sub.id)).scalars().all()
    assert len(sse_attempts) == 1
    assert sse_attempts[0].status == "delivered"
    assert sse_attempts[0].next_retry_at is None
    assert db.execute(select(DeadLetter).where(DeadLetter.subscription_id == sse_sub.id)).scalars().all() == []


@pytest.mark.asyncio
async def test_sse_failure_is_not_retried_or_dead_lettered(session_factory, db):
    """Even if a stream deliver reports a (transient) failure it is not retried.

    The streaming adapter is best-effort, so a non-ok outcome for an sse/ws sub
    must NOT schedule a retry and must NOT dead-letter (SC-DEL-076).
    """
    gw = _make_gateway(db)
    sse_sub = _make_subscription(db, gw, kind="sse", target_ref="sess-1")
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    # Force the stream adapter to report a failure to prove the worker still
    # treats sse/ws as best-effort (no retry, no DLQ) based on the sub kind.
    failing = _ProgrammableAdapter(DeliveryOutcome(ok=False, http_status=500, error="boom"))
    worker = DeliveryWorker(stream=stream, egress=failing, session_factory=session_factory, consumer_name="w1", max_attempts=5, backoff_base=0.0, jitter=False)
    await worker.run_once()

    attempt = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sse_sub.id)).scalars().one()
    assert attempt.status != "retrying"
    assert attempt.next_retry_at is None
    assert db.execute(select(DeadLetter).where(DeadLetter.subscription_id == sse_sub.id)).scalars().all() == []
    # The stream adapter is only attempted once - best-effort fire-and-forget.
    assert failing.calls == 1


# --------------------------------------------------------------------------- #
# TC-DEL-016: per-subscription circuit breaker                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_caps_dispatch(session_factory, db):
    """TC-DEL-016: after N consecutive failures the breaker opens and caps dispatch.

    A zombie http_callback endpoint fails every attempt. Once the per-sub
    breaker trips, further due-retry dispatches are skipped (the adapter is not
    invoked again) until the breaker's cooldown elapses - capping the retry rate
    rather than hammering the dead endpoint every tick.
    """
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw, kind="http_callback", callback_url=CALLBACK)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    failing = _ProgrammableAdapter(DeliveryOutcome(ok=False, http_status=500, error="boom"))
    worker = DeliveryWorker(
        stream=stream,
        egress=failing,
        session_factory=session_factory,
        consumer_name="w1",
        max_attempts=100,  # high enough that exhaustion is not what stops dispatch
        backoff_base=0.0,
        jitter=False,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown=300.0,
    )

    base = datetime.now(timezone.utc)
    await worker.run_once()  # attempt 1 -> failure (1 consecutive)

    # Drive due retries forward; each failing attempt increments the consecutive
    # failure count. After the threshold the breaker opens and dispatch is capped.
    for i in range(1, 12):
        await worker.run_due_retries(now=base + timedelta(seconds=i))

    # Without a breaker this would be ~12 deliver() calls; with the breaker it
    # is capped at roughly the threshold (a small constant), proving the open
    # breaker skipped the bulk of the dispatches.
    assert failing.calls <= 5

    # The breaker is observably open for this subscription.
    assert worker._circuit_open_for(sub.id) is True  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_probe_closes_on_success(session_factory, db):
    """A recovered endpoint closes the breaker on the half-open probe success."""
    gw = _make_gateway(db)
    sub = _make_subscription(db, gw, kind="http_callback", callback_url=CALLBACK)
    evt = _make_event(db, gw)

    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    # Fail enough to open, then recover.
    outcomes = [DeliveryOutcome(ok=False, http_status=500, error="boom") for _ in range(3)] + [DeliveryOutcome(ok=True, http_status=200)]
    egress = InProcessEgressAdapter()
    egress.set_outcomes(CALLBACK, outcomes)

    worker = DeliveryWorker(
        stream=stream,
        egress=egress,
        session_factory=session_factory,
        consumer_name="w1",
        max_attempts=100,
        backoff_base=0.0,
        jitter=False,
        circuit_breaker_threshold=3,
        circuit_breaker_cooldown=10.0,
    )

    base = datetime.now(timezone.utc)
    await worker.run_once()  # attempt 1 fail (1)
    await worker.run_due_retries(now=base + timedelta(seconds=1))  # fail (2)
    await worker.run_due_retries(now=base + timedelta(seconds=2))  # fail (3) -> breaker opens
    assert worker._circuit_open_for(sub.id) is True  # pylint: disable=protected-access

    # Within cooldown, a tick is skipped (breaker open).
    await worker.run_due_retries(now=base + timedelta(seconds=3))

    # After cooldown, the half-open probe is allowed; the endpoint has recovered
    # so the probe succeeds and the breaker closes.
    await worker.run_due_retries(now=base + timedelta(seconds=400))

    db.expire_all()
    delivered = db.execute(select(DeliveryAttempt).where(DeliveryAttempt.subscription_id == sub.id, DeliveryAttempt.status == "delivered")).scalars().all()
    assert len(delivered) == 1
    assert worker._circuit_open_for(sub.id) is False  # pylint: disable=protected-access


# --------------------------------------------------------------------------- #
# TC-DEL-029: high-cardinality fan-out is queue-first, isolated, lossless       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tc_del_029_high_cardinality_fanout_is_queue_first_and_isolated(session_factory, db):
    """TC-DEL-029: a burst fans out to many subs queue-first, per-sub isolated.

    SC-DEL-048/049/077. One accepted event matches a high-cardinality set of
    subscriptions. The contract has three load-shape guarantees we can prove
    hermetically without a real socket:

    * **Queue-first / no backpressure** - the L2 stream write (the ingress
      "202-fast" path) is decoupled from delivery: after the event is XADD-ed
      and *before* the worker runs, there are **zero** delivery attempts. The
      enqueue cost does not scale with the subscriber count or their health.
    * **Per-subscription isolation** - one subscriber whose adapter *raises*
      every call does not abort the fan-out: an adapter exception is contained
      to that sub's own outcome, never the loop.
    * **No drop** - every one of the N matched subs gets exactly one first
      attempt in a single worker pass; none are silently skipped.
    """
    gw = _make_gateway(db)

    # High-cardinality fan-out: many http_callback subs all matching one type.
    fanout = 200
    sub_ids: List[str] = [_make_subscription(db, gw, kind="http_callback", callback_url=f"https://sub-{i}.example/cb").id for i in range(fanout)]

    evt = _make_event(db, gw)

    # Queue-first: enqueue the accepted event onto the L2 stream. This is the
    # only work ingress does for delivery - it must not touch any subscription.
    stream = InMemoryStreamBackend()
    await stream.add(_stream_message(evt, gw))

    # PROVE queue-first: with the event already queued, nothing has been
    # delivered yet (delivery is strictly out-of-band of the enqueue).
    assert db.execute(select(DeliveryAttempt)).scalars().all() == []

    # One "poison" subscriber raises on every deliver; the rest succeed. The
    # poison sub must not starve or drop its neighbours (per-sub isolation).
    poison_id = sub_ids[fanout // 2]

    class _IsolatingAdapter(EgressAdapter):
        def __init__(self) -> None:
            self.delivered: List[str] = []

        async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
            if subscription.id == poison_id:
                raise RuntimeError("simulated subscriber blow-up")
            self.delivered.append(subscription.id)
            return DeliveryOutcome(ok=True, http_status=200)

    adapter = _IsolatingAdapter()
    worker = DeliveryWorker(stream=stream, egress=adapter, session_factory=session_factory, consumer_name="w1", backoff_base=0.0, jitter=False)

    # A single pass processes the whole burst entry (batch covers the one entry).
    await worker.run_once(batch=10)

    # No drop: exactly one first attempt per matched subscription - N rows.
    attempts = db.execute(select(DeliveryAttempt)).scalars().all()
    assert len(attempts) == fanout
    assert {a.subscription_id for a in attempts} == set(sub_ids)
    assert all(a.attempt_no == 1 for a in attempts)

    # Per-sub isolation: every non-poison sub was delivered exactly once; the
    # poison sub's raise was contained to its own (failed) outcome.
    assert len(adapter.delivered) == fanout - 1
    assert poison_id not in adapter.delivered

    by_sub = {a.subscription_id: a for a in attempts}
    assert by_sub[poison_id].status != "delivered"  # poison recorded as a failure...
    # ...while its N-1 neighbours all succeeded (no starvation / no cascade).
    assert sum(1 for sid in sub_ids if sid != poison_id and by_sub[sid].status == "delivered") == fanout - 1


# --------------------------------------------------------------------------- #
# TC-SEC-055 (update arm): SSRF-validate a changed callback_url at UPDATE       #
# --------------------------------------------------------------------------- #


def _fake_validator(*denied: str):
    """Return a validate_url_not_internal stand-in that raises only for *denied*.

    Avoids real DNS in unit tests: a URL whose host appears in *denied* raises
    ``ValueError`` (the shared util's contract); everything else passes.
    """

    def _validate(url: str) -> None:
        if any(d in url for d in denied):
            raise ValueError(f"URL resolves to a private/internal address ({url}).")

    return _validate


def test_update_callback_url_to_private_is_rejected(session, monkeypatch):
    """TC-SEC-055: updating callback_url to a metadata/link-local target -> 422."""
    monkeypatch.setattr(settings, "ssrf_protection_enabled", True)
    monkeypatch.setattr(
        "mcpgateway.services.events.subscription_service.validate_url_not_internal",
        _fake_validator("169.254.169.254"),
    )
    gw = _make_gateway(session)
    svc = _svc(session)

    create = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://safe.example/cb"),
        gateway_id=gw.id,
        source=f"//{gw.id}",
        event_types=["com.github.push"],
    )
    sub = asyncio.run(svc.create(session, create, user_email="finance@bud.studio", team_id=TEAM_ID))

    with pytest.raises(SubscriptionValidationError):
        asyncio.run(svc.update(session, sub.id, {"callback_url": "http://169.254.169.254/latest/meta-data/"}, team_id=TEAM_ID))

    # No half-state: the original safe callback_url is intact.
    session.refresh(sub)
    assert sub.callback_url == "https://safe.example/cb"


def test_update_callback_url_to_public_is_applied(session, monkeypatch):
    """A changed callback_url that passes the SSRF guard is persisted."""
    monkeypatch.setattr(settings, "ssrf_protection_enabled", True)
    # Pass-through validator (no DNS) for both create and update.
    monkeypatch.setattr(
        "mcpgateway.services.events.subscription_service.validate_url_not_internal",
        _fake_validator(),
    )
    gw = _make_gateway(session)
    svc = _svc(session)

    create = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://safe.example/cb"),
        gateway_id=gw.id,
        source=f"//{gw.id}",
        event_types=["com.github.push"],
    )
    sub = asyncio.run(svc.create(session, create, user_email="finance@bud.studio", team_id=TEAM_ID))

    updated = asyncio.run(svc.update(session, sub.id, {"callback_url": "https://new.example/cb"}, team_id=TEAM_ID))
    assert updated.callback_url == "https://new.example/cb"


def test_update_callback_url_ssrf_skipped_when_disabled(session, monkeypatch):
    """When SSRF protection is off the update accepts any callback_url."""
    monkeypatch.setattr(settings, "ssrf_protection_enabled", False)
    gw = _make_gateway(session)
    svc = _svc(session)

    create = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://safe.example/cb"),
        gateway_id=gw.id,
        source=f"//{gw.id}",
        event_types=["com.github.push"],
    )
    sub = asyncio.run(svc.create(session, create, user_email="finance@bud.studio", team_id=TEAM_ID))

    updated = asyncio.run(svc.update(session, sub.id, {"callback_url": "http://169.254.169.254/latest/meta-data/"}, team_id=TEAM_ID))
    assert updated.callback_url == "http://169.254.169.254/latest/meta-data/"
