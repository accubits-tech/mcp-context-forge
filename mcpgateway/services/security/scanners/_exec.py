# -*- coding: utf-8 -*-
"""Helper for invoking a scanner inside a sandboxed one-shot container.

Wraps the driver's ``run_oneshot`` so the scanner wrappers don't repeat the
mount/timeout boilerplate. Source trees are mounted read-only at /src; image
scanners that need access to the docker socket (e.g. trivy) pass mounts
explicitly.
"""

# Standard
import logging
from pathlib import Path
from typing import Callable, List, Optional

# First-Party
from mcpgateway.services.deployment.drivers.base import OneshotMount, OneshotResult, RuntimeDriver
from mcpgateway.services.security.errors import ScannerExecutionError, ScannerTimeoutError

logger = logging.getLogger("mcpgateway.security.scanner_exec")

# Path inside the scanner container where the source tree is mounted.
SCAN_SRC_PATH = "/src"


async def run_scanner(
    *,
    driver_factory: Callable[[], RuntimeDriver],
    image: str,
    args: List[str],
    src_dir: Optional[Path] = None,
    extra_mounts: Optional[List[OneshotMount]] = None,
    timeout_s: int,
    network_none: bool = True,
    workdir: Optional[str] = None,
) -> OneshotResult:
    """Run a scanner container and return the captured result.

    Raises:
        ScannerTimeoutError: per-stage timeout exceeded.
        ScannerExecutionError: scanner could not be started or returned a fatal error.
    """
    mounts: List[OneshotMount] = []
    if src_dir is not None:
        mounts.append(OneshotMount(host_path=src_dir, container_path=SCAN_SRC_PATH, read_only=True))
    if extra_mounts:
        mounts.extend(extra_mounts)

    try:
        driver = driver_factory()
        result = await driver.run_oneshot(
            image=image,
            args=args,
            mounts=mounts,
            timeout_s=timeout_s,
            network_none=network_none,
            workdir=workdir,
        )
    except Exception as e:  # noqa: BLE001 - rewrap any driver-level failure
        raise ScannerExecutionError(f"scanner {image} failed to start: {e}") from e

    if result.timed_out:
        raise ScannerTimeoutError(f"scanner {image} exceeded timeout of {timeout_s}s")
    return result
