# -*- coding: utf-8 -*-
"""
Stdio Bridge Manager - spawns and manages translate bridge subprocesses for stdio MCP gateways.

Each stdio MCP gateway gets a translate bridge process that wraps the stdio command
as an HTTP/SSE endpoint. The gateway service then treats it like any other HTTP gateway.
"""

# Standard
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Third-Party
import httpx
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings

logger = logging.getLogger("mcpgateway.stdio_bridge_manager")


@dataclass
class BridgeProcess:
    """Tracks a running translate bridge subprocess."""

    gateway_id: str
    process: asyncio.subprocess.Process
    port: int
    command: str
    args: list[str]
    bridge_url: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    restart_count: int = 0
    status: str = "running"  # running | crashed | stopped


class StdioBridgeManager:
    """Manages translate bridge subprocesses for stdio MCP gateways."""

    def __init__(self) -> None:
        self._bridges: dict[str, BridgeProcess] = {}
        self._used_ports: set[int] = set()
        self._health_check_task: asyncio.Task | None = None
        self._initialized = False

    async def initialize(self, db: Session | None = None) -> None:
        """Start bridge processes for all enabled stdio gateways on app startup."""
        if not settings.mcpgateway_stdio_enabled:
            logger.info("Stdio gateway support is disabled")
            return

        self._initialized = True
        logger.info("Stdio Bridge Manager initialized")

        if db is not None:
            await self._restore_bridges(db)

        # Start periodic health check
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def _restore_bridges(self, db: Session) -> None:
        """Restore bridges for enabled stdio gateways from the database."""
        # First-Party
        from mcpgateway.db import Gateway  # pylint: disable=import-outside-toplevel
        from mcpgateway.utils.services_auth import decode_auth  # pylint: disable=import-outside-toplevel

        gateways = db.query(Gateway).filter(Gateway.stdio_command.isnot(None), Gateway.enabled.is_(True)).all()
        for gw in gateways:
            try:
                env = {}
                if gw.stdio_env:
                    decoded = decode_auth(gw.stdio_env)
                    if decoded:
                        env = decoded
                bridge_url = await self.start_bridge(
                    gateway_id=gw.id,
                    command=gw.stdio_command,
                    args=gw.stdio_args or [],
                    env=env,
                    cwd=gw.stdio_cwd,
                    timeout=gw.stdio_timeout or 60,
                )
                # Update the gateway URL and port in the database
                gw.url = bridge_url
                gw.stdio_bridge_port = self._bridges[gw.id].port
                db.commit()
                logger.info(f"Restored stdio bridge for gateway {gw.name} on {bridge_url}")
            except Exception as e:
                logger.error(f"Failed to restore stdio bridge for gateway {gw.name}: {e}")

    async def start_bridge(
        self,
        gateway_id: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: int = 60,
    ) -> str:
        """Spawn a translate bridge subprocess for a stdio MCP gateway.

        Args:
            gateway_id: Gateway ID this bridge serves
            command: Executable command (e.g., uv, npx)
            args: Command arguments
            env: Environment variables for the subprocess
            cwd: Working directory
            timeout: Startup timeout in seconds

        Returns:
            Bridge URL (e.g., http://127.0.0.1:9101)

        Raises:
            RuntimeError: If max processes exceeded, no ports available, or bridge fails to start
        """
        if not settings.mcpgateway_stdio_enabled:
            raise RuntimeError("Stdio gateway support is disabled")

        if len(self._bridges) >= settings.mcpgateway_stdio_max_processes:
            raise RuntimeError(f"Maximum stdio bridge processes ({settings.mcpgateway_stdio_max_processes}) reached")

        # Stop existing bridge if one is running
        if gateway_id in self._bridges:
            await self.stop_bridge(gateway_id)

        port = self._allocate_port()
        if port is None:
            raise RuntimeError("No available ports in the configured range")

        # Build the stdio command string for translate
        stdio_cmd = command
        if args:
            # Escape individual args that contain spaces
            escaped_args = []
            for arg in args:
                if " " in arg:
                    escaped_args.append(f'"{arg}"')
                else:
                    escaped_args.append(arg)
            stdio_cmd = f"{command} {' '.join(escaped_args)}"

        # Merge host env with user-specified env
        process_env = {**os.environ.copy(), **(env or {})}

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "mcpgateway.translate",
                "--stdio",
                stdio_cmd,
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
                "--expose-streamable-http",
                env=process_env,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            self._used_ports.discard(port)
            raise RuntimeError(f"Failed to spawn translate bridge: {e}") from e

        bridge_url = f"http://127.0.0.1:{port}"

        bridge = BridgeProcess(
            gateway_id=gateway_id,
            process=process,
            port=port,
            command=command,
            args=args or [],
            bridge_url=bridge_url,
        )
        self._bridges[gateway_id] = bridge

        # Wait for the bridge to become healthy
        try:
            await self._wait_for_health(bridge_url, timeout)
        except Exception:
            # Log stderr/stdout from the failed bridge process for diagnostics
            await self._log_process_output(process, gateway_id)
            # If health check fails, clean up
            await self._kill_process(process)
            self._used_ports.discard(port)
            del self._bridges[gateway_id]
            raise

        logger.info(f"Stdio bridge started for gateway {gateway_id} on port {port}: {stdio_cmd}")
        return bridge_url

    async def stop_bridge(self, gateway_id: str) -> None:
        """Stop the bridge process for a gateway."""
        bridge = self._bridges.pop(gateway_id, None)
        if bridge is None:
            return

        bridge.status = "stopped"
        await self._kill_process(bridge.process)
        self._used_ports.discard(bridge.port)
        logger.info(f"Stdio bridge stopped for gateway {gateway_id} (port {bridge.port})")

    async def restart_bridge(self, gateway_id: str, db: Session | None = None) -> str | None:
        """Stop and restart the bridge for a gateway.

        Args:
            gateway_id: Gateway ID to restart
            db: Database session for fetching gateway config

        Returns:
            New bridge URL or None if restart fails
        """
        bridge = self._bridges.get(gateway_id)
        if bridge is None:
            return None

        command = bridge.command
        args = bridge.args
        # We don't have env/cwd stored in the bridge, fetch from DB if available
        env = None
        cwd = None
        timeout = 60

        if db is not None:
            # First-Party
            from mcpgateway.db import Gateway  # pylint: disable=import-outside-toplevel
            from mcpgateway.utils.services_auth import decode_auth  # pylint: disable=import-outside-toplevel

            gw = db.query(Gateway).filter_by(id=gateway_id).first()
            if gw:
                if gw.stdio_env:
                    decoded = decode_auth(gw.stdio_env)
                    if decoded:
                        env = decoded
                cwd = gw.stdio_cwd
                timeout = gw.stdio_timeout or 60

        await self.stop_bridge(gateway_id)
        return await self.start_bridge(gateway_id, command, args, env, cwd, timeout)

    def get_status(self, gateway_id: str) -> dict | None:
        """Get status information for a bridge process."""
        bridge = self._bridges.get(gateway_id)
        if bridge is None:
            return None

        # Check if process is still alive
        if bridge.process.returncode is not None and bridge.status == "running":
            bridge.status = "crashed"

        return {
            "status": bridge.status,
            "port": bridge.port,
            "bridge_url": bridge.bridge_url,
            "command": bridge.command,
            "args": bridge.args,
            "started_at": bridge.started_at.isoformat(),
            "uptime_seconds": (datetime.now(timezone.utc) - bridge.started_at).total_seconds(),
            "restart_count": bridge.restart_count,
            "pid": bridge.process.pid,
        }

    def is_stdio_gateway(self, gateway_id: str) -> bool:
        """Check if a gateway has an active stdio bridge."""
        return gateway_id in self._bridges

    async def shutdown(self) -> None:
        """Stop all bridge processes and the health check task."""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        gateway_ids = list(self._bridges.keys())
        for gw_id in gateway_ids:
            await self.stop_bridge(gw_id)

        self._initialized = False
        logger.info("Stdio Bridge Manager shutdown complete")

    def _allocate_port(self) -> int | None:
        """Find the next available port in the configured range."""
        for port in range(settings.mcpgateway_stdio_port_range_start, settings.mcpgateway_stdio_port_range_end + 1):
            if port not in self._used_ports:
                self._used_ports.add(port)
                return port
        return None

    async def _wait_for_health(self, bridge_url: str, timeout: int) -> None:
        """Poll the bridge's health endpoint until it responds or times out."""
        health_url = f"{bridge_url}/healthz"
        deadline = asyncio.get_event_loop().time() + timeout

        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(health_url, timeout=2.0)
                    if resp.status_code == 200:
                        return
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass
                await asyncio.sleep(0.5)

        raise RuntimeError(f"Stdio bridge at {bridge_url} failed to become healthy within {timeout}s")

    async def _health_check_loop(self) -> None:
        """Periodically check bridge health and restart crashed bridges."""
        while True:
            try:
                await asyncio.sleep(settings.mcpgateway_stdio_health_check_interval)
            except asyncio.CancelledError:
                return

            for gateway_id in list(self._bridges.keys()):
                bridge = self._bridges.get(gateway_id)
                if bridge is None or bridge.status == "stopped":
                    continue

                # Check if process is still alive
                if bridge.process.returncode is not None:
                    bridge.status = "crashed"
                    logger.warning(f"Stdio bridge for gateway {gateway_id} has crashed (exit code {bridge.process.returncode})")
                    await self._log_process_output(bridge.process, gateway_id)

                    if bridge.restart_count < settings.mcpgateway_stdio_restart_max_retries:
                        bridge.restart_count += 1
                        logger.info(f"Attempting restart {bridge.restart_count}/{settings.mcpgateway_stdio_restart_max_retries} for gateway {gateway_id}")
                        try:
                            await self.restart_bridge(gateway_id)
                        except Exception as e:
                            logger.error(f"Failed to restart stdio bridge for gateway {gateway_id}: {e}")
                    else:
                        logger.error(f"Stdio bridge for gateway {gateway_id} exceeded max restart retries")
                    continue

                # HTTP health check
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(f"{bridge.bridge_url}/healthz", timeout=5.0)
                        if resp.status_code != 200:
                            bridge.status = "crashed"
                except Exception:
                    bridge.status = "crashed"

    @staticmethod
    async def _log_process_output(process: asyncio.subprocess.Process, gateway_id: str) -> None:
        """Read and log any available stdout/stderr from a bridge process for diagnostics."""
        try:
            if process.stderr:
                stderr_data = await asyncio.wait_for(process.stderr.read(8192), timeout=2.0)
                if stderr_data:
                    stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
                    for line in stderr_text.splitlines()[:20]:
                        logger.error(f"[STDIO_BRIDGE:{gateway_id}] stderr: {line}")
            if process.stdout:
                stdout_data = await asyncio.wait_for(process.stdout.read(8192), timeout=2.0)
                if stdout_data:
                    stdout_text = stdout_data.decode("utf-8", errors="replace").strip()
                    for line in stdout_text.splitlines()[:20]:
                        logger.warning(f"[STDIO_BRIDGE:{gateway_id}] stdout: {line}")
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Could not read bridge process output for gateway {gateway_id}: {e}")

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """Gracefully terminate a subprocess with SIGTERM, then SIGKILL after timeout."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass


# Module-level singleton
stdio_bridge_manager = StdioBridgeManager()
