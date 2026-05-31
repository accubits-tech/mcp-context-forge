# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/signing.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Outbound delivery signing for the L3 HTTP-callback egress adapter.

The HTTP-callback adapter delivers each event as one signed outbound ``POST`` of
the canonical §7.2 / §9.1a delivery envelope (FRD §9, §10.1). This module owns
the *outbound* header construction:

* :func:`build_signed_headers` always sets ``Idempotency-Key`` (= the event id;
  at-least-once delivery, receiver dedupes - FR-26). When a shared secret is
  configured it additionally stamps ``X-MCPGW-Timestamp``, ``X-MCPGW-Event-Id``,
  and ``X-MCPGW-Signature = "sha256=" + hex(HMAC(secret, "{ts}.{body}"))``. The
  signature binds the timestamp + the exact body bytes, so a receiver that
  recomputes the HMAC over ``"{ts}.{body}"`` with the shared secret verifies it
  (TC-SEC-051), and any body/timestamp tamper invalidates it.

* :func:`auth_headers` dispatches on the per-subscription ``delivery.auth``
  block: ``hmac`` signs (delegating to :func:`build_signed_headers`),
  ``bearer`` sets ``Authorization: Bearer <token>``, ``none`` adds nothing, and
  ``mtls`` is handled by the HTTP client (client-cert paths), not via headers.

This is HMAC *message authentication* of an outbound request; it is deliberately
distinct from :mod:`mcpgateway.utils.services_auth` (AES-GCM credential
encryption, used only to encrypt the per-subscription secret at rest). The wire
recipe mirrors the inbound verifier in :mod:`mcpgateway.services.events.verify`
so the two halves of the HMAC contract stay symmetric.
"""

# Future
from __future__ import annotations

# Standard
import hashlib
import hmac
from typing import Any, Dict, Mapping, Optional

__all__ = ["build_signed_headers", "auth_headers"]

# Header names for the outbound HMAC signature scheme. Kept module-level so the
# adapter and tests reference one source of truth.
HEADER_SIGNATURE = "X-MCPGW-Signature"
HEADER_TIMESTAMP = "X-MCPGW-Timestamp"
HEADER_EVENT_ID = "X-MCPGW-Event-Id"
HEADER_IDEMPOTENCY = "Idempotency-Key"

# Digest algorithms allowed for outbound signing. The algorithm is chosen by the
# gateway (never read from any payload), mirroring the inbound verifier.
_ALGOS = {
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
    "sha1": hashlib.sha1,
}


def build_signed_headers(
    *,
    body: bytes,
    secret: Optional[str],
    event_id: str,
    now_epoch: int,
    algo: str = "sha256",
) -> Dict[str, str]:
    """Build the outbound delivery headers for one HTTP-callback POST.

    ``Idempotency-Key`` is always set to ``event_id`` (at-least-once delivery;
    receivers dedupe on it - FR-26). When ``secret`` is a non-empty string the
    body is HMAC-signed: the signature is computed over the exact bytes
    ``"{now_epoch}.".encode() + body`` so it binds both the timestamp and the
    request body, and the timestamp + event id are echoed in their own headers
    for the receiver to bind into its own recomputation.

    The result is deterministic for a fixed ``now_epoch`` and ``body``, and the
    signature is byte-for-byte the value a receiver obtains by recomputing
    ``algo + "=" + hex(HMAC(secret, "{ts}.{body}"))`` and comparing with
    :func:`hmac.compare_digest`.

    Args:
        body: The exact serialized request body bytes that will be sent.
        secret: The shared HMAC secret, already decrypted. ``None`` or empty
            disables signing (only ``Idempotency-Key`` is set).
        event_id: The stable per-event id, sent as ``Idempotency-Key`` and (when
            signed) echoed as ``X-MCPGW-Event-Id``.
        now_epoch: The signing timestamp as integer Unix epoch seconds; echoed
            as ``X-MCPGW-Timestamp`` and bound into the signature.
        algo: HMAC digest algorithm name (``sha256`` default); becomes the
            ``"<algo>="`` prefix of the signature.

    Returns:
        A header mapping. Always contains ``Idempotency-Key``; when signed it
        also contains ``X-MCPGW-Timestamp``, ``X-MCPGW-Event-Id``, and
        ``X-MCPGW-Signature``.

    Raises:
        ValueError: If ``algo`` is not a supported digest.

    Examples:
        >>> import hashlib, hmac
        >>> h = build_signed_headers(body=b'{"a":1}', secret="k", event_id="e1", now_epoch=42)
        >>> h["Idempotency-Key"], h["X-MCPGW-Event-Id"], h["X-MCPGW-Timestamp"]
        ('e1', 'e1', '42')
        >>> mac = hmac.new(b"k", b"42." + b'{"a":1}', hashlib.sha256)
        >>> h["X-MCPGW-Signature"] == "sha256=" + mac.hexdigest()
        True
        >>> build_signed_headers(body=b"{}", secret=None, event_id="e2", now_epoch=42)
        {'Idempotency-Key': 'e2'}
    """
    headers: Dict[str, str] = {HEADER_IDEMPOTENCY: event_id}
    if not secret:
        return headers

    digest = _ALGOS.get(algo)
    if digest is None:
        raise ValueError(f"unsupported signing algo: {algo!r}")

    ts = str(now_epoch)
    signing_input = ts.encode("utf-8") + b"." + body
    mac = hmac.new(secret.encode("utf-8"), signing_input, digest)
    headers[HEADER_TIMESTAMP] = ts
    headers[HEADER_EVENT_ID] = event_id
    headers[HEADER_SIGNATURE] = f"{algo}=" + mac.hexdigest()
    return headers


def auth_headers(
    delivery_auth: Optional[Mapping[str, Any]],
    *,
    body: bytes = b"",
    event_id: str = "",
    now_epoch: int = 0,
    algo: str = "sha256",
) -> Dict[str, str]:
    """Build the extra auth headers for a delivery from its ``delivery.auth`` block.

    Dispatches on ``delivery_auth["strategy"]``:

    * ``hmac`` - sign the body via :func:`build_signed_headers` using the
        block's ``secret`` (already decrypted), returning the signature /
        timestamp / event-id headers (the ``Idempotency-Key`` from that call is
        dropped here; the adapter sets it from the envelope). The signing
        timestamp/body/event id are supplied by the caller.
    * ``bearer`` - ``Authorization: Bearer <token>`` from the block's ``token``.
    * ``none`` / missing / unknown - no headers.
    * ``mtls`` - no headers; client-cert material is handled by the HTTP client.

    Args:
        delivery_auth: The per-subscription ``delivery.auth`` mapping (or
            ``None``). Expected keys: ``strategy`` plus ``secret`` (hmac) or
            ``token`` (bearer).
        body: Request body bytes, used only by the ``hmac`` strategy.
        event_id: Event id, used only by the ``hmac`` strategy.
        now_epoch: Signing timestamp (epoch seconds), used only by ``hmac``.
        algo: HMAC digest algorithm, used only by ``hmac``.

    Returns:
        The extra headers to merge into the outbound request (never includes
        ``Idempotency-Key``, which the adapter owns).

    Examples:
        >>> auth_headers({"strategy": "bearer", "token": "t0k"})
        {'Authorization': 'Bearer t0k'}
        >>> auth_headers({"strategy": "none"})
        {}
        >>> auth_headers(None)
        {}
    """
    if not delivery_auth:
        return {}

    strategy = (delivery_auth.get("strategy") or "none").lower()

    if strategy == "bearer":
        token = delivery_auth.get("token")
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    if strategy == "hmac":
        secret = delivery_auth.get("secret")
        signed = build_signed_headers(body=body, secret=secret, event_id=event_id, now_epoch=now_epoch, algo=algo)
        # Idempotency-Key is owned by the adapter (set from the envelope), so it
        # is not part of the auth-strategy contribution.
        signed.pop(HEADER_IDEMPOTENCY, None)
        return signed

    # "none", "mtls" (client-side cert), or any unknown strategy -> no headers.
    return {}
