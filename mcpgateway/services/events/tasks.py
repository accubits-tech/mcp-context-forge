# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/tasks.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tolerant MCP Tasks parser + poller (correlate-mode support, FRD §8.9 / §2.4).

The async-tool / correlate flow (FRD §7.3 / §8.9) needs to (a) recognize when a
``tools/call`` returned a *task handle* instead of a final result, (b) read the
task's status, and (c) poll the upstream task to a **terminal** state so a
completion event can be synthesized and resumed against the waiting correlate
subscription. The problem: **the installed MCP SDK (1.21.0) has no Task types**
— ``mcp.types`` exposes zero ``Task`` names and ``ClientSession`` has no
``tasks/*`` method. Tasks is a *provisional* extension still at RC (FRD §2.4
table row "Tasks ... RC"); the method spelling (``tasks/get``) and the status
enum are **not** frozen.

This module therefore deliberately avoids any SDK Task type and operates over
**raw dicts** with a *tolerant* parser:

* identifier — accept ``taskId`` **or** ``id`` (canonical ``taskId`` wins);
* status — accept ``status`` **or** ``state`` (``status`` wins);
* terminal set — ``{completed, failed, cancelled, canceled}`` (case-insensitive);
  ``input_required`` is explicitly **non**-terminal (the task is paused awaiting
  input, not done);
* unknown / renamed / missing fields — preserved on ``raw`` and never raise
  ``KeyError`` (a missing status degrades to ``unknown`` / non-terminal).

Downstream correlate logic asserts **behavior** (terminal-ness, the task id),
never exact wire field names, so a future SDK rename does not break it.

:class:`TaskPoller` drives a task to terminal over an **injectable**
``send_task_get(task_id) -> dict`` callable (the real implementation issues a raw
``tasks/get`` request over the M6 persistent ``ClientSession`` via
``send_request``; tests inject a mock). It is:

* **single-flight per ``task_id``** — concurrent ``poll_until_terminal`` calls for
  the same task coalesce onto one in-flight poll loop and one terminal result
  (TC-COR-028), so a restart that re-polls known task ids and a duplicate kick do
  not double-poll;
* **paced** — successive polls are gated by ``poll_interval`` (+ optional jitter)
  (TC-COR-022);
* **resilient** — a transient ``send_task_get`` error is retried with capped
  exponential backoff while the run stays pending (TC-COR-023);
* **bounded** — an optional ``max_wait`` watchdog guarantees the poll loop never
  hangs: on elapse it returns a *synthetic* ``timed-out`` terminal status
  (TC-COR-007).
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from dataclasses import dataclass, field
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

# First-Party
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

__all__ = [
    "TaskHandle",
    "TaskStatus",
    "is_task_result",
    "parse_task_handle",
    "parse_task_status",
    "TaskPoller",
]

# Provisional Tasks status enum (FRD §2.4 — RC, NOT frozen). Compared
# case-insensitively. ``input_required`` is intentionally absent: a task awaiting
# input is paused, not done, so the poller keeps waiting.
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "canceled"})

#: Synthetic terminal state the watchdog emits when ``max_wait`` elapses without
#: the upstream reporting a real terminal status (TC-COR-007).
TIMED_OUT_STATE = "timed-out"

#: Status reported when the carrier has no recognizable status field at all.
UNKNOWN_STATE = "unknown"

# Identifier / status carrier keys, in precedence order (canonical name first).
_ID_KEYS = ("taskId", "id")
_STATUS_KEYS = ("status", "state")


@dataclass
class TaskHandle:
    """A reference to a provisional MCP task extracted from a tool result.

    Attributes:
        task_id: The task identifier (read from ``taskId`` or, failing that,
            ``id``).
        raw: The full carrier dict/object as received, preserved so unknown or
            renamed provisional fields survive for downstream inspection.

    Examples:
        >>> h = TaskHandle(task_id="t-1", raw={"taskId": "t-1"})
        >>> h.task_id
        't-1'
    """

    task_id: str
    raw: Any = field(default=None)


@dataclass
class TaskStatus:
    """A tolerant view of a provisional MCP task status.

    Attributes:
        state: The normalized status string (from ``status`` or ``state``),
            ``"unknown"`` when no status field is present.
        terminal: ``True`` when ``state`` is in the terminal set (or is the
            synthetic ``timed-out``); ``input_required`` is **non**-terminal.
        raw: The full carrier dict/object as received.

    Examples:
        >>> TaskStatus(state="completed", terminal=True, raw={}).terminal
        True
        >>> TaskStatus(state="input_required", terminal=False, raw={}).terminal
        False
    """

    state: str
    terminal: bool
    raw: Any = field(default=None)


def _get_field(carrier: Any, keys: tuple) -> Optional[Any]:
    """Read the first present *key* from a dict or attribute-bearing object.

    Tolerant of both the raw-dict wire shape and an object exposing the
    provisional fields as attributes; never raises on a missing key.

    Args:
        carrier: A ``dict`` or arbitrary object carrying the provisional fields.
        keys: Candidate field names in precedence order.

    Returns:
        The first non-``None`` value found, else ``None``.
    """
    if carrier is None:
        return None
    if isinstance(carrier, dict):
        for key in keys:
            value = carrier.get(key)
            if value is not None:
                return value
        return None
    for key in keys:
        value = getattr(carrier, key, None)
        if value is not None:
            return value
    return None


def _is_terminal(state: Optional[str]) -> bool:
    """Return whether *state* is a terminal task state (case-insensitive).

    Args:
        state: A normalized status string or ``None``.

    Returns:
        bool: ``True`` for the terminal set or the synthetic ``timed-out``.
    """
    if not isinstance(state, str):
        return False
    lowered = state.lower()
    return lowered in TERMINAL_STATES or lowered == TIMED_OUT_STATE


def parse_task_handle(result: Any) -> Optional[TaskHandle]:
    """Tolerantly extract a :class:`TaskHandle` from a tool result.

    Accepts the canonical ``taskId`` **or** the ``id`` alias and preserves the
    full carrier on ``raw``. Extra / renamed fields are ignored for parsing but
    kept on ``raw``. Returns ``None`` when no task identifier is present (the
    result is an ordinary, non-task tool output) or the input is not a
    dict/object.

    Args:
        result: A raw ``tools/call`` result dict, an attribute-bearing object, or
            anything else.

    Returns:
        Optional[TaskHandle]: The handle, or ``None`` when not task-shaped.

    Examples:
        >>> parse_task_handle({"taskId": "t-1"}).task_id
        't-1'
        >>> parse_task_handle({"id": "t-2"}).task_id
        't-2'
        >>> parse_task_handle({"status": "completed"}) is None
        True
    """
    if result is None or isinstance(result, (str, bytes, int, float, bool, list)):
        return None
    task_id = _get_field(result, _ID_KEYS)
    if task_id is None:
        return None
    return TaskHandle(task_id=str(task_id), raw=result)


def parse_task_status(result: Any) -> TaskStatus:
    """Tolerantly parse a task status carrier into a :class:`TaskStatus`.

    Accepts ``status`` **or** ``state`` (``status`` wins). Never raises on a
    missing or non-string status: it degrades to the ``unknown`` /
    non-terminal :class:`TaskStatus`. Terminal detection is case-insensitive
    over the provisional terminal set; ``input_required`` is non-terminal.

    Args:
        result: A raw status dict, an attribute-bearing object, or anything else.

    Returns:
        TaskStatus: The normalized status (never ``None``).

    Examples:
        >>> parse_task_status({"status": "completed"}).terminal
        True
        >>> parse_task_status({"state": "working"}).terminal
        False
        >>> parse_task_status({"taskId": "t"}).state
        'unknown'
    """
    raw_state = _get_field(result, _STATUS_KEYS)
    if isinstance(raw_state, str):
        state = raw_state
    elif raw_state is None:
        state = UNKNOWN_STATE
    else:
        # Non-string status (e.g. an enum-ish object); coerce defensively.
        state = str(raw_state)
    return TaskStatus(state=state, terminal=_is_terminal(state), raw=result)


def is_task_result(result: Any) -> bool:
    """Return whether *result* looks like a provisional ``CreateTaskResult``.

    A result is task-shaped when it carries a task identifier (``taskId`` or
    ``id``). This lets any ``tools/call`` response be probed for an async task
    handle regardless of which call produced it (TC-COR-025), without depending
    on absent SDK Task types.

    Args:
        result: A raw result dict, an attribute-bearing object, or anything else.

    Returns:
        bool: ``True`` when a task identifier is present.

    Examples:
        >>> is_task_result({"taskId": "t-1"})
        True
        >>> is_task_result({"id": "t-1", "status": "working"})
        True
        >>> is_task_result({"content": []})
        False
    """
    return parse_task_handle(result) is not None


class TaskPoller:
    """Poll a provisional MCP task to a terminal status (single-flight).

    The poller is transport-agnostic: it drives an **injectable**
    ``send_task_get(task_id) -> dict`` coroutine (the real implementation issues a
    raw ``tasks/get`` request over the persistent session; tests inject a fake).
    A single :class:`TaskPoller` instance coalesces concurrent
    :meth:`poll_until_terminal` calls for the *same* ``task_id`` onto one
    in-flight poll loop (TC-COR-028).

    Args:
        send_task_get: Coroutine ``(task_id) -> dict`` returning the raw task
            status carrier. Transient errors are retried with backoff.
        poll_interval: Base delay (seconds) between successive polls
            (TC-COR-022).
        jitter: When ``True`` apply +/- jitter to each sleep to de-synchronize
            many concurrent pollers.
        max_wait: Optional watchdog (seconds). When the total elapsed poll time
            exceeds it without a real terminal status, the poller returns a
            synthetic ``timed-out`` terminal (TC-COR-007) and never hangs.
    """

    def __init__(
        self,
        *,
        send_task_get: Callable[[str], Awaitable[dict]],
        poll_interval: float = 1.0,
        jitter: bool = True,
        max_wait: Optional[float] = None,
    ) -> None:
        """Initialize the poller.

        Args:
            send_task_get: Injectable coroutine fetching the raw task status.
            poll_interval: Base inter-poll delay in seconds.
            jitter: Whether to apply jitter to sleeps.
            max_wait: Optional watchdog ceiling in seconds.
        """
        self._send_task_get = send_task_get
        self._poll_interval = max(0.0, float(poll_interval))
        self._jitter = bool(jitter)
        self._max_wait = max_wait
        # Single-flight registry: task_id -> the shared in-flight asyncio.Task.
        self._inflight: Dict[str, "asyncio.Task[TaskStatus]"] = {}

    async def poll_until_terminal(self, task_id: str) -> TaskStatus:
        """Poll *task_id* until terminal, coalescing concurrent callers.

        Concurrent calls for the same ``task_id`` share **one** underlying poll
        loop and resolve to the **same** terminal :class:`TaskStatus`
        (single-flight, TC-COR-028). The loop honors ``poll_interval`` + jitter
        (TC-COR-022), retries transient ``send_task_get`` errors with backoff
        (TC-COR-023), and is bounded by the ``max_wait`` watchdog so it never
        hangs (TC-COR-007).

        Args:
            task_id: The provisional task identifier to poll.

        Returns:
            TaskStatus: The terminal status (real or synthetic ``timed-out``).
        """
        existing = self._inflight.get(task_id)
        if existing is not None and not existing.done():
            # Coalesce: ride the in-flight poll loop. Shielded so a cancelled
            # peer caller never tears down the shared poll for the others.
            return await asyncio.shield(existing)

        loop_task: "asyncio.Task[TaskStatus]" = asyncio.ensure_future(self._run_poll_loop(task_id))
        self._inflight[task_id] = loop_task
        try:
            return await asyncio.shield(loop_task)
        finally:
            # Clear only our own entry (a re-kick may have replaced it).
            if self._inflight.get(task_id) is loop_task:
                self._inflight.pop(task_id, None)

    async def _run_poll_loop(self, task_id: str) -> TaskStatus:
        """Run the bounded, paced, retrying poll loop for one task.

        Args:
            task_id: The provisional task identifier to poll.

        Returns:
            TaskStatus: The terminal status (real or synthetic ``timed-out``).
        """
        started = time.monotonic()
        backoff = self._poll_interval if self._poll_interval > 0 else 0.05
        first = True
        while True:
            if self._watchdog_elapsed(started):
                logger.debug("Task %s poll watchdog elapsed; synthesizing timed-out terminal.", task_id)
                return TaskStatus(state=TIMED_OUT_STATE, terminal=True, raw=None)

            if not first:
                await self._sleep(self._poll_interval)
            first = False

            try:
                raw = await self._send_task_get(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transient: retry with backoff, run stays pending.
                logger.debug("Transient tasks/get error for %s (%s); retrying with backoff.", task_id, exc)
                if self._watchdog_elapsed(started):
                    return TaskStatus(state=TIMED_OUT_STATE, terminal=True, raw=None)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                continue

            backoff = self._poll_interval if self._poll_interval > 0 else 0.05
            status = parse_task_status(raw)
            if status.terminal:
                return status

    def _watchdog_elapsed(self, started: float) -> bool:
        """Return whether the ``max_wait`` watchdog ceiling has been crossed.

        Args:
            started: The ``time.monotonic()`` instant the loop began.

        Returns:
            bool: ``True`` when ``max_wait`` is set and elapsed.
        """
        if self._max_wait is None:
            return False
        return (time.monotonic() - started) >= self._max_wait

    async def _sleep(self, base: float) -> None:
        """Sleep ``base`` seconds, optionally jittered, clamped at the watchdog.

        Args:
            base: The base delay in seconds.
        """
        delay = base
        if self._jitter and base > 0:
            delay = base * (0.5 + random.random())  # nosec B311 - jitter only, not security-sensitive.
        if delay > 0:
            await asyncio.sleep(delay)
