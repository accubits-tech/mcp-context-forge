# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/ssrf.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Egress SSRF guard for the L3 HTTP-callback adapter (PG-FR-A, FRD §10.1.9).

Every push delivery is an outbound HTTP POST to a *caller-supplied* callback
URL, so the gateway is a confused-deputy SSRF risk on its only push path. This
module is the load-bearing guard. It is used at subscription create/update time
**and again immediately before each delivery** (DNS can be re-pointed after
validation - the TOCTOU / DNS-rebinding attack).

:func:`validate_and_pin` performs *resolve-once-and-pin*:

1. Parse the URL and **reject userinfo** (``user@evil@127.0.0.1`` parses with a
   username; we refuse it outright rather than trust the parsed host).
2. Enforce the **scheme allowlist** - ``https`` only by default; ``file`` /
   ``gopher`` / ``ftp`` / ``dict`` and even ``http`` are rejected unless
   ``https_only=False``.
3. **Canonicalize obfuscated IP-literal hosts before classification** - decimal
   integer (``2130706433``), hex (``0x7f000001`` / ``0x7f.0.0.1``), octal
   (``0177.0.0.1``), short (``127.1``), and IPv4-mapped IPv6
   (``[::ffff:169.254.169.254]``) all canonicalize to their real address, then
   are classified.
4. **Resolve the hostname and classify every resolved IP**, rejecting if *any*
   is private / loopback / link-local / reserved / multicast / unspecified or
   the cloud-metadata address ``169.254.169.254``. The hostname
   ``metadata.google.internal`` is denied by name as well.
5. Enforce the **port allowlist** when ``allowed_ports`` is supplied.

It returns a :class:`PinnedTarget` carrying the validated resolved IP. The
delivery client then connects to *that exact IP* (defeating a resolver that
flips to a private address between validate and connect) while keeping the
original hostname for SNI and certificate verification, via
:func:`pinned_getaddrinfo`.

Examples:
    >>> import socket
    >>> _orig = socket.getaddrinfo
    >>> socket.getaddrinfo = lambda h, p, *a, **k: [
    ...     (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", p))]
    >>> t = validate_and_pin("https://example.com/cb")
    >>> (t.scheme, t.host, t.ip, t.port)
    ('https', 'example.com', '93.184.216.34', 443)
    >>> socket.getaddrinfo = _orig
"""

# Future
from __future__ import annotations

# Standard
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import logging
import socket
from typing import Iterator, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = ["SsrfError", "PinnedTarget", "validate_and_pin", "pinned_getaddrinfo"]

# Hostnames that must be denied by name regardless of what a (possibly poisoned)
# resolver returns. ``169.254.169.254`` is link-local and caught by IP
# classification too, but the GCP metadata alias is denied here by name so a
# rebinding resolver cannot launder it through a public A record.
_DENIED_HOSTNAMES = frozenset({"metadata.google.internal"})

# The canonical cloud-metadata address (AWS/GCP/Azure IMDS). Already link-local,
# but called out explicitly so the intent and the diagnostic are unambiguous.
_CLOUD_METADATA_IP = "169.254.169.254"

_DEFAULT_PORTS = {"https": 443, "http": 80}


class SsrfError(Exception):
    """Raised when an egress URL fails the SSRF guard.

    The message is safe to log but MUST NOT be reflected to the caller in a way
    that turns the guard into an internal-network oracle.
    """


@dataclass
class PinnedTarget:
    """A validated egress target, pinned to a specific resolved IP.

    Attributes:
        scheme: The validated URL scheme (``https`` or, when allowed, ``http``).
        host: The original URL hostname - used for SNI and TLS cert verification.
        ip: The validated, resolved IP literal the socket must connect to.
        port: The validated destination port.
    """

    scheme: str
    host: str
    ip: str
    port: int


def _classify_reject(ip: ipaddress._BaseAddress) -> Optional[str]:  # pylint: disable=protected-access
    """Return a rejection reason for an IP, or ``None`` if it is a public target.

    Mirrors the classification in
    :func:`mcpgateway.utils.url_validation.validate_url_not_internal`
    (private / loopback / link-local / reserved) and additionally denies
    multicast, unspecified, and the explicit cloud-metadata address.

    Args:
        ip: The parsed (and already-canonicalized) IP address.

    Returns:
        A short rejection reason string, or ``None`` when the IP is acceptable.
    """
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) reports as a global IPv6
    # by the public predicates; unwrap to the embedded IPv4 and classify that.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _classify_reject(mapped)

    if str(ip) == _CLOUD_METADATA_IP:
        return "cloud-metadata address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    return None


def _canonicalize_ip_literal(host: str) -> Optional[ipaddress._BaseAddress]:  # pylint: disable=protected-access
    """Canonicalize an IP-literal host (incl. obfuscated forms), if it is one.

    Handles the encoding-bypass forms (SC-SEC-022): decimal integer
    (``2130706433``), hex (``0x7f000001`` / ``0x7f.0.0.1``), octal
    (``0177.0.0.1``), short (``127.1``), plain dotted-quad, and any IPv6 literal
    (including IPv4-mapped). Returns ``None`` when the host is a real DNS name
    that must be resolved instead.

    Args:
        host: The URL hostname (already lower-cased, no brackets).

    Returns:
        The canonical :class:`ipaddress` object for an IP literal, else ``None``.
    """
    # IPv6 literals (``:`` can only appear in IPv6 here - the port was stripped
    # by urlparse). ipaddress canonicalizes shortened/mapped forms.
    if ":" in host:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    # IPv4 literals, including the libc inet_aton obfuscations (decimal int,
    # hex, octal, dotted-hex, short form). socket.inet_aton accepts exactly the
    # forms a naive HTTP client / OS resolver would treat as IPv4 and folds them
    # to a dotted-quad we can classify.
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.ip_address(socket.inet_ntoa(packed))


def validate_and_pin(
    url: str,
    *,
    https_only: bool = True,
    allowed_ports: Optional[Set[int]] = None,
    allow_hosts: Optional[Set[str]] = None,
) -> PinnedTarget:
    """Validate an egress callback URL and pin it to a validated resolved IP.

    Args:
        url: The caller-supplied callback URL.
        https_only: When ``True`` (default) only ``https`` is accepted; ``http``
            and every non-http(s) scheme is rejected. Set ``False`` to permit
            an explicitly-configured ``http`` internal target.
        allowed_ports: When given, the destination port MUST be in this set
            (else rejected). When ``None`` the default scheme port is used and
            any explicit port is accepted (subject to IP classification).
        allow_hosts: An optional set of hostnames (matched exactly,
            case-insensitively) that are trusted in-cluster receivers - e.g. a
            ClusterIP service like ``bud-budprompt``. For a matched host the
            private-IP denial in :func:`_classify_reject` is **skipped** and
            ``http`` is permitted even under ``https_only``. Every other guard
            still applies: userinfo is still refused, the ``_DENIED_HOSTNAMES``
            metadata aliases are still denied by name, non-http(s) schemes are
            still rejected, and the host is **still resolved and pinned to a
            concrete IP** so DNS-rebinding protection is preserved. When ``None``
            or empty, behavior is byte-identical to the unparameterized guard.

    Returns:
        A :class:`PinnedTarget` whose ``ip`` is the validated address the
        delivery socket must connect to, with ``host`` preserved for SNI/cert.

    Raises:
        SsrfError: For userinfo, a disallowed scheme, a missing/denied host, a
            private/loopback/link-local/reserved/multicast/unspecified or
            cloud-metadata resolution (unless the host is allow-listed), an
            unresolvable host, or a port outside ``allowed_ports``.
    """
    parsed = urlparse(url)

    # 1) userinfo - refuse outright (defeats user@evil@127.0.0.1 confusion).
    #    UNCONDITIONAL and FIRST: allow-listing never bypasses this.
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise SsrfError("callback URL must not contain userinfo")

    # Parse the host EARLY so the allow-list decision can gate the scheme and
    # IP-classification steps below.
    host = parsed.hostname
    if not host:
        raise SsrfError("callback URL has no host")
    host = host.lower()

    # Is this host explicitly trusted as an in-cluster receiver? Exact match,
    # case-insensitive. Empty/None allow_hosts -> never allowed (unchanged).
    is_allowed = bool(allow_hosts) and host in {h.lower() for h in allow_hosts}

    # 2) scheme allowlist. ``http`` is permitted for an allow-listed host even
    #    under https_only; every other (non-http(s)) scheme is still rejected.
    scheme = (parsed.scheme or "").lower()
    if is_allowed:
        if scheme not in ("http", "https"):
            raise SsrfError(f"scheme '{parsed.scheme}' not allowed; only http/https")
    elif https_only:
        if scheme != "https":
            raise SsrfError(f"scheme '{parsed.scheme}' not allowed; https only")
    elif scheme not in ("http", "https"):
        raise SsrfError(f"scheme '{parsed.scheme}' not allowed; only http/https")

    # Denied-by-name hosts (cloud-metadata aliases) - independent of resolution
    # AND of the allow-list, so allow-listing can never launder the IMDS alias.
    if host in _DENIED_HOSTNAMES:
        raise SsrfError(f"host '{host}' is denied")

    # 4b) port resolution + allowlist.
    try:
        port = parsed.port
    except ValueError as exc:
        raise SsrfError(f"invalid port in callback URL: {exc}") from exc
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if allowed_ports is not None and port not in allowed_ports:
        raise SsrfError(f"port {port} not in allowlist")

    # 3) IP-literal hosts: canonicalize obfuscated forms BEFORE classifying.
    literal = _canonicalize_ip_literal(host)
    if literal is not None:
        # An allow-listed host skips the private/loopback/etc denial but is still
        # pinned to its canonical IP (rebinding defence still holds).
        if not is_allowed:
            reason = _classify_reject(literal)
            if reason is not None:
                raise SsrfError(f"callback resolves to {reason} ({literal})")
        # A bare public IP literal: pin to the canonical IP; SNI/cert will use it.
        return PinnedTarget(scheme=scheme, host=str(literal), ip=str(literal), port=port)

    # 4) DNS name: resolve, classify EVERY address, pin the first acceptable one.
    #    An allow-listed host STILL resolves + pins (so a concrete validated IP is
    #    returned for pinned_getaddrinfo), but its IP classification denial is
    #    skipped - the only relaxation.
    try:
        addr_infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"could not resolve host '{host}'") from exc

    pinned_ip: Optional[str] = None
    for info in addr_infos:
        ip_text = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_text)
        except ValueError:
            raise SsrfError(f"resolver returned non-IP address for '{host}'")
        if not is_allowed:
            reason = _classify_reject(ip_obj)
            if reason is not None:
                raise SsrfError(f"callback resolves to {reason} ({ip_obj})")
        if pinned_ip is None:
            pinned_ip = ip_text

    if pinned_ip is None:
        raise SsrfError(f"could not resolve host '{host}'")

    return PinnedTarget(scheme=scheme, host=host, ip=pinned_ip, port=port)


@contextmanager
def pinned_getaddrinfo(host: str, ip: str) -> Iterator[None]:
    """Force ``socket.getaddrinfo(host, ...)`` to return only the pinned IP.

    Installs a scoped wrapper around :func:`socket.getaddrinfo` for the duration
    of the context: a lookup of the pinned ``host`` returns a single record for
    the already-validated ``ip`` (so the delivery socket connects only to the
    validated address, defeating DNS-rebinding), while every other host resolves
    through the original resolver unchanged. The wrapper is removed on exit.

    This is intended to wrap a single delivery's HTTP send (a short-lived client
    / one ``send``). It MUST NOT be left installed on a shared long-lived async
    client: it mutates the process-global ``socket.getaddrinfo`` and is racy
    under concurrency.

    Args:
        host: The original hostname to pin (preserved for SNI / cert checks).
        ip: The validated IP literal that ``host`` must resolve to.

    Yields:
        None - use as ``with pinned_getaddrinfo(host, ip): ...``.
    """
    original = socket.getaddrinfo
    target_host = host.lower()
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def _pinned(node, service, *args, **kwargs):  # pylint: disable=too-many-arguments
        """Resolve the pinned host to the validated IP; defer others to original."""
        if isinstance(node, str) and node.lower() == target_host:
            if family == socket.AF_INET6:
                sockaddr = (ip, service if isinstance(service, int) else 0, 0, 0)
            else:
                sockaddr = (ip, service if isinstance(service, int) else 0)
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
        return original(node, service, *args, **kwargs)

    socket.getaddrinfo = _pinned
    try:
        yield
    finally:
        socket.getaddrinfo = original
