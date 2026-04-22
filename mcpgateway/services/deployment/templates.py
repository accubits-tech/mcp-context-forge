# -*- coding: utf-8 -*-
"""Jinja rendering for the gateway-generated Containerfile.

Writes a file named 'Containerfile' into the build context. The render path is
the only way a Containerfile ever enters a build context; user-supplied
Dockerfiles are rejected at ingest time.
"""

# Standard
import shlex
from pathlib import Path
from typing import List

# Third-Party
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

# First-Party
from mcpgateway.config import settings

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "deployment" / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=(), default_for_string=False),  # not HTML
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _argv(command: str) -> List[str]:
    """Split an entry command into an argv list for CMD JSON form (exec form)."""
    if not command:
        raise ValueError("entry_command must not be empty")
    return shlex.split(command)


def render_containerfile(
    runtime: str,
    entry_mode: str,
    entry_command: str,
    build_ctx: Path,
) -> Path:
    """Render the Containerfile for a deployment into build_ctx/Containerfile.

    Args:
        runtime: 'python' or 'node'.
        entry_mode: 'stdio' or 'http'.
        entry_command: User's command. For stdio mode it is the MCP server command
            wrapped by mcpgateway.translate; for http mode it is the server
            entrypoint (it must listen on $PORT=8080).
        build_ctx: Directory that will be docker-build's context (already contains
            the user source). The Containerfile is written here.

    Returns:
        Path to the written Containerfile.

    Raises:
        ValueError: On unknown runtime/entry_mode or empty command.
    """
    if runtime not in ("python", "node"):
        raise ValueError(f"unknown runtime: {runtime}")
    if entry_mode not in ("stdio", "http"):
        raise ValueError(f"unknown entry_mode: {entry_mode}")

    base_image = (
        settings.mcpgateway_deploy_base_image_python if runtime == "python" else settings.mcpgateway_deploy_base_image_node
    )
    tpl_name = f"{runtime}.Containerfile.jinja"
    tpl = _env().get_template(tpl_name)
    rendered = tpl.render(
        base_image=base_image,
        entry_mode=entry_mode,
        entry_command=entry_command,
        entry_command_argv=_argv(entry_command),
    )
    out = build_ctx / "Containerfile"
    out.write_text(rendered, encoding="utf-8")
    return out
