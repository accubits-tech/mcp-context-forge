# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_ingress_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Integration test-suite for **mcpgateway.services.events.ingress_service**.

These tests drive the config-driven ingress service end-to-end against a real
(temporary, in-memory) database. Each test stands up a ``Gateway`` row with a
real encrypted signing secret and an ``events`` capability block, computes a
REAL provider signature against that secret (reusing the same fixture builders
as the verifier tests), and asserts the security-critical ingress behaviour:

* a valid GitHub push verifies, normalizes, persists an ``event_log`` row, and
  publishes the canonical event onto the in-process bus (TC-ING-001);
* a tampered signature is rejected with ``401`` and produces no row and no
  publish (SC-SEC-008 / TC-ING-002);
* an unknown ``conn-id`` with a bad signature returns the SAME ``401`` as a
  known ``conn-id`` with a bad signature (no existence oracle, SC-SEC-010 /
  TC-SEC-010);
* with the master flag off, every request returns ``404`` (TC behind FR-7a);
* a Slack ``url_verification`` challenge is echoed only AFTER signature
  verification (``200`` valid sig, ``401`` + NOT echoed for a bad sig,
  SC-SEC-009 / TC-ING-020 / TC-ING-021);
* a GitHub ``ping`` is acknowledged with ``202`` and emits no domain event
  (SC-ING-017 / TC-ING-022);
* a redelivered ``X-GitHub-Delivery`` is deduplicated - the first POST emits,
  the second is silently dropped with no second publish (SC-ING-020 /
  TC-ING-025);
* malformed JSON with a valid signature is rejected with ``400`` (parse runs
  only after verify), while malformed JSON with a bad signature is rejected
  with ``401`` before any parse attempt (SC-ING-044 / TC-ING-051).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_ingress_service.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import hashlib
import hmac
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EventLog, Gateway
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events.ingress_service import IngressResult, IngressService
from mcpgateway.utils.services_auth import encode_auth

# --------------------------------------------------------------------------- #
# Constants / signing helpers (mirror the verifier-test fixture builders)      #
# --------------------------------------------------------------------------- #

KNOWN_SECRET = "s3cr3t-signing-key"
OLD_SECRET = "previous-rotated-key"
NOW = 1_700_000_000  # fixed "now" epoch for deterministic replay-free tests


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
    }


# --- Slack (hmac_timestamped, scheme=slack) --------------------------------- #


def _slack_headers(body: bytes, ts: int, *, secret: str = KNOWN_SECRET) -> dict:
    """Build valid Slack webhook headers (``v0:{ts}:{body}`` signed, hex)."""
    signed = f"v0:{ts}:".encode("ascii") + body
    return {
        "X-Slack-Signature": "v0=" + _hex_hmac(secret, signed),
        "X-Slack-Request-Timestamp": str(ts),
    }


SLACK_CHALLENGE = "3eZbrw1aB-uBI3T-A-challenge-token"
SLACK_URL_VERIFICATION_BODY = b'{"type":"url_verification","token":"abc","challenge":"' + SLACK_CHALLENGE.encode("ascii") + b'"}'
SLACK_MESSAGE_BODY = b'{"type":"event_callback","event_id":"Ev123","event":{"type":"message","channel":"C1"}}'


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session():
    """Create a fresh in-memory database session with all tables built."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _events_enabled(monkeypatch):
    """Enable the events master flag for the duration of each test."""
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", True)


@pytest.fixture(autouse=True)
def _fresh_bus(monkeypatch):
    """Reset the process-wide event bus singleton between tests."""
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)


def _make_gateway(db, *, descriptor_ref: str, secret: str = KNOWN_SECRET, secret_prev: str | None = None, enabled: bool = True) -> Gateway:
    """Persist a Gateway wired for events ingress with an encrypted secret."""
    secret_payload = {"secret": secret}
    if secret_prev is not None:
        secret_payload["secret_prev"] = secret_prev
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{descriptor_ref}-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities={"events": {"ingress": {"descriptor_ref": descriptor_ref}}},
        events_enabled=enabled,
        webhook_signing_secret=encode_auth(secret_payload),
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _ingest(svc, **kwargs):
    """Run the async ``ingest`` coroutine to completion."""
    defaults = {"query_params": {}, "now_epoch": NOW}
    defaults.update(kwargs)
    return asyncio.run(svc.ingest(**defaults))


def _drain(queue) -> list:
    """Drain all currently-queued events from a bus subscriber queue."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# --------------------------------------------------------------------------- #
# Happy path: GitHub push -> 202 + event_log row + bus publish (TC-ING-001)    #
# --------------------------------------------------------------------------- #


def test_github_push_accepted_persisted_and_published(session):
    gw = _make_gateway(session, descriptor_ref="github")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, delivery="d-aaa"),
        db=session,
    )

    assert isinstance(result, IngressResult)
    assert result.status == 202
    assert result.deduped is False
    assert result.envelope is not None
    assert result.envelope.type == "com.github.push"

    # event_log row written
    rows = session.execute(select(EventLog)).scalars().all()
    assert len(rows) == 1
    assert rows[0].evt_type == "com.github.push"
    assert rows[0].evt_id == "d-aaa"
    assert rows[0].gateway_id == gw.id
    assert rows[0].data == {"ref": "refs/heads/main", "repository": {"full_name": "octo/repo"}}

    # event fanned out onto the bus
    published = _drain(queue)
    assert len(published) == 1
    assert published[0]["type"] == "com.github.push"
    assert published[0]["id"] == "d-aaa"


# --------------------------------------------------------------------------- #
# Tampered signature -> 401, no row, no publish (SC-SEC-008 / TC-ING-002)      #
# --------------------------------------------------------------------------- #


def test_tampered_signature_rejected_no_side_effects(session):
    gw = _make_gateway(session, descriptor_ref="github")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    headers = _github_headers(GITHUB_BODY, delivery="d-bbb")
    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=GITHUB_BODY + b"tampered-extra-bytes",  # body no longer matches sig
        headers=headers,
        db=session,
    )

    assert result.status == 401
    assert session.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


# --------------------------------------------------------------------------- #
# No existence oracle: unknown conn + bad sig == known conn + bad sig          #
# (SC-SEC-010 / TC-SEC-010)                                                    #
# --------------------------------------------------------------------------- #


def test_unknown_conn_and_bad_sig_are_indistinguishable(session):
    gw = _make_gateway(session, descriptor_ref="github")
    svc = IngressService()

    bad_headers = {"X-Hub-Signature-256": "sha256=" + ("0" * 64), "X-GitHub-Event": "push", "X-GitHub-Delivery": "d-ccc"}

    known_bad = _ingest(svc, conn_id=gw.id, raw_body=GITHUB_BODY, headers=bad_headers, db=session)
    unknown = _ingest(svc, conn_id="does-not-exist-" + uuid.uuid4().hex, raw_body=GITHUB_BODY, headers=bad_headers, db=session)

    assert known_bad.status == 401
    assert unknown.status == 401
    # Byte-identical body and dedup flag: no differential response.
    assert known_bad.body == unknown.body
    assert known_bad.deduped == unknown.deduped
    assert known_bad.envelope is None and unknown.envelope is None


# --------------------------------------------------------------------------- #
# Master flag off -> 404 (FR-7a)                                              #
# --------------------------------------------------------------------------- #


def test_flag_off_returns_404(session, monkeypatch):
    gw = _make_gateway(session, descriptor_ref="github")
    monkeypatch.setattr(settings, "mcpgateway_events_enabled", False)
    svc = IngressService()

    result = _ingest(svc, conn_id=gw.id, raw_body=GITHUB_BODY, headers=_github_headers(GITHUB_BODY), db=session)

    assert result.status == 404
    assert session.execute(select(EventLog)).scalars().all() == []


# --------------------------------------------------------------------------- #
# Slack url_verification handshake (SC-SEC-009 / TC-ING-020 / TC-ING-021)      #
# --------------------------------------------------------------------------- #


def test_slack_url_verification_valid_sig_echoes_challenge(session):
    gw = _make_gateway(session, descriptor_ref="slack")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=SLACK_URL_VERIFICATION_BODY,
        headers=_slack_headers(SLACK_URL_VERIFICATION_BODY, NOW),
        db=session,
    )

    assert result.status == 200
    assert result.body == SLACK_CHALLENGE
    # Handshake answers carry no domain event and persist nothing.
    assert session.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


def test_slack_url_verification_bad_sig_not_echoed(session):
    gw = _make_gateway(session, descriptor_ref="slack")
    svc = IngressService()

    headers = _slack_headers(SLACK_URL_VERIFICATION_BODY, NOW)
    headers["X-Slack-Signature"] = "v0=" + ("a" * 64)  # invalid

    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=SLACK_URL_VERIFICATION_BODY,
        headers=headers,
        db=session,
    )

    assert result.status == 401
    # The challenge MUST NOT appear anywhere in the response (CWE-203 no oracle).
    assert result.body is None or SLACK_CHALLENGE not in str(result.body)


# --------------------------------------------------------------------------- #
# GitHub ping noop -> 202, no domain event (SC-ING-017 / TC-ING-022)          #
# --------------------------------------------------------------------------- #


def test_github_ping_acknowledged_without_event(session):
    gw = _make_gateway(session, descriptor_ref="github")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    ping_body = b'{"zen":"Keep it simple.","hook_id":1}'
    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=ping_body,
        headers=_github_headers(ping_body, event="ping", delivery="d-ping"),
        db=session,
    )

    assert result.status == 202
    assert result.envelope is None
    assert session.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


# --------------------------------------------------------------------------- #
# Redelivery dedup (SC-ING-020 / TC-ING-025 / TC-SEC-011 in-window arm)        #
# --------------------------------------------------------------------------- #


def test_duplicate_delivery_id_is_deduped(session):
    gw = _make_gateway(session, descriptor_ref="github")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    headers = _github_headers(GITHUB_BODY, delivery="d-dup")

    first = _ingest(svc, conn_id=gw.id, raw_body=GITHUB_BODY, headers=headers, db=session)
    second = _ingest(svc, conn_id=gw.id, raw_body=GITHUB_BODY, headers=headers, db=session)

    assert first.status == 202 and first.deduped is False
    assert second.status == 202 and second.deduped is True

    # Exactly one persisted row and one publish.
    assert len(session.execute(select(EventLog)).scalars().all()) == 1
    assert len(_drain(queue)) == 1


# --------------------------------------------------------------------------- #
# Malformed JSON ordering (SC-ING-044 / TC-ING-051)                           #
# --------------------------------------------------------------------------- #


def test_malformed_json_valid_sig_returns_400_after_verify(session):
    gw = _make_gateway(session, descriptor_ref="github")
    svc = IngressService()

    bad_json = b'{"ref": "refs/heads/main", not-valid-json'
    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=bad_json,
        headers=_github_headers(bad_json, delivery="d-bad-json"),  # signature is VALID over the bytes
        db=session,
    )

    assert result.status == 400
    assert session.execute(select(EventLog)).scalars().all() == []


def test_malformed_json_bad_sig_returns_401_before_parse(session):
    gw = _make_gateway(session, descriptor_ref="github")
    svc = IngressService()

    bad_json = b'{"ref": "refs/heads/main", not-valid-json'
    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=bad_json,
        headers={"X-Hub-Signature-256": "sha256=" + ("0" * 64), "X-GitHub-Event": "push", "X-GitHub-Delivery": "d-x"},
        db=session,
    )

    # 401 (signature) wins over 400 (parse): verify runs first.
    assert result.status == 401
    assert session.execute(select(EventLog)).scalars().all() == []


# --------------------------------------------------------------------------- #
# Secret rotation: a body signed with the previous secret still verifies       #
# --------------------------------------------------------------------------- #


def test_rotation_previous_secret_accepted(session):
    gw = _make_gateway(session, descriptor_ref="github", secret=KNOWN_SECRET, secret_prev=OLD_SECRET)
    svc = IngressService()

    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, secret=OLD_SECRET, delivery="d-rot"),
        db=session,
    )

    assert result.status == 202


# --------------------------------------------------------------------------- #
# Slack message event normalizes end-to-end                                   #
# --------------------------------------------------------------------------- #


def test_slack_message_event_normalized_and_published(session):
    gw = _make_gateway(session, descriptor_ref="slack")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    result = _ingest(
        svc,
        conn_id=gw.id,
        raw_body=SLACK_MESSAGE_BODY,
        headers=_slack_headers(SLACK_MESSAGE_BODY, NOW),
        db=session,
    )

    assert result.status == 202
    assert result.envelope.type == "com.slack.message"
    assert result.envelope.id == "Ev123"
    published = _drain(queue)
    assert len(published) == 1
    assert published[0]["type"] == "com.slack.message"


# --------------------------------------------------------------------------- #
# TC-SEC-001: an unsigned ("none") recipe is refused unless the descriptor      #
# explicitly opts in via allow_unsigned; a missing signature persists nothing.  #
# --------------------------------------------------------------------------- #


def _write_descriptor(tmp_path, stem: str, body: str) -> None:
    """Write a YAML descriptor into ``tmp_path`` under ``stem``.yaml."""
    (tmp_path / f"{stem}.yaml").write_text(body, encoding="utf-8")


def test_none_recipe_without_allow_unsigned_refused(session, tmp_path, monkeypatch):
    """TC-SEC-001: a ``strategy: none`` descriptor that does NOT opt in is refused
    with ``401`` and persists no row (unsigned traffic is not honored by default)."""
    _write_descriptor(
        tmp_path,
        "unsigned_default",
        "display_name: Unsigned\n" "verify:\n" "  strategy: none\n" "event_type:\n" "  from: const\n" "  ref: com.unsigned.event\n",
    )
    monkeypatch.setenv("MCPGATEWAY_EVENTS_DESCRIPTORS_DIR", str(tmp_path))

    gw = _make_gateway(session, descriptor_ref="unsigned_default")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    result = _ingest(svc, conn_id=gw.id, raw_body=b'{"hello":"world"}', headers={}, db=session)

    assert result.status == 401
    assert session.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


def test_none_recipe_with_allow_unsigned_accepted(session, tmp_path, monkeypatch):
    """TC-SEC-001 (opt-in arm): a ``strategy: none`` descriptor that sets
    ``allow_unsigned: true`` accepts the unsigned POST with ``202`` and publishes."""
    _write_descriptor(
        tmp_path,
        "unsigned_optin",
        "display_name: UnsignedOptIn\n" "verify:\n" "  strategy: none\n" "  allow_unsigned: true\n" "event_type:\n" "  from: const\n" "  ref: com.unsigned.event\n" "  template: com.unsigned.event\n",
    )
    monkeypatch.setenv("MCPGATEWAY_EVENTS_DESCRIPTORS_DIR", str(tmp_path))

    gw = _make_gateway(session, descriptor_ref="unsigned_optin")
    queue = bus_mod.get_event_bus().subscribe()
    svc = IngressService()

    result = _ingest(svc, conn_id=gw.id, raw_body=b'{"hello":"world"}', headers={}, db=session)

    assert result.status == 202
    assert len(session.execute(select(EventLog)).scalars().all()) == 1
    assert len(_drain(queue)) == 1
