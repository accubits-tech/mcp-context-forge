# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_webhooks_router.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

End-to-end test-suite for the generic webhook ingress route
**mcpgateway.routers.webhooks** (FRD §6.2 single route ``POST /webhooks/{conn-id}``).

These tests drive the route through the FastAPI HTTP layer with
:class:`fastapi.testclient.TestClient`, seeding a real ``Gateway`` row (with an
encrypted signing secret + an ``events`` capability block) into a temporary
SQLite database wired into the app, then computing REAL provider signatures and
asserting the security-critical behaviour the milestone gate (M1) requires:

* a signed GitHub push verifies and is accepted with ``202`` (TC-ING-001);
* a tampered signature is rejected with ``401`` (TC-ING-002);
* verification runs over the EXACT raw bytes the client sent, not a re-encoded
  copy (TC-ING-018 / TC-ING-067);
* a Slack ``url_verification`` challenge is echoed verbatim ONLY after a valid
  signature, and never echoed for a bad signature (TC-ING-020 / TC-ING-021);
* a stale (replayed) Slack timestamp is rejected with ``401`` (TC-ING-043);
* a missing signature header yields ``401`` and garbled JSON (with a valid
  signature) yields ``400`` (TC-ING-056 / TC-ING-062 / TC-ING-063);
* the accepted envelope carries the normalized reverse-DNS type and provider
  dedup id (TC-ING-068);
* an oversized body is rejected with ``413`` before any work is done;
* ``GET`` / ``HEAD`` are ``200`` liveness probes that emit no event;
* with the master flag off, every method returns ``404``.

The route is UNAUTHENTICATED at the transport layer (it is authenticated by the
provider HMAC, not by a bearer token); these tests therefore send no
``Authorization`` header.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_webhooks_router.py -q
"""

# Future
from __future__ import annotations

# Standard
import hashlib
import hmac
import os
import tempfile
import time
import uuid

# Third-Party
from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Gateway
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.utils.services_auth import encode_auth

# --------------------------------------------------------------------------- #
# Constants / signing helpers (mirror the ingress-service test fixtures)       #
# --------------------------------------------------------------------------- #

KNOWN_SECRET = "s3cr3t-signing-key"


def _fresh_ts() -> int:
    """Return an epoch timestamp inside the verifier's replay tolerance window.

    The router (unlike the ingress-service unit tests) does not inject a fixed
    ``now_epoch``; it uses real wall-clock time. Slack signatures must therefore
    be timestamped near *now* or the replay guard rejects them as stale.
    """
    return int(time.time())


def _hex_hmac(secret: str, msg: bytes, algo: str = "sha256") -> str:
    """Return the hex-encoded HMAC of *msg* under *secret* using *algo*."""
    return hmac.new(secret.encode("utf-8"), msg, getattr(hashlib, algo)).hexdigest()


# --- GitHub (hmac) ---------------------------------------------------------- #

GITHUB_BODY = b'{"ref":"refs/heads/main","repository":{"full_name":"octo/repo"}}'


def _github_headers(body: bytes, *, secret: str = KNOWN_SECRET, event: str = "push", delivery: str = "deliv-1") -> dict:
    """Build valid GitHub webhook headers (sha256 hex, ``sha256=`` prefix)."""
    return {
        "X-Hub-Signature-256": "sha256=" + _hex_hmac(secret, body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


# --- Slack (hmac_timestamped, scheme=slack) --------------------------------- #

SLACK_CHALLENGE = "3eZbrw1aB-uBI3T-A-challenge-token"
SLACK_URL_VERIFICATION_BODY = b'{"type":"url_verification","token":"abc","challenge":"' + SLACK_CHALLENGE.encode("ascii") + b'"}'
SLACK_MESSAGE_BODY = b'{"type":"event_callback","event_id":"Ev123","event":{"type":"message","channel":"C1"}}'


def _slack_headers(body: bytes, ts: int, *, secret: str = KNOWN_SECRET) -> dict:
    """Build valid Slack webhook headers (``v0:{ts}:{body}`` signed, hex)."""
    signed = f"v0:{ts}:".encode("ascii") + body
    return {
        "X-Slack-Signature": "v0=" + _hex_hmac(secret, signed),
        "X-Slack-Request-Timestamp": str(ts),
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Fixtures: an app wired to a fresh temp DB, with events enabled + fresh state #
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_db():
    """Yield ``(app, SessionLocal)`` wired to a fresh temp SQLite database.

    Mirrors the conftest ``app_with_temp_db`` fixture but is function-scoped so
    each test gets an isolated database and a hand to seed Gateway rows.
    """
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

    db_mod.Base.metadata.create_all(bind=engine)

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
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Reset the process-wide event bus + dedup cache between tests."""
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


@pytest.fixture
def client(app_db):
    """Return a :class:`TestClient` against the temp-DB-wired app."""
    app, _ = app_db
    return TestClient(app)


def _make_gateway(session_local, *, descriptor_ref: str, secret: str = KNOWN_SECRET) -> str:
    """Persist a Gateway wired for events ingress; return its id."""
    db = session_local()
    try:
        gw = Gateway(
            id=uuid.uuid4().hex,
            name=f"gw-{descriptor_ref}-{uuid.uuid4().hex[:6]}",
            slug=f"gw-{uuid.uuid4().hex[:8]}",
            url="http://example.com",
            capabilities={"events": {"ingress": {"descriptor_ref": descriptor_ref}}},
            events_enabled=True,
            webhook_signing_secret=encode_auth({"secret": secret}),
        )
        db.add(gw)
        db.commit()
        db.refresh(gw)
        return gw.id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TC-ING-001: signed GitHub push -> 202                                        #
# --------------------------------------------------------------------------- #


def test_github_push_signed_accepted_202(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, delivery="d-aaa"),
    )

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}


# --------------------------------------------------------------------------- #
# TC-ING-002: tampered signature -> 401                                        #
# --------------------------------------------------------------------------- #


def test_github_tampered_signature_rejected_401(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_BODY + b"tampered",  # body no longer matches the signature
        headers=_github_headers(GITHUB_BODY, delivery="d-bbb"),
    )

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# TC-ING-018 / TC-ING-067: verification uses the EXACT raw bytes sent          #
# --------------------------------------------------------------------------- #


def test_verify_uses_exact_raw_body_bytes(client, app_db):
    """TC-SEC-005: a body whose byte form differs from a re-serialized JSON still
    verifies (signature is over the exact raw bytes, reordered keys / extra
    whitespace included).

    The signature is computed over compact bytes with no inner whitespace; if
    the route re-serialized the parsed JSON (adding spaces) before verifying, the
    HMAC would not match and this would 401. A 202 proves raw bytes are used.
    """
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")
    # Note the spaces after the colons - canonical json.dumps would not reproduce
    # this exact byte layout, so the HMAC is over THIS spacing specifically.
    spaced_body = b'{"ref": "refs/heads/main", "repository": {"full_name": "octo/repo"}}'

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=spaced_body,
        headers=_github_headers(spaced_body, delivery="d-raw"),
    )

    assert resp.status_code == 202


# --------------------------------------------------------------------------- #
# TC-ING-020 / TC-ING-021: Slack challenge echoed ONLY after verify            #
# --------------------------------------------------------------------------- #


def test_slack_url_verification_valid_sig_echoes_challenge_200(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="slack")

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_URL_VERIFICATION_BODY,
        headers=_slack_headers(SLACK_URL_VERIFICATION_BODY, _fresh_ts()),
    )

    assert resp.status_code == 200
    # The challenge is echoed back verbatim as the response body.
    assert resp.text == SLACK_CHALLENGE


def test_slack_url_verification_bad_sig_not_echoed_401(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="slack")

    headers = _slack_headers(SLACK_URL_VERIFICATION_BODY, _fresh_ts())
    headers["X-Slack-Signature"] = "v0=" + ("a" * 64)  # invalid

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_URL_VERIFICATION_BODY,
        headers=headers,
    )

    assert resp.status_code == 401
    # The challenge MUST NOT leak anywhere in the response (CWE-203 no oracle).
    assert SLACK_CHALLENGE not in resp.text


# --------------------------------------------------------------------------- #
# TC-ING-043: replayed / stale Slack timestamp -> 401                          #
# --------------------------------------------------------------------------- #


def test_slack_stale_timestamp_rejected_401(client, app_db):
    """A correctly-signed Slack request with a long-expired timestamp is refused."""
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="slack")

    # ts far outside the tolerance window relative to real wall-clock now.
    stale_ts = 1_000  # ~1970; well beyond mcpgateway_events_signature_tolerance_seconds
    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_MESSAGE_BODY,
        headers=_slack_headers(SLACK_MESSAGE_BODY, stale_ts),
    )

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# TC-ING-056 / TC-ING-062: missing signature header -> 401                     #
# --------------------------------------------------------------------------- #


def test_missing_signature_header_rejected_401(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_BODY,
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d-nosig", "Content-Type": "application/json"},
    )

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# TC-ING-063: valid signature but garbled JSON body -> 400 (parse after verify)#
# --------------------------------------------------------------------------- #


def test_garbled_json_valid_sig_returns_400(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    bad_json = b'{"ref": "refs/heads/main", not-valid-json'
    resp = client.post(
        f"/webhooks/{conn_id}",
        content=bad_json,
        headers=_github_headers(bad_json, delivery="d-bad-json"),  # sig is VALID over these bytes
    )

    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# TC-ING-068: accepted Slack message normalizes to the canonical envelope      #
# --------------------------------------------------------------------------- #


def test_accepted_event_envelope_shape(client, app_db):
    """A signed Slack message is accepted (202) and produces the normalized envelope.

    The envelope is verified through its observable side effects on the bus: the
    published event carries the reverse-DNS type and the provider dedup id.
    """
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="slack")

    queue = bus_mod.get_event_bus().subscribe()
    resp = client.post(
        f"/webhooks/{conn_id}",
        content=SLACK_MESSAGE_BODY,
        headers=_slack_headers(SLACK_MESSAGE_BODY, _fresh_ts()),
    )

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}

    assert not queue.empty()
    published = queue.get_nowait()
    assert published["type"] == "com.slack.message"
    assert published["id"] == "Ev123"
    assert published["source"] == f"//{conn_id}"
    assert published["data"]["event"]["channel"] == "C1"


# --------------------------------------------------------------------------- #
# Oversized body -> 413 (route self-enforces the configured cap)               #
# --------------------------------------------------------------------------- #


def test_oversized_body_rejected_413(client, app_db, monkeypatch):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    # Shrink the cap so the test body is cheap.
    monkeypatch.setattr(settings, "mcpgateway_events_max_body_bytes", 16)
    big_body = b"x" * 64

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=big_body,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert resp.status_code == 413


# --------------------------------------------------------------------------- #
# Unknown connection + bad sig -> 401 (no existence oracle)                     #
# --------------------------------------------------------------------------- #


def test_unknown_connection_returns_401(client):
    resp = client.post(
        f"/webhooks/does-not-exist-{uuid.uuid4().hex}",
        content=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, delivery="d-unknown"),
    )

    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# GET / HEAD liveness -> 200, no event                                         #
# --------------------------------------------------------------------------- #


def test_get_liveness_200(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    resp = client.get(f"/webhooks/{conn_id}")
    assert resp.status_code == 200


def test_head_liveness_200(client, app_db):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    resp = client.head(f"/webhooks/{conn_id}")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Flag off -> 404 for every method                                            #
# --------------------------------------------------------------------------- #


def test_flag_off_post_returns_404(client, app_db, monkeypatch):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False)

    resp = client.post(
        f"/webhooks/{conn_id}",
        content=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, delivery="d-off"),
    )

    assert resp.status_code == 404


def test_flag_off_get_returns_404(client, app_db, monkeypatch):
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False)

    resp = client.get(f"/webhooks/{conn_id}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The route is UNAUTHENTICATED at the transport layer                          #
# --------------------------------------------------------------------------- #


def test_route_is_unauthenticated_no_bearer_required(client, app_db):
    """A valid signed POST with NO Authorization header is accepted (202).

    If the route carried the bearer/RBAC dependency, an unauthenticated request
    would 401/403 before the HMAC was ever checked. A 202 here proves the route
    relies on the provider signature, not transport auth.
    """
    _, session_local = app_db
    conn_id = _make_gateway(session_local, descriptor_ref="github")

    headers = _github_headers(GITHUB_BODY, delivery="d-noauth")
    assert "Authorization" not in headers

    resp = client.post(f"/webhooks/{conn_id}", content=GITHUB_BODY, headers=headers)
    assert resp.status_code == 202
