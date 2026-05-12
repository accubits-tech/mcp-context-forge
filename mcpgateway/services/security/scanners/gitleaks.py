# -*- coding: utf-8 -*-
"""Gitleaks-based secrets scanner.

Runs gitleaks in detect mode against the source tree. Verified-secret findings
(scanned successfully against the upstream provider) block deploy; entropy-only
findings warn.
"""

# Standard
import json
import logging
import uuid
from pathlib import Path
from typing import Callable, List

# First-Party
from mcpgateway.services.deployment.drivers.base import RuntimeDriver
from mcpgateway.services.security.report import Finding
from mcpgateway.services.security.scanners._exec import SCAN_SRC_PATH, run_scanner
from mcpgateway.services.security.scanners.base import SourceContext, truncate

logger = logging.getLogger("mcpgateway.security.scanners.gitleaks")


class GitleaksScanner:
    """Secrets scanner using gitleaks detect (no-git mode)."""

    name = "gitleaks"
    stage = "secrets"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: SourceContext) -> List[Finding]:
        # Stream JSON output to stdout via gitleaks's `--report-path /dev/stdout` so
        # the host process never needs to mount a writable host path - the scanner's
        # tmpfs is the only writable surface.
        args = [
            "detect",
            "--source", SCAN_SRC_PATH,
            "--no-git",
            "--report-format", "json",
            "--report-path", "/dev/stdout",
            "--exit-code", "0",
            "--no-banner",
        ]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            src_dir=ctx.src_dir,
            timeout_s=ctx.timeout_s,
        )
        return _parse_gitleaks(result.stdout, ctx.src_dir)


def _parse_gitleaks(raw: str, src_dir: Path) -> List[Finding]:
    """Parse gitleaks JSON output into Findings.

    Gitleaks emits an array of finding objects; missing or non-JSON output
    means no findings (or a benign warning preceding the array).
    """
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    # gitleaks may emit a banner on stderr - we asked for /dev/stdout so stdout is the JSON.
    # Defensively locate the first '[' to skip any pre-amble.
    text = raw.strip()
    start = text.find("[")
    if start == -1:
        return findings
    payload = text[start:]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning("gitleaks: could not parse JSON output: %s", e)
        return findings
    if not isinstance(data, list):
        return findings

    for item in data:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("RuleID", "unknown"))
        # Gitleaks does not always populate "Verified" - treat true/false explicitly.
        verified = bool(item.get("Verified", False))
        secret = item.get("Secret", "")
        file_field = item.get("File", "")
        # Strip the in-container /src prefix if present so the path is repo-relative.
        if file_field.startswith(SCAN_SRC_PATH + "/"):
            file_field = file_field[len(SCAN_SRC_PATH) + 1:]
        elif file_field.startswith(SCAN_SRC_PATH):
            file_field = file_field[len(SCAN_SRC_PATH):]
        line = item.get("StartLine") or item.get("Line")
        try:
            line_int = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_int = None
        # Treat verified secrets as critical, entropy-only as high.
        severity = "critical" if verified else "high"
        message = item.get("Description") or f"Possible secret matched rule {rule}"
        findings.append(
            Finding(
                id=str(uuid.uuid4()),
                scanner="gitleaks",
                stage="secrets",
                severity=severity,
                rule_id=rule,
                file=file_field or None,
                line=line_int,
                message=message,
                cwe="CWE-798",
                raw_excerpt=truncate(secret if secret else None),
                verified_secret=verified,
            )
        )
    return findings
