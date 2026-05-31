# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_delivery_secret.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for **M4 outbound delivery-secret encryption at rest**.

The per-subscription egress credential (the ``delivery.auth`` HMAC ``secret`` /
bearer ``token``) must never be persisted in plaintext (SC-SEC-015 / SC-SEC-039,
FRD section 10.1). On create/update the service rewrites the block so the
sensitive value is stored only as ``secret_encrypted`` / ``token_encrypted``
(AES-GCM ciphertext via :mod:`mcpgateway.utils.services_auth`); the
HTTP-callback adapter decrypts a throwaway in-memory copy immediately before
computing the outbound HMAC / bearer header, so the signed request a receiver
gets is byte-for-byte identical to one signed with the plaintext secret.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_delivery_secret.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import hashlib
import hmac
import json
import uuid

# Third-Party
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EventSubscription, Gateway
from mcpgateway.schemas import SubscriberRef, SubscriptionCreate
from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter
from mcpgateway.services.events.provisioner import NoopProvisioner
from mcpgateway.services.events.subscription_service import SubscriptionService
from mcpgateway.utils.services_auth import decode_auth

TEAM_A = "team-a"
USER_A = "a@example.com"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _disable_ssrf(monkeypatch):
    """Disable SSRF admission so unresolvable example callback URLs are accepted.

    These tests exercise credential encryption, not the SSRF guard (covered in
    test_events_subscription_service.py), so the create-time URL validation is
    turned off the same way that suite's ``test_callback_url_ssrf_skipped...`` does.
    """
    monkeypatch.setattr(settings, "ssrf_protection_enabled", False)


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


def _make_gateway(db) -> Gateway:
    """Persist a minimal events-capable Gateway row to subscribe against."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities={"events": {"webhooksSupported": True}},
        team_id=TEAM_A,
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _hmac_create(gw_id, secret="shh") -> SubscriptionCreate:
    """Build an http_callback create payload carrying an HMAC delivery secret."""
    return SubscriptionCreate(
        gateway_id=gw_id,
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://receiver.example.com/hook"),
        event_types=["com.github.push"],
        delivery={"auth": {"strategy": "hmac", "secret": secret}},
    )


def _create(session, payload) -> EventSubscription:
    """Run the async create against the in-memory session."""
    svc = SubscriptionService(session)
    return asyncio.run(svc.create(session, payload, user_email=USER_A, team_id=TEAM_A, provisioner=NoopProvisioner()))


# --------------------------------------------------------------------------- #
# At-rest encryption on create                                                 #
# --------------------------------------------------------------------------- #
def test_create_encrypts_hmac_secret_at_rest(session):
    """SC-SEC-015/039: a created sub stores ciphertext, never the plaintext secret."""
    gw = _make_gateway(session)
    sub = _create(session, _hmac_create(gw.id, secret="shh"))

    # Re-read the raw persisted row (not the in-memory return) to assert at-rest shape.
    persisted = session.get(EventSubscription, sub.id)
    auth = persisted.delivery["auth"]

    assert "secret" not in auth  # no plaintext at rest
    assert "secret_encrypted" in auth
    assert auth["secret_encrypted"] != "shh"  # ciphertext, not the literal value
    assert decode_auth(auth["secret_encrypted"]) == {"v": "shh"}  # round-trips back
    assert auth["strategy"] == "hmac"  # non-sensitive fields preserved


def test_create_encrypts_bearer_token_at_rest(session):
    """The bearer token path is encrypted at rest equivalently to the HMAC secret."""
    gw = _make_gateway(session)
    payload = SubscriptionCreate(
        gateway_id=gw.id,
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://receiver.example.com/hook"),
        event_types=["com.github.push"],
        delivery={"auth": {"strategy": "bearer", "token": "t0k"}},
    )
    sub = _create(session, payload)

    auth = session.get(EventSubscription, sub.id).delivery["auth"]
    assert "token" not in auth
    assert auth["token_encrypted"] != "t0k"
    assert decode_auth(auth["token_encrypted"]) == {"v": "t0k"}


def test_create_without_auth_leaves_delivery_untouched(session):
    """A delivery with no auth block (or no sensitive fields) is persisted as-is."""
    gw = _make_gateway(session)
    payload = SubscriptionCreate(
        gateway_id=gw.id,
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://receiver.example.com/hook"),
        event_types=["com.github.push"],
        delivery={"auth": {"strategy": "none"}},
    )
    sub = _create(session, payload)
    auth = session.get(EventSubscription, sub.id).delivery["auth"]
    assert auth == {"strategy": "none"}


# --------------------------------------------------------------------------- #
# At-rest encryption on update + idempotency                                   #
# --------------------------------------------------------------------------- #
def test_update_re_encrypts_new_secret(session):
    """Updating delivery to a new secret re-encrypts the new value at rest."""
    gw = _make_gateway(session)
    sub = _create(session, _hmac_create(gw.id, secret="shh"))
    svc = SubscriptionService(session)

    asyncio.run(svc.update(session, sub.id, {"delivery": {"auth": {"strategy": "hmac", "secret": "new-secret"}}}, team_id=TEAM_A))

    auth = session.get(EventSubscription, sub.id).delivery["auth"]
    assert "secret" not in auth
    assert decode_auth(auth["secret_encrypted"]) == {"v": "new-secret"}


def test_update_with_already_encrypted_delivery_does_not_double_encrypt(session):
    """Re-running update with an already-encrypted delivery is idempotent."""
    gw = _make_gateway(session)
    sub = _create(session, _hmac_create(gw.id, secret="shh"))

    # The persisted (already-encrypted) delivery dict, fed straight back into update.
    encrypted_delivery = json.loads(json.dumps(session.get(EventSubscription, sub.id).delivery))
    ciphertext_before = encrypted_delivery["auth"]["secret_encrypted"]

    svc = SubscriptionService(session)
    asyncio.run(svc.update(session, sub.id, {"delivery": encrypted_delivery}, team_id=TEAM_A))

    auth = session.get(EventSubscription, sub.id).delivery["auth"]
    # No plaintext got introduced and the ciphertext was not re-wrapped (decodes 1x).
    assert "secret" not in auth
    assert auth["secret_encrypted"] == ciphertext_before
    assert decode_auth(auth["secret_encrypted"]) == {"v": "shh"}


# --------------------------------------------------------------------------- #
# Decrypt-then-sign: outbound signature matches a plaintext control            #
# --------------------------------------------------------------------------- #
def _capture_adapter():
    """Build an HTTP-callback adapter whose client captures the outbound request.

    Returns:
        tuple: ``(adapter, captured)`` where ``captured`` is a one-element list
        that receives the :class:`httpx.Request` once a delivery is attempted.
    """
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    adapter = HttpCallbackEgressAdapter(allow_loopback=True, https_only=False, client_factory=client_factory)
    return adapter, captured


def _envelope(event_id="evt-1") -> dict:
    """Minimal section 9.1a delivery envelope with a stable idempotency key."""
    return {
        "event": {"id": event_id, "source": "//gw", "type": "com.github.push", "data": {"ref": "main"}},
        "subscription": {"id": "sub-1", "delivery_id": "d-1", "mode": "fanout", "target": {}},
        "idempotency_key": event_id,
    }


class _Sub:
    """Subscription stub carrying the fields the adapter reads."""

    def __init__(self, callback_url, *, delivery=None):
        self.callback_url = callback_url
        self.delivery = delivery
        self.subscriber_kind = "http_callback"
        self.target = {"callback_url": callback_url}
        self.id = "sub-1"
        self.mode = "fanout"
        self.correlation_value = None
        self.subscriber_target_ref = None


def test_encrypted_secret_produces_same_signature_as_plaintext(session):
    """Decrypt-then-sign: an encrypted secret yields the SAME X-MCPGW-Signature as plaintext."""
    gw = _make_gateway(session)
    # Persist with encryption-at-rest, then read back the encrypted delivery block.
    sub = _create(session, _hmac_create(gw.id, secret="shh"))
    encrypted_delivery = session.get(EventSubscription, sub.id).delivery
    assert "secret" not in encrypted_delivery["auth"]  # precondition: ciphertext at rest

    url = "http://localhost:1/cb"

    # Subject: delivers with the encrypted-at-rest delivery block.
    enc_adapter, enc_captured = _capture_adapter()
    enc_sub = _Sub(url, delivery=json.loads(json.dumps(encrypted_delivery)))
    out_enc = asyncio.run(enc_adapter.deliver(delivery_envelope=_envelope("evt-x"), subscription=enc_sub))

    # Control: delivers with a plaintext secret.
    ctl_adapter, ctl_captured = _capture_adapter()
    ctl_sub = _Sub(url, delivery={"auth": {"strategy": "hmac", "secret": "shh"}})
    out_ctl = asyncio.run(ctl_adapter.deliver(delivery_envelope=_envelope("evt-x"), subscription=ctl_sub))

    assert out_enc.ok is True and out_ctl.ok is True
    enc_req, ctl_req = enc_captured[0], ctl_captured[0]

    # Identical bodies (same envelope) -> compare signatures over the same ts.
    assert enc_req.content == ctl_req.content
    enc_sig = enc_req.headers["X-MCPGW-Signature"]
    ctl_sig = ctl_req.headers["X-MCPGW-Signature"]
    ts = enc_req.headers["X-MCPGW-Timestamp"]

    expected = "sha256=" + hmac.new(b"shh", ts.encode() + b"." + enc_req.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(enc_sig, expected)
    # And the encrypted-at-rest path matches the plaintext control byte-for-byte
    # (when both sign with the same timestamp).
    if enc_req.headers["X-MCPGW-Timestamp"] == ctl_req.headers["X-MCPGW-Timestamp"]:
        assert hmac.compare_digest(enc_sig, ctl_sig)


def test_decrypting_does_not_repersist_plaintext(session):
    """The decrypt-at-delivery copy must not mutate the stored ciphertext back to plaintext."""
    gw = _make_gateway(session)
    sub = _create(session, _hmac_create(gw.id, secret="shh"))
    encrypted_delivery = session.get(EventSubscription, sub.id).delivery
    stored_auth = json.loads(json.dumps(encrypted_delivery["auth"]))

    adapter, _captured = _capture_adapter()
    live_sub = _Sub("http://localhost:1/cb", delivery=encrypted_delivery)
    asyncio.run(adapter.deliver(delivery_envelope=_envelope("evt-y"), subscription=live_sub))

    # The subscription object the adapter held must still carry ciphertext, not plaintext.
    assert "secret" not in live_sub.delivery["auth"]
    assert live_sub.delivery["auth"] == stored_auth


def test_encrypted_bearer_token_produces_authorization_header(session):
    """The bearer token is decrypted at delivery and emitted as Authorization: Bearer."""
    gw = _make_gateway(session)
    payload = SubscriptionCreate(
        gateway_id=gw.id,
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://receiver.example.com/hook"),
        event_types=["com.github.push"],
        delivery={"auth": {"strategy": "bearer", "token": "t0k"}},
    )
    sub = _create(session, payload)
    encrypted_delivery = session.get(EventSubscription, sub.id).delivery
    assert "token" not in encrypted_delivery["auth"]

    adapter, captured = _capture_adapter()
    live_sub = _Sub("http://localhost:1/cb", delivery=encrypted_delivery)
    out = asyncio.run(adapter.deliver(delivery_envelope=_envelope("evt-z"), subscription=live_sub))

    assert out.ok is True
    assert captured[0].headers["Authorization"] == "Bearer t0k"
