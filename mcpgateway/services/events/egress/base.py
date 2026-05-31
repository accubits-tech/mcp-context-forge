# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/base.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Shared L3 egress adapter interface and delivery outcome.

The delivery worker is adapter-agnostic: for each matched subscription it builds
a delivery envelope (the §9.1a body that echoes the subscriber/agent identity)
and awaits :meth:`EgressAdapter.deliver`, then interprets the returned
:class:`DeliveryOutcome` to decide ACK / retry / dead-letter (see FRD §8.7 /
§9.1 / §10.2 - one source of truth).

:class:`DeliveryOutcome` carries everything the worker needs to make that
decision without knowing the concrete adapter:

* ``ok`` - the receiver accepted the delivery (HTTP 2xx); the worker marks the
  attempt ``delivered`` and ACKs the stream entry.
* ``http_status`` - the receiver status code, when an HTTP transport was used.
* ``permanent`` - the failure is non-retryable (e.g. a 4xx rejection such as
  400/403, or 410 Gone which additionally auto-disables the subscription); the
  worker dead-letters immediately rather than scheduling a retry.
* ``retry_after`` - a receiver-supplied delay (seconds) for a 429 / rate-limit
  response; the worker schedules ``next_retry_at = now + retry_after`` (capped).
* ``error`` - a human-readable diagnostic recorded on the attempt row.

The interface mirrors FRD §9.1; the M2b in-process adapter and the M3 real
``http_callback`` adapter both implement it behind the same seam.
"""

# Future
from __future__ import annotations

# Standard
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["DeliveryOutcome", "EgressAdapter"]


@dataclass
class DeliveryOutcome:
    """Result of a single egress delivery attempt.

    Attributes:
        ok: ``True`` when the receiver accepted the delivery (HTTP 2xx).
        http_status: Receiver HTTP status code, when an HTTP transport was used.
        permanent: ``True`` when the failure is non-retryable (4xx / 410); the
            worker dead-letters immediately instead of scheduling a retry.
        retry_after: Receiver-supplied retry delay in seconds (429 / rate
            limit); the worker schedules the next retry after this delay.
        error: Human-readable diagnostic recorded on the delivery attempt row.

    Examples:
        >>> DeliveryOutcome(ok=True)
        DeliveryOutcome(ok=True, http_status=None, permanent=False, retry_after=None, error=None)
        >>> o = DeliveryOutcome(ok=False, http_status=429, retry_after=30.0)
        >>> o.retry_after
        30.0
    """

    ok: bool
    http_status: Optional[int] = None
    permanent: bool = False
    retry_after: Optional[float] = None
    error: Optional[str] = None


class EgressAdapter(ABC):
    """Abstract L3 egress adapter.

    Concrete adapters perform the final delivery hop out of the gateway to a
    subscriber. They are driven by the delivery worker, which supplies the
    §9.1a delivery envelope and the resolved subscription, and consume the
    returned :class:`DeliveryOutcome` for ACK / retry / dead-letter decisions.

    The HTTP-callback adapter sets ``Idempotency-Key`` from the envelope's
    ``idempotency_key`` (stable across retries) so the at-least-once bus is safe
    against duplicate delivery; the receiver dedupes on it.
    """

    @abstractmethod
    async def deliver(self, *, delivery_envelope: dict, subscription: Any) -> DeliveryOutcome:
        """Deliver one event to a subscriber and report the outcome.

        Args:
            delivery_envelope: The §9.1a delivery body - an ``event`` block plus
                a ``subscription`` block (id, delivery_id, mode, target,
                correlation_id) and the ``idempotency_key`` to send as the
                ``Idempotency-Key`` header.
            subscription: The resolved subscription record (callback_url, auth,
                target, mode, correlation_id, delivery knobs).

        Returns:
            A :class:`DeliveryOutcome` describing the result of the attempt.
        """
        raise NotImplementedError
