# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_egress.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the L3 egress delivery adapters: the shared :class:`DeliveryOutcome`
dataclass + :class:`EgressAdapter` interface, the in-process fake-subscriber
adapter used by reliability tests, and the :func:`get_egress_adapter` factory.

The in-process adapter is the configurable fake subscriber that the M2b
delivery worker tests drive: it records every received delivery (including the
``Idempotency-Key``) and returns a programmable :class:`DeliveryOutcome` per
target URL. M3 swaps the real ``http_callback`` egress behind the same seam.
"""

# Standard
from types import SimpleNamespace

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.egress.base import DeliveryOutcome, EgressAdapter
from mcpgateway.services.events.egress.inprocess import get_egress_adapter, InProcessEgressAdapter


def _envelope(*, callback_url: str = "https://sub.example/cb", idempotency_key: str = "evt-1", delivery_id: str = "1", correlation_id=None):
    """Build a minimal §9.1a-shaped delivery envelope for tests."""
    return {
        "event": {
            "id": idempotency_key,
            "source": "github",
            "type": "com.github.push",
            "subject": "repo/main",
            "time": "2026-05-30T00:00:00Z",
            "data": {"x": 1},
        },
        "subscription": {
            "id": "sub-1",
            "delivery_id": delivery_id,
            "mode": "fanout",
            "target": {"callback_url": callback_url},
            "correlation_id": correlation_id,
        },
        "idempotency_key": idempotency_key,
    }


def _subscription(callback_url: str = "https://sub.example/cb"):
    """A lightweight stand-in for an EventSubscription row."""
    return SimpleNamespace(id="sub-1", kind="http_callback", callback_url=callback_url)


class TestDeliveryOutcome:
    """Shape and defaults of the shared outcome dataclass."""

    def test_defaults(self):
        """All optional fields default to None / False."""
        outcome = DeliveryOutcome(ok=True)
        assert outcome.ok is True
        assert outcome.http_status is None
        assert outcome.permanent is False
        assert outcome.retry_after is None
        assert outcome.error is None

    def test_all_fields(self):
        """Every field is settable."""
        outcome = DeliveryOutcome(ok=False, http_status=429, permanent=False, retry_after=12.5, error="rate limited")
        assert outcome.ok is False
        assert outcome.http_status == 429
        assert outcome.permanent is False
        assert outcome.retry_after == 12.5
        assert outcome.error == "rate limited"


class TestEgressAdapterInterface:
    """The ABC cannot be instantiated and defines the deliver seam."""

    def test_is_abstract(self):
        """EgressAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            EgressAdapter()  # type: ignore[abstract]

    def test_inprocess_is_egress_adapter(self):
        """InProcessEgressAdapter satisfies the interface."""
        assert issubclass(InProcessEgressAdapter, EgressAdapter)


class TestInProcessEgressAdapterDefault:
    """Default behaviour: record + return ok."""

    @pytest.mark.asyncio
    async def test_default_deliver_records_and_returns_ok(self):
        """An un-programmed URL records the delivery and returns ok=True."""
        adapter = InProcessEgressAdapter()
        env = _envelope(callback_url="https://a.example/cb", idempotency_key="evt-42")

        outcome = await adapter.deliver(delivery_envelope=env, subscription=_subscription("https://a.example/cb"))

        assert isinstance(outcome, DeliveryOutcome)
        assert outcome.ok is True
        assert outcome.http_status in (None, 200)

        recorded = adapter.received
        assert len(recorded) == 1
        assert recorded[0].idempotency_key == "evt-42"
        assert recorded[0].callback_url == "https://a.example/cb"
        assert recorded[0].delivery_envelope == env

    @pytest.mark.asyncio
    async def test_records_in_order_across_calls(self):
        """Multiple deliveries are recorded in arrival order."""
        adapter = InProcessEgressAdapter()
        for key in ("a", "b", "c"):
            await adapter.deliver(delivery_envelope=_envelope(idempotency_key=key), subscription=_subscription())
        assert [r.idempotency_key for r in adapter.received] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_received_for_url_filters_by_target(self):
        """received_for(url) returns only deliveries to that URL."""
        adapter = InProcessEgressAdapter()
        await adapter.deliver(delivery_envelope=_envelope(callback_url="https://x/cb", idempotency_key="x1"), subscription=_subscription("https://x/cb"))
        await adapter.deliver(delivery_envelope=_envelope(callback_url="https://y/cb", idempotency_key="y1"), subscription=_subscription("https://y/cb"))
        xs = adapter.received_for("https://x/cb")
        assert [r.idempotency_key for r in xs] == ["x1"]


class TestProgrammedOutcomes:
    """Per-URL programmable outcomes consumed in order."""

    @pytest.mark.asyncio
    async def test_set_url_outcomes_returned_in_order(self):
        """A URL programmed 500-then-200 returns those outcomes in sequence."""
        adapter = InProcessEgressAdapter()
        url = "https://flaky.example/cb"
        adapter.set_outcomes(
            url,
            [
                DeliveryOutcome(ok=False, http_status=500, error="boom"),
                DeliveryOutcome(ok=True, http_status=200),
            ],
        )

        o1 = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert o1.ok is False and o1.http_status == 500

        o2 = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert o2.ok is True and o2.http_status == 200

        # Exhausted programme -> falls back to default ok.
        o3 = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert o3.ok is True

        # Every attempt is still recorded.
        assert len(adapter.received_for(url)) == 3

    @pytest.mark.asyncio
    async def test_410_surfaces_permanent(self):
        """A 410 outcome surfaces as permanent (worker auto-disables sub)."""
        adapter = InProcessEgressAdapter()
        url = "https://gone.example/cb"
        adapter.set_outcomes(url, [DeliveryOutcome(ok=False, http_status=410, permanent=True, error="gone")])
        outcome = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert outcome.ok is False
        assert outcome.http_status == 410
        assert outcome.permanent is True

    @pytest.mark.asyncio
    async def test_429_surfaces_retry_after(self):
        """A 429 outcome surfaces retry_after for capped backoff."""
        adapter = InProcessEgressAdapter()
        url = "https://busy.example/cb"
        adapter.set_outcomes(url, [DeliveryOutcome(ok=False, http_status=429, retry_after=30.0, error="slow down")])
        outcome = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert outcome.ok is False
        assert outcome.http_status == 429
        assert outcome.retry_after == 30.0
        assert outcome.permanent is False

    @pytest.mark.asyncio
    async def test_4xx_permanent_no_retry(self):
        """A 403 outcome surfaces as a permanent non-retryable failure."""
        adapter = InProcessEgressAdapter()
        url = "https://forbidden.example/cb"
        adapter.set_outcomes(url, [DeliveryOutcome(ok=False, http_status=403, permanent=True, error="forbidden")])
        outcome = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert outcome.permanent is True
        assert outcome.http_status == 403

    @pytest.mark.asyncio
    async def test_5xx_transient(self):
        """A 503 outcome is transient (not permanent)."""
        adapter = InProcessEgressAdapter()
        url = "https://down.example/cb"
        adapter.set_outcomes(url, [DeliveryOutcome(ok=False, http_status=503, error="unavailable")])
        outcome = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert outcome.ok is False
        assert outcome.permanent is False

    @pytest.mark.asyncio
    async def test_timeout_outcome(self):
        """A programmed timeout-style outcome (ok=False, no http_status) is transient."""
        adapter = InProcessEgressAdapter()
        url = "https://hang.example/cb"
        adapter.set_outcomes(url, [DeliveryOutcome(ok=False, error="timeout")])
        outcome = await adapter.deliver(delivery_envelope=_envelope(callback_url=url), subscription=_subscription(url))
        assert outcome.ok is False
        assert outcome.http_status is None
        assert outcome.permanent is False
        assert outcome.error == "timeout"

    @pytest.mark.asyncio
    async def test_idempotency_key_recorded_across_retries(self):
        """The same Idempotency-Key is recorded on each retry attempt (TC-DEL-001)."""
        adapter = InProcessEgressAdapter()
        url = "https://retry.example/cb"
        adapter.set_outcomes(
            url,
            [
                DeliveryOutcome(ok=False, http_status=500),
                DeliveryOutcome(ok=False, http_status=500),
                DeliveryOutcome(ok=True, http_status=200),
            ],
        )
        for delivery_id in ("d1", "d2", "d3"):
            await adapter.deliver(
                delivery_envelope=_envelope(callback_url=url, idempotency_key="stable-evt", delivery_id=delivery_id),
                subscription=_subscription(url),
            )
        recs = adapter.received_for(url)
        assert [r.idempotency_key for r in recs] == ["stable-evt", "stable-evt", "stable-evt"]


class TestGetEgressAdapter:
    """The factory resolves the production adapter per subscriber kind (M3).

    M3 wires the real two-adapter set (FRD §9.2) behind the factory: the
    HTTP-callback push adapter for ``http_callback`` (and unknown kinds,
    fail-safe to the single push path) and the best-effort SSE/WS streaming
    adapter for ``sse``/``ws``. The in-process fake adapter remains available as
    a test seam via ``DeliveryWorker(egress=...)``, not via this factory.
    """

    def test_returns_http_callback_adapter_for_http_callback(self):
        """http_callback resolves to the real signed-POST adapter."""
        # First-Party
        from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter  # pylint: disable=import-outside-toplevel

        assert isinstance(get_egress_adapter("http_callback"), HttpCallbackEgressAdapter)

    def test_returns_streaming_adapter_for_sse(self):
        """sse resolves to the best-effort streaming adapter."""
        # First-Party
        from mcpgateway.services.events.egress.streaming import StreamingEgressAdapter  # pylint: disable=import-outside-toplevel

        assert isinstance(get_egress_adapter("sse"), StreamingEgressAdapter)

    def test_returns_http_callback_adapter_for_unknown_kind(self):
        """An unknown kind falls back to the HTTP-callback push adapter."""
        # First-Party
        from mcpgateway.services.events.egress.http_callback import HttpCallbackEgressAdapter  # pylint: disable=import-outside-toplevel

        assert isinstance(get_egress_adapter("anything"), HttpCallbackEgressAdapter)
