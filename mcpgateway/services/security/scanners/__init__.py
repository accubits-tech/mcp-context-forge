# -*- coding: utf-8 -*-
"""Per-tool scanner wrappers.

Each scanner implements the ``Scanner`` protocol from ``base.py``:

- ``name``: scanner identifier (gitleaks, semgrep, osv, ...).
- ``stage``: pipeline stage name this scanner contributes to.
- ``async run(context) -> list[Finding]``.

Scanners do not own gating decisions; they just produce findings. The
``policy.py`` module decides which findings block.
"""
