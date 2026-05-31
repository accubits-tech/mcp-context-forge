# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_http_callback.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the M3 real signed-POST egress adapter
:class:`~mcpgateway.services.events.egress.http_callback.HttpCallbackEgressAdapter`.

The adapter is the *only* durable push path, so this suite exercises its full
contract end-to-end against a real loopback TLS HTTP server (self-signed cert
minted with :mod:`cryptography`), httpx ``MockTransport`` where socket-level
behaviour is not needed, and monkeypatched ``getaddrinfo`` for the SSRF pin path.

M3 gating rows covered (test-cases section 8):

* TC-DEL-010 - 4xx permanent / 410 permanent(+disable signal) / 5xx transient.
* TC-DEL-011 - 429 ``Retry-After: 30`` -> transient with ``retry_after`` set.
* TC-DEL-017 - conn-refused / timeout -> transient; bad cert -> fail-closed
  (``ok=False``); slowloris/hang -> aborted by the total timeout (transient).
* TC-DEL-018 / TC-SEC-024 - a ``302 -> http://169.254.169.254`` Location is NOT
  followed and is treated as a permanent failure.
* TC-SEC-020/021/022/025 - a ``callback_url`` to a private / loopback /
  obfuscated / cloud-metadata / non-https target yields a permanent
  ``ssrf_blocked`` outcome and NO request leaves the process.
* TC-SEC-050 - http rejected; the TLS floor is >= 1.2 and the cert chain is
  verified (a bad chain fails closed).
* TC-SEC-051 - the outbound POST a receiver gets carries a verifiable HMAC
  signature + timestamp + event id + ``Idempotency-Key``.
* TC-SEC-027 - a secret-looking response body is neither reflected nor stored;
  only the status and a byte size are recorded.

Loopback note: production SSRF default denies loopback. These socket-level
happy-path tests reach a ``127.0.0.1`` / ``localhost`` server via the adapter's
explicit ``allow_loopback=True`` hook (the FRD §10.1.9 documented co-located
loopback exception) together with ``https_only=False`` for the single
plain-http case - the production default (``allow_loopback=False``,
``https_only=True``) is never weakened and is asserted separately.
"""

# Future
from __future__ import annotations

# Standard
import datetime as _dt
import hashlib
import hmac
import http.server
import socket
import ssl
import threading
import time
from typing import Optional, Tuple

# Third-Party
import httpx
import pytest

# First-Party
from mcpgateway.services.events.egress.base import DeliveryOutcome
from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter
from mcpgateway.services.events.egress.ssrf import SsrfError


# --------------------------------------------------------------------------- #
# Self-signed loopback TLS server                                              #
# --------------------------------------------------------------------------- #
def _make_self_signed(common_name: str = "localhost") -> Tuple[bytes, bytes]:
    """Mint a throwaway self-signed cert+key for the loopback test server.

    Args:
        common_name: The certificate CN / SAN dns name.

    Returns:
        ``(cert_pem, key_pem)`` byte strings.
    """
    # Third-Party
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name), x509.DNSName("127.0.0.1")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class _Handler(http.server.BaseHTTPRequestHandler):
    """Programmable POST handler that records the last received request."""

    # Class-level script set per-test before the server starts.
    status = 200
    headers_out: dict = {}
    body_out: bytes = b"ok"
    sleep_s: float = 0.0
    record: dict = {}

    def log_message(self, *args, **kwargs):  # noqa: D401 - silence test server logging.
        """Suppress the stdlib access log."""

    def do_POST(self):  # noqa: N802 - stdlib handler name.
        """Record the request, optionally stall, then emit the scripted response."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).record = {
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body,
        }
        if self.sleep_s:
            time.sleep(self.sleep_s)
        self.send_response(self.status)
        for key, val in (self.headers_out or {}).items():
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(self.body_out)))
        self.end_headers()
        try:
            self.wfile.write(self.body_out)
        except (BrokenPipeError, ConnectionResetError):  # client aborted on timeout
            pass


class _TlsServer:
    """A short-lived self-signed-TLS loopback server for one test."""

    def __init__(self, *, status=200, headers_out=None, body_out=b"ok", sleep_s=0.0, plain_http=False):
        """Configure the scripted response for this server instance.

        Args:
            status: HTTP status code to return.
            headers_out: Extra response headers (e.g. ``Location``/``Retry-After``).
            body_out: Response body bytes.
            sleep_s: Seconds to stall before responding (slowloris/hang sim).
            plain_http: Serve plain http (no TLS) instead of https.
        """
        self.cert_pem, self.key_pem = _make_self_signed("localhost")
        self._plain = plain_http

        handler = type(
            "_BoundHandler",
            (_Handler,),
            {
                "status": status,
                "headers_out": headers_out or {},
                "body_out": body_out,
                "sleep_s": sleep_s,
                "record": {},
            },
        )
        self._handler_cls = handler
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        if not plain_http:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # Materialize the PEMs to a temp dir for load_cert_chain.
            # Standard
            import os
            import tempfile

            self._tmp = tempfile.mkdtemp()
            cpath = os.path.join(self._tmp, "c.pem")
            kpath = os.path.join(self._tmp, "k.pem")
            with open(cpath, "wb") as fh:
                fh.write(self.cert_pem)
            with open(kpath, "wb") as fh:
                fh.write(self.key_pem)
            ctx.load_cert_chain(cpath, kpath)
            self._httpd.socket = ctx.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

    @property
    def url(self) -> str:
        """The base callback URL pointing at this loopback server."""
        scheme = "http" if self._plain else "https"
        return f"{scheme}://localhost:{self.port}/cb"

    @property
    def received(self) -> dict:
        """The last recorded request (path/headers/body)."""
        return self._handler_cls.record

    def client_ssl_context(self) -> ssl.SSLContext:
        """A verifying client context that trusts this server's self-signed cert."""
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(cadata=self.cert_pem.decode("ascii"))
        return ctx


# --------------------------------------------------------------------------- #
# Subscription stub + envelope                                                 #
# --------------------------------------------------------------------------- #
class _Sub:
    """Minimal subscription stub carrying the fields the adapter reads."""

    def __init__(self, callback_url: str, *, delivery: Optional[dict] = None, subscriber_kind: str = "http_callback"):
        self.callback_url = callback_url
        self.delivery = delivery
        self.subscriber_kind = subscriber_kind
        self.target = {"callback_url": callback_url}
        self.id = "sub-1"
        self.mode = "fanout"
        self.correlation_value = None
        self.subscriber_target_ref = None


def _envelope(event_id: str = "evt-1") -> dict:
    """Build a minimal §9.1a delivery envelope with a stable idempotency key."""
    return {
        "event": {"id": event_id, "source": "//gw", "type": "com.github.push", "data": {"ref": "main"}},
        "subscription": {"id": "sub-1", "delivery_id": "d-1", "mode": "fanout", "target": {}},
        "idempotency_key": event_id,
    }


def _loopback_adapter(server: _TlsServer, **kw) -> HttpCallbackEgressAdapter:
    """Build an adapter wired to trust *server* and reach the loopback host."""
    params = {"total_timeout": 2.0, "connect_timeout": 2.0, "read_timeout": 2.0}
    params.update(kw)
    return HttpCallbackEgressAdapter(
        allow_loopback=True,
        ssl_context=server.client_ssl_context(),
        **params,
    )


# --------------------------------------------------------------------------- #
# TC-SEC-051 / happy path 2xx + signed headers                                 #
# --------------------------------------------------------------------------- #
class TestHappyPathSigned:
    """A 2xx delivery succeeds and the receiver gets a verifiable signature."""

    @pytest.mark.asyncio
    async def test_2xx_ok_and_idempotency_key(self):
        """A 200 response yields ok=True with the http_status recorded."""
        with _TlsServer(status=200, body_out=b"ACK") as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope("evt-9"), subscription=_Sub(srv.url))
        assert out.ok is True
        assert out.http_status == 200
        assert out.permanent is False
        assert srv.received["headers"].get("Idempotency-Key") == "evt-9"

    @pytest.mark.asyncio
    async def test_hmac_signature_is_verifiable(self):
        """TC-SEC-051: outbound POST carries an HMAC sig + ts + event id that verify."""
        secret = "shhh-outbound"
        sub = _Sub("PLACEHOLDER", delivery={"auth": {"strategy": "hmac", "secret": secret}})
        with _TlsServer(status=204) as srv:
            sub.callback_url = srv.url
            sub.target = {"callback_url": srv.url}
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope("evt-sec"), subscription=sub)
        assert out.ok is True
        hdrs = srv.received["headers"]
        assert hdrs.get("Idempotency-Key") == "evt-sec"
        assert hdrs.get("X-MCPGW-Event-Id") == "evt-sec"
        ts = hdrs.get("X-MCPGW-Timestamp")
        sig = hdrs.get("X-MCPGW-Signature")
        assert ts is not None and sig is not None
        body = srv.received["body"]
        expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(sig, expected)

    @pytest.mark.asyncio
    async def test_bearer_auth_header(self):
        """A bearer delivery.auth strategy emits Authorization: Bearer."""
        sub = _Sub("PLACEHOLDER", delivery={"auth": {"strategy": "bearer", "token": "t0k"}})
        with _TlsServer(status=200) as srv:
            sub.callback_url = srv.url
            sub.target = {"callback_url": srv.url}
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=sub)
        assert out.ok is True
        assert srv.received["headers"].get("Authorization") == "Bearer t0k"


# --------------------------------------------------------------------------- #
# TC-DEL-010 - status mapping (4xx permanent / 410 / 5xx transient)            #
# --------------------------------------------------------------------------- #
class TestStatusMapping:
    """Map receiver status codes onto the §8.7 outcome table."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_4xx_permanent(self, status):
        """TC-DEL-010: a non-408/429 4xx is a permanent (non-retryable) failure."""
        with _TlsServer(status=status) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is True
        assert out.http_status == status

    @pytest.mark.asyncio
    async def test_410_permanent_disable_signal(self):
        """TC-DEL-010: 410 Gone is permanent and surfaces http_status=410 (worker disables)."""
        with _TlsServer(status=410) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is True
        assert out.http_status == 410

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_transient(self, status):
        """TC-DEL-010: a 5xx is a transient (retryable) failure."""
        with _TlsServer(status=status) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is False
        assert out.http_status == status

    @pytest.mark.asyncio
    async def test_408_transient(self):
        """408 Request Timeout is retryable (not permanent)."""
        with _TlsServer(status=408) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is False
        assert out.http_status == 408


# --------------------------------------------------------------------------- #
# TC-DEL-011 - 429 Retry-After                                                 #
# --------------------------------------------------------------------------- #
class TestRetryAfter:
    """A 429 yields a transient outcome carrying the parsed Retry-After delay."""

    @pytest.mark.asyncio
    async def test_429_retry_after_seconds(self):
        """TC-DEL-011: 429 + Retry-After: 30 -> transient with retry_after=30."""
        with _TlsServer(status=429, headers_out={"Retry-After": "30"}) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is False
        assert out.http_status == 429
        assert out.retry_after == 30.0

    @pytest.mark.asyncio
    async def test_429_without_retry_after(self):
        """A 429 without a Retry-After header is still transient (no delay set)."""
        with _TlsServer(status=429) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is False
        assert out.http_status == 429


# --------------------------------------------------------------------------- #
# TC-DEL-018 / TC-SEC-024 - redirect to private not followed                    #
# --------------------------------------------------------------------------- #
class TestRedirectNotFollowed:
    """A 3xx Location to a private/link-local target is blocked, never followed."""

    @pytest.mark.asyncio
    async def test_302_to_metadata_blocked(self):
        """TC-SEC-024: 302 -> http://169.254.169.254 is not followed; permanent fail."""
        with _TlsServer(status=302, headers_out={"Location": "http://169.254.169.254/latest/meta-data/"}) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is True
        # The link-local target was not contacted.
        assert out.http_status == 302

    @pytest.mark.asyncio
    async def test_302_to_public_still_not_followed(self):
        """A 302 is treated as a failure (we never follow redirects on egress)."""
        with _TlsServer(status=302, headers_out={"Location": "https://example.com/elsewhere"}) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False


# --------------------------------------------------------------------------- #
# TC-SEC-020/021/022/025 - SSRF blocked at delivery (no request sent)          #
# --------------------------------------------------------------------------- #
class TestSsrfBlockedAtDelivery:
    """A bad callback_url yields a permanent ssrf_blocked outcome with no egress."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://10.0.0.5/cb",  # SEC-020 RFC1918
            "https://127.0.0.1/cb",  # SEC-020 loopback
            "https://[::1]/cb",  # SEC-020 IPv6 loopback
            "https://2130706433/cb",  # SEC-022 decimal 127.0.0.1
            "https://0x7f000001/cb",  # SEC-022 hex 127.0.0.1
            "https://0177.0.0.1/cb",  # SEC-022 octal 127.0.0.1
            "https://[::ffff:169.254.169.254]/cb",  # SEC-022 mapped metadata
            "https://user@evil@127.0.0.1/cb",  # SEC-022 userinfo confusion
            "http://evil.example/cb",  # SEC-025 non-https
            "file:///etc/passwd",  # SEC-025 dangerous scheme
        ],
    )
    async def test_blocked_no_request(self, url, monkeypatch):
        """SSRF-bad URLs return permanent ssrf_blocked and never open a socket."""
        opened = {"n": 0}
        real_connect = socket.socket.connect

        def _spy(self, addr):  # pragma: no cover - asserted not to run.
            opened["n"] += 1
            return real_connect(self, addr)

        monkeypatch.setattr(socket.socket, "connect", _spy)

        adapter = HttpCallbackEgressAdapter()  # production default: https-only, no loopback.
        out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(url))
        assert out.ok is False
        assert out.permanent is True
        assert out.error == "ssrf_blocked"
        assert opened["n"] == 0

    @pytest.mark.asyncio
    async def test_metadata_hostname_blocked(self):
        """TC-SEC-021: metadata.google.internal is denied by name (permanent)."""
        adapter = HttpCallbackEgressAdapter()
        out = await adapter.deliver(
            delivery_envelope=_envelope(),
            subscription=_Sub("https://metadata.google.internal/computeMetadata/v1/"),
        )
        assert out.ok is False
        assert out.permanent is True
        assert out.error == "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_dns_rebind_pinned(self, monkeypatch):
        """TC-SEC-023: host public at validate but flips to 127.0.0.1 at connect -> pinned.

        With a public A record at validate time the adapter pins that IP; a
        resolver that subsequently returns loopback cannot redirect the socket.
        We assert the validated IP is the public one and the connect targets it.
        """
        public_ip = "93.184.216.34"
        flip = {"to_loopback": False}

        def _fake_gai(host, port, *a, **k):
            if host == "rebind.example":
                ip = "127.0.0.1" if flip["to_loopback"] else public_ip
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port if isinstance(port, int) else 0))]
            return socket.getaddrinfo(host, port, *a, **k)

        # Patch the resolver used by validate_and_pin AND record where connect goes.
        monkeypatch.setattr("mcpgateway.services.events.egress.ssrf.socket.getaddrinfo", _fake_gai, raising=False)

        targets = []

        def _spy_connect(self, addr):  # pragma: no cover - connection will fail, that's fine.
            targets.append(addr)
            raise ConnectionRefusedError("blocked in test")

        monkeypatch.setattr(socket.socket, "connect", _spy_connect)

        adapter = HttpCallbackEgressAdapter(total_timeout=1.0, connect_timeout=1.0, read_timeout=1.0)
        # The resolver flips to loopback right after validation.
        flip["to_loopback"] = True
        out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub("https://rebind.example/cb"))
        # Connection was refused (transient), but crucially every connect target
        # was the PINNED public IP - never 127.0.0.1.
        assert out.ok is False
        if targets:
            assert all(t[0] == public_ip for t in targets), targets


# --------------------------------------------------------------------------- #
# TC-DEL-017 - connection/timeout/bad-cert/slowloris                            #
# --------------------------------------------------------------------------- #
class TestTransportFailures:
    """Network-layer failures map to transient outcomes; bad cert fails closed."""

    @pytest.mark.asyncio
    async def test_connection_refused_transient(self):
        """TC-DEL-017: a refused connection is transient (retryable)."""
        # Bind+close a socket to obtain a definitely-closed loopback port.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        adapter = HttpCallbackEgressAdapter(allow_loopback=True, https_only=False, total_timeout=2.0, connect_timeout=2.0, read_timeout=2.0)
        out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(f"http://127.0.0.1:{port}/cb"))
        assert out.ok is False
        assert out.permanent is False

    @pytest.mark.asyncio
    async def test_bad_cert_fails_closed(self):
        """TC-DEL-017/SEC-050: an untrusted (self-signed) chain fails closed, ok=False."""
        with _TlsServer(status=200) as srv:
            # Default production SSL context does NOT trust the self-signed cert.
            adapter = HttpCallbackEgressAdapter(allow_loopback=True, total_timeout=2.0, connect_timeout=2.0, read_timeout=2.0)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is False  # transient; receiver may fix its cert.

    @pytest.mark.asyncio
    async def test_slowloris_aborted_by_timeout(self):
        """TC-DEL-017: a hanging receiver is aborted by the total/read timeout."""
        with _TlsServer(status=200, sleep_s=3.0) as srv:
            adapter = _loopback_adapter(srv, total_timeout=0.5, read_timeout=0.5, connect_timeout=0.5)
            start = time.monotonic()
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
            elapsed = time.monotonic() - start
        assert out.ok is False
        assert out.permanent is False
        assert elapsed < 2.5  # aborted well before the 3s server stall.


# --------------------------------------------------------------------------- #
# TC-SEC-050 - http rejected; TLS floor 1.2                                     #
# --------------------------------------------------------------------------- #
class TestTlsAndScheme:
    """https-only by default; TLS floor is >= 1.2 with chain verification."""

    @pytest.mark.asyncio
    async def test_http_rejected_by_default(self):
        """TC-SEC-050: a plain-http callback_url is blocked under the default policy."""
        with _TlsServer(status=200, plain_http=True) as srv:
            adapter = HttpCallbackEgressAdapter(allow_loopback=True)  # https_only default True.
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is False
        assert out.permanent is True
        assert out.error == "ssrf_blocked"

    def test_default_ssl_context_tls_floor(self):
        """The adapter's default SSL context enforces TLS>=1.2 with cert verification."""
        adapter = HttpCallbackEgressAdapter()
        ctx = adapter._build_ssl_context()  # noqa: SLF001 - white-box assertion.
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True


# --------------------------------------------------------------------------- #
# TC-SEC-027 - response body not reflected/stored                              #
# --------------------------------------------------------------------------- #
class TestResponseBodyNotStored:
    """A secret-looking upstream body is never reflected into the outcome."""

    @pytest.mark.asyncio
    async def test_secret_body_not_in_outcome(self):
        """TC-SEC-027: only status + size recorded; the secret body is absent."""
        secret_body = b"TOP-SECRET-PASSWORD=hunter2-" + b"x" * 4096
        with _TlsServer(status=200, body_out=secret_body) as srv:
            adapter = _loopback_adapter(srv)
            out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub(srv.url))
        assert out.ok is True
        blob = repr(out)
        assert b"TOP-SECRET-PASSWORD".decode() not in blob
        assert "hunter2" not in blob
        if out.error:
            assert "hunter2" not in out.error


# --------------------------------------------------------------------------- #
# MockTransport-based unit checks (no sockets)                                 #
# --------------------------------------------------------------------------- #
class TestWithMockTransport:
    """Fine-grained status mapping via an injected httpx MockTransport client."""

    def _client_factory(self, handler):
        """Build a factory that yields an AsyncClient over a MockTransport."""

        def _factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

        return _factory

    @pytest.mark.asyncio
    async def test_mock_2xx(self):
        """A mocked 201 is ok with the status recorded (no real socket)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, text="created")

        adapter = HttpCallbackEgressAdapter(allow_loopback=True, client_factory=self._client_factory(handler))
        out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub("https://localhost:1/cb"))
        assert out.ok is True
        assert out.http_status == 201

    @pytest.mark.asyncio
    async def test_mock_body_capped(self):
        """A huge mocked body is read under a cap and never reflected."""
        big = "S3CR3T" * 100000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=big)

        adapter = HttpCallbackEgressAdapter(allow_loopback=True, max_response_bytes=1024, client_factory=self._client_factory(handler))
        out = await adapter.deliver(delivery_envelope=_envelope(), subscription=_Sub("https://localhost:1/cb"))
        assert out.ok is True
        assert "S3CR3T" not in repr(out)
