# -*- coding: utf-8 -*-
"""Hadolint Dockerfile linter.

Scans the rendered Containerfile in the source tree (the gateway-controlled
template, not any user-supplied Dockerfile - those are renamed *.user during
ingest). Output is warn-only in the default policy.
"""

# Standard
import json
import logging
import uuid
from typing import Callable, List

# First-Party
from mcpgateway.services.deployment.drivers.base import RuntimeDriver
from mcpgateway.services.security.report import Finding
from mcpgateway.services.security.scanners._exec import SCAN_SRC_PATH, run_scanner
from mcpgateway.services.security.scanners.base import SourceContext, truncate

logger = logging.getLogger("mcpgateway.security.scanners.hadolint")


class HadolintScanner:
    """Dockerfile lint."""

    name = "hadolint"
    stage = "image_hygiene"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: SourceContext) -> List[Finding]:
        # Hadolint expects the Dockerfile path; the rendered Containerfile lives
        # at the build context root.
        target = f"{SCAN_SRC_PATH}/Containerfile"
        args = ["--format", "json", "--no-fail", target]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            src_dir=ctx.src_dir,
            timeout_s=ctx.timeout_s,
            network_none=True,
        )
        return _parse_hadolint(result.stdout)


def _parse_hadolint(raw: str) -> List[Finding]:
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return findings
    if not isinstance(data, list):
        return findings
    for item in data:
        if not isinstance(item, dict):
            continue
        code = item.get("code", "DL000")
        severity_raw = (item.get("level") or "info").lower()
        severity = {
            "error": "high",
            "warning": "medium",
            "info": "info",
            "style": "info",
        }.get(severity_raw, "info")
        findings.append(
            Finding(
                id=str(uuid.uuid4()),
                scanner="hadolint",
                stage="image_hygiene",
                severity=severity,
                rule_id=code,
                file="Containerfile",
                line=item.get("line"),
                message=item.get("message", "Dockerfile lint finding"),
                cwe=None,
                raw_excerpt=truncate(item.get("message")),
            )
        )
    return findings
