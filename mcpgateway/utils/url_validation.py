# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/url_validation.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Shared SSRF URL validation utility.

Provides a reusable function to validate that URLs do not resolve to
internal/private network addresses, preventing Server-Side Request Forgery attacks.

Examples:
    >>> from mcpgateway.utils.url_validation import validate_url_not_internal
    >>> try:
    ...     validate_url_not_internal("ftp://example.com")
    ... except ValueError as e:
    ...     "not allowed" in str(e)
    True
"""

# Standard
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def validate_url_not_internal(target_url: str) -> None:
    """Validate that a URL does not point to internal/private network addresses.

    Args:
        target_url: URL to validate

    Raises:
        ValueError: If the URL points to an internal address or uses an unsupported scheme

    Examples:
        >>> validate_url_not_internal("ftp://example.com")
        Traceback (most recent call last):
        ...
        ValueError: URL scheme 'ftp' is not allowed. Only http and https are supported.
    """
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed. Only http and https are supported.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname found")

    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addr_infos = socket.getaddrinfo(hostname, parsed.port or default_port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"Could not resolve hostname: {hostname}")

    for addr_info in addr_infos:
        ip = ipaddress.ip_address(addr_info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"URL resolves to a private/internal address ({ip}). Only public URLs are allowed.")
