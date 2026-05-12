# -*- coding: utf-8 -*-
"""In-memory progress tracker for live scan-status polling.

Mirrors the in-process pattern used by the OpenAPI-tool-generation jobs in
``mcp-foundry-fe/src/components/admin/OpenApiGenerator.jsx`` (2-second poll).
Single-process only; if you want HA you'd swap this for Redis without changing
the call sites.
"""

# Standard
import asyncio
from typing import Dict, List, Optional

# First-Party
from mcpgateway.services.security.report import ScanSummary, StageState, utcnow


class ProgressTracker:
    """Per-gateway live state. Thread/async safe via asyncio.Lock."""

    def __init__(self) -> None:
        self._states: Dict[str, ScanSummary] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, gateway_id: str) -> asyncio.Lock:
        lock = self._locks.get(gateway_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[gateway_id] = lock
        return lock

    async def init_run(self, gateway_id: str, scan_run_id: str, stages: List[StageState]) -> None:
        async with self._lock(gateway_id):
            self._states[gateway_id] = ScanSummary(
                scan_run_id=scan_run_id,
                overall_status="running",
                gate_outcome="pass",
                stages=stages,
            )

    async def start_stage(self, gateway_id: str, stage_name: str) -> None:
        async with self._lock(gateway_id):
            summary = self._states.get(gateway_id)
            if summary is None:
                return
            for s in summary.stages:
                if s.name == stage_name:
                    s.status = "running"
                    s.started_at = utcnow()
                    return

    async def finish_stage(
        self,
        gateway_id: str,
        stage_name: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock(gateway_id):
            summary = self._states.get(gateway_id)
            if summary is None:
                return
            for s in summary.stages:
                if s.name == stage_name:
                    s.status = status
                    s.finished_at = utcnow()
                    if error:
                        s.error = error
                    return

    async def update_stage_counts(self, gateway_id: str, stage_name: str, counts_by_severity: Dict[str, int]) -> None:
        async with self._lock(gateway_id):
            summary = self._states.get(gateway_id)
            if summary is None:
                return
            for s in summary.stages:
                if s.name == stage_name:
                    s.findings_count_by_severity = dict(counts_by_severity)
                    return

    async def finalize(self, gateway_id: str, overall_status: str, gate_outcome: str, blocking_count: int) -> None:
        async with self._lock(gateway_id):
            summary = self._states.get(gateway_id)
            if summary is None:
                return
            summary.overall_status = overall_status
            summary.gate_outcome = gate_outcome
            summary.blocking_findings_count = blocking_count

    def snapshot(self, gateway_id: str) -> Optional[ScanSummary]:
        """Non-locking peek; OK for status responses (eventual consistency is fine here)."""
        return self._states.get(gateway_id)

    def clear(self, gateway_id: str) -> None:
        """Drop the in-memory record (called once persisted to DB)."""
        self._states.pop(gateway_id, None)
        self._locks.pop(gateway_id, None)


# Singleton used across the runner + admin endpoints.
TRACKER = ProgressTracker()
