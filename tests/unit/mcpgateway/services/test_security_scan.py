# -*- coding: utf-8 -*-
"""Unit tests for the vulnerability-scan gate (services/security)."""

# Standard
import asyncio
import json
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

# Third-Party
import pytest

# First-Party
from mcpgateway.services.security.policy import PolicyConfig, evaluate, is_blocking
from mcpgateway.services.security.progress import ProgressTracker
from mcpgateway.services.security.report import Finding, ScanReport, ScanSummary, StageState
from mcpgateway.services.security.scanners.gitleaks import _parse_gitleaks
from mcpgateway.services.security.scanners.malicious_pkg import MaliciousPackageScanner
from mcpgateway.services.security.scanners.osv import _parse_osv
from mcpgateway.services.security.scanners.semgrep import _parse_semgrep
from mcpgateway.services.security.scanners.trivy import _parse_trivy


# ---------------------------------------------------------------------------
# policy.evaluate
# ---------------------------------------------------------------------------


def _f(stage: str, severity: str, **kw) -> Finding:
    return Finding(id="x", scanner="s", stage=stage, severity=severity, rule_id=kw.pop("rule_id", "r"), message="m", **kw)


@pytest.mark.parametrize(
    "findings,expected",
    [
        ([_f("sast", "critical")], "block"),
        ([_f("secrets", "high", verified_secret=True)], "block"),
        ([_f("secrets", "high", verified_secret=False)], "warn"),
        ([_f("malicious", "medium")], "block"),
        ([_f("sca", "high", direct_dependency=True)], "block"),
        ([_f("sca", "high", direct_dependency=False)], "warn"),
        ([_f("image_vuln", "high")], "warn"),
        ([_f("image_vuln", "critical")], "block"),
        ([_f("sast", "high")], "block"),
        ([_f("sast", "medium")], "warn"),
        ([], "pass"),
    ],
)
def test_policy_default(findings, expected):
    assert evaluate(findings, PolicyConfig()) == expected


def test_policy_ignore_rules():
    cfg = PolicyConfig(ignore_rules=["banned-rule"])
    # The ignored rule is critical; without it the only finding left is a medium
    # SAST warning, which evaluates to 'warn' under the balanced policy.
    findings = [_f("sast", "critical", rule_id="banned-rule"), _f("sast", "medium", rule_id="other")]
    assert evaluate(findings, cfg) == "warn"


def test_policy_block_warnings_promotes_warn_to_block():
    cfg = PolicyConfig(block_warnings=True)
    assert evaluate([_f("sast", "medium")], cfg) == "block"


def test_is_blocking_critical_image():
    assert is_blocking(_f("image_vuln", "critical")) is True


def test_is_blocking_warn_only_image_high():
    assert is_blocking(_f("image_vuln", "high")) is False


# ---------------------------------------------------------------------------
# ProgressTracker
# ---------------------------------------------------------------------------


def test_progress_tracker_lifecycle():
    async def run() -> ScanSummary:
        tracker = ProgressTracker()
        stages = [StageState(name="secrets", label="Secrets", scanner="gitleaks"), StageState(name="sast", label="SAST", scanner="semgrep")]
        await tracker.init_run("g1", "run1", stages)
        await tracker.start_stage("g1", "secrets")
        await tracker.update_stage_counts("g1", "secrets", {"high": 2})
        await tracker.finish_stage("g1", "secrets", "warned")
        await tracker.finalize("g1", "warned", "warn", 0)
        return tracker.snapshot("g1")

    summary = asyncio.run(run())
    assert summary is not None
    assert summary.scan_run_id == "run1"
    assert summary.overall_status == "warned"
    assert summary.gate_outcome == "warn"
    secrets = next(s for s in summary.stages if s.name == "secrets")
    assert secrets.status == "warned"
    assert secrets.findings_count_by_severity == {"high": 2}


# ---------------------------------------------------------------------------
# Scanner output parsers
# ---------------------------------------------------------------------------


def test_parse_gitleaks_verified_secret_is_critical():
    raw = json.dumps([
        {"RuleID": "aws-access-key", "Verified": True, "Secret": "AKIAxxxxxxxxxxxxxxxx", "File": "/src/handler.py", "StartLine": 12, "Description": "AWS access key"},
        {"RuleID": "generic-api-key", "Verified": False, "Secret": "abc123", "File": "/src/utils.py", "StartLine": 3},
    ])
    findings = _parse_gitleaks(raw, Path("/tmp/src"))
    sev = sorted([f.severity for f in findings])
    assert "critical" in sev and "high" in sev
    verified = [f for f in findings if f.verified_secret]
    assert verified and verified[0].file == "handler.py"


def test_parse_gitleaks_skips_pre_amble():
    raw = "WARNING: gitleaks: stale config\n[]"
    assert _parse_gitleaks(raw, Path("/tmp/src")) == []


def test_parse_semgrep_severity_map():
    raw = json.dumps({
        "results": [
            {"check_id": "py.eval", "path": "/src/a.py", "start": {"line": 5}, "extra": {"severity": "ERROR", "message": "use of eval", "metadata": {"cwe": ["CWE-95: Eval"]}}},
            {"check_id": "py.style", "path": "/src/b.py", "start": {"line": 1}, "extra": {"severity": "WARNING", "message": "style"}},
        ]
    })
    findings = _parse_semgrep(raw, "sast")
    sev = {f.rule_id: f.severity for f in findings}
    assert sev["py.eval"] == "high"
    assert sev["py.style"] == "medium"
    eval_finding = next(f for f in findings if f.rule_id == "py.eval")
    assert eval_finding.cwe == "CWE-95"


def test_parse_osv_extracts_aliases_and_direct_default():
    raw = json.dumps({
        "results": [
            {
                "source": {"path": "/src/requirements.txt"},
                "packages": [
                    {
                        "package": {"ecosystem": "PyPI", "name": "flask", "version": "0.12.0"},
                        "vulnerabilities": [
                            {"id": "GHSA-xxxx", "summary": "Open redirect", "aliases": ["CVE-2018-1000656"], "database_specific": {"severity": "high"}},
                        ],
                    }
                ],
            }
        ]
    })
    findings = _parse_osv(raw)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "high"
    assert f.direct_dependency is True
    assert "CVE-2018-1000656" in f.message


def test_parse_trivy_image_vuln():
    raw = json.dumps({
        "Results": [
            {"Target": "alpine 3.19", "Vulnerabilities": [
                {"VulnerabilityID": "CVE-2023-9999", "Severity": "CRITICAL", "PkgName": "openssl", "InstalledVersion": "3.0.0", "FixedVersion": "3.0.10", "Title": "OpenSSL bug"}
            ]}
        ]
    })
    findings = _parse_trivy(raw)
    assert len(findings) == 1 and findings[0].severity == "critical" and findings[0].rule_id == "CVE-2023-9999"


# ---------------------------------------------------------------------------
# Malicious-package denylist
# ---------------------------------------------------------------------------


def test_malicious_pkg_detects_typosquat_in_requirements(tmp_path):
    denylist = tmp_path / "denylist.yml"
    denylist.write_text("pypi:\n  - name: requets\n    reason: typosquat of requests\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.txt").write_text("flask==2.0\nrequets==1.0\n", encoding="utf-8")

    scanner = MaliciousPackageScanner(denylist_path=denylist)
    from mcpgateway.services.security.scanners.base import SourceContext  # local import to avoid heavy module init
    findings = asyncio.run(scanner.run(SourceContext(src_dir=src, source_sha256="abc", timeout_s=30)))
    assert any(f.rule_id == "denylist:pypi:requets" for f in findings)
    assert all(f.severity == "critical" for f in findings)


def test_malicious_pkg_detects_npm_typosquat(tmp_path):
    denylist = tmp_path / "denylist.yml"
    denylist.write_text("npm:\n  - name: colorsama\n    reason: typosquat of chalk\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "package.json").write_text(json.dumps({"name": "x", "dependencies": {"colorsama": "^1.0", "express": "^4"}}), encoding="utf-8")

    scanner = MaliciousPackageScanner(denylist_path=denylist)
    from mcpgateway.services.security.scanners.base import SourceContext  # local import
    findings = asyncio.run(scanner.run(SourceContext(src_dir=src, source_sha256="abc", timeout_s=30)))
    assert any(f.rule_id == "denylist:npm:colorsama" for f in findings)


# ---------------------------------------------------------------------------
# Runner orchestration (with mocked scanners)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_pre_build_aggregates_findings(tmp_path, monkeypatch):
    from mcpgateway.services.security import runner as runner_mod  # pylint: disable=import-outside-toplevel

    src = tmp_path / "src"
    src.mkdir()

    # Stub each scanner class to return a small finding set.
    canned: dict = {
        "secrets": [_f("secrets", "high", verified_secret=False)],
        "sast": [_f("sast", "medium")],
        "sca": [],
        "malicious": [],
        "mcp_rules": [],
    }

    class _StubScanner:
        def __init__(self, stage: str):
            self._stage = stage

        async def run(self, _ctx) -> List[Finding]:
            return list(canned[self._stage])

    monkeypatch.setattr(runner_mod, "GitleaksScanner", lambda **kw: _StubScanner("secrets"))
    monkeypatch.setattr(runner_mod, "SemgrepStockScanner", lambda **kw: _StubScanner("sast"))
    monkeypatch.setattr(runner_mod, "OsvScanner", lambda **kw: _StubScanner("sca"))
    monkeypatch.setattr(runner_mod, "MaliciousPackageScanner", lambda **kw: _StubScanner("malicious"))
    monkeypatch.setattr(runner_mod, "SemgrepMcpRulesScanner", lambda **kw: _StubScanner("mcp_rules"))

    driver_factory = MagicMock()
    runner = runner_mod.SecurityScanRunner(driver=driver_factory, semaphore=None)
    report = await runner.run_pre_build(gateway_id="g1", src_dir=src, source_sha256="sha", artifact_dir=tmp_path)

    assert report.summary.gate_outcome == "warn"  # entropy secret + sast medium = warn
    # Stages "secrets" and "sast" have findings; the rest are passed.
    statuses = {s.name: s.status for s in report.summary.stages}
    assert statuses["secrets"] == "warned"
    assert statuses["sast"] == "warned"
    assert statuses["sca"] == "passed"
    assert statuses["upload"] == "passed"
    # On-disk report exists.
    assert (tmp_path / "scan-report.json").exists()


@pytest.mark.asyncio
async def test_runner_blocks_on_critical(tmp_path, monkeypatch):
    from mcpgateway.services.security import runner as runner_mod
    from mcpgateway.services.security.report import Finding as F

    src = tmp_path / "src"
    src.mkdir()

    class _Stub:
        def __init__(self, payload):
            self._p = payload

        async def run(self, _ctx):
            return list(self._p)

    monkeypatch.setattr(runner_mod, "GitleaksScanner", lambda **kw: _Stub([F(id="i1", scanner="gitleaks", stage="secrets", severity="critical", rule_id="aws", message="AWS key", verified_secret=True)]))
    monkeypatch.setattr(runner_mod, "SemgrepStockScanner", lambda **kw: _Stub([]))
    monkeypatch.setattr(runner_mod, "OsvScanner", lambda **kw: _Stub([]))
    monkeypatch.setattr(runner_mod, "MaliciousPackageScanner", lambda **kw: _Stub([]))
    monkeypatch.setattr(runner_mod, "SemgrepMcpRulesScanner", lambda **kw: _Stub([]))

    driver_factory = MagicMock()
    runner = runner_mod.SecurityScanRunner(driver=driver_factory, semaphore=None)
    report = await runner.run_pre_build(gateway_id="g2", src_dir=src, source_sha256="sha", artifact_dir=tmp_path)

    assert report.summary.gate_outcome == "block"
    assert report.summary.blocking_findings_count == 1
    assert report.summary.overall_status == "blocked"
