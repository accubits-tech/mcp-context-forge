# -*- coding: utf-8 -*-
"""Simple in-memory rate limiter for authentication endpoints.

Location: ./mcpgateway/utils/rate_limit.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Provides a lightweight token-bucket style rate limiter keyed by client IP
to protect auth endpoints against brute-force and credential-stuffing attacks.

Examples:
    >>> limiter = RateLimiter(max_requests=5, window_seconds=60)
    >>> isinstance(limiter, RateLimiter)
    True
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request


class RateLimiter:
    """Token bucket rate limiter keyed by client IP.

    Tracks request timestamps per client IP within a sliding time window
    and raises HTTP 429 when the limit is exceeded.

    Attributes:
        max_requests: Maximum number of requests allowed per window.
        window_seconds: Duration of the sliding window in seconds.

    Examples:
        >>> limiter = RateLimiter(max_requests=10, window_seconds=60)
        >>> limiter.max_requests
        10
        >>> limiter.window_seconds
        60
    """

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from the request, respecting X-Forwarded-For.

        Args:
            request: FastAPI request object.

        Returns:
            str: The client IP address.

        Examples:
            >>> limiter = RateLimiter()
            >>> callable(limiter._get_client_ip)
            True
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request) -> None:
        """Check rate limit for the given request and raise HTTP 429 if exceeded.

        Args:
            request: FastAPI request object.

        Raises:
            HTTPException: 429 Too Many Requests when the rate limit is exceeded.

        Examples:
            >>> limiter = RateLimiter()
            >>> callable(limiter.check)
            True
        """
        client_ip = self._get_client_ip(request)
        now = time.monotonic()

        # Prune timestamps outside the current window
        self._requests[client_ip] = [ts for ts in self._requests[client_ip] if now - ts < self.window_seconds]

        if len(self._requests[client_ip]) >= self.max_requests:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(self.window_seconds)},
            )

        self._requests[client_ip].append(now)


# Shared rate limiters for auth endpoints
# - auth: tighter limit (login, register, password change)
# - token: slightly more permissive (API token creation)
auth_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
token_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)
