# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_verify.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.verify**.

This is the first signature-verification code in the repository, so the tests
build SIGNED fixtures from scratch (computing real HMACs with a known secret)
and assert the security-critical behaviour of the descriptor-driven verifier:

* GitHub ``X-Hub-Signature-256`` (``hmac``, sha256, hex, ``sha256=`` prefix) and
  the legacy sha1 variant - valid plus tampered/missing.
* Stripe ``Stripe-Signature`` (``hmac_timestamped``, ``t=,v1=,v1=``) - valid,
  stale timestamp, bad ``v1``, and multi-``v1`` rotation.
* Slack ``v0:{timestamp}:{body}`` (``hmac_timestamped``) - valid plus bad sig.
* base64-vs-hex encoding mismatch is rejected.
* multi-secret rotation accepts the previous secret.
* missing signature header fails closed.
* a payload claiming ``alg=none``/``md5`` does NOT change the outcome - the
  verifier always uses ``recipe['algo']`` (CWE-347).
* the comparison routes through ``hmac.compare_digest`` (constant-time).

These cover the M1-gating SEC scenarios SC-SEC-002/003/004/005/006/011.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_verify.py -q
"""

# Future
from __future__ import annotations

# Standard
import base64
import hashlib
import hmac

# First-Party
from mcpgateway.services.events import verify as verify_mod
from mcpgateway.services.events.verify import constant_time_eq, verify_signature, VerifyResult

# --------------------------------------------------------------------------- #
# Helpers / fixture builders (compute REAL HMACs with a known secret)         #
# --------------------------------------------------------------------------- #

KNOWN_SECRET = "s3cr3t-signing-key"
OLD_SECRET = "previous-rotated-key"
NOW = 1_700_000_000  # fixed "now" epoch for deterministic replay tests


def _hex_hmac(secret: str, msg: bytes, algo: str = "sha256") -> str:
    """Return the hex-encoded HMAC of *msg* under *secret* using *algo*."""
    return hmac.new(secret.encode("utf-8"), msg, getattr(hashlib, algo)).hexdigest()


def _b64_hmac(secret: str, msg: bytes, algo: str = "sha256") -> str:
    """Return the base64-encoded HMAC of *msg* under *secret* using *algo*."""
    digest = hmac.new(secret.encode("utf-8"), msg, getattr(hashlib, algo)).digest()
    return base64.b64encode(digest).decode("ascii")


# --- GitHub (hmac) ---------------------------------------------------------- #

GITHUB_BODY = b'{"ref":"refs/heads/main","repository":{"full_name":"octo/repo"}}'


def _github_recipe(algo: str = "sha256") -> dict:
    header = "X-Hub-Signature-256" if algo == "sha256" else "X-Hub-Signature"
    prefix = "sha256=" if algo == "sha256" else "sha1="
    return {
        "strategy": "hmac",
        "header": header,
        "algo": algo,
        "encoding": "hex",
        "prefix": prefix,
    }


def _github_headers(body: bytes, *, secret: str = KNOWN_SECRET, algo: str = "sha256") -> dict:
    header = "X-Hub-Signature-256" if algo == "sha256" else "X-Hub-Signature"
    prefix = "sha256=" if algo == "sha256" else "sha1="
    return {header: prefix + _hex_hmac(secret, body, algo), "X-GitHub-Event": "push"}


# --- Stripe (hmac_timestamped, scheme=stripe) ------------------------------- #

STRIPE_BODY = b'{"id":"evt_1","type":"payment_intent.succeeded"}'


def _stripe_recipe() -> dict:
    return {
        "strategy": "hmac_timestamped",
        "header": "Stripe-Signature",
        "algo": "sha256",
        "encoding": "hex",
        "signature_scheme": "stripe",
        "signed_payload": "{timestamp}.{body}",
    }


def _stripe_header(body: bytes, ts: int, *, secrets, extra_bad: bool = False, rotate_first: bool = False) -> str:
    """Build a ``t=...,v1=...`` Stripe-Signature header.

    secrets: list of secrets to emit a v1 candidate for (rotation/multi-cand).
    extra_bad: prepend a junk v1 candidate that should be ignored.
    rotate_first: emit a junk v1 BEFORE the real ones (rotation accept-any).
    """
    signed = f"{ts}.".encode("ascii") + body
    parts = [f"t={ts}"]
    if extra_bad or rotate_first:
        parts.append("v1=deadbeef")
    for s in secrets:
        parts.append("v1=" + _hex_hmac(s, signed, "sha256"))
    return ",".join(parts)


# --- Slack (hmac_timestamped, scheme=slack) --------------------------------- #

SLACK_BODY = b'{"type":"event_callback","event":{"type":"message"}}'


def _slack_recipe() -> dict:
    return {
        "strategy": "hmac_timestamped",
        "header": "X-Slack-Signature",
        "algo": "sha256",
        "encoding": "hex",
        "prefix": "v0=",
        "signature_scheme": "slack",
        "signed_payload": "v0:{timestamp}:{body}",
        "timestamp_header": "X-Slack-Request-Timestamp",
    }


def _slack_headers(body: bytes, ts: int, *, secret: str = KNOWN_SECRET) -> dict:
    signed = f"v0:{ts}:".encode("ascii") + body
    sig = "v0=" + _hex_hmac(secret, signed, "sha256")
    return {"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": str(ts)}


# --------------------------------------------------------------------------- #
# constant_time_eq                                                            #
# --------------------------------------------------------------------------- #


def test_constant_time_eq_matches_and_mismatches():
    assert constant_time_eq("abc123", "abc123") is True
    assert constant_time_eq("abc123", "abc124") is False
    assert constant_time_eq("abc", "abcd") is False
    assert constant_time_eq("", "") is True


def test_constant_time_eq_uses_compare_digest(monkeypatch):
    """SC-SEC-002 / TC-ING-017: comparison MUST route through hmac.compare_digest
    (constant-time, no early return on the first differing character)."""
    calls = {"n": 0}
    real = hmac.compare_digest

    def _spy(a, b):
        calls["n"] += 1
        return real(a, b)

    monkeypatch.setattr(verify_mod.hmac, "compare_digest", _spy)
    assert constant_time_eq("xyz", "xyz") is True
    assert calls["n"] >= 1


# --------------------------------------------------------------------------- #
# GitHub hmac (valid / tampered / missing)                                    #
# --------------------------------------------------------------------------- #


def test_github_valid_signature_ok():
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY),
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert isinstance(res, VerifyResult)
    assert res.ok is True


def test_github_tampered_body_rejected():
    headers = _github_headers(GITHUB_BODY)
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY + b"tampered",
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


def test_github_legacy_sha1_valid():
    res = verify_signature(
        recipe=_github_recipe("sha1"),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers=_github_headers(GITHUB_BODY, algo="sha1"),
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


def test_github_missing_signature_fails_closed():
    """SC-ING-056 / fail-closed: missing signature header => not ok."""
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers={"X-GitHub-Event": "push"},  # no signature header
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "missing_signature"


def test_github_empty_signature_fails_closed():
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers={"X-Hub-Signature-256": ""},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "missing_signature"


def test_github_header_lookup_case_insensitive():
    headers = {"x-hub-signature-256": "sha256=" + _hex_hmac(KNOWN_SECRET, GITHUB_BODY)}
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


# --------------------------------------------------------------------------- #
# SC-SEC-003: algorithm confusion / downgrade (CWE-347)                       #
# --------------------------------------------------------------------------- #


def test_payload_claiming_alg_none_does_not_change_outcome():
    """SC-SEC-003: a body claiming alg=none must not bypass verification."""
    body = b'{"alg":"none","type":"payment_intent.succeeded"}'
    # No valid signature provided for this body -> must be rejected.
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=body,
        headers={"X-Hub-Signature-256": "sha256=" + ("0" * 64)},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


def test_payload_claiming_alg_md5_still_verified_with_recipe_algo():
    """SC-SEC-003: even if the body claims md5, the verifier uses recipe algo.

    A signature computed with the recipe's sha256 over a body that lies about
    its alg STILL verifies (the verifier never reads alg from the payload), and
    a body whose only "signature" is an md5 digest is rejected.
    """
    body = b'{"alg":"md5","hello":"world"}'
    # Correct sha256 signature over the lying body -> accepted (algo is recipe-pinned).
    good_headers = {"X-Hub-Signature-256": "sha256=" + _hex_hmac(KNOWN_SECRET, body, "sha256")}
    res_good = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=body,
        headers=good_headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res_good.ok is True

    # An md5-based forgery is rejected because the recipe pins sha256.
    md5_forgery = hmac.new(KNOWN_SECRET.encode(), body, hashlib.md5).hexdigest()
    res_bad = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=body,
        headers={"X-Hub-Signature-256": "sha256=" + md5_forgery},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res_bad.ok is False


# --------------------------------------------------------------------------- #
# SC-SEC-004: HMAC key confusion across providers/tenants                     #
# --------------------------------------------------------------------------- #


def test_wrong_secret_rejected():
    """SC-SEC-004: a payload signed with provider A's secret must not verify
    against provider B's secret."""
    headers = _github_headers(GITHUB_BODY, secret="provider-A-secret")
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=["provider-B-secret"],
        raw_body=GITHUB_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


# --------------------------------------------------------------------------- #
# SC-SEC-005: encoding mismatch (base64 vs hex)                               #
# --------------------------------------------------------------------------- #


def test_base64_encoding_valid():
    recipe = {"strategy": "hmac", "header": "X-Shopify-Hmac-Sha256", "algo": "sha256", "encoding": "base64"}
    headers = {"X-Shopify-Hmac-Sha256": _b64_hmac(KNOWN_SECRET, GITHUB_BODY)}
    res = verify_signature(recipe=recipe, secrets=[KNOWN_SECRET], raw_body=GITHUB_BODY, headers=headers, now_epoch=NOW, tolerance_seconds=300)
    assert res.ok is True


def test_sec005_verifies_over_raw_bytes_not_reserialized():
    """TC-SEC-005: the HMAC is taken over the EXACT raw bytes.

    A body with reordered keys and extra whitespace verifies when the signature
    is computed over those exact bytes (pass), but the same signature over a
    re-serialized (whitespace-stripped) copy of the body would NOT match - the
    verifier never canonicalizes/re-serializes before hashing.
    """
    spaced_body = b'{"b": "2", "a": "1"}'  # reordered keys + inner whitespace
    compact_body = b'{"a":"1","b":"2"}'  # what json.dumps(sort_keys) would emit

    sig = "sha256=" + _hex_hmac(KNOWN_SECRET, spaced_body)
    headers = {"X-Hub-Signature-256": sig}

    # Verifies over the exact bytes that were signed.
    res_raw = verify_signature(recipe=_github_recipe(), secrets=[KNOWN_SECRET], raw_body=spaced_body, headers=headers, now_epoch=NOW, tolerance_seconds=300)
    assert res_raw.ok is True

    # The same signature against a re-serialized copy must NOT verify.
    res_reser = verify_signature(recipe=_github_recipe(), secrets=[KNOWN_SECRET], raw_body=compact_body, headers=headers, now_epoch=NOW, tolerance_seconds=300)
    assert res_reser.ok is False


def test_base64_signature_rejected_under_hex_recipe():
    """SC-SEC-005: a base64 signature must not validate when recipe says hex."""
    recipe = _github_recipe()  # encoding hex
    headers = {"X-Hub-Signature-256": "sha256=" + _b64_hmac(KNOWN_SECRET, GITHUB_BODY)}
    res = verify_signature(recipe=recipe, secrets=[KNOWN_SECRET], raw_body=GITHUB_BODY, headers=headers, now_epoch=NOW, tolerance_seconds=300)
    assert res.ok is False


# --------------------------------------------------------------------------- #
# Multi-secret rotation                                                       #
# --------------------------------------------------------------------------- #


def test_rotation_accepts_previous_secret():
    """A request signed with the OLD secret still verifies during rotation."""
    headers = _github_headers(GITHUB_BODY, secret=OLD_SECRET)
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET, OLD_SECRET],  # current first, previous second
        raw_body=GITHUB_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


def test_rotation_rejects_unknown_secret():
    headers = _github_headers(GITHUB_BODY, secret="never-issued")
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET, OLD_SECRET],
        raw_body=GITHUB_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


# --------------------------------------------------------------------------- #
# Stripe (hmac_timestamped, scheme=stripe)                                    #
# --------------------------------------------------------------------------- #


def test_stripe_valid_signature_ok():
    header = _stripe_header(STRIPE_BODY, NOW, secrets=[KNOWN_SECRET])
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": header},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


def test_stripe_stale_timestamp_rejected():
    """SC-SEC-011: a captured-but-old signed request is rejected as stale."""
    old_ts = NOW - 600  # 10 min in the past, tolerance 300
    header = _stripe_header(STRIPE_BODY, old_ts, secrets=[KNOWN_SECRET])
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": header},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "stale"


def test_stripe_future_timestamp_rejected():
    future_ts = NOW + 600
    header = _stripe_header(STRIPE_BODY, future_ts, secrets=[KNOWN_SECRET])
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": header},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "stale"


def test_stripe_bad_v1_rejected():
    """A v1 candidate that does not match any secret is rejected."""
    header = f"t={NOW},v1={'0' * 64}"
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": header},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


def test_stripe_multi_v1_rotation_accepts_one():
    """SC-SEC-011-adjacent: multiple v1 candidates - accept if ANY matches."""
    header = _stripe_header(STRIPE_BODY, NOW, secrets=[OLD_SECRET], rotate_first=True)
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET, OLD_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": header},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


def test_stripe_missing_signature_fails_closed():
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "missing_signature"


def test_stripe_signature_binds_timestamp_sec006():
    """SC-SEC-006: keep body + sig, alter the timestamp -> rejected.

    The signed payload binds the timestamp, so substituting a different ``t``
    while reusing the old ``v1`` breaks the HMAC.
    """
    real = _stripe_header(STRIPE_BODY, NOW, secrets=[KNOWN_SECRET])
    real_v1 = real.split("v1=")[1]
    tampered = f"t={NOW - 5},v1={real_v1}"  # same sig, different (still-fresh) ts
    res = verify_signature(
        recipe=_stripe_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=STRIPE_BODY,
        headers={"Stripe-Signature": tampered},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


# --------------------------------------------------------------------------- #
# Slack (hmac_timestamped, scheme=slack)                                      #
# --------------------------------------------------------------------------- #


def test_slack_valid_signature_ok():
    res = verify_signature(
        recipe=_slack_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=SLACK_BODY,
        headers=_slack_headers(SLACK_BODY, NOW),
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True


def test_slack_bad_signature_rejected():
    headers = _slack_headers(SLACK_BODY, NOW)
    headers["X-Slack-Signature"] = "v0=" + ("a" * 64)
    res = verify_signature(
        recipe=_slack_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=SLACK_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


def test_slack_stale_timestamp_rejected():
    res = verify_signature(
        recipe=_slack_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=SLACK_BODY,
        headers=_slack_headers(SLACK_BODY, NOW - 1000),
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "stale"


def test_slack_missing_signature_fails_closed():
    res = verify_signature(
        recipe=_slack_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=SLACK_BODY,
        headers={"X-Slack-Request-Timestamp": str(NOW)},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "missing_signature"


def test_slack_missing_timestamp_fails_closed():
    headers = _slack_headers(SLACK_BODY, NOW)
    del headers["X-Slack-Request-Timestamp"]
    res = verify_signature(
        recipe=_slack_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=SLACK_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


# --------------------------------------------------------------------------- #
# strategy none / plugin                                                      #
# --------------------------------------------------------------------------- #


def test_strategy_none_is_ok_unsigned():
    res = verify_signature(
        recipe={"strategy": "none"},
        secrets=[],
        raw_body=b"anything",
        headers={},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True
    assert res.reason == "unsigned"


def test_strategy_plugin_not_wired_for_m1():
    res = verify_signature(
        recipe={"strategy": "plugin", "plugin_ref": "aws_sns_verifier"},
        secrets=[KNOWN_SECRET],
        raw_body=b"{}",
        headers={},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False
    assert res.reason == "plugin_not_wired"


def test_unknown_strategy_fails_closed():
    res = verify_signature(
        recipe={"strategy": "totally-bogus"},
        secrets=[KNOWN_SECRET],
        raw_body=b"{}",
        headers={},
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is False


# --------------------------------------------------------------------------- #
# Multi-candidate comma-separated header (hmac strategy)                       #
# --------------------------------------------------------------------------- #


def test_hmac_multi_candidate_header_accepts_any():
    good = "sha256=" + _hex_hmac(KNOWN_SECRET, GITHUB_BODY)
    headers = {"X-Hub-Signature-256": "sha256=" + ("0" * 64) + "," + good}
    res = verify_signature(
        recipe=_github_recipe(),
        secrets=[KNOWN_SECRET],
        raw_body=GITHUB_BODY,
        headers=headers,
        now_epoch=NOW,
        tolerance_seconds=300,
    )
    assert res.ok is True
