# -*- coding: utf-8 -*-
"""Trivy image-vulnerability scanner.

Runs against the freshly built container image (post-build stage). Scans OS
packages and language ecosystems found inside the image.
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

logger = logging.getLogger("mcpgateway.security.scanners.trivy")


class TrivyImageScanner:
    """Image vuln scanner."""

    name = "trivy"
    stage = "image_vuln"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: ImageContext) -> List[Finding]:
        # Trivy needs the docker socket to introspect a locally-built image.
        # We mount the daemon's socket read-only; Trivy itself runs as user 10001
        # with read-only rootfs and dropped caps.
        socket_mount = OneshotMount(host_path=Path("/var/run/docker.sock"), container_path="/var/run/docker.sock", read_only=False)
        args = [
            "image",
            "--format", "json",
            "--quiet",
            "--severity", "CRITICAL,HIGH,MEDIUM",
            "--ignore-unfixed",
            "--scanners", "vuln",
            ctx.image_tag,
        ]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            extra_mounts=[socket_mount],
            timeout_s=ctx.timeout_s,
            network_none=False,  # trivy DB updates need network
        )
        return _parse_trivy(result.stdout)


def _parse_trivy(raw: str) -> List[Finding]:
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("trivy: could not parse JSON output (%s)", e)
        return findings
    for result in data.get("Results", []) or []:
        target = result.get("Target", "")
        for vuln in result.get("Vulnerabilities", []) or []:
            vid = vuln.get("VulnerabilityID", "TRIVY-UNKNOWN")
            severity = (vuln.get("Severity") or "MEDIUM").lower()
            pkg = vuln.get("PkgName", "?")
            installed = vuln.get("InstalledVersion", "?")
            fixed = vuln.get("FixedVersion") or "no-fix"
            title = vuln.get("Title") or vuln.get("Description", "")[:200]
            cwes = vuln.get("CweIDs") or []
            cwe = cwes[0] if cwes else None
            findings.append(
                Finding(
                    id=str(uuid.uuid4()),
                    scanner="trivy",
                    stage="image_vuln",
                    severity=severity,
                    rule_id=vid,
                    file=target or None,
                    line=None,
                    message=f"{pkg} {installed} (fix: {fixed}) - {title}",
                    cwe=cwe,
                    raw_excerpt=truncate(title),
                )
            )
    return findings
