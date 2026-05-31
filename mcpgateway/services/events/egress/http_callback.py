# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/http_callback.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

The L3 HTTP-callback egress adapter: the gateway's only durable push path.

Each matched event is delivered as a single **signed outbound HTTP POST** of the
§9.1a delivery envelope to the subscription's ``callback_url`` (FRD §9.1/§9.2,
§10.1.9). Because that URL is caller-supplied, this adapter is the load-bearing
SSRF surface, so every delivery:

1. **Re-validates and pins** the callback URL immediately before connecting
   (:func:`~mcpgateway.services.events.egress.ssrf.validate_and_pin`) - rejecting
   private / loopback / link-local / reserved / cloud-metadata targets and
   obfuscated IP literals, enforcing the https-only allowlist, and returning the
   validated IP. On any :class:`SsrfError` the delivery is a **permanent**
   ``ssrf_blocked`` failure and **no socket is opened**.
2. Connects to that *exact pinned IP* for the duration of the one send
   (:func:`~mcpgateway.services.events.egress.ssrf.pinned_getaddrinfo`), so a
   resolver that flips to a private address between validate and connect cannot
   redirect the socket (DNS-rebinding / TOCTOU), while SNI + certificate
   verification still use the original hostname.
3. Uses an :class:`ssl.SSLContext` with a **TLS 1.2 floor** and full
   chain + hostname verification; a bad chain **fails closed** (transient).
4. **Disables redirect following** (a 30x could point an allowed host at an
   internal one); a 3xx is treated as a delivery failure, and its ``Location``
   is re-validated so a redirect to a private target is reported permanent.
5. **Caps the response read** and records only the status and a byte size - the
   upstream body is never reflected into the outcome or stored (SC-SEC-027).

Outbound headers carry an ``Idempotency-Key`` (= the envelope event id; stable
across retries) and, per the subscription's ``delivery.auth`` block, an HMAC
signature / bearer token (:mod:`mcpgateway.services.events.egress.signing`).

Status mapping follows the single-source-of-truth §8.7 outcome table:

================================  ==========================================
Receiver result                   :class:`DeliveryOutcome`
================================  ==========================================
``2xx``                           ``ok=True``
``408`` / ``429``                 transient (``429`` parses ``Retry-After``)
other ``4xx`` (400/401/403/...)   ``permanent=True`` (dead-letter)
``410``                           ``permanent=True`` (worker auto-disables)
``5xx``                           transient
``3xx``                           failure; permanent if ``Location`` is private
conn refused / timeout / bad TLS  transient (fail-closed: ``ok=False``)
================================  ==========================================
"""

# Future
from __future__ import annotations

# Standard
from email.utils import parsedate_to_datetime
import json
import ssl
import time
from typing import Any, Callable, Optional, Set

# Third-Party
import httpx

# First-Party
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter
from mcpgateway.services.events.egress.signing import auth_headers, build_signed_headers
from mcpgateway.services.events.egress.ssrf import pinned_getaddrinfo, PinnedTarget, SsrfError, validate_and_pin
from mcpgateway.utils.services_auth import decode_auth

__all__ = ["HttpCallbackEgressAdapter"]

# Map of encrypted-at-rest ``delivery.auth`` sentinel fields to the plaintext
# key each is decrypted back into for the in-memory signing copy. Mirrors
# ``subscription_service._DELIVERY_SECRET_FIELDS`` (the persist side).
_DELIVERY_ENCRYPTED_FIELDS = {"secret_encrypted": "secret", "token_encrypted": "token"}

# Default response-read cap (bytes). We only ever record status + size, so this
# bounds the read of a misbehaving / hostile receiver (slow drip / huge body).
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024


class HttpCallbackEgressAdapter(EgressAdapter):
    """Deliver an event as one signed, SSRF-guarded, TLS-verified HTTP POST.

    The adapter is configured for safe production defaults: https-only,
    loopback denied, TLS>=1.2 with full chain + hostname verification, redirects
    disabled, and bounded timeouts + response read. The constructor knobs double
    as the test seam (``allow_loopback`` / ``https_only`` / ``ssl_context`` /
    ``client_factory``) so the socket-level paths can be exercised against a
    loopback server without weakening the production policy.
    """

    def __init__(
        self,
        *,
        https_only: bool = True,
        allow_loopback: bool = False,
        allowed_ports: Optional[Set[int]] = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        total_timeout: float = 30.0,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        ssl_context: Optional[ssl.SSLContext] = None,
        client_factory: Optional[Callable[..., httpx.AsyncClient]] = None,
    ) -> None:
        """Configure the adapter.

        Args:
            https_only: When ``True`` (production default) only ``https`` callback
                URLs are accepted; ``http`` is rejected by the SSRF guard.
            allow_loopback: When ``True`` a loopback callback target is permitted
                (the FRD §10.1.9 co-located-receiver exception / the test hook);
                the default ``False`` denies loopback like every other private
                range.
            allowed_ports: Optional destination-port allowlist enforced by the
                SSRF guard.
            connect_timeout: TCP connect timeout (seconds).
            read_timeout: Socket read timeout (seconds) - bounds a slow drip.
            total_timeout: Overall request deadline (seconds) - aborts a hang.
            max_response_bytes: Hard cap on the response bytes read; only the
                status and size are recorded, never the body.
            ssl_context: An explicit verifying SSL context (tests inject one that
                trusts a self-signed loopback cert). ``None`` builds the
                production default (TLS>=1.2, chain + hostname verification).
            client_factory: Optional factory returning an :class:`httpx.AsyncClient`
                (tests inject a ``MockTransport`` client). ``None`` builds a real
                client per delivery.
        """
        self._https_only = https_only
        self._allow_loopback = allow_loopback
        self._allowed_ports = allowed_ports
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._total_timeout = total_timeout
        self._max_response_bytes = max_response_bytes
        self._ssl_context = ssl_context
        self._client_factory = client_factory

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _build_ssl_context(self) -> ssl.SSLContext:
        """Build the verifying SSL context with a TLS 1.2 floor.

        Returns:
            ssl.SSLContext: A context with ``verify_mode=CERT_REQUIRED``,
            ``check_hostname=True`` (both set by :func:`ssl.create_default_context`),
            and ``minimum_version`` pinned to TLS 1.2 so TLS<1.2 is refused while
            the full certificate chain + hostname are still verified.
        """
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def _pin(self, url: str) -> PinnedTarget:
        """Validate + pin a callback URL, honoring the ``allow_loopback`` knob.

        Args:
            url: The subscriber callback URL.

        Returns:
            PinnedTarget: The validated, IP-pinned target.

        Raises:
            SsrfError: If the URL fails the SSRF guard (and loopback is either
                disallowed or not the reason for the failure).
        """
        try:
            return validate_and_pin(url, https_only=self._https_only, allowed_ports=self._allowed_ports)
        except SsrfError:
            if not self._allow_loopback:
                raise
            return self._pin_allow_loopback(url)

    def _pin_allow_loopback(self, url: str) -> PinnedTarget:
        """Re-validate a URL permitting *only* loopback among the private ranges.

        Used solely when ``allow_loopback`` is set (the co-located-receiver
        exception). Every other SSRF rule (scheme allowlist, userinfo rejection,
        obfuscation canonicalization, link-local / private / metadata denial)
        still applies; only a loopback resolution is allowed through.

        Args:
            url: The subscriber callback URL.

        Returns:
            PinnedTarget: The validated, loopback-pinned target.

        Raises:
            SsrfError: If the URL fails any non-loopback SSRF rule.
        """
        # Standard
        import ipaddress  # pylint: disable=import-outside-toplevel
        import socket  # pylint: disable=import-outside-toplevel
        from urllib.parse import urlparse  # pylint: disable=import-outside-toplevel

        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
            raise SsrfError("callback URL must not contain userinfo")
        scheme = (parsed.scheme or "").lower()
        if self._https_only:
            if scheme != "https":
                raise SsrfError(f"scheme '{parsed.scheme}' not allowed; https only")
        elif scheme not in ("http", "https"):
            raise SsrfError(f"scheme '{parsed.scheme}' not allowed; only http/https")
        host = (parsed.hostname or "").lower()
        if not host:
            raise SsrfError("callback URL has no host")
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise SsrfError(f"invalid port in callback URL: {exc}") from exc
        if self._allowed_ports is not None and port not in self._allowed_ports:
            raise SsrfError(f"port {port} not in allowlist")

        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise SsrfError(f"could not resolve host '{host}'") from exc

        pinned_ip: Optional[str] = None
        for info in infos:
            ip_text = info[4][0]
            ip_obj = ipaddress.ip_address(ip_text)
            mapped = getattr(ip_obj, "ipv4_mapped", None)
            classify = mapped if mapped is not None else ip_obj
            if classify.is_loopback:
                if pinned_ip is None:
                    pinned_ip = ip_text
                continue
            # Anything non-loopback must still pass the full guard.
            raise SsrfError(f"callback host '{host}' resolves to a non-loopback address ({ip_obj})")
        if pinned_ip is None:
            raise SsrfError(f"could not resolve host '{host}'")
        return PinnedTarget(scheme=scheme, host=host, ip=pinned_ip, port=port)

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        """Parse a ``Retry-After`` header into a delay in seconds.

        Accepts the delta-seconds form (``"30"``) and the HTTP-date form,
        returning the non-negative delay from now for the latter.

        Args:
            value: The raw ``Retry-After`` header value, or ``None``.

        Returns:
            Optional[float]: The delay in seconds, or ``None`` when absent /
            unparseable.
        """
        if not value:
            return None
        value = value.strip()
        try:
            return float(int(value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        delay = when.timestamp() - time.time()
        return delay if delay > 0 else 0.0

    def _delivery_auth(self, subscription: Any) -> Optional[dict]:
        """Extract the per-subscription ``delivery.auth`` block, decrypted for signing.

        The credential is persisted encrypted at rest (``secret_encrypted`` /
        ``token_encrypted`` — SC-SEC-015/039). This returns a **throwaway copy**
        of the block with those sentinel fields decoded back into their plaintext
        ``secret`` / ``token`` keys (``decode_auth(...)["v"]``) so the signing
        helpers can compute the outbound HMAC / bearer header. The decrypted
        value is never written back onto the subscription, so it is never
        re-persisted.

        Args:
            subscription: The resolved subscription record.

        Returns:
            Optional[dict]: A decrypted copy of the ``delivery.auth`` mapping, or
            ``None``.
        """
        delivery = getattr(subscription, "delivery", None)
        if not isinstance(delivery, dict):
            return None
        auth = delivery.get("auth")
        if not isinstance(auth, dict):
            return None
        # Decrypt into a fresh copy so the stored block keeps its ciphertext.
        decrypted = dict(auth)
        for enc_field, plain_field in _DELIVERY_ENCRYPTED_FIELDS.items():
            encoded = decrypted.pop(enc_field, None)
            if encoded:
                value = decode_auth(encoded).get("v")
                if value:
                    decrypted[plain_field] = value
        return decrypted

    def _build_headers(self, *, body: bytes, event_id: str, subscription: Any) -> dict:
        """Build the outbound headers: Idempotency-Key + content-type + auth.

        Args:
            body: The exact serialized request body bytes.
            event_id: The envelope event id (the idempotency key).
            subscription: The resolved subscription (for ``delivery.auth``).

        Returns:
            dict: The outbound header mapping.
        """
        now_epoch = int(time.time())
        headers = build_signed_headers(body=body, secret=None, event_id=event_id, now_epoch=now_epoch)
        headers["Content-Type"] = "application/json"
        auth = self._delivery_auth(subscription)
        headers.update(auth_headers(auth, body=body, event_id=event_id, now_epoch=now_epoch))
        return headers

    def _make_client(self, ctx: ssl.SSLContext) -> httpx.AsyncClient:
        """Construct the per-delivery HTTP client (or the injected test client).

        Args:
            ctx: The verifying SSL context to use for a real client.

        Returns:
            httpx.AsyncClient: A client with redirects disabled and bounded
            timeouts.
        """
        timeout = httpx.Timeout(self._total_timeout, connect=self._connect_timeout, read=self._read_timeout)
        if self._client_factory is not None:
            return self._client_factory(verify=ctx, timeout=timeout, follow_redirects=False)
        return httpx.AsyncClient(verify=ctx, timeout=timeout, follow_redirects=False)

    # ------------------------------------------------------------------ #
    # Delivery                                                            #
    # ------------------------------------------------------------------ #

    async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
        """Deliver one event via a signed, SSRF-guarded, TLS-verified POST.

        Args:
            delivery_envelope: The §9.1a delivery envelope to POST.
            subscription: The resolved subscription (``callback_url`` + auth).

        Returns:
            DeliveryOutcome: The mapped result per the §8.7 outcome table.
        """
        url = self._resolve_callback_url(delivery_envelope, subscription)
        if not url:
            return DeliveryOutcome(ok=False, permanent=True, error="no callback_url")

        # 1) Re-validate + pin immediately before connecting (TOCTOU defence).
        try:
            target = self._pin(url)
        except SsrfError:
            return DeliveryOutcome(ok=False, permanent=True, error="ssrf_blocked")

        event_id = self._event_id(delivery_envelope)
        body = json.dumps(delivery_envelope, separators=(",", ":")).encode("utf-8")
        headers = self._build_headers(body=body, event_id=event_id, subscription=subscription)
        ctx = self._ssl_context or self._build_ssl_context()

        try:
            async with self._make_client(ctx) as client:
                # 2) Pin the resolver to the validated IP for this one send so the
                #    socket connects only to the pinned address (anti-rebinding),
                #    while SNI / cert verification still bind to the hostname.
                with pinned_getaddrinfo(target.host, target.ip):
                    response, read_bytes = await self._send(client, url=url, body=body, headers=headers)
        except httpx.HTTPError as exc:
            # conn refused / timeout / bad cert / slowloris-abort: transient,
            # fail-closed. The exception type is recorded; no body is reflected.
            return DeliveryOutcome(ok=False, permanent=False, error=type(exc).__name__)
        except ssl.SSLError as exc:  # pragma: no cover - httpx normally wraps this.
            return DeliveryOutcome(ok=False, permanent=False, error=type(exc).__name__)

        return self._map_response(response, read_bytes)

    async def _send(self, client: httpx.AsyncClient, *, url: str, body: bytes, headers: dict) -> "tuple[httpx.Response, int]":
        """POST the body and read the response under the byte cap.

        The response is streamed and the read stops once ``max_response_bytes``
        have been consumed, so a hostile/large body cannot exhaust memory and -
        crucially - the body is never returned to the caller: only the number of
        bytes read is reported (SC-SEC-027).

        Args:
            client: The (already pinned) HTTP client to send on.
            url: The original callback URL (hostname preserved for SNI/cert).
            body: The serialized request body bytes.
            headers: The outbound header mapping.

        Returns:
            tuple[httpx.Response, int]: The response (headers/status only - its
            body is not buffered) and the number of body bytes read (capped).
        """
        request = client.build_request("POST", url, content=body, headers=headers)
        response = await client.send(request, stream=True)
        read = 0
        try:
            async for chunk in response.aiter_bytes():
                read += len(chunk)
                if read >= self._max_response_bytes:
                    break
        finally:
            await response.aclose()
        return response, read

    def _map_response(self, response: httpx.Response, read_bytes: int) -> DeliveryOutcome:
        """Map an HTTP response to a :class:`DeliveryOutcome` per §8.7.

        Only the status and the (capped) number of body bytes read are used; the
        upstream body itself is never stored in or reflected by the outcome
        (SC-SEC-027).

        Args:
            response: The received (non-redirect-followed) response - status and
                headers only; its body is not buffered.
            read_bytes: The number of response body bytes read (capped).

        Returns:
            DeliveryOutcome: The mapped outcome.
        """
        status = response.status_code
        size = read_bytes

        if 200 <= status < 300:
            return DeliveryOutcome(ok=True, http_status=status, error=f"size={size}")

        # 3xx: redirects are NOT followed. Re-validate the Location: a redirect
        # to a private/internal target is a permanent failure; any 3xx is a
        # failure (we never chase egress redirects).
        if 300 <= status < 400:
            location = response.headers.get("Location") or response.headers.get("location")
            permanent = True
            if location:
                try:
                    self._pin(location)
                    # Location is itself a valid public https target, but we still
                    # do not follow it; treat as a (permanent) misconfiguration.
                except SsrfError:
                    permanent = True
            return DeliveryOutcome(ok=False, http_status=status, permanent=permanent, error="redirect_not_followed")

        # 408 / 429: transient. 429 honors Retry-After.
        if status in (408, 429):
            retry_after = self._parse_retry_after(response.headers.get("Retry-After") or response.headers.get("retry-after")) if status == 429 else None
            return DeliveryOutcome(ok=False, http_status=status, permanent=False, retry_after=retry_after, error=f"http_{status}")

        # 410 Gone: permanent (worker additionally auto-disables the sub).
        if status == 410:
            return DeliveryOutcome(ok=False, http_status=410, permanent=True, error="http_410")

        # Other 4xx (400/401/403/404/422/...): permanent (dead-letter).
        if 400 <= status < 500:
            return DeliveryOutcome(ok=False, http_status=status, permanent=True, error=f"http_{status}")

        # 5xx and anything else: transient.
        return DeliveryOutcome(ok=False, http_status=status, permanent=False, error=f"http_{status}")

    # ------------------------------------------------------------------ #
    # Envelope extraction                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _event_id(delivery_envelope: dict) -> str:
        """Extract the stable idempotency key (event id) from the envelope.

        Args:
            delivery_envelope: The §9.1a delivery envelope.

        Returns:
            str: The ``idempotency_key`` (preferred) or the event id, or ``""``.
        """
        key = delivery_envelope.get("idempotency_key")
        if key:
            return str(key)
        event = delivery_envelope.get("event") or {}
        return str(event.get("id") or "")

    @staticmethod
    def _resolve_callback_url(delivery_envelope: dict, subscription: Any) -> Optional[str]:
        """Best-effort extraction of the target callback URL.

        Args:
            delivery_envelope: The §9.1a delivery envelope.
            subscription: The resolved subscription record (may be ``None``).

        Returns:
            Optional[str]: The target ``callback_url`` if discoverable.
        """
        if subscription is not None:
            url = getattr(subscription, "callback_url", None)
            if url:
                return url
        sub_block = delivery_envelope.get("subscription") or {}
        target = sub_block.get("target") or {}
        return target.get("callback_url") or sub_block.get("callback_url") or delivery_envelope.get("callback_url")
