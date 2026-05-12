# -*- coding: utf-8 -*-
"""Semgrep-based SAST scanner.

Used twice in the pipeline:
- ``stage='sast'``      : stock OWASP / language packs.
- ``stage='mcp_rules'`` : the bundled ``rules/mcp-rules.yml`` ruleset.

Both invocations share the same image; the orchestrator runs them in parallel.
"""

# Standard
import json
import logging
import uuid
from pathlib import Path
from typing import Callable, List, Optional

# First-Party
from mcpgateway.services.deployment.drivers.base import OneshotMount, RuntimeDriver
from mcpgateway.services.security.report import Finding
from mcpgateway.services.security.scanners._exec import SCAN_SRC_PATH, run_scanner
from mcpgateway.services.security.scanners.base import SourceContext, truncate

logger = logging.getLogger("mcpgateway.security.scanners.semgrep")

# Stock packs used by the SAST stage. We deliberately avoid heavyweight packs
# (e.g. p/r2c-ci) in v1 to keep scan time inside the 5-min budget.
_STOCK_CONFIGS = ["p/security-audit", "p/python", "p/javascript", "p/owasp-top-ten"]


class SemgrepStockScanner:
    """Stock-pack SAST scan."""

    name = "semgrep"
    stage = "sast"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: SourceContext) -> List[Finding]:
        args = ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--timeout", str(max(ctx.timeout_s - 5, 5))]
        for cfg in _STOCK_CONFIGS:
            args.extend(["--config", cfg])
        args.append(SCAN_SRC_PATH)
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            src_dir=ctx.src_dir,
            timeout_s=ctx.timeout_s,
            # Semgrep's stock packs are bundled into the image; no network needed.
            network_none=True,
        )
        return _parse_semgrep(result.stdout, "sast")


class SemgrepMcpRulesScanner:
    """MCP-specific custom rules.

    Mounts the package's bundled rules dir read-only into the scanner container.
    """

    name = "mcp-rules"
    stage = "mcp_rules"

    def __init__(self, image: str, rules_path: Path, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._rules_path = rules_path  # host path to mcp-rules.yml
        self._driver_factory = driver_factory

    async def run(self, ctx: SourceContext) -> List[Finding]:
        rules_mount = OneshotMount(host_path=self._rules_path, container_path="/rules/mcp-rules.yml", read_only=True)
        args = [
            "semgrep", "scan", "--json", "--quiet", "--metrics=off",
            "--timeout", str(max(ctx.timeout_s - 5, 5)),
            "--config", "/rules/mcp-rules.yml",
            SCAN_SRC_PATH,
        ]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            src_dir=ctx.src_dir,
            extra_mounts=[rules_mount],
            timeout_s=ctx.timeout_s,
            network_none=True,
        )
        return _parse_semgrep(result.stdout, "mcp_rules")


def _parse_semgrep(raw: str, stage: str) -> List[Finding]:
    """Parse semgrep --json output into Findings."""
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("semgrep: could not parse JSON output (%s)", e)
        return findings
    results = data.get("results") or []
    for r in results:
        check_id = r.get("check_id", "unknown")
        path = r.get("path", "") or ""
        if path.startswith(SCAN_SRC_PATH + "/"):
            path = path[len(SCAN_SRC_PATH) + 1:]
        elif path.startswith(SCAN_SRC_PATH):
            path = path[len(SCAN_SRC_PATH):]
        start = r.get("start") or {}
        line = start.get("line")
        try:
            line_int = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_int = None
        extra = r.get("extra") or {}
        sev_raw = (extra.get("severity") or "WARNING").upper()
        # Map ERROR->high, WARNING->medium, INFO->info; CRITICAL is rare but supported.
        severity = {
            "CRITICAL": "critical",
            "ERROR": "high",
            "HIGH": "high",
            "WARNING": "medium",
            "MEDIUM": "medium",
            "INFO": "info",
            "LOW": "low",
        }.get(sev_raw, "medium")
        message = extra.get("message") or ""
        cwe: Optional[str] = None
        metadata = extra.get("metadata") or {}
        cwe_raw = metadata.get("cwe")
        if isinstance(cwe_raw, list) and cwe_raw:
            cwe = str(cwe_raw[0]).split(":")[0].strip() or None
        elif isinstance(cwe_raw, str):
            cwe = cwe_raw.split(":")[0].strip() or None
        excerpt = extra.get("lines") or extra.get("rendered_fix") or ""
        scanner_name = "mcp-rules" if stage == "mcp_rules" else "semgrep"
        findings.append(
            Finding(
                id=str(uuid.uuid4()),
                scanner=scanner_name,
                stage=stage,
                severity=severity,
                rule_id=check_id,
                file=path or None,
                line=line_int,
                message=message,
                cwe=cwe,
                raw_excerpt=truncate(excerpt),
            )
        )
    return findings
