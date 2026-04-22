# -*- coding: utf-8 -*-
"""Docker / Podman (via Docker-compatible socket) runtime driver.

Enforces the deployment sandbox defaults: read-only rootfs, 127.0.0.1-only port
publish, dropped caps, seccomp RuntimeDefault, tmpfs /tmp, no privileged, no host
net/pid/ipc, no docker-sock mount, user 10001, pids and memory limits. Network
defaults to none; an egress proxy sidecar (Envoy) is attached when an allowlist
is provided (sidecar launch is the runtime service's responsibility, not the
driver's).
"""

# Standard
import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict

# First-Party
from mcpgateway.services.deployment.drivers.base import (
    BuildFailedError,
    BuildResult,
    ContainerHandle,
    ContainerStatus,
    DriverUnavailableError,
    EgressPolicy,
    ResourceLimits,
    RuntimeDriver,
)

logger = logging.getLogger("mcpgateway.deployment.docker_driver")

_CONTAINER_PORT = 8080  # Rendered templates always listen on 8080 inside the container.


class DockerDriver(RuntimeDriver):
    """Docker / Podman driver using the docker-py SDK.

    The `docker` package (optional extra 'deploy') must be installed; import is
    lazy so gateways without this feature enabled never need the dependency.
    """

    def __init__(self) -> None:
        self._client: Any = None  # docker.DockerClient, lazily created

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker  # pylint: disable=import-outside-toplevel
        except ImportError as e:  # pragma: no cover
            raise DriverUnavailableError("docker SDK is not installed; install with 'pip install mcp-contextforge-gateway[deploy]'") from e
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as e:  # noqa: BLE001 - rewrap any daemon-connection error
            raise DriverUnavailableError(f"Docker daemon is unreachable: {e}") from e
        return self._client

    async def build(self, build_ctx: Path, image_tag: str, limits: ResourceLimits) -> BuildResult:
        """Build an image from the Containerfile in build_ctx. Streams log to build.log."""
        client = self._get_client()
        build_log_path = build_ctx / "build.log"

        def _do_build() -> BuildResult:
            log_fh = build_log_path.open("w", encoding="utf-8")
            try:
                image = None
                log_stream = client.api.build(
                    path=str(build_ctx),
                    dockerfile="Containerfile",
                    tag=image_tag,
                    rm=True,
                    forcerm=True,
                    pull=True,
                    decode=True,
                    timeout=limits.build_timeout_s,
                    network_mode="none",  # Build must not reach the network except via base-image pull
                )
                for chunk in log_stream:
                    if "stream" in chunk:
                        log_fh.write(chunk["stream"])
                        log_fh.flush()
                    elif "error" in chunk:
                        log_fh.write(chunk["error"])
                        log_fh.flush()
                        raise BuildFailedError(chunk["error"])
                image = client.images.get(image_tag)
                digest = None
                if image.attrs.get("RepoDigests"):
                    digest = image.attrs["RepoDigests"][0].split("@", 1)[-1]
                return BuildResult(image_tag=image_tag, image_digest=digest, build_log_path=str(build_log_path))
            finally:
                log_fh.close()

        try:
            return await asyncio.to_thread(_do_build)
        except BuildFailedError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BuildFailedError(f"image build failed: {e}") from e

    async def start(
        self,
        image_tag: str,
        env: Dict[str, str],
        limits: ResourceLimits,
        egress: EgressPolicy,
        host_port: int,
        container_name: str,
    ) -> ContainerHandle:
        """Start a container with the full sandbox defaults applied."""
        client = self._get_client()

        # Network: deny by default. If an allowlist is configured, the runtime
        # service is responsible for attaching an egress-proxy sidecar and passing
        # its network name via egress.allowlist[0] (see DeploymentRuntimeService).
        # The driver itself always uses 'none' for the deployed container.
        network_mode = "none" if egress.is_deny_all else None
        network = None if egress.is_deny_all else f"mcpdeploy-egress-{container_name}"

        def _do_start() -> ContainerHandle:
            container = client.containers.run(
                image=image_tag,
                name=container_name,
                detach=True,
                environment=env,
                ports={f"{_CONTAINER_PORT}/tcp": ("127.0.0.1", host_port)},
                read_only=True,
                tmpfs={"/tmp": "size=64m,mode=1777,noexec,nosuid,nodev"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges", "seccomp=default"],  # seccomp=default == RuntimeDefault
                user="10001:10001",
                mem_limit=f"{limits.memory_mb}m",
                nano_cpus=int(limits.cpu * 1_000_000_000),
                pids_limit=limits.pids,
                network_mode=network_mode,
                network=network,
                privileged=False,
                ipc_mode="private",
                pid_mode=None,  # default (container-private)
                restart_policy={"Name": "no"},  # runtime service decides restarts
                labels={"mcpgateway.managed": "true", "mcpgateway.role": "deployed-mcp-server"},
            )
            return ContainerHandle(container_id=container.id, host_port=host_port, image_tag=image_tag)

        return await asyncio.to_thread(_do_start)

    async def stop(self, handle: ContainerHandle) -> None:
        """Stop and remove the container. Idempotent."""
        client = self._get_client()

        def _do_stop() -> None:
            try:
                container = client.containers.get(handle.container_id)
            except Exception:  # noqa: BLE001 - NotFound variants across docker-py versions
                return
            try:
                container.stop(timeout=10)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop: container stop raised (will force remove): %s", e)
            try:
                container.remove(force=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("stop: container remove raised: %s", e)

        await asyncio.to_thread(_do_stop)

    async def logs(self, handle: ContainerHandle, tail: int = 500) -> AsyncIterator[str]:
        """Yield recent runtime logs (non-streaming snapshot)."""
        client = self._get_client()

        def _fetch() -> bytes:
            container = client.containers.get(handle.container_id)
            return container.logs(tail=tail, stdout=True, stderr=True)

        raw = await asyncio.to_thread(_fetch)
        yield raw.decode("utf-8", errors="replace")

    async def status(self, handle: ContainerHandle) -> ContainerStatus:
        """Report the current container state."""
        client = self._get_client()

        def _inspect() -> ContainerStatus:
            try:
                container = client.containers.get(handle.container_id)
            except Exception:  # noqa: BLE001
                return ContainerStatus(container_id=handle.container_id, state="unknown")
            state = container.attrs.get("State", {}) or {}
            health_raw = (state.get("Health") or {}).get("Status")
            health = {"healthy": "healthy", "unhealthy": "unhealthy"}.get(health_raw or "", "unknown")
            return ContainerStatus(
                container_id=handle.container_id,
                state=state.get("Status", "unknown"),
                exit_code=state.get("ExitCode"),
                health=health,
                started_at=state.get("StartedAt"),
            )

        return await asyncio.to_thread(_inspect)

    async def prune(self, image_tag: str) -> None:
        """Remove the image. Idempotent."""
        client = self._get_client()

        def _do_prune() -> None:
            try:
                client.images.remove(image=image_tag, force=True)
            except Exception as e:  # noqa: BLE001
                logger.info("prune: image remove raised (likely already gone): %s", e)

        await asyncio.to_thread(_do_prune)
