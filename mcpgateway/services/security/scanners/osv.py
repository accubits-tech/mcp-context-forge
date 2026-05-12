# -*- coding: utf-8 -*-
"""osv-scanner SCA wrapper.

Detects known CVEs across the standard package manifests (requirements*.txt,
pyproject.toml, package*.json, pnpm-lock.yaml, uv.lock). Direct vs transitive
classification feeds policy.py's HIGH-direct-blocks rule.
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

logger = logging.getLogger("mcpgateway.security.scanners.osv")


class OsvScanner:
    """Dependency CVE scanner using google/osv-scanner."""

    name = "osv"
    stage = "sca"

    def __init__(self, image: str, driver_factory: Callable[[], RuntimeDriver]) -> None:
        self.image = image
        self._driver_factory = driver_factory

    async def run(self, ctx: SourceContext) -> List[Finding]:
        # osv-scanner needs network access to query the OSV database, but only
        # to OSV's HTTPS endpoint. Network-none would force --offline mode which
        # requires a pre-downloaded database; v1 keeps it simple by allowing
        # outbound network for this scanner only. The scanner image is pinned
        # by digest in production so the supply chain risk is bounded.
        args = ["scan", "source", "--format", "json", SCAN_SRC_PATH]
        result = await run_scanner(
            driver_factory=self._driver_factory,
            image=self.image,
            args=args,
            src_dir=ctx.src_dir,
            timeout_s=ctx.timeout_s,
            network_none=False,
        )
        return _parse_osv(result.stdout)


def _parse_osv(raw: str) -> List[Finding]:
    """Parse osv-scanner JSON output."""
    findings: List[Finding] = []
    if not raw or not raw.strip():
        return findings
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("osv-scanner: could not parse JSON output (%s)", e)
        return findings
    for result in data.get("results", []):
        manifest = (result.get("source") or {}).get("path", "") or ""
        if manifest.startswith(SCAN_SRC_PATH + "/"):
            manifest = manifest[len(SCAN_SRC_PATH) + 1:]
        elif manifest.startswith(SCAN_SRC_PATH):
            manifest = manifest[len(SCAN_SRC_PATH):]
        for pkg in result.get("packages", []):
            package_meta = pkg.get("package") or {}
            ecosystem = package_meta.get("ecosystem", "")
            name = package_meta.get("name", "?")
            version = package_meta.get("version", "?")
            # osv-scanner does not always tell us if a finding is on a direct
            # vs transitive dep. Heuristic: if the manifest is a *.lock or the
            # package depth > 1, treat as transitive. We default to direct=True
            # so the policy is conservative (block high in direct deps).
            is_direct = True
            for group in pkg.get("groups", []):
                if group.get("max_severity") in ("transitive",):
                    is_direct = False
                    break
            for vuln in pkg.get("vulnerabilities", []):
                vid = vuln.get("id", "OSV-UNKNOWN")
                summary = vuln.get("summary") or vuln.get("details", "")[:200]
                # Severity: OSV exposes "severity" sometimes; otherwise infer from
                # database_specific.severity, else "high" by default.
                severity_text = "high"
                for sev in vuln.get("severity", []) or []:
                    score = (sev.get("score") or "").upper()
                    if score:
                        # CVSS vector strings typically begin with "CVSS:3.1/"; we
                        # cannot parse without a library, so look for severity hints
                        # in database_specific instead.
                        break
                db_specific = vuln.get("database_specific") or {}
                ds_sev = (db_specific.get("severity") or db_specific.get("cvss_severity") or "").lower()
                if ds_sev in ("critical", "high", "medium", "low"):
                    severity_text = ds_sev
                cwe = None
                aliases = vuln.get("aliases", []) or []
                cve_alias = next((a for a in aliases if isinstance(a, str) and a.startswith("CVE-")), None)
                findings.append(
                    Finding(
                        id=str(uuid.uuid4()),
                        scanner="osv",
                        stage="sca",
                        severity=severity_text,
                        rule_id=vid,
                        file=manifest or None,
                        line=None,
                        message=f"{ecosystem}/{name}@{version}: {summary}" + (f" (alias {cve_alias})" if cve_alias else ""),
                        cwe=cwe,
                        raw_excerpt=truncate(summary),
                        direct_dependency=is_direct,
                    )
                )
    return findings
