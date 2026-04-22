# -*- coding: utf-8 -*-
"""Kubernetes runtime driver (MINIMAL STUB in v1).

Raises DriverUnavailableError on every method. The Docker driver is the
feature-complete v1 choice; this stub exists so MCPGATEWAY_DEPLOY_DRIVER=kubernetes
fails fast at build time with a clear message rather than during startup.

The real implementation will build via a k8s Job (kaniko/buildkit) and run via a
Deployment + ClusterIP Service, then expose the service cluster-internally to the
gateway pod. Until then, operators who need Kubernetes should run the gateway on a
node with a Docker-compatible socket (e.g. Podman) and use the Docker driver.
"""

# Standard
from pathlib import Path
from typing import AsyncIterator, Dict

# First-Party
from mcpgateway.services.deployment.drivers.base import (
    BuildResult,
    ContainerHandle,
    ContainerStatus,
    DriverUnavailableError,
    EgressPolicy,
    ResourceLimits,
    RuntimeDriver,
)


_NOT_IMPLEMENTED_MSG = "KubernetesDriver is a stub in v1; set MCPGATEWAY_DEPLOY_DRIVER=docker"


class KubernetesDriver(RuntimeDriver):
    """Placeholder Kubernetes driver."""

    async def build(self, build_ctx: Path, image_tag: str, limits: ResourceLimits) -> BuildResult:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)

    async def start(
        self,
        image_tag: str,
        env: Dict[str, str],
        limits: ResourceLimits,
        egress: EgressPolicy,
        host_port: int,
        container_name: str,
    ) -> ContainerHandle:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)

    async def stop(self, handle: ContainerHandle) -> None:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)

    async def logs(self, handle: ContainerHandle, tail: int = 500) -> AsyncIterator[str]:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)
        yield ""  # pragma: no cover - satisfy the async-generator return type

    async def status(self, handle: ContainerHandle) -> ContainerStatus:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)

    async def prune(self, image_tag: str) -> None:
        raise DriverUnavailableError(_NOT_IMPLEMENTED_MSG)
