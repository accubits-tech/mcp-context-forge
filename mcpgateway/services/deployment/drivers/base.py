# -*- coding: utf-8 -*-
"""Runtime driver protocol for deployed MCP servers.

A driver owns image and container lifecycle for one deployed server. Implementations
MUST enforce the sandbox defaults documented in DeploymentRuntimeService: read-only
rootfs, non-root UID, dropped caps, seccomp RuntimeDefault, 127.0.0.1-bound publish,
egress deny-by-default.
"""

# Standard
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Protocol


@dataclass
class ResourceLimits:
    """Per-deployment resource caps. Populated from config defaults + per-deploy overrides."""

    cpu: float = 1.0
    memory_mb: int = 512
    pids: int = 128
    disk_mb: int = 512
    build_timeout_s: int = 600


@dataclass
class EgressPolicy:
    """Egress policy. allowlist=None or [] means --network=none."""

    allowlist: Optional[List[str]] = None

    @property
    def is_deny_all(self) -> bool:
        return not self.allowlist


@dataclass
class BuildResult:
    """Outcome of a container image build."""

    image_tag: str
    image_digest: Optional[str]
    build_log_path: str


@dataclass
class ContainerHandle:
    """Handle to a running container. Opaque to callers beyond the fields below."""

    container_id: str
    host_port: int
    image_tag: str


@dataclass
class ContainerStatus:
    """Point-in-time container state."""

    container_id: str
    state: str  # running|exited|restarting|unknown
    exit_code: Optional[int] = None
    health: str = "unknown"  # healthy|unhealthy|unknown
    started_at: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)


class RuntimeDriver(Protocol):
    """Protocol every container runtime driver must implement.

    Drivers are stateless w.r.t. database; the runtime service persists state.
    """

    async def build(self, build_ctx: Path, image_tag: str, limits: ResourceLimits) -> BuildResult:
        """Build an image from a Containerfile in build_ctx. Must stream build log to disk."""
        ...

    async def start(
        self,
        image_tag: str,
        env: Dict[str, str],
        limits: ResourceLimits,
        egress: EgressPolicy,
        host_port: int,
        container_name: str,
    ) -> ContainerHandle:
        """Start a container for an already-built image. host_port MUST be bound to 127.0.0.1."""
        ...

    async def stop(self, handle: ContainerHandle) -> None:
        """Stop and remove the container. Idempotent."""
        ...

    async def logs(self, handle: ContainerHandle, tail: int = 500) -> AsyncIterator[str]:
        """Yield runtime logs as async chunks."""
        ...

    async def status(self, handle: ContainerHandle) -> ContainerStatus:
        """Return the current container state."""
        ...

    async def prune(self, image_tag: str) -> None:
        """Remove the image. Idempotent. Called after the last container is stopped."""
        ...


class DriverError(Exception):
    """Base exception for driver failures."""


class DriverUnavailableError(DriverError):
    """The underlying runtime daemon is unreachable or not configured."""


class BuildFailedError(DriverError):
    """Image build failed. Build log on disk has the details."""
