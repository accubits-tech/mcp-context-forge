# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_signing.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the L3 outbound delivery signing helpers
(:mod:`mcpgateway.services.events.egress.signing`).

Covers TC-SEC-051: the outbound HTTP-callback POST carries an HMAC signature,
a timestamp, the event id, and an ``Idempotency-Key``, and a receiver that
recomputes ``HMAC(secret, "{ts}.{body}")`` with the shared secret verifies it;
plus the ``bearer`` / ``none`` auth strategies and a body-tamper negative.
"""

# Standard
import hashlib
import hmac

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.egress.signing import auth_headers, build_signed_headers


def _recompute(secret: str, ts: str, body: bytes, algo: str = "sha256") -> str:
    """Receiver-side recomputation of the wire signature over ``{ts}.{body}``."""
    mac = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, getattr(hashlib, algo))
    return f"{algo}=" + mac.hexdigest()


class TestBuildSignedHeaders:
    """Wire shape and verifiability of :func:`build_signed_headers`."""

    def test_idempotency_key_always_present_without_secret(self):
        """No secret -> only the Idempotency-Key header is set."""
        headers = build_signed_headers(body=b'{"a":1}', secret=None, event_id="evt-1", now_epoch=1_700_000_000)
        assert headers["Idempotency-Key"] == "evt-1"
        assert "X-MCPGW-Signature" not in headers
        assert "X-MCPGW-Timestamp" not in headers
        assert "X-MCPGW-Event-Id" not in headers

    def test_empty_secret_is_treated_as_no_secret(self):
        """An empty-string secret does not produce a signature."""
        headers = build_signed_headers(body=b"{}", secret="", event_id="evt-2", now_epoch=1_700_000_000)
        assert headers["Idempotency-Key"] == "evt-2"
        assert "X-MCPGW-Signature" not in headers

    def test_signed_headers_carry_sig_ts_event_id_and_idempotency_key(self):
        """TC-SEC-051: signed delivery carries sig + ts + event id + Idempotency-Key."""
        body = b'{"event":{"id":"evt-9"},"n":42}'
        secret = "s3cr3t-shared"
        headers = build_signed_headers(body=body, secret=secret, event_id="evt-9", now_epoch=1_700_000_123)

        assert headers["Idempotency-Key"] == "evt-9"
        assert headers["X-MCPGW-Event-Id"] == "evt-9"
        assert headers["X-MCPGW-Timestamp"] == "1700000123"
        assert headers["X-MCPGW-Signature"].startswith("sha256=")

    def test_receiver_recomputes_and_verifies_signature(self):
        """A receiver recomputing HMAC over ``{ts}.{body}`` with the secret matches."""
        body = b'{"hello":"world"}'
        secret = "shared-key"
        ts_epoch = 1_700_000_456
        headers = build_signed_headers(body=body, secret=secret, event_id="evt-r", now_epoch=ts_epoch)

        expected = _recompute(secret, headers["X-MCPGW-Timestamp"], body)
        assert hmac.compare_digest(headers["X-MCPGW-Signature"], expected)

    def test_deterministic_for_fixed_timestamp(self):
        """Same inputs (fixed ts) produce byte-identical signatures."""
        body = b'{"k":"v"}'
        h1 = build_signed_headers(body=body, secret="key", event_id="e", now_epoch=42)
        h2 = build_signed_headers(body=body, secret="key", event_id="e", now_epoch=42)
        assert h1["X-MCPGW-Signature"] == h2["X-MCPGW-Signature"]

    def test_tampering_body_breaks_recomputed_signature(self):
        """Altering the body after signing invalidates the receiver-side check."""
        body = b'{"amount":10}'
        secret = "shared-key"
        headers = build_signed_headers(body=body, secret=secret, event_id="evt-t", now_epoch=1_700_000_789)

        tampered = b'{"amount":99999}'
        forged = _recompute(secret, headers["X-MCPGW-Timestamp"], tampered)
        assert not hmac.compare_digest(headers["X-MCPGW-Signature"], forged)

    def test_signature_binds_timestamp(self):
        """A different timestamp yields a different signature for the same body."""
        body = b"{}"
        a = build_signed_headers(body=body, secret="key", event_id="e", now_epoch=1)
        b = build_signed_headers(body=body, secret="key", event_id="e", now_epoch=2)
        assert a["X-MCPGW-Signature"] != b["X-MCPGW-Signature"]

    def test_custom_algo_sha512(self):
        """The algo argument selects the digest and is reflected in the prefix."""
        body = b'{"x":1}'
        secret = "k"
        headers = build_signed_headers(body=body, secret=secret, event_id="e", now_epoch=7, algo="sha512")
        assert headers["X-MCPGW-Signature"].startswith("sha512=")
        expected = _recompute(secret, headers["X-MCPGW-Timestamp"], body, algo="sha512")
        assert hmac.compare_digest(headers["X-MCPGW-Signature"], expected)


class TestAuthHeaders:
    """Per-subscription :func:`auth_headers` strategy dispatch."""

    def test_bearer_strategy_sets_authorization(self):
        """bearer -> Authorization: Bearer <token>, no signature headers."""
        headers = auth_headers({"strategy": "bearer", "token": "abc.def.ghi"})
        assert headers["Authorization"] == "Bearer abc.def.ghi"
        assert "X-MCPGW-Signature" not in headers

    def test_none_strategy_adds_no_auth(self):
        """none -> no Authorization / signature headers (Idempotency-Key handled elsewhere)."""
        headers = auth_headers({"strategy": "none"})
        assert "Authorization" not in headers
        assert "X-MCPGW-Signature" not in headers

    def test_missing_strategy_defaults_to_none(self):
        """An empty / missing auth block adds nothing."""
        assert auth_headers({}) == {}
        assert auth_headers(None) == {}

    def test_hmac_strategy_signs_with_secret(self):
        """hmac -> X-MCPGW-Signature/-Timestamp/-Event-Id verifiable by the receiver."""
        body = b'{"e":1}'
        secret = "hmac-secret"
        headers = auth_headers(
            {"strategy": "hmac", "secret": secret},
            body=body,
            event_id="evt-h",
            now_epoch=1_700_000_999,
        )
        assert headers["X-MCPGW-Event-Id"] == "evt-h"
        assert headers["X-MCPGW-Timestamp"] == "1700000999"
        expected = _recompute(secret, headers["X-MCPGW-Timestamp"], body)
        assert hmac.compare_digest(headers["X-MCPGW-Signature"], expected)

    def test_mtls_strategy_adds_no_headers(self):
        """mtls is handled by the client (cert paths), not via headers."""
        assert auth_headers({"strategy": "mtls", "cert": "/x.pem"}) == {}
