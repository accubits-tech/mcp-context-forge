# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/admin/test_deploy_sample_template.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for GET /admin/gateways/deploy/sample-template — the endpoint that
streams a ready-to-deploy sample MCP server zip for the requested runtime.
"""

# Standard
import io
from unittest.mock import patch
import zipfile

# Third-Party
from fastapi.responses import JSONResponse, StreamingResponse
import pytest

# First-Party
from mcpgateway.admin import admin_deploy_sample_template


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,expected_member",
    [
        ("python", "pyproject.toml"),
        ("node", "package.json"),
    ],
)
async def test_returns_zip_for_supported_runtime(runtime, expected_member):
    """Both supported runtimes return a streamed zip whose contents include the runtime's mandatory file."""
    with patch("mcpgateway.config.settings.mcpgateway_deploy_enabled", True):
        result = await admin_deploy_sample_template(runtime=runtime, user="test-user")

    assert isinstance(result, StreamingResponse)
    assert result.media_type == "application/zip"
    assert f'filename="mcp-sample-template-{runtime}.zip"' in result.headers["content-disposition"]

    body = b"".join([chunk async for chunk in result.body_iterator])
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
    assert expected_member in names, f"expected {expected_member!r} in zip, got {names!r}"
    assert "README.md" in names


@pytest.mark.asyncio
async def test_unknown_runtime_returns_400():
    with patch("mcpgateway.config.settings.mcpgateway_deploy_enabled", True):
        result = await admin_deploy_sample_template(runtime="ruby", user="test-user")

    assert isinstance(result, JSONResponse)
    assert result.status_code == 400
    assert b"unknown runtime" in result.body


@pytest.mark.asyncio
async def test_disabled_feature_returns_403():
    with patch("mcpgateway.config.settings.mcpgateway_deploy_enabled", False):
        result = await admin_deploy_sample_template(runtime="python", user="test-user")

    assert isinstance(result, JSONResponse)
    assert result.status_code == 403
    assert b"disabled" in result.body
