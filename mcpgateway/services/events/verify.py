# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/verify.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Inbound webhook signature verification for MCP server-initiated events.

This is the first signature-verification code in the repository. It is purely
data-driven from a provider descriptor's ``verify`` recipe (see the provider
descriptor schema, FRD §6.3) and lives off the request hot-path: it operates on
the *raw request bytes* captured before any JSON parsing (re-serialization
would change byte order/whitespace and break the HMAC, FRD §10.1.1).

Security invariants (FRD §10.1.1, scenarios SC-SEC-002/003/004/005/006/011):

* The digest algorithm comes ONLY from ``recipe["algo"]``. The verifier NEVER
  reads an algorithm from the payload (CWE-347, algorithm confusion/downgrade).
* Comparison is constant-time via :func:`hmac.compare_digest`.
* A missing or empty signature header fails closed
  (``VerifyResult(False, "missing_signature")``).
* ``hmac_timestamped`` recipes reject requests outside the replay tolerance
  window (``VerifyResult(False, "stale")``).
* The signing secret is selected by route/connection, never by the payload;
  multiple secrets (rotation window) and multiple header candidates are each
  tried and accepted only if ANY candidate matches ANY secret.

The crypto here is HMAC message-authentication; it is intentionally separate
from ``mcpgateway/utils/services_auth.py`` (AES-GCM credential encryption),
which MUST NOT be repurposed for message authentication.
"""

# Future
from __future__ import annotations

# Standard
import base64
from dataclasses import dataclass
import hashlib
import hmac
from typing import List, Mapping, Optional, Tuple

__all__ = ["VerifyResult", "constant_time_eq", "verify_signature"]

# Digest algorithms allowed in a recipe. The algorithm is ALWAYS taken from the
# descriptor recipe, never inferred from the request payload (CWE-347).
_ALGOS = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}


@dataclass
class VerifyResult:
    """Outcome of a signature verification.

    Attributes:
        ok: ``True`` when the request is authenticated (or unsigned-and-allowed
            for ``strategy="none"``).
        reason: Short machine-readable reason for the outcome. Notable values:
            ``"ok"``, ``"unsigned"``, ``"missing_signature"``, ``"stale"``,
            ``"mismatch"``, ``"bad_recipe"``, ``"plugin_not_wired"``.
    """

    ok: bool
    reason: str = ""


def constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time.

    Wraps :func:`hmac.compare_digest`, encoding ``str`` inputs to ``bytes`` so
    the comparison is byte-safe and does not short-circuit on the first
    differing character (SC-SEC-002).

    Args:
        a: First value.
        b: Second value.

    Returns:
        ``True`` if the values are equal, ``False`` otherwise.

    Examples:
        >>> constant_time_eq("abc", "abc")
        True
        >>> constant_time_eq("abc", "abd")
        False
        >>> constant_time_eq("abc", "abcd")
        False
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _encode(digest: bytes, encoding: str) -> str:
    """Encode a raw digest per the recipe encoding (``hex`` or ``base64``).

    Args:
        digest: Raw HMAC digest bytes.
        encoding: ``"hex"`` or ``"base64"``.

    Returns:
        The encoded digest string. Unknown encodings fall back to hex.
    """
    if encoding == "base64":
        return base64.b64encode(digest).decode("ascii")
    return digest.hex()


def _hmac_encode(secret: str, message: bytes, algo: str, encoding: str) -> str:
    """Compute ``ENCODE(HMAC(secret, message, algo))``.

    Args:
        secret: Signing secret (per-connection, route-selected).
        message: Exact bytes that were signed.
        algo: Digest algorithm name from the recipe (sha1/sha256/sha512).
        encoding: Output encoding (hex/base64).

    Returns:
        The encoded HMAC digest string.
    """
    digestmod = _ALGOS.get(algo, hashlib.sha256)
    mac = hmac.new(secret.encode("utf-8"), message, digestmod)
    return _encode(mac.digest(), encoding)


def _get_header(headers: Mapping[str, str], name: Optional[str]) -> Optional[str]:
    """Case-insensitive header lookup.

    Args:
        headers: Request headers (any case).
        name: Header name to look up.

    Returns:
        The header value, or ``None`` if the name is falsy or absent.
    """
    if not name:
        return None
    # Fast path for exact / Starlette-style mappings.
    val = headers.get(name)
    if val is not None:
        return val
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _strip_prefix(value: str, prefix: Optional[str]) -> str:
    """Strip a literal prefix (e.g. ``"sha256="``) from a header value.

    Args:
        value: Raw signature candidate.
        prefix: Optional literal prefix to remove.

    Returns:
        The value with the prefix removed if present.
    """
    if prefix and value.startswith(prefix):
        return value[len(prefix) :]
    return value


def _candidates(header_value: str, prefix: Optional[str]) -> List[str]:
    """Split a (possibly comma-separated) header value into trimmed candidates.

    Each candidate has the optional ``prefix`` stripped. Empty fragments are
    dropped.

    Args:
        header_value: Raw header value.
        prefix: Optional literal prefix on each candidate.

    Returns:
        List of non-empty candidate signature strings.
    """
    out: List[str] = []
    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(_strip_prefix(part, prefix))
    return out


def _any_match(candidates: List[str], secrets: List[str], message: bytes, algo: str, encoding: str) -> bool:
    """Return ``True`` if any candidate matches any secret in constant time.

    Args:
        candidates: Signature candidates from the header.
        secrets: Signing secrets to try (current first, previous after).
        message: Exact signed bytes.
        algo: Recipe digest algorithm.
        encoding: Recipe encoding.

    Returns:
        ``True`` if at least one (candidate, secret) pair matches.
    """
    matched = False
    for secret in secrets:
        if not secret:
            continue
        expected = _hmac_encode(secret, message, algo, encoding)
        for cand in candidates:
            # Do not short-circuit the loop: evaluate every pair so the work is
            # independent of where the match is (defense-in-depth alongside the
            # constant-time compare itself).
            if constant_time_eq(expected, cand):
                matched = True
    return matched


def _render_signed_payload(template: Optional[str], *, timestamp: str, raw_body: bytes) -> bytes:
    """Render the ``signed_payload`` template into the exact signed bytes.

    Only ``{timestamp}`` and ``{body}`` tokens are substituted; the body is the
    raw request bytes (never re-parsed/re-serialized JSON, SC-SEC-005).

    Args:
        template: Recipe ``signed_payload`` template, e.g. ``"{timestamp}.{body}"``.
            Defaults to ``"{body}"`` when ``None``.
        timestamp: Provider timestamp string.
        raw_body: Exact raw request bytes.

    Returns:
        The byte string that should have been signed.
    """
    tmpl = template if template is not None else "{body}"
    # Split on {body} so the raw bytes are spliced verbatim (no decode round-trip).
    head, _, tail = tmpl.partition("{body}")
    head = head.replace("{timestamp}", timestamp)
    tail = tail.replace("{timestamp}", timestamp)
    return head.encode("utf-8") + raw_body + tail.encode("utf-8")


def _parse_timestamped(recipe: dict, headers: Mapping[str, str]) -> Tuple[Optional[str], List[str]]:
    """Parse the timestamp and signature candidates for an ``hmac_timestamped``
    recipe according to its ``signature_scheme``.

    Args:
        recipe: The descriptor ``verify`` recipe.
        headers: Request headers.

    Returns:
        A ``(timestamp, candidates)`` tuple. ``timestamp`` is ``None`` and the
        candidate list empty when the required material is missing.
    """
    scheme = recipe.get("signature_scheme", "simple")
    header_name = recipe.get("header")
    prefix = recipe.get("prefix")

    if scheme == "stripe":
        # Header form: "t=NNN,v1=aaa,v1=bbb" (multiple v1 candidates allowed).
        raw = _get_header(headers, header_name)
        if not raw:
            return None, []
        ts: Optional[str] = None
        sigs: List[str] = []
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "t":
                ts = value
            elif key == "v1":
                if value:
                    sigs.append(value)
        return ts, sigs

    if scheme == "slack":
        # ts from X-Slack-Request-Timestamp; sig from header (strip "v0=").
        ts = _get_header(headers, recipe.get("timestamp_header"))
        raw = _get_header(headers, header_name)
        if not ts or not raw:
            return ts, []
        return ts, _candidates(raw, prefix)

    # "simple": ts from timestamp_header; sig from header (strip optional prefix).
    ts = _get_header(headers, recipe.get("timestamp_header"))
    raw = _get_header(headers, header_name)
    if not raw:
        return ts, []
    return ts, _candidates(raw, prefix)


def verify_signature(  # pylint: disable=too-many-return-statements
    *,
    recipe: dict,
    secrets: List[str],
    raw_body: bytes,
    headers: Mapping[str, str],
    now_epoch: int,
    tolerance_seconds: int,
) -> VerifyResult:
    """Verify an inbound webhook signature against a descriptor recipe.

    The verifier is fully data-driven: it dispatches on ``recipe["strategy"]``
    and uses ``recipe["algo"]`` / ``recipe["encoding"]`` for the digest. It
    NEVER inspects the payload for an algorithm (CWE-347), operates on the raw
    request bytes, and fails closed when signature material is missing.

    Args:
        recipe: The descriptor ``verify`` block. Recognized keys include
            ``strategy`` (``hmac`` | ``hmac_timestamped`` | ``none`` |
            ``plugin``), ``header``, ``algo`` (default ``sha256``), ``encoding``
            (default ``hex``), ``prefix``, ``signed_payload``,
            ``timestamp_header``, ``signature_scheme`` (``simple`` | ``stripe``
            | ``slack``), and ``allow_unsigned``.
        secrets: Signing secrets to try, current first then any rotation slots.
        raw_body: The exact raw request bytes (captured pre-parse).
        headers: Request headers (case-insensitive lookup).
        now_epoch: Current time as a Unix epoch integer.
        tolerance_seconds: Replay window for ``hmac_timestamped``.

    Returns:
        VerifyResult: ``ok`` is ``True`` only when the signature is valid (or
        ``strategy="none"``).

    Examples:
        >>> verify_signature(recipe={"strategy": "none"}, secrets=[],
        ...     raw_body=b"x", headers={}, now_epoch=0, tolerance_seconds=300).ok
        True
        >>> r = verify_signature(recipe={"strategy": "plugin"}, secrets=[],
        ...     raw_body=b"x", headers={}, now_epoch=0, tolerance_seconds=300)
        >>> (r.ok, r.reason)
        (False, 'plugin_not_wired')
    """
    strategy = recipe.get("strategy", "hmac")

    if strategy == "none":
        # The verifier returns "unsigned"; the INGRESS layer decides whether to
        # actually allow unsigned traffic (SC-SEC-001).
        return VerifyResult(True, "unsigned")

    if strategy == "plugin":
        # Escape hatch (e.g. AWS SNS cert chain). Real dispatch lands later; for
        # now fail closed.
        return VerifyResult(False, "plugin_not_wired")

    algo = recipe.get("algo", "sha256")
    encoding = recipe.get("encoding", "hex")

    if strategy == "hmac":
        header_value = _get_header(headers, recipe.get("header"))
        if not header_value:
            return VerifyResult(False, "missing_signature")
        candidates = _candidates(header_value, recipe.get("prefix"))
        if not candidates:
            return VerifyResult(False, "missing_signature")
        message = _render_signed_payload(recipe.get("signed_payload"), timestamp="", raw_body=raw_body)
        if _any_match(candidates, secrets, message, algo, encoding):
            return VerifyResult(True, "ok")
        return VerifyResult(False, "mismatch")

    if strategy == "hmac_timestamped":
        timestamp, candidates = _parse_timestamped(recipe, headers)
        if not candidates:
            return VerifyResult(False, "missing_signature")
        if timestamp is None:
            return VerifyResult(False, "missing_signature")
        try:
            ts_int = int(timestamp)
        except (TypeError, ValueError):
            return VerifyResult(False, "missing_signature")
        # Replay guard (SC-SEC-011): reject stale OR future-skewed timestamps.
        if abs(now_epoch - ts_int) > tolerance_seconds:
            return VerifyResult(False, "stale")
        message = _render_signed_payload(recipe.get("signed_payload"), timestamp=timestamp, raw_body=raw_body)
        if _any_match(candidates, secrets, message, algo, encoding):
            return VerifyResult(True, "ok")
        return VerifyResult(False, "mismatch")

    # Unknown strategy: fail closed.
    return VerifyResult(False, "bad_recipe")
