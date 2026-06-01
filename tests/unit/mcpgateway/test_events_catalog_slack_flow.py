# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_catalog_slack_flow.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

End-to-end REGISTRY->INGRESS flow proof for Slack.

Where ``test_events_catalog_slack.py`` proves the unit-level catalog persistence
(the declared non-secret ``events`` block + the encrypted signing secret land on
the Gateway row), this suite proves the WHOLE vertical: a Slack connector created
*purely from the catalog YAML* plus a registration-time signing secret actually
verifies and ingests real Slack webhooks over the live FastAPI route.

The flow, against a REAL temp SQLite database and the real
``POST /webhooks/{conn-id}`` route (no network, no upstream call):

1. Hermetically register the ``slack`` catalog entry through
   :meth:`CatalogService.register_catalog_server` (the OAuth-without-credentials
   skip-initialization path, so NO upstream/tool-discovery call is attempted).
   The catalog entry carries only the non-secret ``events`` block (descriptor_ref
   ``slack``); the inbound webhook signing secret is supplied at registration
   time via the write-only request field and stored ONLY in the encrypted
   ``webhook_signing_secret`` column. The resulting Gateway row IS the connection
   (``conn_id``) the ingress route resolves.

2. A Slack ``url_verification`` request signed with the real Slack recipe
   (``X-Slack-Signature = "v0=" + hex HMAC over "v0:{ts}:{raw_body}"``, fresh
   ``X-Slack-Request-Timestamp``) -> ``200`` with the ``challenge`` echoed back
   verbatim.

3. The SAME ``url_verification`` body with a BAD signature -> ``401`` and the
   challenge is NOT echoed anywhere in the response (no oracle).

4. A real signed Slack ``event_callback`` -> ``202`` and exactly one persisted
   :class:`EventLog` row whose ``evt_type`` is ``com.slack.*`` and whose
   ``evt_subject`` is the event channel.

All behind the master flag, monkeypatched on
(:data:`settings.mcpgateway_events_enabled` = ``True``).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_catalog_slack_flow.py -q
"""

# Future
from __future__ import annotations

# Standard
import hashlib
import hmac
import os
import tempfile
import time

# Third-Party
from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EventLog
from mcpgateway.schemas import CatalogServerRegisterRequest
from mcpgateway.services.catalog_service import CatalogService
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.utils.services_auth import decode_auth

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

SIGNING_SECRET = "slack-flow-signing-secret"  # supplied at registration time
OWNER_EMAIL = "finance@bud.studio"

SLACK_CHALLENGE = "3eZbrw1aB-uBI3T-A-challenge-token"
SLACK_URL_VERIFICATION_BODY = b'{"type":"url_verification","token":"abc","challenge":"' + SLACK_CHALLENGE.encode("ascii") + b'"}'
# A real Slack event_callback. The descriptor maps type from $.event.type
# (-> com.slack.{type}), subject from $.event.channel, and dedup id from
# $.event_id.
SLACK_MESSAGE_BODY = b'{"type":"event_callback","event_id":"Ev0FLOW01","event_time":1700000000,"event":{"type":"message","channel":"C0FLOWCHN","text":"hi"}}'


# --------------------------------------------------------------------------- #
# Slack signing helper (mirrors the built-in Slack descriptor recipe)          #
# --------------------------------------------------------------------------- #


def _slack_headers(body: bytes, ts: int, *, secret: str = SIGNING_SECRET) -> dict:
    """Build valid Slack webhook headers (``v0:{ts}:{body}`` signed, hex, ``v0=`` prefix)."""
    signed = f"v0:{ts}:".encode("ascii") + body
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Signature": "v0=" + sig,
        "X-Slack-Request-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


def _fresh_ts() -> int:
    """A timestamp inside the replay tolerance window."""
    return int(time.time())


# --------------------------------------------------------------------------- #
# A Slack catalog entry carrying only the non-secret events block (FRD §5.2)   #
# --------------------------------------------------------------------------- #


def _slack_catalog_entry() -> dict:
    """The catalog YAML view of Slack: OAuth2.1 + a non-secret events block.

    The signing secret is intentionally absent here - it is supplied at
    registration time and stored only in the encrypted column, never in YAML.
    """
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
            "eventTypes": ["com.slack.message"],
        },
    }


# --------------------------------------------------------------------------- #
# App + temp-DB fixtures (mirror test_events_e2e.py)                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_db():
    """Yield ``(app, SessionLocal)`` wired to a fresh temp SQLite database."""
    mp = MonkeyPatch()

    fd, path = tempfile.mkstemp(suffix=".db")
    url = f"sqlite:///{path}"
    mp.setattr(settings, "database_url", url, raising=False)

    # First-Party
    import mcpgateway.db as db_mod

    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    mp.setattr(db_mod, "engine", engine, raising=False)
    mp.setattr(db_mod, "SessionLocal", testing_session_local, raising=False)

    # First-Party
    import mcpgateway.main as main_mod

    mp.setattr(main_mod, "SessionLocal", testing_session_local, raising=False)
    mp.setattr(main_mod, "engine", engine, raising=False)

    Base.metadata.create_all(bind=engine)

    # First-Party
    from mcpgateway.main import app

    yield app, testing_session_local

    mp.undo()
    engine.dispose()
    os.close(fd)
    os.unlink(path)


@pytest.fixture(autouse=True)
def _events_enabled(monkeypatch):
    """Enable the events master flag for the duration of each test."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """Reset the process-wide bus, L2 stream, and ingress dedup-cache singletons."""
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


@pytest.fixture
def client(app_db):
    """Return a :class:`TestClient` against the temp-DB-wired app."""
    app, _ = app_db
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Registration helper                                                          #
# --------------------------------------------------------------------------- #


async def _register_slack_from_catalog(session_local, *, secret: str = SIGNING_SECRET) -> str:
    """Hermetically register the ``slack`` catalog entry; return the conn_id.

    Drives the real :meth:`CatalogService.register_catalog_server` against the
    temp DB. Because the entry is OAuth2.1 and no OAuth credentials/api_key are
    supplied, it takes the skip-initialization path: NO upstream/tool-discovery
    call is made. The catalog load is the ONLY thing patched (so the test does
    not depend on the on-disk mcp-catalog.yml).
    """
    # Standard
    from unittest.mock import AsyncMock, patch

    service = CatalogService()
    fake_catalog = {"catalog_servers": [_slack_catalog_entry()]}

    db = session_local()
    try:
        with patch.object(service, "load_catalog", AsyncMock(return_value=fake_catalog)):
            result = await service.register_catalog_server(
                "slack",
                CatalogServerRegisterRequest(server_id="slack", webhook_signing_secret=secret),
                db,
                user={"email": OWNER_EMAIL},
            )
        assert result.success is True, result.error
        return result.server_id
    finally:
        db.close()


# =========================================================================== #
# The flow: catalog register -> live ingress over POST /webhooks/{conn-id}     #
# =========================================================================== #


@pytest.mark.asyncio
async def test_slack_catalog_register_then_url_verification_echoes_challenge(app_db, client):
    """A connector configured purely from the catalog + a registration-time secret
    echoes a valid Slack ``url_verification`` challenge with ``200``."""
    _, session_local = app_db
    conn_id = await _register_slack_from_catalog(session_local)

    # The persisted connection carries the §5.2 events block + the encrypted
    # secret only (proving the registry wiring is what the ingress will read).
    # First-Party
    from mcpgateway.db import Gateway

    db = session_local()
    try:
        row = db.get(Gateway, conn_id)
        assert row is not None
        assert row.capabilities["events"]["ingress"]["descriptor_ref"] == "slack"
        assert row.events_enabled is True
        assert "secret" not in row.capabilities["events"]
        assert decode_auth(row.webhook_signing_secret)["secret"] == SIGNING_SECRET
    finally:
        db.close()

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_URL_VERIFICATION_BODY,
        headers=_slack_headers(SLACK_URL_VERIFICATION_BODY, _fresh_ts()),
    )
    assert resp.status_code == 200
    # The challenge is echoed back verbatim (text/plain), byte-for-byte.
    assert resp.text == SLACK_CHALLENGE

    # A handshake emits no domain event.
    assert _count_event_logs(session_local) == 0


@pytest.mark.asyncio
async def test_slack_catalog_register_then_bad_signature_not_echoed_401(app_db, client):
    """A bad-signature ``url_verification`` is rejected with ``401`` and the
    challenge is NOT echoed anywhere in the response (no oracle)."""
    _, session_local = app_db
    conn_id = await _register_slack_from_catalog(session_local)

    headers = _slack_headers(SLACK_URL_VERIFICATION_BODY, _fresh_ts())
    headers["X-Slack-Signature"] = "v0=" + ("a" * 64)  # invalid signature

    resp = client.post(f"/webhooks/{conn_id}", content=SLACK_URL_VERIFICATION_BODY, headers=headers)

    assert resp.status_code == 401
    # The challenge MUST NOT leak anywhere in the response (CWE-203 no oracle).
    assert SLACK_CHALLENGE not in resp.text
    assert resp.content == b""
    assert _count_event_logs(session_local) == 0


@pytest.mark.asyncio
async def test_slack_catalog_register_then_real_event_ingested_202_with_eventlog(app_db, client):
    """A real signed Slack ``event_callback`` -> ``202`` and exactly one EventLog
    row typed ``com.slack.message`` with the channel as the subject."""
    _, session_local = app_db
    conn_id = await _register_slack_from_catalog(session_local)

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_MESSAGE_BODY,
        headers=_slack_headers(SLACK_MESSAGE_BODY, _fresh_ts()),
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}

    db = session_local()
    try:
        logs = db.execute(select(EventLog)).scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.evt_type == "com.slack.message"
        assert log.evt_subject == "C0FLOWCHN"
        assert log.evt_id == "Ev0FLOW01"
        assert log.evt_source == f"//{conn_id}"
        assert log.gateway_id == conn_id
        assert log.provider_id == "slack"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #


def _count_event_logs(session_local) -> int:
    """Count persisted EventLog rows in a fresh session."""
    db = session_local()
    try:
        return len(db.execute(select(EventLog)).scalars().all())
    finally:
        db.close()
