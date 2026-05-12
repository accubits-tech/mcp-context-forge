# -*- coding: utf-8 -*-
"""Vulnerability-scan gate for source-uploaded MCP server deployments.

The orchestrator (``runner.SecurityScanRunner``) runs a sequence of scanners
against an extracted source tree (pre-build) and the freshly built container
image (post-build). Each scanner runs in an ephemeral container via the
existing ``RuntimeDriver``; no user code is executed on the host.

Stages (rendered in this order in the FE stepper):

1. ``upload``           - framing only; bytes received, hash recorded
2. ``extract``          - framing only; archive extracted with PEP 706 filter
3. ``secrets``          - gitleaks
4. ``sast``             - semgrep with stock security packs
5. ``sca``              - osv-scanner against pyproject.toml/requirements*/package-lock
6. ``malicious``        - curated typosquat + known-malicious denylist
7. ``mcp_rules``        - semgrep with the bundled mcp-rules.yml
8. ``image_vuln``       - trivy against the built image
9. ``image_hygiene``    - hadolint + dockle
"""
