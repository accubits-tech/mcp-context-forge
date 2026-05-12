# -*- coding: utf-8 -*-
"""Internal models for security-scan findings, stages, and reports.

These mirror the public Pydantic schemas in ``mcpgateway.schemas`` but use
plain dataclasses internally so the runner can build them up incrementally
without paying for Pydantic validation on every append.
"""

# Standard
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass
class Finding:
    """One vulnerability-scan finding."""

    id: str
    scanner: str
    stage: str
    severity: str  # critical | high | medium | low | info
    rule_id: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    cwe: Optional[str] = None
    raw_excerpt: Optional[str] = None
    # When True, the SCA scanner detected this as a direct dependency (not transitive).
    # Used by policy.py for the "HIGH-direct" vs "HIGH-transitive" distinction.
    direct_dependency: Optional[bool] = None
    # When True, gitleaks reported this as verified (live secret); only entropy hits
    # without active verification get downgraded to warn.
    verified_secret: Optional[bool] = None


@dataclass
class StageState:
    """Live state for one pipeline stage.

    The ProgressTracker mutates this in place as scanners progress; the API
    snapshots it for /security-scan/status responses.
    """

    name: str  # secrets | sast | sca | ...
    label: str
    scanner: Optional[str]
    status: str = "pending"  # pending | running | passed | warned | failed | skipped | error
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    findings_count_by_severity: Dict[str, int] = field(default_factory=dict)


@dataclass
class ScanSummary:
    """Compact summary persisted on the gateway row."""

    scan_run_id: str
    overall_status: str = "pending"
    gate_outcome: str = "pass"  # pass | warn | block | error
    blocking_findings_count: int = 0
    counts_by_severity: Dict[str, int] = field(default_factory=dict)
    stages: List[StageState] = field(default_factory=list)


@dataclass
class ScanReport:
    """Full per-run report including all findings."""

    summary: ScanSummary
    findings: List[Finding] = field(default_factory=list)
    source_sha256: Optional[str] = None
    image_tag: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def add_findings(self, stage_name: str, new: List[Finding]) -> None:
        """Append findings, update stage counters, update summary counters."""
        self.findings.extend(new)
        stage = next((s for s in self.summary.stages if s.name == stage_name), None)
        for f in new:
            if stage is not None:
                stage.findings_count_by_severity[f.severity] = stage.findings_count_by_severity.get(f.severity, 0) + 1
            self.summary.counts_by_severity[f.severity] = self.summary.counts_by_severity.get(f.severity, 0) + 1
