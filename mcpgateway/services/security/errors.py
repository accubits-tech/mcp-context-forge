# -*- coding: utf-8 -*-
"""Errors raised by the vulnerability-scan gate."""

# Standard
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcpgateway.services.security.report import ScanReport


@dataclass
class SecurityGateError(Exception):
    """Raised when a scan run produces a blocking finding.

    The ``stage`` indicates whether the gate tripped pre-build (source-side
    scanners) or post-build (image-side scanners). The ``report`` carries the
    full set of findings, with ``gate_outcome == "block"`` and
    ``blocking_findings_count > 0``.
    """

    stage: str  # "pre_build" | "post_build"
    report: "ScanReport"
    message: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        msg = self.message or f"security gate blocked deploy at {self.stage}"
        if self.report is not None:
            return f"{msg}: {self.report.summary.blocking_findings_count} blocking finding(s)"
        return msg


class ScannerExecutionError(Exception):
    """Raised when a scanner container cannot be executed (image missing, daemon down)."""


class ScannerTimeoutError(ScannerExecutionError):
    """Raised when a scanner container exceeds its per-stage timeout."""
