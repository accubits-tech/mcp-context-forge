# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/delivery_worker.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

L3 delivery worker: the at-least-once egress pump (FRD §8.6/§8.7).

The worker consumes the durable L2 stream
(:class:`~mcpgateway.services.events.stream.EventStream`), matches each accepted
event against candidate subscriptions
(:func:`~mcpgateway.services.events.matching.find_candidate_subscriptions`),
records a per-subscription :class:`~mcpgateway.db.DeliveryAttempt` ledger row, and
drives the delivery through the L3 egress adapter
(:class:`~mcpgateway.services.events.egress.base.EgressAdapter`). It interprets
the returned :class:`~mcpgateway.services.events.egress.base.DeliveryOutcome` to
decide ACK / retry / dead-letter, exactly per the §8.7 single-source-of-truth
outcome table.

Exactly-one-row + stable idempotency key
----------------------------------------
The cross-retry idempotency key is the **event id** (``event_log.evt_id``); it is
stamped on every :class:`~mcpgateway.db.DeliveryAttempt` row for a given
``(event, subscription)`` and sent as the ``Idempotency-Key`` header on every
delivery, so a receiver dedups all retries of one logical delivery (FRD §8.7,
TC-DEL-001). Exactly-one ledger row per ``(event_id, subscription_id,
attempt_no)`` is guaranteed by the ``uq_delivery_attempt_event_sub_no`` unique
constraint plus a guarded insert: two workers racing the same reclaimed stream
entry (or a claim-reprocess after a crash) cannot create a duplicate attempt row
(TC-DEL-004/021/027/061-063). Retries reuse the same ``(event, subscription)``
pair with an incremented ``attempt_no``.

Durability / reclaim
--------------------
``run_once`` reads new entries into this consumer's PEL, processes each, and
``XACK``s **after** every candidate subscription has a persisted attempt row -
so durability lives in ``delivery_attempts`` from that point on. A worker that
crashes after ``XADD`` but before reading loses nothing (a fresh ``run_once``
consumes the entry - TC-DEL-020); a worker that crashes after reading but before
ack leaves the entry in the PEL, where ``reclaim_once`` (``XAUTOCLAIM``) re-owns
and reprocesses it idempotently (TC-DEL-021/027).
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# Third-Party
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import DeadLetter, DeliveryAttempt, EventLog
from mcpgateway.db import Gateway as DbGateway
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events import matching
from mcpgateway.services.events import tasks as tasks_mod
from mcpgateway.services.events.egress.base import DeliveryOutcome
from mcpgateway.services.events.egress.inprocess import get_egress_adapter
from mcpgateway.services.events.stream import EventStream, get_event_stream
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = ["DeliveryWorker"]

# Statuses for the delivery_attempts ledger (mirrors db.py column comment).
_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_RETRYING = "retrying"
_STATUS_FAILED = "failed"

# Subscriber kinds served by the best-effort SSE/WS streaming adapter: their
# deliveries are never retried and never dead-lettered (FRD §2.5/§9.2.2,
# SC-DEL-076 / TC-DEL-037).
_STREAMING_KINDS = ("sse", "ws")


@dataclass
class _CircuitState:
    """Per-subscription circuit-breaker state for the HTTP-callback path.

    Attributes:
        consecutive_failures: Number of consecutive failed deliveries observed
            since the last success.
        opened_at: Delivery-clock timestamp at which the breaker last opened, or
            ``None`` while the breaker is closed.
    """

    consecutive_failures: int = 0
    opened_at: Optional[datetime] = None


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly-naive timestamp to UTC-aware (SQLite drops tzinfo).

    Args:
        value: A timestamp that may be naive or ``None``.

    Returns:
        Optional[datetime]: The UTC-aware timestamp, or ``None``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class DeliveryWorker:
    """Drain the L2 stream and deliver each matched event at-least-once.

    The worker is collaborator-injected so the reliability tests can drive it
    over the in-memory stream + in-process egress adapter without Redis. In
    production the defaults wire the process-wide stream singleton, the egress
    factory, and :data:`mcpgateway.db.SessionLocal`.
    """

    def __init__(
        self,
        *,
        stream: Optional[EventStream] = None,
        egress: Any = None,
        session_factory: Optional[Callable[[], Any]] = None,
        consumer_name: str = "w1",
        max_attempts: Optional[int] = None,
        backoff_base: float = 0.5,
        backoff_cap: float = 60.0,
        jitter: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown: float = 60.0,
    ) -> None:
        """Initialize the worker.

        Args:
            stream: The L2 stream backend. Defaults to
                :func:`~mcpgateway.services.events.stream.get_event_stream`.
            egress: A single egress adapter or a per-kind factory. When ``None``
                each subscription's kind is resolved through
                :func:`~mcpgateway.services.events.egress.inprocess.get_egress_adapter`.
            session_factory: A zero-arg callable returning a SQLAlchemy session.
                Defaults to :data:`mcpgateway.db.SessionLocal`.
            consumer_name: This worker's consumer name within the group.
            max_attempts: Max delivery attempts before dead-lettering. Defaults
                to :data:`settings.mcpgateway_events_max_delivery_attempts`.
            backoff_base: Base seconds for exponential backoff.
            backoff_cap: Maximum backoff delay in seconds.
            jitter: Whether to apply +/-20% jitter to the backoff delay.
            circuit_breaker_threshold: Consecutive HTTP-callback failures for one
                subscription that trip its circuit breaker open (TC-DEL-016).
            circuit_breaker_cooldown: Seconds the breaker stays open before a
                single half-open probe is allowed through to test recovery. The
                cooldown is measured on the same delivery clock that schedules
                retries, so it is deterministic under injected ``now``.
        """
        self._stream: EventStream = stream if stream is not None else get_event_stream()
        self._egress = egress
        self._session_factory = session_factory
        self.consumer_name = consumer_name
        self.max_attempts = int(max_attempts if max_attempts is not None else settings.mcpgateway_events_max_delivery_attempts)
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.jitter = jitter
        self.circuit_breaker_threshold = int(circuit_breaker_threshold)
        self.circuit_breaker_cooldown = float(circuit_breaker_cooldown)
        # Per-subscription circuit-breaker state (HTTP-callback path only).
        self._circuits: Dict[str, _CircuitState] = {}

    # ------------------------------------------------------------------ #
    # Collaborator resolution                                            #
    # ------------------------------------------------------------------ #

    def _session(self) -> Any:
        """Open a fresh session for one unit of work.

        Returns:
            Any: A new SQLAlchemy session.
        """
        if self._session_factory is not None:
            return self._session_factory()
        # First-Party
        from mcpgateway.db import SessionLocal  # pylint: disable=import-outside-toplevel

        return SessionLocal()

    def _egress_for(self, subscriber_kind: str) -> Any:
        """Resolve the egress adapter for a subscriber kind.

        Args:
            subscriber_kind: The subscription kind (``http_callback`` / ``sse`` /
                ``ws``).

        Returns:
            Any: The egress adapter to use for this delivery.
        """
        if self._egress is not None:
            return self._egress
        return get_egress_adapter(subscriber_kind)

    # ------------------------------------------------------------------ #
    # Public worker entrypoints                                          #
    # ------------------------------------------------------------------ #

    async def run_once(self, batch: int = 10) -> int:
        """Read a batch of new stream entries, deliver each, then ACK.

        Args:
            batch: Maximum number of new entries to read in this pass.

        Returns:
            int: The number of stream entries handled (acked) this pass.
        """
        await self._stream.ensure_group()
        entries = await self._stream.read_group(self.consumer_name, count=batch)
        handled = 0
        for entry in entries:
            await self._handle_entry(entry)
            handled += 1
        return handled

    async def reclaim_once(self, min_idle_ms: int, batch: int = 10) -> int:
        """Reclaim stale PEL entries (``XAUTOCLAIM``) and reprocess them.

        Reprocessing is idempotent: the guarded attempt insert skips a row that
        already exists for ``(event, subscription, attempt_no)``, so a reclaim of
        a half-processed entry does not double-deliver or duplicate a ledger row
        (TC-DEL-021/027).

        Args:
            min_idle_ms: Minimum idle time (ms) before a PEL entry is claimable.
            batch: Maximum number of entries to claim in this pass.

        Returns:
            int: The number of entries reclaimed and handled.
        """
        await self._stream.ensure_group()
        entries = await self._stream.claim_stale(self.consumer_name, min_idle_ms=min_idle_ms, count=batch)
        handled = 0
        for entry in entries:
            await self._handle_entry(entry)
            handled += 1
        return handled

    async def run_due_retries(self, now: Optional[datetime] = None) -> int:
        """Re-dispatch delivery attempts whose backoff window has elapsed.

        Scans ``delivery_attempts`` for rows in ``pending``/``retrying`` with
        ``next_retry_at <= now`` and re-dispatches each (incrementing
        ``attempt_no`` on a fresh ledger row that carries the SAME idempotency
        key). On exhaustion the row is failed and dead-lettered.

        Args:
            now: Override for the current instant (testing). Defaults to UTC now.

        Returns:
            int: The number of due attempts re-dispatched.
        """
        now = now or datetime.now(timezone.utc)
        db = self._session()
        try:
            due = self._load_due_attempts(db, now)
            count = 0
            for attempt in due:
                await self._redispatch_due(db, attempt, now)
                count += 1
            return count
        finally:
            db.close()

    async def replay(self, *, event_id: str, subscription_id: str) -> bool:
        """Operator-initiated replay of a stored (often dead-lettered) delivery.

        Re-dispatches the ``(event, subscription)`` pair on a fresh ledger row
        whose ``attempt_no`` sits above any existing attempt, while carrying the
        SAME stable idempotency key (= event id) as the original attempts. The
        receiver therefore dedups the replay against the prior delivery
        (TC-DEL-026 / FRD §8.7 Admin-UI replay).

        Args:
            event_id: The ``event_log.id`` to replay.
            subscription_id: The ``event_subscriptions.id`` to replay to.

        Returns:
            bool: ``True`` if a delivery was re-dispatched, ``False`` if the
            event/subscription could not be resolved.
        """
        now = datetime.now(timezone.utc)
        db = self._session()
        try:
            event_log = db.get(EventLog, event_id)
            # First-Party
            from mcpgateway.db import EventSubscription  # pylint: disable=import-outside-toplevel

            sub = db.get(EventSubscription, subscription_id)
            if event_log is None or sub is None:
                return False

            existing = (
                db.execute(
                    select(DeliveryAttempt).where(
                        DeliveryAttempt.event_id == event_id,
                        DeliveryAttempt.subscription_id == subscription_id,
                    )
                )
                .scalars()
                .all()
            )
            next_no = (max((a.attempt_no for a in existing), default=0)) + 1
            idempotency_key = existing[0].idempotency_key if existing else event_log.evt_id

            new_attempt = self._insert_attempt_guarded(
                db,
                event_id=event_id,
                subscription_id=subscription_id,
                attempt_no=next_no,
                idempotency_key=idempotency_key,
            )
            if new_attempt is None:
                return False
            await self._deliver_and_record(db, event_log, sub, new_attempt, now=now)
            return True
        finally:
            db.close()

    async def run(self, stop_event: "asyncio.Event") -> None:  # pragma: no cover - production loop
        """Production loop: deliver, reclaim, retry, sweep, with small sleeps.

        Never raises out of the loop; each pass is wrapped so a transient error
        (e.g. a momentary DB/stream hiccup) is logged and retried rather than
        killing the worker task. The flag-gated correlate TTL sweep runs at most
        once per ``mcpgateway_events_correlation_sweep_interval_seconds`` so an
        abandoned correlate waiter is eventually expired (FRD §7.3 / TC-COR-008);
        it is a no-op when ``mcpgateway_events_correlation_sweep_enabled`` is off.

        Args:
            stop_event: An :class:`asyncio.Event`; the loop exits once it is set.
        """
        reclaim_idle_ms = int(self.backoff_cap * 1000)
        last_sweep = 0.0
        sweep_interval = float(getattr(settings, "mcpgateway_events_correlation_sweep_interval_seconds", 60) or 0)
        while not stop_event.is_set():
            try:
                await self.run_once()
                await self.reclaim_once(min_idle_ms=reclaim_idle_ms)
                await self.run_due_retries()
                now = time.monotonic()
                if sweep_interval <= 0 or (now - last_sweep) >= sweep_interval:
                    await self.sweep_expired_correlations()
                    last_sweep = now
            except Exception:  # noqa: BLE001 - the worker loop must never die.
                logger.exception("Delivery worker pass failed; continuing")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def sweep_expired_correlations(self) -> int:
        """Expire abandoned correlate waiters (flag-gated, best-effort).

        Periodically called from :meth:`run`. When
        :data:`settings.mcpgateway_events_correlation_sweep_enabled` is on, it
        opens a fresh session and runs
        :func:`~mcpgateway.services.events.correlate.expire_correlations` to
        resolve each expired correlate waiter to *timed-out* and delete it, so a
        late completion finds nothing (TC-COR-007 / TC-COR-008). It is a no-op
        (returns ``0``) when the sweep flag is off, and never raises out (a sweep
        error is logged, not propagated, so it can never kill the worker loop).

        Returns:
            int: The number of correlate waiters swept (``0`` when disabled or
            none expired).
        """
        if not getattr(settings, "mcpgateway_events_correlation_sweep_enabled", False):
            return 0
        db = self._session()
        try:
            return await correlate_mod.expire_correlations(db)
        except Exception:  # noqa: BLE001 - sweep is best-effort; never kill the loop.
            logger.exception("Correlate TTL sweep failed; continuing")
            return 0
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # Entry processing                                                   #
    # ------------------------------------------------------------------ #

    async def _handle_entry(self, entry: Tuple[str, dict]) -> None:
        """Process one stream entry in its own session, then ACK it.

        Args:
            entry: The ``(entry_id, message)`` pair from the stream.
        """
        db = self._session()
        try:
            await self._process_event(db, entry)
        finally:
            db.close()
        # ACK only after every candidate subscription has a persisted attempt
        # row: durability lives in delivery_attempts from here on (FRD §8.7).
        await self._stream.ack(entry[0])

    async def _process_event(self, db: Any, entry: Tuple[str, dict]) -> None:
        """Match candidate subscriptions for an entry and dispatch first attempts.

        Args:
            db: An active SQLAlchemy session.
            entry: The ``(entry_id, message)`` pair from the stream.
        """
        _entry_id, message = entry
        event_log_id = message.get("event_log_id")
        gateway_id = message.get("gateway_id")

        event_log = db.get(EventLog, event_log_id) if event_log_id else None
        if event_log is None:
            # The event row is gone (e.g. purged); nothing to deliver.
            logger.warning("Delivery worker: event_log %s not found; skipping entry", event_log_id)
            return

        gateway = db.get(DbGateway, gateway_id) if gateway_id else None

        envelope = message.get("envelope") or self._envelope_from_log(event_log)

        # Correlate-first (FRD §7.3 / §8.9): an async-task completion resumes the
        # single waiting run by exact (tenant-scoped) correlation_value match - it
        # is never fanned out and never spawns a new run (FR-22). This is
        # attempted BEFORE the fanout candidate scan.
        if await self._try_correlate(db, event_log, envelope, gateway):
            return

        subs = matching.find_candidate_subscriptions(db, envelope=envelope, gateway=gateway)

        for sub in subs:
            await self._dispatch_first_attempt(db, event_log, sub)

    async def _try_correlate(self, db: Any, event_log: EventLog, envelope: Any, gateway: Any) -> bool:
        """Resolve + resume a correlate waiter, or dead-letter an unmatched completion.

        The single-target resume half of routing (FRD §7.3 / §8.9). The decision
        is:

        * a same-tenant, active, non-expired correlate waiter matches the
          envelope's resolved ``correlation_value`` **and** the completion is
          **terminal** -> deliver to that ONE target (reusing
          :meth:`_dispatch_first_attempt`, whose §9.1a envelope already echoes
          ``mode="correlate"`` + ``correlation_id``) then idempotently consume
          (DELETE) the waiter. A replayed completion then resolves to nothing and
          is a no-op (TC-COR-001 / TC-COR-010). Returns ``True`` (handled, no
          fanout).
        * no waiter matches **but** the event is correlate-shaped (a
          task-completion carrier) -> the completion was meant to resume a run
          that is gone: dead-letter it (no run, no fanout, TC-COR-011) and return
          ``True``.
        * otherwise (an ordinary event, or a non-terminal status update for a
          still-waiting task) -> return ``False`` so the caller runs the existing
          fanout path unchanged.

        Stale-status regression guard (TC-COR-014/015/016, best-effort): a
        non-terminal status (e.g. ``working``/``input_required``) never consumes a
        live waiter, so an out-of-order/older status cannot prematurely tear down
        a waiting run; and because a terminal consume DELETEs the waiter, a later
        stale status simply finds nothing (dead-lettered as unmatched).

        Args:
            db: An active SQLAlchemy session.
            event_log: The event being delivered.
            envelope: The normalized inbound event envelope (object or dict).
            gateway: The connection the event arrived on (supplies the tenant).

        Returns:
            bool: ``True`` when the correlate arm handled the event (resume or
            unmatched dead-letter); ``False`` to fall through to fanout.
        """
        matched = correlate_mod.resolve_correlation(db, envelope=envelope, gateway=gateway)
        if matched is not None:
            # Stale-status guard: only a terminal completion resumes + consumes a
            # live waiter. A non-terminal status update leaves the waiter waiting
            # (and is not fanned out either - it is a correlate-shaped no-op).
            if not self._is_terminal_completion(envelope):
                return True
            await self._dispatch_first_attempt(db, event_log, matched)
            await correlate_mod.consume_correlation(db, matched)
            return True

        # No waiter. If the event is correlate-shaped, it was meant to resume a
        # run that is gone -> dead-letter (TC-COR-011); never fanout/spawn.
        if correlate_mod.is_correlate_shaped(envelope):
            self._dead_letter_unmatched_correlate(db, event_log)
            db.commit()
            return True

        return False

    @staticmethod
    def _is_terminal_completion(envelope: Any) -> bool:
        """Return whether a correlate-shaped envelope represents a terminal task.

        Reuses the tolerant Tasks parser
        (:func:`mcpgateway.services.events.tasks.parse_task_status`) over the
        envelope ``data`` body. A carrier that exposes **no** status field is
        treated as terminal: a bare ``com.<provider>.task.completed`` event whose
        ``type`` already asserts completion need not repeat a ``status`` (the type
        is the terminal signal). Only an explicit *non-terminal* status (e.g.
        ``working`` / ``input_required``) is treated as non-terminal.

        Args:
            envelope: The normalized inbound event envelope (object or dict).

        Returns:
            bool: ``True`` when the completion is terminal (or status-less).
        """
        data = envelope.get("data") if isinstance(envelope, dict) else getattr(envelope, "data", None)
        status = tasks_mod.parse_task_status(data)
        if status.state == tasks_mod.UNKNOWN_STATE:
            # No status field: the ``*.task.completed`` type itself is terminal.
            return True
        return status.terminal

    @staticmethod
    def _envelope_from_log(event_log: EventLog) -> dict:
        """Rebuild the inner event block from a persisted log row.

        Args:
            event_log: The persisted :class:`~mcpgateway.db.EventLog` row.

        Returns:
            dict: The event block (``id``/``source``/``type``/``subject``/
            ``time``/``data``).
        """
        return {
            "id": event_log.evt_id,
            "source": event_log.evt_source,
            "type": event_log.evt_type,
            "subject": event_log.evt_subject,
            "time": event_log.evt_time.isoformat() if event_log.evt_time else None,
            "data": event_log.data,
        }

    # ------------------------------------------------------------------ #
    # Attempt lifecycle                                                  #
    # ------------------------------------------------------------------ #

    async def _dispatch_first_attempt(self, db: Any, event_log: EventLog, sub: Any) -> None:
        """Insert (guarded) the first attempt row for a sub and dispatch it.

        The guarded insert is the exactly-one-row backstop: if a row for
        ``(event_id, subscription_id, attempt_no=1)`` already exists (e.g. a
        racing worker or a claim-reprocess), this is a no-op and no second
        delivery is fired (TC-DEL-004/021/027).

        Args:
            db: An active SQLAlchemy session.
            event_log: The event being delivered.
            sub: The matched :class:`~mcpgateway.db.EventSubscription`.
        """
        idempotency_key = event_log.evt_id
        attempt = self._insert_attempt_guarded(db, event_id=event_log.id, subscription_id=sub.id, attempt_no=1, idempotency_key=idempotency_key)
        if attempt is None:
            # A row already exists for attempt 1: idempotent skip (no re-deliver).
            return
        await self._deliver_and_record(db, event_log, sub, attempt)

    async def _redispatch_due(self, db: Any, attempt: DeliveryAttempt, now: datetime) -> None:
        """Re-dispatch a due retry on a fresh, incremented attempt row.

        Args:
            db: An active SQLAlchemy session.
            attempt: The prior (``pending``/``retrying``) ledger row that is due.
            now: The current instant.
        """
        event_log = db.get(EventLog, attempt.event_id)
        # First-Party
        from mcpgateway.db import EventSubscription  # pylint: disable=import-outside-toplevel

        sub = db.get(EventSubscription, attempt.subscription_id)
        if event_log is None or sub is None:
            return

        next_no = attempt.attempt_no + 1
        new_attempt = self._insert_attempt_guarded(
            db,
            event_id=attempt.event_id,
            subscription_id=attempt.subscription_id,
            attempt_no=next_no,
            idempotency_key=attempt.idempotency_key,  # stable across all retries.
        )
        if new_attempt is None:
            return
        # Retire the prior row so it is not re-selected as due.
        attempt.status = _STATUS_RETRYING
        attempt.next_retry_at = None
        db.add(attempt)
        await self._deliver_and_record(db, event_log, sub, new_attempt, now=now)

    async def _deliver_and_record(self, db: Any, event_log: EventLog, sub: Any, attempt: DeliveryAttempt, now: Optional[datetime] = None) -> None:
        """Drive the egress delivery for one attempt and apply the outcome.

        The SSE/WS streaming kind is **best-effort**: it is dispatched once and
        never retried or dead-lettered (TC-DEL-037). The HTTP-callback kind is
        gated by a per-subscription circuit breaker: while the breaker is open
        (and within its cooldown) the adapter is **not** invoked and the attempt
        is parked until the cooldown elapses, capping the dispatch rate against a
        zombie endpoint (TC-DEL-016).

        Args:
            db: An active SQLAlchemy session.
            event_log: The event being delivered.
            sub: The matched subscription.
            attempt: The freshly-inserted attempt ledger row.
            now: Override for the current instant (used by retry scheduling).
        """
        now = now or datetime.now(timezone.utc)
        subscriber_kind = getattr(sub, "subscriber_kind", "http_callback")
        is_streaming = subscriber_kind in _STREAMING_KINDS

        # Circuit breaker (HTTP-callback path only): skip dispatch while open.
        if not is_streaming and self._circuit_should_skip(sub, now):
            attempt.status = _STATUS_RETRYING
            attempt.next_retry_at = self._circuit_reopen_at(sub, now)
            db.add(attempt)
            db.commit()
            return

        envelope = self._build_delivery_envelope(event_log, sub, attempt)
        adapter = self._egress_for(subscriber_kind)

        try:
            outcome = await adapter.deliver(delivery_envelope=envelope, subscription=sub)
        except Exception as exc:  # noqa: BLE001 - an adapter raise is a transient failure.
            outcome = DeliveryOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")

        if not is_streaming:
            self._circuit_record(sub, now, ok=outcome.ok)

        self._apply_outcome(db, event_log, sub, attempt, outcome, now, is_streaming=is_streaming)

    def _apply_outcome(self, db: Any, event_log: EventLog, sub: Any, attempt: DeliveryAttempt, outcome: DeliveryOutcome, now: datetime, *, is_streaming: bool = False) -> None:
        """Update the attempt ledger + side effects per the §8.7 outcome table.

        Args:
            db: An active SQLAlchemy session.
            event_log: The event being delivered.
            sub: The matched subscription.
            attempt: The attempt ledger row to update in place.
            outcome: The egress :class:`DeliveryOutcome`.
            now: The current instant (for backoff scheduling).
            is_streaming: ``True`` for the best-effort SSE/WS kind, which is
                never retried and never dead-lettered (TC-DEL-037 / SC-DEL-076).
        """
        attempt.http_status = outcome.http_status
        attempt.error = outcome.error

        if outcome.ok:
            attempt.status = _STATUS_DELIVERED
            attempt.next_retry_at = None
            db.add(attempt)
            db.commit()
            return

        # SSE/WS is best-effort: a failed live push is terminal but is NOT
        # retried and NOT dead-lettered (a dropped stream is recovered from the
        # durable L2 stream, not the DLQ - FRD §9.2.2).
        if is_streaming:
            attempt.status = _STATUS_FAILED
            attempt.next_retry_at = None
            db.add(attempt)
            db.commit()
            return

        # 410 Gone: dead-letter AND auto-disable the subscription.
        if outcome.http_status == 410:
            attempt.status = _STATUS_FAILED
            attempt.next_retry_at = None
            db.add(attempt)
            sub.active = False
            db.add(sub)
            self._dead_letter(db, event_log, sub, attempt, outcome)
            db.commit()
            return

        # Permanent non-retryable 4xx (e.g. 400/403): dead-letter immediately.
        if outcome.permanent:
            attempt.status = _STATUS_FAILED
            attempt.next_retry_at = None
            db.add(attempt)
            self._dead_letter(db, event_log, sub, attempt, outcome)
            db.commit()
            return

        # Retryable failure (5xx / timeout / connection / 429). If this was the
        # last permitted attempt, dead-letter; otherwise schedule a retry.
        if attempt.attempt_no >= self.max_attempts:
            attempt.status = _STATUS_FAILED
            attempt.next_retry_at = None
            db.add(attempt)
            self._dead_letter(db, event_log, sub, attempt, outcome)
            db.commit()
            return

        attempt.status = _STATUS_RETRYING
        attempt.next_retry_at = self._next_retry_at(attempt.attempt_no, outcome, now)
        db.add(attempt)
        db.commit()

    # ------------------------------------------------------------------ #
    # Circuit breaker (per-subscription, HTTP-callback path)             #
    # ------------------------------------------------------------------ #

    def _circuit_open_for(self, subscription_id: str) -> bool:
        """Report whether a subscription's circuit breaker is currently open.

        Args:
            subscription_id: The subscription id to inspect.

        Returns:
            bool: ``True`` if the breaker has tripped open for this subscription.
        """
        state = self._circuits.get(subscription_id)
        return bool(state and state.opened_at is not None)

    def _circuit_should_skip(self, sub: Any, now: datetime) -> bool:
        """Whether this delivery should be skipped because the breaker is open.

        While the breaker is open and within its cooldown, dispatch is skipped
        (the adapter is not invoked) so a dead endpoint is not hammered every
        tick. Once the cooldown elapses a single half-open probe is permitted
        through (this returns ``False`` so the caller dispatches).

        Args:
            sub: The matched subscription.
            now: The current delivery instant.

        Returns:
            bool: ``True`` to skip the dispatch; ``False`` to dispatch (including
            the half-open probe after cooldown).
        """
        sub_id = getattr(sub, "id", None)
        state = self._circuits.get(sub_id) if sub_id is not None else None
        if state is None or state.opened_at is None:
            return False
        elapsed = (now - state.opened_at).total_seconds()
        # Cooldown elapsed -> allow exactly one half-open probe this tick.
        return elapsed < self.circuit_breaker_cooldown

    def _circuit_reopen_at(self, sub: Any, now: datetime) -> datetime:
        """Compute when a skipped (breaker-open) attempt should be retried.

        Parks the attempt until just after the cooldown window so the next
        due-retry sweep is the half-open probe rather than another no-op skip.

        Args:
            sub: The matched subscription.
            now: The current delivery instant.

        Returns:
            datetime: The UTC-aware next-retry timestamp.
        """
        sub_id = getattr(sub, "id", None)
        state = self._circuits.get(sub_id) if sub_id is not None else None
        opened_at = state.opened_at if state and state.opened_at is not None else now
        return opened_at + timedelta(seconds=self.circuit_breaker_cooldown)

    def _circuit_record(self, sub: Any, now: datetime, *, ok: bool) -> None:
        """Record a delivery outcome against the subscription's breaker state.

        A success resets the breaker (closes it). A failure increments the
        consecutive-failure count and, on reaching the threshold, opens the
        breaker; a failure during the half-open probe re-opens it from ``now``.

        Args:
            sub: The matched subscription.
            now: The current delivery instant.
            ok: Whether the delivery succeeded.
        """
        sub_id = getattr(sub, "id", None)
        if sub_id is None:
            return
        state = self._circuits.setdefault(sub_id, _CircuitState())

        if ok:
            # Recovery (incl. a successful half-open probe): close the breaker.
            state.consecutive_failures = 0
            state.opened_at = None
            return

        state.consecutive_failures += 1
        if state.opened_at is not None:
            # Failed half-open probe: re-open the cooldown window from now.
            state.opened_at = now
            return
        if state.consecutive_failures >= self.circuit_breaker_threshold:
            state.opened_at = now

    # ------------------------------------------------------------------ #
    # Ledger helpers                                                     #
    # ------------------------------------------------------------------ #

    def _insert_attempt_guarded(self, db: Any, *, event_id: str, subscription_id: str, attempt_no: int, idempotency_key: str) -> Optional[DeliveryAttempt]:
        """Insert one attempt row, treating a unique-key collision as a no-op.

        Backstops exactly-one-row per ``(event_id, subscription_id, attempt_no)``
        under concurrent workers / claim-reprocess (TC-DEL-004/021/027). On a
        collision the session is rolled back and ``None`` is returned so the
        caller skips the (duplicate) delivery.

        Args:
            db: An active SQLAlchemy session.
            event_id: The ``event_log.id`` foreign key.
            subscription_id: The ``event_subscriptions.id`` foreign key.
            attempt_no: The 1-based attempt number.
            idempotency_key: The stable cross-retry idempotency key (event id).

        Returns:
            Optional[DeliveryAttempt]: The persisted row, or ``None`` on collision.
        """
        attempt = DeliveryAttempt(
            event_id=event_id,
            subscription_id=subscription_id,
            attempt_no=attempt_no,
            status=_STATUS_PENDING,
            idempotency_key=idempotency_key,
        )
        db.add(attempt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        db.refresh(attempt)
        return attempt

    def _next_retry_at(self, attempt_no: int, outcome: DeliveryOutcome, now: datetime) -> datetime:
        """Compute the next retry instant for a retryable failure.

        Honors a 429-style ``retry_after`` (capped) when present; otherwise uses
        exponential backoff ``base * 2^(attempt-1)`` capped at ``backoff_cap``
        with optional +/-20% jitter.

        Args:
            attempt_no: The attempt number that just failed (1-based).
            outcome: The failure outcome (may carry ``retry_after``).
            now: The current instant.

        Returns:
            datetime: The UTC-aware next-retry timestamp.
        """
        if outcome.retry_after is not None:
            delay = min(float(outcome.retry_after), self.backoff_cap)
        else:
            delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt_no - 1)))
            if self.jitter and delay > 0:
                # random.uniform is safe here: jitter is for retry spread, not security.
                delay *= 1.0 + random.uniform(-0.2, 0.2)  # noqa: DUO102 # nosec B311
        return now + timedelta(seconds=delay)

    @staticmethod
    def _dead_letter(db: Any, event_log: EventLog, sub: Any, attempt: DeliveryAttempt, outcome: DeliveryOutcome) -> None:
        """Write a :class:`~mcpgateway.db.DeadLetter` row for a terminal failure.

        Args:
            db: An active SQLAlchemy session.
            event_log: The event that failed delivery.
            sub: The matched subscription.
            attempt: The terminal attempt ledger row.
            outcome: The terminal failure outcome.
        """
        dead = DeadLetter(
            event_id=event_log.id,
            subscription_id=getattr(sub, "id", None),
            attempts=attempt.attempt_no,
            last_error=outcome.error or (f"HTTP {outcome.http_status}" if outcome.http_status else "delivery failed"),
            payload_snapshot={
                "event": {
                    "id": event_log.evt_id,
                    "source": event_log.evt_source,
                    "type": event_log.evt_type,
                },
                "http_status": outcome.http_status,
            },
        )
        db.add(dead)

    @staticmethod
    def _dead_letter_unmatched_correlate(db: Any, event_log: EventLog) -> None:
        """Write a :class:`~mcpgateway.db.DeadLetter` for an unmatched completion.

        A correlate-shaped completion (a task-completion carrier) that resolves to
        **no** waiting sub is dead-lettered rather than fanned out (TC-COR-011):
        the run it was meant to resume is gone (already consumed, expired, or never
        opened). There is no subscription and no delivery attempt, so this writes a
        ``subscription_id=None`` / ``attempts=0`` row directly, keeping the
        attempt/outcome contract of :meth:`_dead_letter` intact.
        ``dead_letters.subscription_id`` is nullable (``ON DELETE SET NULL``), so a
        null subscription is schema-valid.

        Args:
            db: An active SQLAlchemy session.
            event_log: The unmatched correlate-shaped completion event.
        """
        dead = DeadLetter(
            event_id=event_log.id,
            subscription_id=None,
            attempts=0,
            last_error="unmatched correlate completion (no waiting subscription)",
            payload_snapshot={
                "event": {
                    "id": event_log.evt_id,
                    "source": event_log.evt_source,
                    "type": event_log.evt_type,
                    "subject": event_log.evt_subject,
                },
            },
        )
        db.add(dead)

    def _load_due_attempts(self, db: Any, now: datetime) -> List[DeliveryAttempt]:
        """Return retryable attempts whose backoff window has elapsed.

        Args:
            db: An active SQLAlchemy session.
            now: The current instant.

        Returns:
            List[DeliveryAttempt]: Rows in ``pending``/``retrying`` with
            ``next_retry_at <= now``.
        """
        rows = (
            db.execute(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.status.in_((_STATUS_PENDING, _STATUS_RETRYING)),
                    DeliveryAttempt.next_retry_at.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        due: List[DeliveryAttempt] = []
        for row in rows:
            nra = _aware(row.next_retry_at)
            if nra is not None and nra <= now:
                due.append(row)
        return due

    @staticmethod
    def _build_delivery_envelope(event_log: EventLog, sub: Any, attempt: DeliveryAttempt) -> dict:
        """Build the §5.6/§9.1a delivery envelope for one attempt.

        Args:
            event_log: The event being delivered.
            sub: The matched subscription (supplies target/mode/correlation).
            attempt: The attempt ledger row (its id is the per-attempt
                ``delivery_id``).

        Returns:
            dict: The delivery envelope ``{event, subscription, idempotency_key}``.
        """
        # The wire ``subscription.target`` is the agent identity the receiver
        # invokes - exactly ``{agent_id, version, params}`` (FRD §9.1a / S11 /
        # D3). It is echoed verbatim and never polluted with the delivery
        # ``callback_url``: the destination is resolved by the egress adapter
        # from the live subscription record, not carried in the envelope body,
        # so the locked contract a budprompt/bda receiver parses stays clean.
        target = dict(getattr(sub, "target", None) or {})
        return {
            "event": {
                "id": event_log.evt_id,
                "source": event_log.evt_source,
                "type": event_log.evt_type,
                "subject": event_log.evt_subject,
                "time": event_log.evt_time.isoformat() if event_log.evt_time else None,
                "data": event_log.data,
            },
            "subscription": {
                "id": getattr(sub, "id", None),
                "delivery_id": attempt.id,
                "mode": getattr(sub, "mode", "fanout"),
                "target": target,
                "correlation_id": getattr(sub, "correlation_value", None),
            },
            "idempotency_key": attempt.idempotency_key,
        }
