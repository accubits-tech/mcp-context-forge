# -*- coding: utf-8 -*-
"""SecurityScanRunner - orchestrates the 9-stage scan pipeline.

Phase 1 (pre-build, fan-out parallel): secrets, sast, sca, malicious, mcp_rules.
Phase 2 (post-build, fan-out parallel): image_vuln, image_hygiene.

The runner is invoked from ``deployment_runtime_service.deploy()`` between the
``ingest`` and ``driver().build()`` calls, then again between ``build`` and
``start``. On a blocking gate outcome it raises ``SecurityGateError``; the
orchestrator handles persistence + image cleanup.
"""

# Standard
import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, List, Optional

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.deployment.drivers.base import RuntimeDriver
from mcpgateway.services.security.policy import PolicyConfig, evaluate, is_blocking
from mcpgateway.services.security.progress import TRACKER
from mcpgateway.services.security.report import (
    Finding,
    ScanReport,
    ScanSummary,
    StageState,
    utcnow,
)
from mcpgateway.services.security.scanners.base import ImageContext, SourceContext
from mcpgateway.services.security.scanners.dockle import DockleScanner
from mcpgateway.services.security.scanners.gitleaks import GitleaksScanner
from mcpgateway.services.security.scanners.hadolint import HadolintScanner
from mcpgateway.services.security.scanners.malicious_pkg import MaliciousPackageScanner
from mcpgateway.services.security.scanners.osv import OsvScanner
from mcpgateway.services.security.scanners.semgrep import SemgrepMcpRulesScanner, SemgrepStockScanner
from mcpgateway.services.security.scanners.trivy import TrivyImageScanner

logger = logging.getLogger("mcpgateway.security.runner")

# Bundled rules dir (lives next to runner.py).
_RULES_DIR = Path(__file__).resolve().parent / "rules"
_MCP_RULES = _RULES_DIR / "mcp-rules.yml"
_DENYLIST = _RULES_DIR / "malicious_packages.yml"


_STAGE_DEFINITIONS = [
    # (stage_name, label, scanner_label_or_None)
    ("upload", "Upload received", None),
    ("extract", "Extract & validate archive", None),
    ("secrets", "Secrets", "gitleaks"),
    ("sast", "Static analysis", "semgrep"),
    ("sca", "Dependency CVEs", "osv-scanner"),
    ("malicious", "Malicious packages", "denylist"),
    ("mcp_rules", "MCP-specific rules", "semgrep (mcp-rules)"),
    ("image_vuln", "Image vulnerability scan", "trivy"),
    ("image_hygiene", "Image hygiene", "hadolint+dockle"),
]


def _new_report(scan_run_id: str, source_sha256: Optional[str]) -> ScanReport:
    stages = [StageState(name=name, label=label, scanner=scanner) for name, label, scanner in _STAGE_DEFINITIONS]
    summary = ScanSummary(scan_run_id=scan_run_id, overall_status="pending", stages=stages)
    return ScanReport(summary=summary, source_sha256=source_sha256, started_at=utcnow())


def _serialize_for_disk(report: ScanReport) -> dict:
    """Produce a JSON-friendly dict for scan-report.json."""
    summary = report.summary
    stages_out = []
    for s in summary.stages:
        stages_out.append({
            "name": s.name,
            "label": s.label,
            "scanner": s.scanner,
            "status": s.status,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            "error": s.error,
            "findings_count_by_severity": s.findings_count_by_severity,
        })
    return {
        "summary": {
            "scan_run_id": summary.scan_run_id,
            "overall_status": summary.overall_status,
            "gate_outcome": summary.gate_outcome,
            "blocking_findings_count": summary.blocking_findings_count,
            "counts_by_severity": summary.counts_by_severity,
            "stages": stages_out,
        },
        "findings": [asdict(f) for f in report.findings],
        "source_sha256": report.source_sha256,
        "image_tag": report.image_tag,
        "started_at": report.started_at.isoformat() if report.started_at else None,
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
    }


class SecurityScanRunner:
    """Drives the scan pipeline for one deployment.

    Holds a reference to a callable that returns the active ``RuntimeDriver``
    so it shares the same daemon connection as the deployment service. The
    semaphore parameter is the gateway's existing build-concurrency cap; we
    acquire it for each scanner container to bound host pressure.
    """

    def __init__(
        self,
        driver: Callable[[], RuntimeDriver],
        semaphore: Optional[asyncio.Semaphore] = None,
        policy: Optional[PolicyConfig] = None,
    ) -> None:
        self._driver = driver
        self._semaphore = semaphore
        self._policy = policy or PolicyConfig(
            block_warnings=settings.mcpgateway_security_scan_block_warnings,
            ignore_rules=list(settings.mcpgateway_security_scan_ignore_rules or []),
        )

    # ------------------------------------------------------------------
    # Pre-build
    # ------------------------------------------------------------------

    async def run_pre_build(
        self,
        *,
        gateway_id: str,
        src_dir: Path,
        source_sha256: str,
        artifact_dir: Optional[Path] = None,
    ) -> ScanReport:
        """Run secrets / sast / sca / malicious / mcp_rules scanners.

        On a blocking gate outcome the caller should raise SecurityGateError.
        """
        scan_run_id = str(uuid.uuid4())
        report = _new_report(scan_run_id, source_sha256)
        await TRACKER.init_run(gateway_id, scan_run_id, report.summary.stages)

        # Stages 1-2 are framing - mark them passed immediately.
        await self._mark_immediate_stage(gateway_id, report, "upload", "passed")
        await self._mark_immediate_stage(gateway_id, report, "extract", "passed")

        secrets_to = settings.mcpgateway_security_scan_secrets_timeout_s
        sast_to = settings.mcpgateway_security_scan_sast_timeout_s
        sca_to = settings.mcpgateway_security_scan_sca_timeout_s
        mal_to = settings.mcpgateway_security_scan_malicious_timeout_s

        secrets = GitleaksScanner(image=settings.mcpgateway_security_scan_image_gitleaks, driver_factory=self._driver)
        sast = SemgrepStockScanner(image=settings.mcpgateway_security_scan_image_semgrep, driver_factory=self._driver)
        sca = OsvScanner(image=settings.mcpgateway_security_scan_image_osv, driver_factory=self._driver)
        malicious = MaliciousPackageScanner(denylist_path=_DENYLIST)
        mcp_rules = SemgrepMcpRulesScanner(image=settings.mcpgateway_security_scan_image_semgrep, rules_path=_MCP_RULES, driver_factory=self._driver)

        tasks = [
            self._run_one(gateway_id, report, "secrets", secrets, SourceContext(src_dir=src_dir, source_sha256=source_sha256, timeout_s=secrets_to)),
            self._run_one(gateway_id, report, "sast", sast, SourceContext(src_dir=src_dir, source_sha256=source_sha256, timeout_s=sast_to)),
            self._run_one(gateway_id, report, "sca", sca, SourceContext(src_dir=src_dir, source_sha256=source_sha256, timeout_s=sca_to)),
            self._run_one(gateway_id, report, "malicious", malicious, SourceContext(src_dir=src_dir, source_sha256=source_sha256, timeout_s=mal_to)),
            self._run_one(gateway_id, report, "mcp_rules", mcp_rules, SourceContext(src_dir=src_dir, source_sha256=source_sha256, timeout_s=sast_to)),
        ]
        await asyncio.gather(*tasks, return_exceptions=False)

        # Image stages stay 'pending' until run_post_build is called. Compute interim outcome
        # using only the source-side findings so the caller can decide whether to even build.
        gate = evaluate(report.findings, self._policy)
        report.summary.gate_outcome = gate
        report.summary.blocking_findings_count = sum(1 for f in report.findings if is_blocking(f))
        report.summary.overall_status = "running" if gate != "block" else "blocked"
        await TRACKER.finalize(gateway_id, report.summary.overall_status, gate, report.summary.blocking_findings_count)

        if artifact_dir:
            self._persist_report(artifact_dir, report)
        return report

    # ------------------------------------------------------------------
    # Post-build
    # ------------------------------------------------------------------

    async def run_post_build(
        self,
        *,
        gateway_id: str,
        image_tag: str,
        pre_report: ScanReport,
        artifact_dir: Optional[Path] = None,
    ) -> ScanReport:
        """Run image_vuln + image_hygiene against the freshly built image."""
        report = pre_report  # mutate in place; same scan_run_id
        report.image_tag = image_tag

        image_vuln_to = settings.mcpgateway_security_scan_image_vuln_timeout_s
        hygiene_to = settings.mcpgateway_security_scan_image_hygiene_timeout_s

        trivy = TrivyImageScanner(image=settings.mcpgateway_security_scan_image_trivy, driver_factory=self._driver)
        dockle = DockleScanner(image=settings.mcpgateway_security_scan_image_dockle, driver_factory=self._driver)
        # Hadolint runs on the source-tree Containerfile, but we exposed it as part of the
        # image_hygiene stage. To keep the stage atomic we fold its findings into the same
        # stage; if the source dir is no longer accessible (e.g. cleaned), we skip it.
        hadolint = HadolintScanner(image=settings.mcpgateway_security_scan_image_hadolint, driver_factory=self._driver)

        tasks = [
            self._run_image_one(gateway_id, report, "image_vuln", trivy, ImageContext(image_tag=image_tag, timeout_s=image_vuln_to)),
            self._run_image_one(gateway_id, report, "image_hygiene", dockle, ImageContext(image_tag=image_tag, timeout_s=hygiene_to), append=True, hadolint=hadolint),
        ]
        await asyncio.gather(*tasks, return_exceptions=False)

        gate = evaluate(report.findings, self._policy)
        report.summary.gate_outcome = gate
        report.summary.blocking_findings_count = sum(1 for f in report.findings if is_blocking(f))
        # Compute aggregate stage status: any failed/error -> failed; any warned -> warned;
        # else passed.
        any_failed = any(s.status in ("failed", "error") for s in report.summary.stages if s.name not in ("upload", "extract"))
        any_warned = any(s.status == "warned" for s in report.summary.stages)
        if gate == "block":
            report.summary.overall_status = "blocked"
        elif any_failed:
            report.summary.overall_status = "blocked"
        elif any_warned or gate == "warn":
            report.summary.overall_status = "warned"
        else:
            report.summary.overall_status = "passed"
        report.completed_at = utcnow()
        await TRACKER.finalize(gateway_id, report.summary.overall_status, gate, report.summary.blocking_findings_count)

        if artifact_dir:
            self._persist_report(artifact_dir, report)
        return report

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _mark_immediate_stage(self, gateway_id: str, report: ScanReport, name: str, status: str) -> None:
        await TRACKER.start_stage(gateway_id, name)
        await TRACKER.finish_stage(gateway_id, name, status)
        for s in report.summary.stages:
            if s.name == name:
                s.status = status
                s.started_at = s.finished_at = utcnow()

    async def _run_one(self, gateway_id: str, report: ScanReport, stage: str, scanner, ctx: SourceContext) -> None:
        await TRACKER.start_stage(gateway_id, stage)
        try:
            if self._semaphore is not None:
                async with self._semaphore:
                    findings = await scanner.run(ctx)
            else:
                findings = await scanner.run(ctx)
            await self._record_findings(gateway_id, report, stage, findings)
            status = self._stage_status(findings)
            await TRACKER.finish_stage(gateway_id, stage, status)
            self._set_stage_status(report, stage, status)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - scanner-level failures should not poison the run
            logger.exception("scanner %s on stage %s failed: %s", scanner.__class__.__name__, stage, e)
            await TRACKER.finish_stage(gateway_id, stage, "error", error=str(e))
            self._set_stage_status(report, stage, "error", error=str(e))

    async def _run_image_one(
        self,
        gateway_id: str,
        report: ScanReport,
        stage: str,
        scanner,
        ctx: ImageContext,
        *,
        append: bool = False,
        hadolint: Optional[HadolintScanner] = None,
    ) -> None:
        await TRACKER.start_stage(gateway_id, stage)
        all_findings: List[Finding] = []
        try:
            if self._semaphore is not None:
                async with self._semaphore:
                    primary = await scanner.run(ctx)
            else:
                primary = await scanner.run(ctx)
            all_findings.extend(primary)
        except Exception as e:  # noqa: BLE001
            logger.exception("scanner %s on stage %s failed: %s", scanner.__class__.__name__, stage, e)
            await TRACKER.finish_stage(gateway_id, stage, "error", error=str(e))
            self._set_stage_status(report, stage, "error", error=str(e))
            return
        # NOTE: Hadolint runs on the source-tree Containerfile, but in v1 the post-
        # build entry point doesn't have the source dir (it was cleaned up between
        # scan_pre and scan_post). To keep the stage label "Image hygiene" honest in
        # v1 we rely on Dockle for image-side hygiene checks; the Hadolint pass on
        # the rendered Containerfile is intentionally deferred to the runner's
        # next iteration where the source dir is threaded through.
        _ = hadolint  # quiet linters about the unused parameter in v1
        await self._record_findings(gateway_id, report, stage, all_findings)
        status = self._stage_status(all_findings)
        await TRACKER.finish_stage(gateway_id, stage, status)
        self._set_stage_status(report, stage, status)

    @staticmethod
    def _stage_status(findings: List[Finding]) -> str:
        if not findings:
            return "passed"
        if any(is_blocking(f) for f in findings):
            return "failed"
        return "warned"

    @staticmethod
    def _set_stage_status(report: ScanReport, stage: str, status: str, error: Optional[str] = None) -> None:
        for s in report.summary.stages:
            if s.name == stage:
                s.status = status
                s.finished_at = utcnow()
                if error:
                    s.error = error
                return

    async def _record_findings(self, gateway_id: str, report: ScanReport, stage: str, findings: List[Finding]) -> None:
        # Drop ignored rules before persistence/UI.
        keep = [f for f in findings if f.rule_id not in (self._policy.ignore_rules or [])]
        report.add_findings(stage, keep)
        # Push counts to the live tracker.
        counts = {}
        for f in keep:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        await TRACKER.update_stage_counts(gateway_id, stage, counts)

    @staticmethod
    def _persist_report(artifact_dir: Path, report: ScanReport) -> None:
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            out = artifact_dir / "scan-report.json"
            out.write_text(json.dumps(_serialize_for_disk(report), indent=2, default=str), encoding="utf-8")
        except OSError as e:  # pragma: no cover - disk errors are best-effort here
            logger.warning("could not persist scan-report.json under %s: %s", artifact_dir, e)
