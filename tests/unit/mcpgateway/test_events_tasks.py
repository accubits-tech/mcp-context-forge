# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_tasks.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the tolerant Tasks parser + poller (M7 correlate support).

The MCP SDK (1.21.0) ships **no** Task types: ``mcp.types`` has zero ``Task``
names and ``ClientSession`` has no ``tasks/*`` method. Tasks is a *provisional*
extension (FRD §2.4 — "Tasks ... RC"), so every helper here is a **tolerant**
parser over raw dicts: it accepts ``taskId`` *or* ``id``, ``status`` *or*
``state``, treats the terminal set ``{completed, failed, cancelled, canceled}``
as done (``input_required`` is **non**-terminal), and never raises ``KeyError``
on renamed / extra / missing fields. The tests assert *behavior*, not exact
wire field names.

Coverage maps to the M7 gating COR rows:

* TC-COR-030 — parse variants (``taskId`` vs ``id``; ``status`` aliases; extra
  fields preserved; missing fields -> ``unknown`` / non-terminal).
* TC-COR-025 — :func:`is_task_result` detects a ``CreateTaskResult``-shaped
  response regardless of which call produced it.
* TC-COR-001 / TC-COR-003 — terminal detection (``completed`` / ``failed``).
* TC-COR-007 — no terminal status ever arrives -> the ``max_wait`` watchdog
  fires a *synthetic* ``timed-out`` terminal (the poller never hangs).
* TC-COR-022 — ``poll_interval`` is honored between polls.
* TC-COR-023 — transient ``tasks/get`` errors are retried with backoff while the
  run stays pending.
* TC-COR-028 — single-flight: concurrent ``poll_until_terminal`` calls for the
  same ``task_id`` coalesce into **one** ``send_task_get`` invocation and **one**
  terminal result.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import time

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events import tasks as tasks_mod
from mcpgateway.services.events.tasks import (
    is_task_result,
    parse_task_handle,
    parse_task_status,
    TaskHandle,
    TaskPoller,
    TaskStatus,
)


# --------------------------------------------------------------------------- #
# TC-COR-030 — tolerant parse_task_handle (taskId vs id; extra fields).        #
# --------------------------------------------------------------------------- #
def test_parse_task_handle_accepts_taskid():
    """A response carrying ``taskId`` yields a handle (TC-COR-030)."""
    handle = parse_task_handle({"taskId": "t-1", "status": "working"})
    assert isinstance(handle, TaskHandle)
    assert handle.task_id == "t-1"
    assert handle.raw == {"taskId": "t-1", "status": "working"}


def test_parse_task_handle_accepts_id_alias():
    """A response carrying ``id`` (no ``taskId``) yields a handle (TC-COR-030)."""
    handle = parse_task_handle({"id": "t-2", "state": "submitted"})
    assert handle is not None
    assert handle.task_id == "t-2"


def test_parse_task_handle_prefers_taskid_over_id():
    """When both keys are present ``taskId`` wins (canonical name, TC-COR-030)."""
    handle = parse_task_handle({"taskId": "canonical", "id": "other"})
    assert handle is not None
    assert handle.task_id == "canonical"


def test_parse_task_handle_keeps_extra_fields():
    """Unknown / renamed fields are preserved verbatim on ``raw`` (TC-COR-030)."""
    payload = {"taskId": "t-3", "weirdField": 7, "nested": {"a": 1}}
    handle = parse_task_handle(payload)
    assert handle is not None
    assert handle.raw["weirdField"] == 7
    assert handle.raw["nested"] == {"a": 1}


def test_parse_task_handle_none_when_no_identifier():
    """No ``taskId``/``id`` -> not a task handle, returns ``None`` (TC-COR-030)."""
    assert parse_task_handle({"status": "completed"}) is None
    assert parse_task_handle({}) is None
    assert parse_task_handle(None) is None
    assert parse_task_handle("not-a-dict") is None


def test_parse_task_handle_reads_attribute_object():
    """A non-dict object exposing ``taskId`` attribute is tolerated (TC-COR-030)."""

    class _Obj:
        taskId = "obj-task"  # noqa: N815 - mirrors provisional wire field.

    handle = parse_task_handle(_Obj())
    assert handle is not None
    assert handle.task_id == "obj-task"


# --------------------------------------------------------------------------- #
# TC-COR-030 — tolerant parse_task_status (status|state; terminal set).        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state_value", ["completed", "failed", "cancelled", "canceled"])
def test_parse_task_status_terminal_states(state_value):
    """The terminal set is recognized via ``status`` (TC-COR-001/003/030)."""
    status = parse_task_status({"taskId": "t", "status": state_value})
    assert isinstance(status, TaskStatus)
    assert status.state == state_value
    assert status.terminal is True


def test_parse_task_status_state_alias():
    """``state`` is honored when ``status`` is absent (TC-COR-030)."""
    status = parse_task_status({"id": "t", "state": "completed"})
    assert status.state == "completed"
    assert status.terminal is True


def test_parse_task_status_status_wins_over_state():
    """``status`` takes precedence over ``state`` when both present (TC-COR-030)."""
    status = parse_task_status({"status": "completed", "state": "working"})
    assert status.state == "completed"
    assert status.terminal is True


@pytest.mark.parametrize("state_value", ["working", "submitted", "input_required", "pending"])
def test_parse_task_status_non_terminal_states(state_value):
    """``input_required`` and other in-flight states are non-terminal (TC-COR-030)."""
    status = parse_task_status({"taskId": "t", "status": state_value})
    assert status.state == state_value
    assert status.terminal is False


def test_parse_task_status_missing_state_is_unknown_non_terminal():
    """A missing status never KeyErrors: -> ``unknown`` / non-terminal (TC-COR-030)."""
    status = parse_task_status({"taskId": "t", "extra": 1})
    assert status.state == "unknown"
    assert status.terminal is False
    assert status.raw == {"taskId": "t", "extra": 1}


def test_parse_task_status_terminal_case_insensitive():
    """Terminal detection ignores case (provisional enums vary, TC-COR-030)."""
    assert parse_task_status({"status": "COMPLETED"}).terminal is True
    assert parse_task_status({"status": "Failed"}).terminal is True


def test_parse_task_status_tolerates_non_dict():
    """A non-dict input degrades to ``unknown`` rather than raising (TC-COR-030)."""
    status = parse_task_status(None)
    assert status.state == "unknown"
    assert status.terminal is False


# --------------------------------------------------------------------------- #
# TC-COR-025 — is_task_result detection.                                       #
# --------------------------------------------------------------------------- #
def test_is_task_result_detects_taskid():
    """A dict carrying ``taskId`` is a task-result shape (TC-COR-025)."""
    assert is_task_result({"taskId": "t-1"}) is True


def test_is_task_result_detects_id_with_status():
    """A dict carrying ``id`` + a status field is a task-result shape (TC-COR-025)."""
    assert is_task_result({"id": "t-1", "status": "working"}) is True


def test_is_task_result_rejects_plain_tool_output():
    """An ordinary tool result (no task identifier) is not a task result (TC-COR-025)."""
    assert is_task_result({"content": [{"type": "text", "text": "hi"}]}) is False
    assert is_task_result({}) is False
    assert is_task_result(None) is False


def test_is_task_result_attribute_object():
    """A non-dict object exposing a task id is detected (TC-COR-025)."""

    class _Obj:
        id = "t-9"
        status = "working"

    assert is_task_result(_Obj()) is True


# --------------------------------------------------------------------------- #
# TaskPoller — TC-COR-001/003 terminal detection.                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poller_returns_terminal_completed():
    """The poller resolves on the first terminal ``completed`` (TC-COR-001)."""
    calls = []

    async def send_task_get(task_id):
        calls.append(task_id)
        return {"taskId": task_id, "status": "completed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)
    status = await poller.poll_until_terminal("t-1")
    assert status.terminal is True
    assert status.state == "completed"
    assert calls == ["t-1"]


@pytest.mark.asyncio
async def test_poller_returns_terminal_failed():
    """The poller resolves on a terminal ``failed`` (TC-COR-003)."""

    async def send_task_get(task_id):
        return {"id": task_id, "state": "failed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)
    status = await poller.poll_until_terminal("t-2")
    assert status.terminal is True
    assert status.state == "failed"


@pytest.mark.asyncio
async def test_poller_polls_until_terminal():
    """Non-terminal statuses are re-polled until a terminal one arrives (TC-COR-001)."""
    sequence = iter(
        [
            {"taskId": "t", "status": "submitted"},
            {"taskId": "t", "status": "working"},
            {"taskId": "t", "status": "completed"},
        ]
    )

    async def send_task_get(task_id):
        return next(sequence)

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)
    status = await poller.poll_until_terminal("t")
    assert status.terminal is True
    assert status.state == "completed"


# --------------------------------------------------------------------------- #
# TC-COR-007 — watchdog -> synthetic timed-out terminal (never hang).         #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poller_watchdog_synthesizes_timeout():
    """A task that never terminalizes hits ``max_wait`` -> synthetic terminal (TC-COR-007)."""

    async def send_task_get(task_id):
        return {"taskId": task_id, "status": "working"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False, max_wait=0.05)
    status = await asyncio.wait_for(poller.poll_until_terminal("t-stuck"), timeout=2.0)
    assert status.terminal is True
    assert status.state == "timed-out"


# --------------------------------------------------------------------------- #
# TC-COR-022 — poll_interval honored between polls.                           #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poller_honors_poll_interval():
    """The configured ``poll_interval`` gates successive polls (TC-COR-022)."""
    stamps = []

    async def send_task_get(task_id):
        stamps.append(time.monotonic())
        if len(stamps) < 3:
            return {"taskId": task_id, "status": "working"}
        return {"taskId": task_id, "status": "completed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.05, jitter=False)
    await poller.poll_until_terminal("t")
    assert len(stamps) == 3
    # Two inter-poll gaps, each >= poll_interval (allow scheduler slack).
    assert stamps[1] - stamps[0] >= 0.04
    assert stamps[2] - stamps[1] >= 0.04


# --------------------------------------------------------------------------- #
# TC-COR-023 — transient errors retried with backoff; run stays pending.      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poller_retries_transient_errors():
    """Transient ``send_task_get`` errors are retried; the run stays pending (TC-COR-023)."""
    attempts = {"n": 0}

    async def send_task_get(task_id):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient network blip")
        return {"taskId": task_id, "status": "completed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)
    status = await poller.poll_until_terminal("t")
    assert attempts["n"] == 3
    assert status.terminal is True
    assert status.state == "completed"


@pytest.mark.asyncio
async def test_poller_transient_errors_bounded_by_watchdog():
    """Endless transient errors still terminate via the watchdog (TC-COR-007/023)."""

    async def send_task_get(task_id):
        raise RuntimeError("always failing")

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False, max_wait=0.05)
    status = await asyncio.wait_for(poller.poll_until_terminal("t"), timeout=2.0)
    assert status.terminal is True
    assert status.state == "timed-out"


# --------------------------------------------------------------------------- #
# TC-COR-028 — single-flight: concurrent polls coalesce.                      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_poller_single_flight_coalesces_concurrent():
    """Concurrent polls for the same task -> ONE send_task_get + one terminal (TC-COR-028)."""
    calls = {"n": 0}
    gate = asyncio.Event()

    async def send_task_get(task_id):
        calls["n"] += 1
        # Hold so concurrent callers pile up on the same in-flight poll.
        await gate.wait()
        return {"taskId": task_id, "status": "completed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)

    task_a = asyncio.create_task(poller.poll_until_terminal("same"))
    task_b = asyncio.create_task(poller.poll_until_terminal("same"))
    task_c = asyncio.create_task(poller.poll_until_terminal("same"))
    await asyncio.sleep(0.02)  # let all three coalesce on the in-flight poll
    gate.set()
    results = await asyncio.gather(task_a, task_b, task_c)

    assert calls["n"] == 1
    for status in results:
        assert status.terminal is True
        assert status.state == "completed"


@pytest.mark.asyncio
async def test_poller_distinct_task_ids_not_coalesced():
    """Different task_ids each get their own poll (single-flight is per-id, TC-COR-028)."""
    seen = []

    async def send_task_get(task_id):
        seen.append(task_id)
        return {"taskId": task_id, "status": "completed"}

    poller = TaskPoller(send_task_get=send_task_get, poll_interval=0.001, jitter=False)
    a, b = await asyncio.gather(
        poller.poll_until_terminal("alpha"),
        poller.poll_until_terminal("beta"),
    )
    assert a.terminal is True and b.terminal is True
    assert set(seen) == {"alpha", "beta"}


# --------------------------------------------------------------------------- #
# Smoke: module exposes the documented public surface.                        #
# --------------------------------------------------------------------------- #
def test_module_public_surface():
    """The contract symbols are exported for downstream M7 modules."""
    for name in ("TaskHandle", "TaskStatus", "is_task_result", "parse_task_handle", "parse_task_status", "TaskPoller"):
        assert hasattr(tasks_mod, name)
