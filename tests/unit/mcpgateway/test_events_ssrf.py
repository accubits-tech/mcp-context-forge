# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_ssrf.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the L3 egress SSRF guard (PG-FR-A delivery arm): ``validate_and_pin``
and ``pinned_getaddrinfo`` in ``mcpgateway.services.events.egress.ssrf``.

These cover the M3 SEC gating rows:

* TC-SEC-020 - private / loopback / IPv6-loopback / link-local rejected.
* TC-SEC-021 - cloud-metadata ``169.254.169.254`` and ``metadata.google.internal``
  rejected.
* TC-SEC-022 - obfuscated IP literals (decimal / hex / octal / IPv4-mapped IPv6)
  and userinfo (``user@evil@127.0.0.1``) canonicalized then rejected.
* TC-SEC-023 - DNS-rebinding / TOCTOU: a host that is public at validate time
  but flips to ``127.0.0.1`` at connect time still connects to the pinned
  public IP via the ``pinned_getaddrinfo`` context (the pin holds).
* TC-SEC-025 - non-https / dangerous schemes (``file`` / ``gopher`` / ``ftp`` /
  ``dict`` / ``http``) rejected by the https-only allowlist.
* TC-SEC-026 - non-allowlisted port rejected when ``allowed_ports`` is set.
"""

# Standard
import socket

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.egress.ssrf import pinned_getaddrinfo, PinnedTarget, SsrfError, validate_and_pin


def _fake_getaddrinfo(mapping):
    """Build a fake ``socket.getaddrinfo`` that maps host -> a single IPv4/IPv6 string.

    Args:
        mapping: dict of hostname -> resolved IP string (the only A/AAAA record).

    Returns:
        A callable with the ``socket.getaddrinfo`` signature.
    """

    def _inner(host, port, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror(f"no fake record for {host}")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    return _inner


# --------------------------------------------------------------------------- #
# TC-SEC-025 - https-only scheme allowlist                                     #
# --------------------------------------------------------------------------- #
class TestSchemeAllowlist:
    """SEC-025: only https is allowed by default; everything else is rejected."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://evil.example/x",
            "ftp://evil.example/x",
            "dict://evil.example:11211/x",
            "http://evil.example/cb",
        ],
    )
    def test_non_https_rejected(self, url, monkeypatch):
        """file/gopher/ftp/dict/http are all rejected when https_only (default)."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"evil.example": "93.184.216.34"}))
        with pytest.raises(SsrfError):
            validate_and_pin(url)

    def test_http_allowed_when_https_only_false(self, monkeypatch):
        """When https_only=False, a public http target validates and pins."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        target = validate_and_pin("http://pub.example/cb", https_only=False)
        assert target.ip == "93.184.216.34"
        assert target.scheme == "http"
        assert target.port == 80


# --------------------------------------------------------------------------- #
# TC-SEC-020 - private / loopback / link-local rejected                        #
# --------------------------------------------------------------------------- #
class TestPrivateRanges:
    """SEC-020: RFC1918 / 127.0.0.1 / ::1 / 169.254.x callbacks are rejected."""

    @pytest.mark.parametrize(
        "url,ip",
        [
            ("https://h.example/cb", "10.0.0.5"),
            ("https://h.example/cb", "127.0.0.1"),
            ("https://h.example/cb", "192.168.1.10"),
            ("https://h.example/cb", "172.16.5.5"),
            ("https://h.example/cb", "169.254.10.20"),
        ],
    )
    def test_private_resolution_rejected(self, url, ip, monkeypatch):
        """A hostname resolving to a private/loopback/link-local IP is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"h.example": ip}))
        with pytest.raises(SsrfError):
            validate_and_pin(url)

    def test_ipv4_loopback_literal_rejected(self, monkeypatch):
        """A bare 127.0.0.1 literal is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"127.0.0.1": "127.0.0.1"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://127.0.0.1/cb")

    def test_ipv6_loopback_literal_rejected(self, monkeypatch):
        """The IPv6 loopback literal [::1] is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"::1": "::1"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://[::1]/cb")


# --------------------------------------------------------------------------- #
# TC-SEC-021 - cloud metadata exfil                                            #
# --------------------------------------------------------------------------- #
class TestCloudMetadata:
    """SEC-021: 169.254.169.254 and metadata.google.internal are denied."""

    def test_imds_ip_literal_rejected(self, monkeypatch):
        """The AWS/GCP IMDS IP literal 169.254.169.254 is rejected (link-local)."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"169.254.169.254": "169.254.169.254"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://169.254.169.254/latest/meta-data/")

    def test_metadata_google_internal_by_name_rejected(self, monkeypatch):
        """metadata.google.internal is rejected by name even if it 'resolves' public.

        The hostname is on the denylist regardless of what getaddrinfo returns,
        so a poisoned resolver cannot whitelist it.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"metadata.google.internal": "93.184.216.34"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://metadata.google.internal/computeMetadata/v1/")

    def test_metadata_google_internal_resolving_linklocal_rejected(self, monkeypatch):
        """metadata.google.internal resolving to its real link-local IP is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"metadata.google.internal": "169.254.169.254"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://metadata.google.internal/")


# --------------------------------------------------------------------------- #
# TC-SEC-022 - encoding-bypass canonicalization + userinfo                     #
# --------------------------------------------------------------------------- #
class TestEncodingBypass:
    """SEC-022: obfuscated literals canonicalize to 127.0.0.1 / IMDS and reject."""

    def test_decimal_int_literal_rejected(self, monkeypatch):
        """2130706433 == 127.0.0.1 (decimal integer literal) is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://2130706433/cb")

    def test_hex_literal_rejected(self, monkeypatch):
        """0x7f000001 == 127.0.0.1 (hex integer literal) is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://0x7f000001/cb")

    def test_dotted_hex_literal_rejected(self, monkeypatch):
        """0x7f.0.0.1 (dotted-hex) canonicalizes to 127.0.0.1 and is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://0x7f.0.0.1/cb")

    def test_octal_literal_rejected(self, monkeypatch):
        """0177.0.0.1 (octal) canonicalizes to 127.0.0.1 and is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://0177.0.0.1/cb")

    def test_ipv4_mapped_ipv6_imds_rejected(self, monkeypatch):
        """[::ffff:169.254.169.254] (IPv4-mapped IPv6) canonicalizes + rejects."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://[::ffff:169.254.169.254]/cb")

    def test_userinfo_rejected(self, monkeypatch):
        """A URL carrying userinfo (user@evil@127.0.0.1) is rejected outright."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"127.0.0.1": "127.0.0.1"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://user@evil@127.0.0.1/cb")


# --------------------------------------------------------------------------- #
# TC-SEC-026 - port allowlist                                                  #
# --------------------------------------------------------------------------- #
class TestPortAllowlist:
    """SEC-026: a non-allowlisted port is rejected when allowed_ports is set."""

    def test_non_allowlisted_port_rejected(self, monkeypatch):
        """https://internal:8080 is rejected when only {443} is allowed."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://pub.example:8080/cb", allowed_ports={443})

    def test_allowlisted_port_accepted(self, monkeypatch):
        """A port in the allowlist validates and pins."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        target = validate_and_pin("https://pub.example:8443/cb", allowed_ports={443, 8443})
        assert target.port == 8443
        assert target.ip == "93.184.216.34"

    def test_default_https_port_when_no_allowlist(self, monkeypatch):
        """With no allowlist, the default https port 443 is used."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        target = validate_and_pin("https://pub.example/cb")
        assert target.port == 443


# --------------------------------------------------------------------------- #
# Happy path - a public host pins to its resolved public IP                    #
# --------------------------------------------------------------------------- #
class TestPublicPin:
    """A public host validates and returns the resolved public IP to pin."""

    def test_public_host_pins(self, monkeypatch):
        """A public hostname returns a PinnedTarget with the resolved IP."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        target = validate_and_pin("https://pub.example/cb")
        assert isinstance(target, PinnedTarget)
        assert target.scheme == "https"
        assert target.host == "pub.example"
        assert target.ip == "93.184.216.34"
        assert target.port == 443

    def test_unresolvable_host_rejected(self, monkeypatch):
        """A hostname that cannot be resolved is rejected."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://nope.example/cb")


# --------------------------------------------------------------------------- #
# TC-SEC-023 - DNS rebinding / TOCTOU: the pin holds                           #
# --------------------------------------------------------------------------- #
class TestDnsRebindingPin:
    """SEC-023: validate-time public, connect-time private -> the pin wins."""

    def test_pinned_getaddrinfo_forces_validated_ip(self, monkeypatch):
        """A flipping resolver still yields the pinned public IP inside the context.

        The validator resolves once to a public IP and pins it. The resolver is
        then re-pointed to 127.0.0.1 (the rebind). Inside ``pinned_getaddrinfo``
        any lookup of the original host returns ONLY the pinned public IP, so the
        delivery socket connects to the validated address, not the attacker's.
        """
        # Validate-time: host is public.
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"rebind.example": "93.184.216.34"}))
        target = validate_and_pin("https://rebind.example/cb")
        assert target.ip == "93.184.216.34"

        # Connect-time: resolver flips to loopback (the rebind attack).
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"rebind.example": "127.0.0.1"}))

        # Inside the pin context the original host must resolve ONLY to the pinned IP.
        with pinned_getaddrinfo(target.host, target.ip):
            results = socket.getaddrinfo("rebind.example", 443, proto=socket.IPPROTO_TCP)
        resolved_ips = {info[4][0] for info in results}
        assert resolved_ips == {"93.184.216.34"}

        # After the context exits the original resolver is restored (now loopback).
        after = socket.getaddrinfo("rebind.example", 443, proto=socket.IPPROTO_TCP)
        assert {info[4][0] for info in after} == {"127.0.0.1"}

    def test_pinned_getaddrinfo_only_pins_target_host(self, monkeypatch):
        """The pin only rewrites the target host; other hosts resolve normally."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"other.example": "8.8.8.8"}))
        with pinned_getaddrinfo("pinned.example", "93.184.216.34"):
            pinned = socket.getaddrinfo("pinned.example", 443, proto=socket.IPPROTO_TCP)
            other = socket.getaddrinfo("other.example", 443, proto=socket.IPPROTO_TCP)
        assert {i[4][0] for i in pinned} == {"93.184.216.34"}
        assert {i[4][0] for i in other} == {"8.8.8.8"}


# --------------------------------------------------------------------------- #
# WS1 - egress allow-list: an in-cluster ClusterIP receiver may be reached     #
# --------------------------------------------------------------------------- #
class TestAllowHosts:
    """WS1: ``allow_hosts`` lets a named in-cluster host (e.g. ``bud-budprompt``)
    bypass the private-IP denial and use ``http``, while every other SSRF guard
    (userinfo, denied-by-name metadata, non-listed private hosts, dangerous
    schemes) stays fully intact and DNS-rebinding pinning is preserved.
    """

    def test_allow_listed_private_host_passes_and_pins(self, monkeypatch):
        """(a) An allow-listed host resolving to a private IP passes and pins it.

        ``bud-budprompt`` is a ClusterIP receiver that resolves to a 10.42.x.x
        pod/service address. Without the allowlist this would be a ``private
        address`` rejection; with it, the IP is still resolved and pinned (so
        rebinding protection is unchanged), but the denial is skipped.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        target = validate_and_pin(
            "https://bud-budprompt/cb",
            allow_hosts={"bud-budprompt"},
        )
        assert isinstance(target, PinnedTarget)
        assert target.host == "bud-budprompt"
        assert target.ip == "10.42.0.17"
        assert target.port == 443

    def test_allow_listed_host_is_case_insensitive(self, monkeypatch):
        """The allow-list match is case-insensitive on the hostname."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        target = validate_and_pin(
            "https://BUD-BudPrompt/cb",
            allow_hosts={"Bud-BudPrompt"},
        )
        assert target.ip == "10.42.0.17"

    def test_http_permitted_only_for_allow_listed_host(self, monkeypatch):
        """(b) ``http`` is permitted for an allow-listed host even under https_only.

        The adapter still runs https-only by default, so the only way an http
        ClusterIP target is reachable is via the allowlist.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        target = validate_and_pin(
            "http://bud-budprompt/cb",
            allow_hosts={"bud-budprompt"},
        )
        assert target.scheme == "http"
        assert target.ip == "10.42.0.17"
        assert target.port == 80

    def test_http_rejected_for_non_allow_listed_host(self, monkeypatch):
        """(b) ``http`` to a NON-listed host is still rejected under https_only.

        The allow-list relaxes the scheme only for its own hosts; another host
        on the same allow_hosts set must not borrow the http relaxation.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"other.svc": "10.42.0.99"}))
        with pytest.raises(SsrfError):
            validate_and_pin("http://other.svc/cb", allow_hosts={"bud-budprompt"})

    def test_non_listed_private_host_still_rejected(self, monkeypatch):
        """(c) A NON-listed host resolving to a private IP still raises SsrfError."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"intranet.svc": "10.42.0.50"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://intranet.svc/cb", allow_hosts={"bud-budprompt"})

    def test_userinfo_still_rejected_for_allow_listed_host(self, monkeypatch):
        """(d) Userinfo is rejected even when the host is allow-listed."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://user@bud-budprompt/cb", allow_hosts={"bud-budprompt"})

    def test_metadata_google_internal_still_denied_when_allow_listed(self, monkeypatch):
        """(e) ``metadata.google.internal`` stays denied-by-name even if allow-listed.

        Allow-listing must never be able to launder the cloud-metadata alias.
        """
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            _fake_getaddrinfo({"metadata.google.internal": "10.42.0.17"}),
        )
        with pytest.raises(SsrfError):
            validate_and_pin(
                "https://metadata.google.internal/computeMetadata/v1/",
                allow_hosts={"metadata.google.internal"},
            )

    def test_dangerous_scheme_still_rejected_when_allow_listed(self, monkeypatch):
        """A non-http(s) scheme (file/gopher) is rejected even for an allow-listed host."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        with pytest.raises(SsrfError):
            validate_and_pin("gopher://bud-budprompt/x", allow_hosts={"bud-budprompt"})
        with pytest.raises(SsrfError):
            validate_and_pin("file:///etc/passwd", allow_hosts={"bud-budprompt"})

    def test_allow_listed_ip_literal_private_passes_and_pins(self, monkeypatch):
        """An allow-listed *IP-literal* private host is pinned (literal path)."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({}))
        target = validate_and_pin(
            "http://10.42.0.17/cb",
            allow_hosts={"10.42.0.17"},
        )
        assert target.ip == "10.42.0.17"
        assert target.scheme == "http"

    def test_allow_listed_host_rebinding_pin_holds(self, monkeypatch):
        """An allow-listed host still resolves+pins a concrete IP (rebinding defence).

        Even though the private-IP denial is skipped, ``validate_and_pin`` must
        still resolve once and return a concrete pinned IP so the delivery socket
        connects to exactly that address, not whatever a flipped resolver returns.
        """
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        target = validate_and_pin("http://bud-budprompt/cb", allow_hosts={"bud-budprompt"})
        # The pin must be a concrete address (not the hostname) so pinned_getaddrinfo
        # can force the connect to it.
        assert target.ip == "10.42.0.17"
        socket.inet_aton(target.ip)  # raises if not a valid IPv4 literal

    def test_empty_allow_hosts_is_unchanged_behavior(self, monkeypatch):
        """(f) An empty/None allow_hosts keeps the original denial behavior."""
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"bud-budprompt": "10.42.0.17"}))
        with pytest.raises(SsrfError):
            validate_and_pin("https://bud-budprompt/cb", allow_hosts=set())
        with pytest.raises(SsrfError):
            validate_and_pin("https://bud-budprompt/cb", allow_hosts=None)
        # And http stays rejected when not allow-listed.
        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo({"pub.example": "93.184.216.34"}))
        with pytest.raises(SsrfError):
            validate_and_pin("http://pub.example/cb")
