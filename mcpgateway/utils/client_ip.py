# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/utils/client_ip.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Shared client IP extraction with trusted proxy support.

Only trusts X-Forwarded-For / X-Real-IP headers when the direct connection
IP matches a configured trusted proxy, preventing IP spoofing attacks.

Examples:
    >>> from mcpgateway.utils.client_ip import get_client_ip
    >>> from unittest.mock import MagicMock
    >>> request = MagicMock()
    >>> request.client.host = "10.0.0.1"
    >>> request.headers = {}
    >>> get_client_ip(request) == "10.0.0.1"
    True
"""

# Standard
import ipaddress
import logging
from typing import List

# Third-Party
from fastapi import Request

logger = logging.getLogger(__name__)


def _is_trusted_proxy(client_ip: str, trusted_proxies: List[str]) -> bool:
    """Check if a client IP is in the list of trusted proxies.

    Args:
        client_ip: The direct connection IP address.
        trusted_proxies: List of trusted proxy IPs or CIDR ranges.

    Returns:
        True if the client IP is a trusted proxy, False otherwise.

    Examples:
        >>> _is_trusted_proxy("10.0.0.1", ["10.0.0.0/24"])
        True
        >>> _is_trusted_proxy("192.168.1.1", ["10.0.0.0/24"])
        False
        >>> _is_trusted_proxy("10.0.0.1", [])
        False
    """
    if not trusted_proxies:
        return False

    try:
        ip = ipaddress.ip_address(client_ip)
    except (ValueError, ipaddress.AddressValueError):
        return False

    for proxy in trusted_proxies:
        try:
            if "/" in proxy:
                if ip in ipaddress.ip_network(proxy, strict=False):
                    return True
            else:
                if ip == ipaddress.ip_address(proxy):
                    return True
        except (ValueError, ipaddress.AddressValueError):
            continue

    return False


def get_client_ip(request: Request, trusted_proxies: List[str] | None = None) -> str:
    """Extract client IP address from request with trusted proxy awareness.

    Only trusts X-Forwarded-For and X-Real-IP headers when the direct
    connection IP is from a configured trusted proxy. Otherwise, returns
    the direct connection IP to prevent IP spoofing.

    Args:
        request: FastAPI request object.
        trusted_proxies: List of trusted proxy IPs/CIDRs. If None, reads from settings.

    Returns:
        Client IP address string.

    Examples:
        >>> from unittest.mock import MagicMock
        >>> request = MagicMock()
        >>> request.client.host = "127.0.0.1"
        >>> request.headers = {"X-Forwarded-For": "1.2.3.4"}
        >>> get_client_ip(request, trusted_proxies=["127.0.0.1"])
        '1.2.3.4'
        >>> get_client_ip(request, trusted_proxies=[])
        '127.0.0.1'
    """
    if trusted_proxies is None:
        # First-Party
        from mcpgateway.config import settings  # pylint: disable=import-outside-toplevel

        trusted_proxies = getattr(settings, "trusted_proxies", [])

    # Get direct connection IP
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust forwarded headers from trusted proxies
    if trusted_proxies and _is_trusted_proxy(direct_ip, trusted_proxies):
        # Check X-Forwarded-For header (first IP is the original client)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

    return direct_ip
