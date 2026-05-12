# -*- coding: utf-8 -*-
"""Dockle image-hygiene scanner.

Runs against the built image to flag misconfigurations: running as root, missing
HEALTHCHECK, world-writable, capability creep. Output is warn-only.
"""

# Standard
import json
import logging
import uuid
from pathlib import Path
from typing import Callable, List

# First-Party
from mcpgateway.services.deployment.drivers.base import OneshotMount, RuntimeDriver
from mcpgateway.services.security.report import Finding
from mcpgateway.services.security.scanners._exec import run_scanner
from mcpgateway.services.security.scanners.base import ImageContext, truncate

logger = logging.getLogger("mcpgateway.security.scanners.dockle")


class DockleScanner:
    """Image-hygiene scanner."""

    name = "dockle"
    stage = "image_hygiene"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: ImageContext) -> List[Finding]:
        socket_mount = OneshotMount(host_path=Path("/var/run/docker.sock"), container_path="/var/run/docker.sock", read_only=False)
        args = ["-f", "json", "--exit-code", "0", ctx.image_tag]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            extra_mounts=[socket_mount],
            timeout_s=ctx.timeout_s,
            network_none=False,
        )
        return _parse_dockle(result.stdout)


def _parse_dockle(raw: str) -> List[Finding]:
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return findings
    for item in data.get("details", []) or []:
        code = item.get("code", "DKL-UNKNOWN")
        title = item.get("title") or "Dockle finding"
        level = (item.get("level") or "INFO").upper()
        severity = {
            "FATAL": "high",
            "WARN": "medium",
            "INFO": "info",
            "PASS": "info",
            "SKIP": "info",
        }.get(level, "info")
        msgs = item.get("alerts") or []
        msg = msgs[0] if msgs else title
        findings.append(
            Finding(
                id=str(uuid.uuid4()),
                scanner="dockle",
                stage="image_hygiene",
                severity=severity,
                rule_id=code,
                file=None,
                line=None,
                message=f"{title}: {msg}",
                cwe=None,
                raw_excerpt=truncate(msg),
            )
        )
    return findings
