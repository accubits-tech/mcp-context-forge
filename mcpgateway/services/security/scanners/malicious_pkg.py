# -*- coding: utf-8 -*-
"""Curated denylist scanner for known-malicious / typosquat packages.

Reads ``rules/malicious_packages.yml`` and inspects the standard manifest files
in the source tree for any matching package name. Runs entirely host-side
(reading manifests as text); no scanner container needed because we never
execute the user code.
"""

# Standard
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List, Set

import yaml  # PyYAML is already a runtime dep of the gateway

# First-Party
from mcpgateway.services.security.report import Finding
from mcpgateway.services.security.scanners.base import SourceContext, truncate

logger = logging.getLogger("mcpgateway.security.scanners.malicious_pkg")

_PYPI_MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml")
_NPM_MANIFESTS = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")

# Match "name==1.2.3", "name>=1", "name", "  - name@1.2.3" - capture the leading name.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:[=<>!~].*)?$")


class MaliciousPackageScanner:
    """Static manifest scan against a curated denylist."""

    name = "malicious-pkg"
    stage = "malicious"

    def __init__(self, denylist_path: Path) -> None:
        self.image = ""  # no scanner container
        self._denylist_path = denylist_path
        self._loaded: Dict[str, Dict[str, str]] = {}

    def _load_denylist(self) -> Dict[str, Dict[str, str]]:
        """Return {ecosystem: {package_name_lower: reason}}."""
        if self._loaded:
            return self._loaded
        try:
            data = yaml.safe_load(self._denylist_path.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, yaml.YAMLError) as e:
            logger.warning("malicious_pkg: could not load denylist %s: %s", self._denylist_path, e)
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for ecosystem, entries in (data or {}).items():
            bucket: Dict[str, str] = {}
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("name") or "").strip().lower()
                if not name:
                    continue
                bucket[name] = entry.get("reason") or "matched curated denylist"
            out[ecosystem] = bucket
        self._loaded = out
        return out

    async def run(self, ctx: SourceContext) -> List[Finding]:
        denylist = self._load_denylist()
        if not denylist:
            return []
        findings: List[Finding] = []
        py_bad = denylist.get("pypi", {})
        npm_bad = denylist.get("npm", {})

        for manifest in _PYPI_MANIFESTS:
            findings.extend(_scan_python_manifest(ctx.src_dir / manifest, py_bad))
        for manifest in _NPM_MANIFESTS:
            findings.extend(_scan_npm_manifest(ctx.src_dir / manifest, npm_bad))
        return findings


def _scan_python_manifest(path: Path, denylist: Dict[str, str]) -> List[Finding]:
    if not path.exists() or not denylist:
        return []
    findings: List[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.name == "pyproject.toml":
        # Naive pull of "name" tokens within [project.dependencies] / [tool.poetry.dependencies].
        names = _extract_pyproject_names(text)
        for name, line in names:
            lc = name.lower()
            if lc in denylist:
                findings.append(_make(name, "pypi", path.name, line, denylist[lc]))
        return findings
    # requirements*.txt
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # skip flags like -r, -e
            continue
        m = _REQ_NAME_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.lower() in denylist:
            findings.append(_make(name, "pypi", path.name, idx, denylist[name.lower()]))
    return findings


def _extract_pyproject_names(text: str) -> List[tuple]:
    names: List[tuple] = []
    in_deps = False
    for idx, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        lower = stripped.lower()
        if lower.startswith("[") and "dependencies" in lower:
            in_deps = True
            continue
        if lower.startswith("["):  # entered another section
            in_deps = False
            continue
        if not in_deps:
            continue
        # PEP 621: name = "value"; Poetry: name = "value".
        m = re.match(r'^\s*"?([A-Za-z0-9][A-Za-z0-9._\-]*)"?\s*[:=]', stripped)
        if m:
            names.append((m.group(1), idx))
        # Inline list form: dependencies = ["foo>=1", "bar"]
        if "[" in stripped and "]" in stripped:
            for token in re.findall(r'"([^"]+)"', stripped):
                m2 = _REQ_NAME_RE.match(token)
                if m2:
                    names.append((m2.group(1), idx))
    return names


def _scan_npm_manifest(path: Path, denylist: Dict[str, str]) -> List[Finding]:
    if not path.exists() or not denylist:
        return []
    findings: List[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    seen: Set[str] = set()
    if path.name == "package.json":
        # Best-effort regex over all "name": "..." entries within dependency sections.
        try:
            import json as _json
            data = _json.loads(text)
        except (ValueError, TypeError):
            data = {}
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name in (data.get(key) or {}).keys():
                if name.lower() in denylist and name.lower() not in seen:
                    seen.add(name.lower())
                    findings.append(_make(name, "npm", path.name, None, denylist[name.lower()]))
        return findings
    # lockfiles: scan for "name": "<token>" patterns.
    for m in re.finditer(r'"([A-Za-z0-9@/._\-]+)"\s*:\s*\{', text):
        token = m.group(1)
        # Strip leading "@scope/" if needed for the lookup.
        candidate = token.split("/")[-1] if not token.startswith("@") else token
        if candidate.lower() in denylist and candidate.lower() not in seen:
            seen.add(candidate.lower())
            findings.append(_make(candidate, "npm", path.name, None, denylist[candidate.lower()]))
    return findings


def _make(name: str, ecosystem: str, file: str, line, reason: str) -> Finding:
    return Finding(
        id=str(uuid.uuid4()),
        scanner="malicious-pkg",
        stage="malicious",
        severity="critical",
        rule_id=f"denylist:{ecosystem}:{name.lower()}",
        file=file,
        line=line,
        message=f"Package '{name}' ({ecosystem}) matched the malicious-package denylist: {reason}",
        cwe="CWE-1357",
        raw_excerpt=truncate(reason),
    )
