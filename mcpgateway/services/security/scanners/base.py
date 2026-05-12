# -*- coding: utf-8 -*-
"""Common types for scanner wrappers."""

# Standard
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

# First-Party
from mcpgateway.services.security.report import Finding


@dataclass
class SourceContext:
    """Inputs for a pre-build (source-side) scanner."""

    src_dir: Path
    source_sha256: str
    timeout_s: int


@dataclass
class ImageContext:
    """Inputs for a post-build (image-side) scanner."""

    image_tag: str
    timeout_s: int


class SourceScanner(Protocol):
    """A scanner that operates on the extracted source tree."""

    name: str
    stage: str
    image: str  # container image used for execution

    async def run(self, ctx: SourceContext) -> List[Finding]:  # pragma: no cover - protocol
        ...


class ImageScanner(Protocol):
    """A scanner that operates on a built container image."""

    name: str
    stage: str
    image: str

    async def run(self, ctx: ImageContext) -> List[Finding]:  # pragma: no cover - protocol
        ...


_RAW_EXCERPT_CAP = 2048


def truncate(value: Optional[str]) -> Optional[str]:
    """Cap raw_excerpt to keep DB rows bounded."""
    if value is None:
        return None
    if len(value) <= _RAW_EXCERPT_CAP:
        return value
    return value[:_RAW_EXCERPT_CAP] + "...[truncated]"
