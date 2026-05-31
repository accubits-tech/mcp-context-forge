# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_catalog_slack.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Hermetic catalog->events wiring tests (Slack).

These exercise the OAuth-without-credentials skip-initialization path of
``CatalogService.register_catalog_server`` so NO live network / upstream call
is ever attempted. They assert that a catalog entry carrying a non-secret
``events`` block is persisted onto the Gateway row as ``capabilities.events``
(per FRD §5.2) and that an inbound webhook signing secret supplied at
registration time is stored ONLY in the encrypted ``webhook_signing_secret``
column (never echoed, never written under ``capabilities``).
"""

# Standard
from unittest.mock import AsyncMock, MagicMock, patch

# Third-Party
import pytest

# First-Party
from mcpgateway.schemas import CatalogServerRegisterRequest
from mcpgateway.services.catalog_service import CatalogService
from mcpgateway.utils.services_auth import decode_auth


@pytest.fixture
def service():
    return CatalogService()


def _slack_catalog_entry():
    """A Slack catalog entry (OAuth2.1) carrying a non-secret events block."""
    return {
        "id": "slack",
        "name": "Slack",
        "url": "https://mcp.slack.com/mcp",
        "description": "Slack MCP server",
        "auth_type": "OAuth2.1",
        "oauth_config": {
            "authorize_url": "https://slack.com/oauth/v2/authorize",
            "token_url": "https://slack.com/api/oauth.v2.access",
            "scopes": ["channels:read", "chat:write"],
        },
        "events": {
            "webhooksSupported": True,
            "ingress": {"mode": "webhook", "descriptor_ref": "slack"},
            "eventTypes": ["com.slack.*"],
        },
    }


def _captured_gateway(db):
    """Return the DbGateway instance passed to db.add(...) on the skip-init path."""
    assert db.add.call_count == 1
    return db.add.call_args.args[0]


@pytest.mark.asyncio
async def test_register_slack_with_secret_persists_events_and_encrypted_secret(service):
    fake_catalog = {"catalog_servers": [_slack_catalog_entry()]}
    req = CatalogServerRegisterRequest(server_id="slack", webhook_signing_secret="shhh")

    with (
        patch.object(service, "load_catalog", AsyncMock(return_value=fake_catalog)),
        patch("mcpgateway.services.catalog_service.select"),
        patch("mcpgateway.schemas.GatewayRead") as mock_gateway_read,
    ):
        mock_gateway_read.model_validate.return_value = MagicMock(id="gw-slack", name="Slack")
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None

        result = await service.register_catalog_server("slack", req, db)

        assert result.success is True

        row = _captured_gateway(db)
        events = row.capabilities["events"]
        assert events["webhooksSupported"] is True
        assert events["ingress"]["mode"] == "webhook"
        assert events["ingress"]["descriptor_ref"] == "slack"
        assert events["ingress"]["path"] == "/webhooks/{conn-id}"
        assert events["eventTypes"] == ["com.slack.*"]

        # events_enabled is True only when a secret was supplied (verify needs it)
        assert row.events_enabled is True
        assert row.webhook_secret_algo == "hmac_timestamped"

        # Secret is stored as ciphertext only and round-trips via decode_auth.
        assert row.webhook_signing_secret
        assert row.webhook_signing_secret != "shhh"
        assert decode_auth(row.webhook_signing_secret)["secret"] == "shhh"

        # Secret-exclusion rule: never under capabilities.
        assert "secret" not in events
        assert "webhook_signing_secret" not in events


@pytest.mark.asyncio
async def test_register_slack_without_secret_advertises_but_disabled(service):
    fake_catalog = {"catalog_servers": [_slack_catalog_entry()]}
    req = CatalogServerRegisterRequest(server_id="slack")

    with (
        patch.object(service, "load_catalog", AsyncMock(return_value=fake_catalog)),
        patch("mcpgateway.services.catalog_service.select"),
        patch("mcpgateway.schemas.GatewayRead") as mock_gateway_read,
    ):
        mock_gateway_read.model_validate.return_value = MagicMock(id="gw-slack", name="Slack")
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None

        result = await service.register_catalog_server("slack", req, db)

        assert result.success is True

        row = _captured_gateway(db)
        events = row.capabilities["events"]
        # Advertised...
        assert events["ingress"]["descriptor_ref"] == "slack"
        assert events["webhooksSupported"] is True
        # ...but not active, because there is no secret to verify with.
        assert row.events_enabled is False
        assert not row.webhook_signing_secret


@pytest.mark.asyncio
async def test_register_non_events_entry_unchanged(service):
    """A catalog entry without an events block registers with no events wiring."""
    fake_catalog = {
        "catalog_servers": [
            {
                "id": "asana",
                "name": "Asana",
                "url": "https://mcp.asana.com/mcp",
                "description": "Asana MCP server",
                "auth_type": "OAuth2.1",
                "oauth_config": {
                    "authorize_url": "https://app.asana.com/-/oauth_authorize",
                    "token_url": "https://app.asana.com/-/oauth_token",
                    "scopes": ["default"],
                },
            }
        ]
    }
    req = CatalogServerRegisterRequest(server_id="asana", webhook_signing_secret="shhh")

    with (
        patch.object(service, "load_catalog", AsyncMock(return_value=fake_catalog)),
        patch("mcpgateway.services.catalog_service.select"),
        patch("mcpgateway.schemas.GatewayRead") as mock_gateway_read,
    ):
        mock_gateway_read.model_validate.return_value = MagicMock(id="gw-asana", name="Asana")
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = None

        result = await service.register_catalog_server("asana", req, db)

        assert result.success is True

        row = _captured_gateway(db)
        assert "events" not in (row.capabilities or {})
        assert not getattr(row, "webhook_signing_secret", None)
        assert getattr(row, "events_enabled", False) in (False, None)
