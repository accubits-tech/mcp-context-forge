# -*- coding: utf-8 -*-
"""Vulnerability-scan gating policy.

Pure function - no I/O, no globals. Easy to unit-test with table-driven cases.
"""

# Standard
from dataclasses import dataclass
from typing import List

# First-Party
from mcpgateway.services.security.report import Finding


@dataclass
class PolicyConfig:
    """Knobs controlling which findings block vs warn.

    Defaults match the user-approved "balanced" policy in the plan:
    - block on CRITICAL, verified secrets, malicious packages, HIGH-direct deps,
      SAST/MCP-rules ERROR, image-CRITICAL.
    - warn on everything else.

    `block_warnings=True` flips warns into blocks (useful for stricter envs).
    `ignore_rules` is a list of scanner-native rule_ids that never produce a finding.
    """

    block_warnings: bool = False
    ignore_rules: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.ignore_rules is None:
            self.ignore_rules = []


def _is_blocking(finding: Finding) -> bool:
    """Return True if this finding should block deploy under the balanced default."""
    sev = finding.severity.lower()

    # Anything CRITICAL blocks regardless of stage.
    if sev == "critical":
        return True

    # Stage-specific rules.
    if finding.stage == "secrets":
        return bool(finding.verified_secret)
    if finding.stage == "malicious":
        return True  # any match in the malicious-package denylist blocks
    if finding.stage == "sca":
        # HIGH in a direct dep blocks; HIGH transitive is warn-only.
        return sev == "high" and bool(finding.direct_dependency)
    if finding.stage in ("sast", "mcp_rules"):
        # Semgrep maps ERROR -> high, WARNING -> medium, INFO -> info in our wrappers.
        return sev == "high"
    if finding.stage == "image_vuln":
        # Image HIGH is warn-only; CRITICAL already handled above.
        return False
    if finding.stage == "image_hygiene":
        return False

    # Conservative default for unknown stages: only critical blocks.
    return False


def evaluate(findings: List[Finding], config: PolicyConfig) -> str:
    """Aggregate findings into a gate outcome.

    Returns one of "pass", "warn", "block".
    """
    has_block = False
    has_warn = False
    for f in findings:
        if config.ignore_rules and f.rule_id in config.ignore_rules:
            continue
        if _is_blocking(f):
            has_block = True
            continue
        # Anything not blocking but with severity >= medium counts as a warning.
        if f.severity.lower() in ("medium", "high"):
            has_warn = True
        elif f.severity.lower() == "critical":
            # Defensive - already caught by _is_blocking but keep symmetry.
            has_block = True
        else:
            has_warn = True if config.block_warnings else has_warn

    if has_block:
        return "block"
    if has_warn:
        return "block" if config.block_warnings else "warn"
    return "pass"


def is_blocking(finding: Finding) -> bool:
    """Public helper used by the runner to count blocking findings."""
    return _is_blocking(finding)
