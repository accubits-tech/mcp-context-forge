# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/task_webhook.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

#523 poller->deliver coordinator: drive an upstream task to terminal, then
synthesize a completion event so the worker's correlate path resumes the waiter.

This is the active half of the correlate / resume flow (FRD §7.3.3 / §8.9). The
passive half lives in :mod:`mcpgateway.services.events.correlate`
(:func:`~mcpgateway.services.events.correlate.register_task_webhook` opens an
ephemeral ``mode="correlate"`` waiter keyed on the task id) and in the delivery
worker (its correlate-first seam resolves + delivers + consumes that waiter when
a completion event arrives). When the upstream provider does **not** push a
completion of its own — the common case for a synchronous-looking ``tools/call``
that returned a task handle — the gateway must *poll* the task to terminal and
inject the completion itself.

:func:`poll_and_deliver` is that coordinator:

1. drive :class:`~mcpgateway.services.events.tasks.TaskPoller` over an injectable
   ``send_task_get(task_id) -> dict`` to a **terminal** status (single-flight per
   task id, TC-COR-028; bounded by the poller watchdog so it never hangs);
2. build a canonical ``com.mcp.task.completed`` completion
   :class:`~mcpgateway.schemas.EventEnvelope` carrying the task id under
   ``data.taskId`` (the correlate carrier the waiter was opened on) plus the
   terminal status; and
3. run it through
   :func:`~mcpgateway.services.events.emit.publish_normalized_event` so it lands
   on the durable L2 stream exactly like a provider-pushed completion. The
   delivery worker's correlate arm then resolves the waiter, delivers the §9.1a
   resume to its ``callback_url``, and consumes (DELETEs) the waiter.

Because the waiter row is persisted (it *is* the pending-run <-> task_id map), a
gateway restart re-polls each known task id individually (no ``tasks/list``,
TC-COR-026), and re-injecting an already-resumed completion is a no-op (the
waiter is gone, TC-COR-010).
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Sequence
import uuid

# First-Party
from mcpgateway.config import settings
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events import correlate as correlate_mod
from mcpgateway.services.events import emit
from mcpgateway.services.events import tasks as tasks_mod
from mcpgateway.services.events.tasks import TaskPoller, TaskStatus
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = ["build_completion_envelope", "poll_and_deliver", "process_tool_call_webhooks"]

# Canonical reverse-DNS task-completion type (FRD §2.7). The worker's
# correlate-shaped detector keys on the ``.task.completed`` suffix, and the
# waiter resolves on the carrier value (``data.taskId``), not the type.
TASK_COMPLETED_TYPE = "com.mcp.task.completed"


def build_completion_envelope(*, gateway: Any, task_id: str, status: TaskStatus) -> EventEnvelope:
    """Build the canonical task-completion envelope for a polled terminal task.

    The envelope mirrors a provider-pushed completion: the task id is carried
    under ``data.taskId`` (the correlate carrier waiters are opened on), the
    terminal status under ``data.status``, and the full tolerant-parser ``raw``
    body (if any) is preserved so unknown/renamed provisional fields survive. The
    envelope ``id`` is synthesized fresh; ingress dedup keys on ``(source, id)``,
    so a freshly-minted id ensures the synthesized completion is not mistaken for
    a duplicate of an unrelated event.

    Args:
        gateway: The connection the task lives behind (supplies the source).
        task_id: The upstream task id (the bound ``correlation_value``).
        status: The terminal :class:`~mcpgateway.services.events.tasks.TaskStatus`.

    Returns:
        EventEnvelope: The synthesized completion envelope.
    """
    gateway_id = getattr(gateway, "id", None)
    source = f"//{gateway_id}" if gateway_id is not None else "//unknown"

    data: dict = {"taskId": str(task_id), "status": status.state}
    if isinstance(status.raw, dict):
        # Preserve provider/raw fields without clobbering the canonical carriers.
        for key, value in status.raw.items():
            data.setdefault(key, value)

    return EventEnvelope(
        id="evt-task-" + uuid.uuid4().hex,
        source=source,
        type=TASK_COMPLETED_TYPE,
        subject=str(task_id),
        time=datetime.now(timezone.utc),
        data=data,
    )


async def poll_and_deliver(
    *,
    sub: Any,
    gateway: Any,
    send_task_get: Callable[[str], Awaitable[dict]],
    session_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = 1.0,
    jitter: bool = True,
    max_wait: Optional[float] = None,
    poller: Optional[TaskPoller] = None,
) -> bool:
    """Poll a registered task webhook to terminal, then publish its completion.

    Drives the upstream task referenced by the correlate waiter *sub* to a
    terminal status and injects a canonical ``com.mcp.task.completed`` event onto
    the L2 stream so the delivery worker's correlate arm resumes the waiter's
    ``callback_url`` (FRD §8.9). Single-flight per task id (TC-COR-028) and
    idempotent (TC-COR-010): re-running after the waiter has been consumed simply
    re-publishes a completion that resolves to nothing.

    Args:
        sub: The ephemeral correlate :class:`~mcpgateway.db.EventSubscription`
            opened by
            :func:`~mcpgateway.services.events.correlate.register_task_webhook`;
            its ``correlation_value`` is the task id to poll.
        gateway: The connection the task lives behind (supplies the source for the
            synthesized completion envelope).
        send_task_get: Injectable coroutine ``(task_id) -> dict`` returning the raw
            task status carrier (the real impl issues a raw ``tasks/get`` over the
            persistent session; tests inject a fake).
        session_factory: Zero-arg callable returning a SQLAlchemy session for the
            publish step. Defaults to :data:`mcpgateway.db.SessionLocal`.
        poll_interval: Base inter-poll delay (seconds) for a fresh poller.
        jitter: Whether the fresh poller applies sleep jitter.
        max_wait: Optional poller watchdog ceiling (seconds); on elapse the poller
            returns a synthetic ``timed-out`` terminal and a timed-out completion
            is still published so the waiter is not orphaned (TC-COR-007).
        poller: Optional pre-built :class:`TaskPoller` (e.g. a process-shared one
            for cross-call single-flight). When ``None`` a fresh poller is built
            from ``send_task_get`` + the pacing args.

    Returns:
        bool: ``True`` if a completion was published (a fresh event), ``False`` if
        it was deduped as a duplicate by the ingress tail.
    """
    task_id = getattr(sub, "correlation_value", None)
    if task_id is None:
        logger.warning("poll_and_deliver: correlate sub %s has no correlation_value; nothing to poll", getattr(sub, "id", None))
        return False

    if poller is None:
        poller = TaskPoller(send_task_get=send_task_get, poll_interval=poll_interval, jitter=jitter, max_wait=max_wait)

    status = await poller.poll_until_terminal(str(task_id))
    envelope = build_completion_envelope(gateway=gateway, task_id=str(task_id), status=status)

    db = _session(session_factory)
    try:
        published, _event_log_id = await emit.publish_normalized_event(db, gateway=gateway, envelope=envelope)
        return published
    finally:
        db.close()


def _session(session_factory: Optional[Callable[[], Any]]) -> Any:
    """Open a session for the publish step.

    Args:
        session_factory: Optional zero-arg session factory; defaults to
            :data:`mcpgateway.db.SessionLocal`.

    Returns:
        Any: A new SQLAlchemy session.
    """
    if session_factory is not None:
        return session_factory()
    # First-Party
    from mcpgateway.db import SessionLocal  # pylint: disable=import-outside-toplevel

    return SessionLocal()


async def process_tool_call_webhooks(
    db: Any,
    *,
    response: Any,
    webhooks: Optional[Sequence[dict]],
    gateway: Any,
    team_id: Optional[str] = None,
    send_task_get: Optional[Callable[[str], Awaitable[dict]]] = None,
    session_factory: Optional[Callable[[], Any]] = None,
    poll_interval: float = 1.0,
    jitter: bool = True,
    max_wait: Optional[float] = None,
    kick_poller: bool = True,
) -> List[Any]:
    """#523 hook: register per-call ``webhooks[]`` against a task-handle response.

    The active entry point for the FRD §2.4 (#523) per-``tools/call``
    ``webhooks[]`` proposal: when an async ``tools/call`` returned a *task handle*
    instead of a final result (:func:`~mcpgateway.services.events.tasks.is_task_result`)
    and the call carried a non-empty ``webhooks[]`` list, this opens one ephemeral
    ``mode="correlate"`` waiter per webhook entry — keyed on the task id, with the
    resume target/callback drawn from the webhook spec (the TC-COR-025
    async-switch) — and kicks off the poller->deliver flow
    (:func:`poll_and_deliver`) so the upstream task's completion resumes each
    registered webhook (FRD §8.9).

    The hook is **additive + flag-gated**: it is a no-op (returns ``[]``,
    registers nothing, kicks nothing) when:

    * :data:`settings.mcpgateway_events_enabled` is off (default-off safety),
    * the ``response`` is **not** a task handle (an ordinary final tool result),
      or
    * no ``webhooks[]`` were supplied.

    This keeps the synchronous ``tools/call`` path unchanged unless events are
    explicitly enabled *and* a caller opted in with a task-returning call + a
    ``webhooks[]`` list. Tolerant throughout (accepts ``taskId``/``id`` and the
    ``url``/``callback_url`` spec aliases; ignores extra fields).

    Args:
        db: An active synchronous SQLAlchemy session (used to open waiters).
        response: The raw ``tools/call`` result (dict or attribute-bearing
            object). Probed for a provisional task handle.
        webhooks: The per-call ``webhooks[]`` list (each ``{"url": ...,
            "auth": {...}, "ttl_seconds": ...}``); ``None``/empty registers
            nothing.
        gateway: The connection the tool lives behind (supplies ``id`` and a
            fallback tenant).
        team_id: The owning tenant; falls back to ``gateway.team_id``.
        send_task_get: Injectable ``(task_id) -> dict`` coroutine the poller
            uses to drive the task to terminal (the real impl issues a raw
            ``tasks/get`` over the persistent session; tests inject a fake).
            Required when ``kick_poller`` is ``True``.
        session_factory: Optional session factory for the poller's publish step.
        poll_interval: Base inter-poll delay (seconds) for the kicked poller.
        jitter: Whether the kicked poller applies sleep jitter.
        max_wait: Optional poller watchdog ceiling (seconds).
        kick_poller: When ``True`` (default) spawn a background
            :func:`poll_and_deliver` task per opened waiter; set ``False`` to
            only register the waiters (e.g. when an external scheduler polls).

    Returns:
        List[Any]: The opened correlate waiter rows (empty when nothing was
        registered).
    """
    if not getattr(settings, "mcpgateway_events_enabled", False):
        return []

    if not webhooks:
        return []

    handle = tasks_mod.parse_task_handle(response)
    if handle is None:
        # Not a task handle -> a final result; nothing async to correlate.
        return []

    resolved_team = team_id if team_id is not None else getattr(gateway, "team_id", None)
    task_id = str(handle.task_id)

    opened: List[Any] = []
    for webhook in webhooks:
        if not isinstance(webhook, dict):
            logger.warning("process_tool_call_webhooks: ignoring non-dict webhook entry %r", webhook)
            continue
        try:
            sub = await correlate_mod.register_task_webhook(db, gateway=gateway, team_id=resolved_team, task_id=task_id, webhook=webhook)
        except correlate_mod.CorrelationCollisionError:
            # A waiter already binds this task id in the tenant: fail-closed,
            # do not open a second. Reuse the existing waiter for the kick so
            # the poller still drives the in-flight task to terminal.
            logger.info("process_tool_call_webhooks: task %s already has a waiter in tenant %s; skipping duplicate open", task_id, resolved_team)
            continue
        opened.append(sub)

    if kick_poller and opened and send_task_get is not None:
        for sub in opened:
            _kick_poll_and_deliver(
                sub=sub,
                gateway=gateway,
                send_task_get=send_task_get,
                session_factory=session_factory,
                poll_interval=poll_interval,
                jitter=jitter,
                max_wait=max_wait,
            )

    return opened


def _kick_poll_and_deliver(
    *,
    sub: Any,
    gateway: Any,
    send_task_get: Callable[[str], Awaitable[dict]],
    session_factory: Optional[Callable[[], Any]],
    poll_interval: float,
    jitter: bool,
    max_wait: Optional[float],
) -> None:
    """Spawn a fire-and-forget background poll->deliver task for one waiter.

    The poll loop runs detached so the synchronous ``tools/call`` caller is not
    blocked waiting for the async task to terminate. Failures are logged, never
    propagated, so a poll error never escapes into the request path.

    Args:
        sub: The ephemeral correlate waiter to drive to terminal.
        gateway: The connection the task lives behind (envelope source).
        send_task_get: Injectable ``(task_id) -> dict`` coroutine.
        session_factory: Optional session factory for the publish step.
        poll_interval: Base inter-poll delay in seconds.
        jitter: Whether to apply sleep jitter.
        max_wait: Optional watchdog ceiling in seconds.
    """

    async def _runner() -> None:
        try:
            await poll_and_deliver(
                sub=sub,
                gateway=gateway,
                send_task_get=send_task_get,
                session_factory=session_factory,
                poll_interval=poll_interval,
                jitter=jitter,
                max_wait=max_wait,
            )
        except Exception:  # noqa: BLE001 - background poll must never escape into the request path.
            logger.exception("Background poll_and_deliver failed for waiter %s", getattr(sub, "id", None))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_runner())
    else:  # pragma: no cover - defensive: no running loop (synchronous caller)
        asyncio.ensure_future(_runner())
