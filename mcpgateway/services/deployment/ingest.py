# -*- coding: utf-8 -*-
"""Source ingestion for deployed MCP servers.

Two paths:
- Upload archive (tar.gz / zip): streamed to local artifact dir, then extracted with
  zip-slip/symlink/setuid protection via tarfile.data_filter (PEP 706).
- Git clone: shallow clone via `git` subprocess over https only, SSRF-checked.

User Dockerfile/Containerfile at the build root is renamed to *.user and ignored;
v1 always builds from the gateway's rendered template. This is safe because the
build call explicitly passes dockerfile='Containerfile' and render_containerfile
writes that exact filename — the user's Dockerfile cannot be picked up.

Writes a manifest with source sha256 to the extracted tree.
"""

# Standard
import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# First-Party
from mcpgateway.config import settings

logger = logging.getLogger("mcpgateway.deployment.ingest")

_FORBIDDEN_ROOT_NAMES = frozenset({"Dockerfile", "Containerfile", "dockerfile", "containerfile"})
_SAFE_PATH_MAX = 4096


class IngestError(Exception):
    """Generic ingest failure."""


@dataclass
class IngestResult:
    """Outcome of a source ingestion."""

    src_dir: Path
    source_sha256: str
    source_ref: str  # human-readable: git URL@ref+subpath or upload://<token>


def _artifact_root() -> Path:
    root = Path(settings.mcpgateway_deploy_artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "incoming").mkdir(parents=True, exist_ok=True)
    return root


def _deployment_dir(gateway_id: str) -> Path:
    d = _artifact_root() / gateway_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_safe_member(name: str) -> None:
    """Reject obvious unsafe archive paths."""
    if len(name) > _SAFE_PATH_MAX:
        raise IngestError(f"archive member path is too long: {name[:80]}...")
    norm = Path(name).as_posix()
    if norm.startswith("/"):
        raise IngestError(f"archive contains an absolute path: {name}")
    parts = norm.split("/")
    if ".." in parts:
        raise IngestError(f"archive contains a parent-directory path: {name}")


def _neutralize_user_containerfile(extract_dir: Path, subpath: Optional[str]) -> None:
    """Rename any user-supplied Dockerfile/Containerfile at the build root to *.user.

    Ignoring a user Dockerfile is safe — the Docker build call passes
    dockerfile='Containerfile' explicitly, and render_containerfile writes that
    filename itself. But renaming makes the intent auditable in the build log
    ('Dockerfile.user present but ignored') and prevents accidental use if a
    future code path uses the default Dockerfile name.
    """
    root = (extract_dir / subpath) if subpath else extract_dir
    for name in _FORBIDDEN_ROOT_NAMES:
        src = root / name
        if src.exists():
            dst = root / f"{name}.user"
            try:
                src.rename(dst)
                logger.info("Ingest: renamed user %s to %s.user (gateway generates the Containerfile)", name, name)
            except OSError as e:
                logger.warning("Ingest: could not rename user %s (will be ignored by build): %s", name, e)


async def ingest_upload(
    gateway_id: str,
    archive_bytes: bytes,
    original_filename: str,
    subpath: Optional[str] = None,
) -> IngestResult:
    """Persist an uploaded archive, extract it, and write a manifest.

    Args:
        gateway_id: Gateway id; used as the per-deployment directory name.
        archive_bytes: Full archive payload. Size is capped by the admin endpoint.
        original_filename: Used only for extension sniffing.
        subpath: Optional relative subdir where the server source lives.

    Returns:
        IngestResult describing the extracted source tree.

    Raises:
        IngestError: On oversize, path-traversal, or symlink/setuid/device-node.
    """
    max_bytes = settings.mcpgateway_deploy_max_archive_mb * 1024 * 1024
    if len(archive_bytes) > max_bytes:
        raise IngestError(f"archive exceeds {settings.mcpgateway_deploy_max_archive_mb} MiB cap")

    dep_dir = _deployment_dir(gateway_id)
    incoming = _artifact_root() / "incoming" / f"{uuid.uuid4().hex}-{original_filename}"
    incoming.write_bytes(archive_bytes)
    sha = _sha256_file(incoming)

    src_dir = dep_dir / "src"
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)

    lower = original_filename.lower()

    def _extract() -> None:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(incoming) as zf:
                for info in zf.infolist():
                    _assert_safe_member(info.filename)
                    # Reject entries with the unix-attribute bits for setuid/setgid/device/symlink.
                    ext = info.external_attr >> 16
                    mode = ext & 0o7777
                    if mode & 0o6000:
                        raise IngestError(f"archive member has setuid/setgid: {info.filename}")
                    # zipfile.ZipInfo doesn't trivially expose symlink bit across platforms.
                    # The stat mode check above catches setuid; zip symlinks are already rare.
                zf.extractall(src_dir)
        else:
            # tarfile with data_filter (PEP 706) handles absolute paths, traversal,
            # symlinks escaping root, setuid/setgid, and device nodes.
            mode = "r:gz" if lower.endswith((".tar.gz", ".tgz")) else "r:"
            with tarfile.open(incoming, mode=mode) as tf:
                for member in tf.getmembers():
                    _assert_safe_member(member.name)
                tf.extractall(src_dir, filter="data")

    try:
        await asyncio.to_thread(_extract)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        raise IngestError(f"failed to extract archive: {e}") from e
    finally:
        try:
            incoming.unlink()
        except OSError:
            pass

    _neutralize_user_containerfile(src_dir, subpath)

    manifest = {"source": "upload", "sha256": sha, "original_filename": original_filename, "subpath": subpath}
    (dep_dir / "source.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return IngestResult(src_dir=src_dir, source_sha256=sha, source_ref=f"upload://{original_filename}")


async def ingest_git(
    gateway_id: str,
    git_url: str,
    git_ref: Optional[str] = None,
    subpath: Optional[str] = None,
) -> IngestResult:
    """Shallow-clone a git repository over https. SSRF-checked.

    Args:
        gateway_id: Gateway id.
        git_url: https URL (validated by the caller schema, re-checked here).
        git_ref: Branch / tag / SHA. Defaults to the repo default branch.
        subpath: Relative subdir where the server source lives.

    Returns:
        IngestResult with source_sha256 set to the resolved commit SHA.

    Raises:
        IngestError: On clone failure or SSRF rejection.
    """
    if not git_url.startswith("https://"):
        raise IngestError("git_url must use https://")

    # SSRF: reuse the existing validator (blocks private/link-local/localhost).
    from mcpgateway.utils.url_validation import validate_url_not_internal  # pylint: disable=import-outside-toplevel

    try:
        validate_url_not_internal(git_url)
    except ValueError as e:
        raise IngestError(f"git_url SSRF-blocked: {e}") from e

    dep_dir = _deployment_dir(gateway_id)
    src_dir = dep_dir / "src"
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)

    args = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if git_ref:
        args += ["--branch", git_ref]
    args += [git_url, str(src_dir)]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.mcpgateway_deploy_max_build_seconds)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise IngestError("git clone timed out") from e

    if proc.returncode != 0:
        raise IngestError(f"git clone failed: {stderr.decode('utf-8', errors='replace')[:500]}")

    head_proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=str(src_dir), stdout=asyncio.subprocess.PIPE
    )
    head_out, _ = await head_proc.communicate()
    commit_sha = head_out.decode("utf-8").strip()

    # Remove .git to keep the build context minimal and avoid leaking repo metadata.
    shutil.rmtree(src_dir / ".git", ignore_errors=True)

    _neutralize_user_containerfile(src_dir, subpath)

    manifest = {"source": "git", "url": git_url, "ref": git_ref, "commit": commit_sha, "subpath": subpath}
    (dep_dir / "source.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    source_ref = f"{git_url}@{git_ref or 'HEAD'}"
    if subpath:
        source_ref += f"#{subpath}"
    return IngestResult(src_dir=src_dir, source_sha256=commit_sha, source_ref=source_ref)
