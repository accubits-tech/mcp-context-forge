# -*- coding: utf-8 -*-
"""Deployment runtime service.

Peer to StdioBridgeManager, but for gateway-hosted MCP servers that run as
isolated containers. Owns:

- Build orchestration (ingest -> render Containerfile -> driver.build).
- Container lifecycle (start/stop/status/logs/prune) + 127.0.0.1 port allocation.
- Per-team quota enforcement + concurrent-build semaphore.
- Background health loop with exponential-backoff restart.
- Secret injection (AES decode at start time, never persisted plaintext).

Gateway rows produced by deploy() are identical to HTTP gateways in shape:
transport='STREAMABLEHTTP', url='http://127.0.0.1:<port>/mcp'. The existing
proxy/discovery path then takes over with no changes.
"""

# Standard
import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Third-Party
import httpx
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.deployment import ingest
from mcpgateway.services.deployment.drivers.base import (
    ContainerHandle,
    DriverUnavailableError,
    EgressPolicy,
    ResourceLimits,
    RuntimeDriver,
)
from mcpgateway.services.deployment.templates import render_containerfile
from mcpgateway.utils.services_auth import decode_auth

logger = logging.getLogger("mcpgateway.deployment_runtime_service")


@dataclass
class DeploymentRecord:
    """In-process state for one running deployment."""

    gateway_id: str
    image_tag: str
    host_port: int
    handle: ContainerHandle
    build_log_path: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    restart_count: int = 0
    status: str = "running"  # running | crashed | stopped


def _select_driver() -> RuntimeDriver:
    """Instantiate the driver configured in settings."""
    name = settings.mcpgateway_deploy_driver
    if name in ("docker", "podman"):
        from mcpgateway.services.deployment.drivers.docker_driver import DockerDriver  # pylint: disable=import-outside-toplevel

        return DockerDriver()
    if name == "kubernetes":
        from mcpgateway.services.deployment.drivers.kubernetes_driver import KubernetesDriver  # pylint: disable=import-outside-toplevel

        return KubernetesDriver()
    raise DriverUnavailableError(f"unknown deploy driver: {name}")


class DeploymentRuntimeService:
    """Manages the image + container lifecycle for deployed MCP servers."""

    def __init__(self) -> None:
        self._records: Dict[str, DeploymentRecord] = {}
        self._used_ports: set[int] = set()
        self._build_semaphore: Optional[asyncio.Semaphore] = None
        self._health_check_task: Optional[asyncio.Task[None]] = None
        self._initialized = False
        self._driver: Optional[RuntimeDriver] = None

    def driver(self) -> RuntimeDriver:
        if self._driver is None:
            self._driver = _select_driver()
        return self._driver

    async def initialize(self, db: Optional[Session] = None) -> None:
        """Start the health loop and restore any previously-running deployments from the DB."""
        if not settings.mcpgateway_deploy_enabled:
            logger.info("Deployed MCP server support is disabled")
            return
        self._build_semaphore = asyncio.Semaphore(settings.mcpgateway_deploy_max_concurrent_builds)
        self._initialized = True
        logger.info("Deployment Runtime Service initialized (driver=%s)", settings.mcpgateway_deploy_driver)
        if db is not None:
            await self._restore(db)
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def shutdown(self) -> None:
        """Stop the health loop and all containers."""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        for gw_id in list(self._records.keys()):
            try:
                await self._stop_record(gw_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("shutdown: stop %s raised: %s", gw_id, e)
        self._initialized = False
        logger.info("Deployment Runtime Service shutdown complete")

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    async def deploy(
        self,
        gateway_id: str,
        *,
        source: str,
        source_ref: Optional[str] = None,
        git_url: Optional[str] = None,
        git_ref: Optional[str] = None,
        subpath: Optional[str] = None,
        archive_bytes: Optional[bytes] = None,
        archive_filename: Optional[str] = None,
        runtime: str,
        entry_mode: str,
        entry_command: str,
        env: Optional[Dict[str, str]] = None,
        resource_limits: Optional[Dict[str, Any]] = None,
        egress_allowlist: Optional[list[str]] = None,
        deploy_token: Optional[str] = None,
    ) -> tuple[str, int, str, str, str]:
        """Ingest source, build image, start container.

        Returns:
            (image_tag, host_port, source_sha256, source_ref_rendered, build_log_path)

        Raises:
            DriverUnavailableError, BuildFailedError, ingest.IngestError, RuntimeError
        """
        if not settings.mcpgateway_deploy_enabled:
            raise RuntimeError("Deployed MCP server support is disabled")
        if len(self._records) >= settings.mcpgateway_deploy_max_per_team * 100:  # global guard; per-team check in caller
            raise RuntimeError("Maximum deployments reached")

        # 1) Ingest source.
        if source == "upload":
            if not archive_bytes or not archive_filename:
                raise ValueError("upload source requires archive_bytes and archive_filename")
            ingest_result = await ingest.ingest_upload(gateway_id, archive_bytes, archive_filename, subpath=subpath)
        elif source == "git":
            if not git_url:
                raise ValueError("git source requires git_url")
            ingest_result = await ingest.ingest_git(gateway_id, git_url, git_ref=git_ref, subpath=subpath)
        else:
            raise ValueError(f"unknown source: {source}")

        # 2) Render Containerfile into the build context (the ingested src_dir).
        build_ctx = ingest_result.src_dir
        if subpath:
            build_ctx = build_ctx / subpath
        render_containerfile(runtime=runtime, entry_mode=entry_mode, entry_command=entry_command, build_ctx=build_ctx)

        # 3) Build the image.
        limits = self._limits(resource_limits)
        image_tag = f"mcpgateway-deploy/{gateway_id}:{ingest_result.source_sha256[:12]}"
        assert self._build_semaphore is not None
        async with self._build_semaphore:
            build_result = await self.driver().build(build_ctx, image_tag, limits)

        # 4) Allocate a 127.0.0.1 host port and start the container.
        port = self._allocate_port()
        if port is None:
            raise RuntimeError("no available ports in the configured deploy range")

        runtime_env = dict(env or {})
        # Inject a per-deployment bearer token that the server can read.
        runtime_env["MCP_DEPLOY_TOKEN"] = deploy_token or secrets.token_urlsafe(32)

        egress = EgressPolicy(allowlist=egress_allowlist or None)
        container_name = f"mcpdeploy-{gateway_id[:12]}-{uuid.uuid4().hex[:6]}"
        try:
            handle = await self.driver().start(
                image_tag=image_tag,
                env=runtime_env,
                limits=limits,
                egress=egress,
                host_port=port,
                container_name=container_name,
            )
        except Exception:
            self._used_ports.discard(port)
            raise

        # 5) Wait for the container's HTTP surface to become healthy. 60s is plenty —
        # by this point the image is built and the container has been started; we're
        # only waiting for the in-container server to bind its port. Capping at 60s
        # means a misconfigured deployment fails fast instead of blocking for 10min.
        bridge_url = f"http://127.0.0.1:{port}"
        try:
            await self._wait_for_health(bridge_url, timeout=60)
        except Exception:
            try:
                await self.driver().stop(handle)
            finally:
                self._used_ports.discard(port)
            raise

        self._records[gateway_id] = DeploymentRecord(
            gateway_id=gateway_id,
            image_tag=image_tag,
            host_port=port,
            handle=handle,
            build_log_path=build_result.build_log_path,
        )
        logger.info("Deployment started for gateway %s on port %d (image=%s)", gateway_id, port, image_tag)
        return image_tag, port, ingest_result.source_sha256, ingest_result.source_ref, build_result.build_log_path

    async def stop(self, gateway_id: str) -> None:
        """Stop the container for a deployment. Does not remove the image."""
        await self._stop_record(gateway_id)

    async def restart(self, gateway_id: str, db: Optional[Session] = None) -> Optional[int]:
        """Restart the container for a deployment. Returns the new host port or None."""
        record = self._records.get(gateway_id)
        if record is None:
            return None
        image_tag = record.image_tag
        await self._stop_record(gateway_id)
        env = {}
        if db is not None:
            # First-Party
            from mcpgateway.db import Gateway  # pylint: disable=import-outside-toplevel

            gw = db.query(Gateway).filter_by(id=gateway_id).first()
            if gw and gw.deployment_env_encrypted:
                decoded = decode_auth(gw.deployment_env_encrypted) or {}
                env = decoded
        port = self._allocate_port()
        if port is None:
            raise RuntimeError("no available ports in the configured deploy range")
        limits = self._limits(None)
        runtime_env = dict(env)
        runtime_env.setdefault("MCP_DEPLOY_TOKEN", secrets.token_urlsafe(32))
        handle = await self.driver().start(
            image_tag=image_tag,
            env=runtime_env,
            limits=limits,
            egress=EgressPolicy(allowlist=None),
            host_port=port,
            container_name=f"mcpdeploy-{gateway_id[:12]}-{uuid.uuid4().hex[:6]}",
        )
        self._records[gateway_id] = DeploymentRecord(
            gateway_id=gateway_id,
            image_tag=image_tag,
            host_port=port,
            handle=handle,
            build_log_path=record.build_log_path,
        )
        return port

    async def delete(self, gateway_id: str, image_tag: Optional[str] = None) -> None:
        """Stop container + remove image + delete artifact dir. Idempotent."""
        record = self._records.get(gateway_id)
        if record is not None:
            await self._stop_record(gateway_id)
            image_tag = image_tag or record.image_tag
        if image_tag:
            try:
                await self.driver().prune(image_tag)
            except Exception as e:  # noqa: BLE001
                logger.warning("delete: prune %s raised: %s", image_tag, e)
        art = Path(settings.mcpgateway_deploy_artifact_dir) / gateway_id
        if art.exists():
            import shutil  # pylint: disable=import-outside-toplevel

            shutil.rmtree(art, ignore_errors=True)

    def status(self, gateway_id: str) -> Optional[Dict[str, Any]]:
        """Synchronous snapshot of in-process state."""
        record = self._records.get(gateway_id)
        if record is None:
            return None
        return {
            "status": record.status,
            "host_port": record.host_port,
            "image_tag": record.image_tag,
            "started_at": record.started_at.isoformat(),
            "uptime_seconds": (datetime.now(timezone.utc) - record.started_at).total_seconds(),
            "restart_count": record.restart_count,
            "container_id": record.handle.container_id,
        }

    async def logs(self, gateway_id: str, tail: int = 500) -> str:
        """Return recent container logs as a single string (SSE wrapper converts this)."""
        record = self._records.get(gateway_id)
        if record is None:
            return ""
        chunks: list[str] = []
        async for c in self.driver().logs(record.handle, tail=tail):
            chunks.append(c)
        return "".join(chunks)

    async def build_log(self, gateway_id: str, tail_bytes: int = 65536) -> str:
        """Return the last tail_bytes of the build log, if present."""
        record = self._records.get(gateway_id)
        if record is None:
            # Fall back to the on-disk location.
            candidate = Path(settings.mcpgateway_deploy_artifact_dir) / gateway_id / "src" / "build.log"
            if not candidate.exists():
                return ""
            path = candidate
        else:
            path = Path(record.build_log_path)
            if not path.exists():
                return ""
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            return f.read().decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _limits(self, override: Optional[Dict[str, Any]]) -> ResourceLimits:
        override = override or {}
        return ResourceLimits(
            cpu=float(override.get("cpu", settings.mcpgateway_deploy_cpu_limit)),
            memory_mb=int(override.get("memory_mb", settings.mcpgateway_deploy_memory_mb)),
            pids=int(override.get("pids", settings.mcpgateway_deploy_pids_limit)),
            disk_mb=int(override.get("disk_mb", settings.mcpgateway_deploy_disk_mb)),
            build_timeout_s=int(override.get("build_timeout_s", settings.mcpgateway_deploy_max_build_seconds)),
        )

    def _allocate_port(self) -> Optional[int]:
        for port in range(settings.mcpgateway_deploy_port_range_start, settings.mcpgateway_deploy_port_range_end + 1):
            if port not in self._used_ports:
                self._used_ports.add(port)
                return port
        return None

    async def _stop_record(self, gateway_id: str) -> None:
        record = self._records.pop(gateway_id, None)
        if record is None:
            return
        record.status = "stopped"
        try:
            await self.driver().stop(record.handle)
        finally:
            self._used_ports.discard(record.host_port)

    async def _wait_for_health(self, bridge_url: str, timeout: int) -> None:
        """Poll the container's health surface until responsive or timeout.

        Catches httpx.HTTPError broadly because the container may accept a
        TCP connection then close it mid-handshake as it's still starting up,
        which httpcore surfaces as RemoteProtocolError rather than
        ConnectError. Any HTTP-level failure is just "not ready yet".
        """
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                for path in ("/healthz", "/mcp", "/"):
                    try:
                        resp = await client.get(f"{bridge_url}{path}", timeout=2.0)
                        # 2xx, 3xx, or even 401/404 means the server is up and HTTP-speaking.
                        if resp.status_code < 500:
                            return
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(0.5)
        raise RuntimeError(f"deployed container at {bridge_url} failed to become healthy within {timeout}s")

    async def _health_check_loop(self) -> None:
        """Restart crashed deployments up to the configured retry cap."""
        interval = settings.mcpgateway_deploy_health_check_interval
        max_retries = settings.mcpgateway_deploy_restart_max_retries
        while True:
            try:
                await asyncio.sleep(interval)
                for gw_id, record in list(self._records.items()):
                    try:
                        status = await self.driver().status(record.handle)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("health: status %s raised: %s", gw_id, e)
                        continue
                    if status.state in ("exited", "unknown") and record.restart_count < max_retries:
                        record.restart_count += 1
                        logger.warning("health: restarting %s (attempt %d)", gw_id, record.restart_count)
                        try:
                            await self.restart(gw_id)
                        except Exception as e:  # noqa: BLE001
                            logger.error("health: restart %s failed: %s", gw_id, e)
                            record.status = "crashed"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("health loop iteration raised: %s", e)

    async def _restore(self, db: Session) -> None:
        """Best-effort: reattach to already-running containers recorded in the DB.

        In v1 we do NOT auto-start containers that were stopped; admins re-enable them
        via the restart endpoint. This avoids surprising behavior after a gateway restart.
        """
        # First-Party
        from mcpgateway.db import Gateway  # pylint: disable=import-outside-toplevel

        rows = (
            db.query(Gateway)
            .filter(Gateway.deployment_source.isnot(None), Gateway.enabled.is_(True))
            .all()
        )
        for gw in rows:
            if not (gw.deployment_container_id and gw.deployment_image_tag and gw.deployment_host_port):
                continue
            handle = ContainerHandle(container_id=gw.deployment_container_id, host_port=gw.deployment_host_port, image_tag=gw.deployment_image_tag)
            try:
                status = await self.driver().status(handle)
            except Exception as e:  # noqa: BLE001
                logger.info("restore: could not inspect %s: %s", gw.id, e)
                continue
            if status.state == "running":
                self._records[gw.id] = DeploymentRecord(
                    gateway_id=gw.id,
                    image_tag=gw.deployment_image_tag,
                    host_port=gw.deployment_host_port,
                    handle=handle,
                    build_log_path=gw.deployment_build_log_ref or "",
                )
                self._used_ports.add(gw.deployment_host_port)
                logger.info("restore: reattached to %s on port %d", gw.id, gw.deployment_host_port)


# Module-level singleton, mirroring stdio_bridge_manager's pattern.
deployment_runtime_service = DeploymentRuntimeService()
