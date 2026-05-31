# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_schemas.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the events/webhooks Pydantic schemas (FRD section 5.6).
"""

# Third-Party
import pytest
from pydantic import ValidationError

# First-Party
from mcpgateway.schemas import (
    DeliveryEnvelope,
    EventEnvelope,
    EventsCapability,
    GatewayCreate,
    SubscriberRef,
    SubscriptionCreate,
)


def test_subscription_correlate_requires_correlation_fields():
    """mode='correlate' without correlation_key/value must fail validation."""
    with pytest.raises(ValidationError):
        SubscriptionCreate(
            subscriber=SubscriberRef(kind="sse"),
            event_types=["com.github.push"],
            mode="correlate",
        )


def test_subscription_correlate_with_correlation_fields_valid():
    """mode='correlate' with both correlation_key and correlation_value is valid."""
    sub = SubscriptionCreate(
        subscriber=SubscriberRef(kind="sse"),
        event_types=["com.github.push"],
        mode="correlate",
        correlation_key="run_id",
        correlation_value="abc123",
    )
    assert sub.mode == "correlate"
    assert sub.correlation_key == "run_id"
    assert sub.correlation_value == "abc123"


def test_subscription_http_callback_requires_callback_url():
    """subscriber.kind='http_callback' without callback_url must fail validation."""
    with pytest.raises(ValidationError):
        SubscriptionCreate(
            subscriber=SubscriberRef(kind="http_callback"),
            event_types=["com.github.push"],
        )


def test_subscription_http_callback_with_callback_url_valid():
    """subscriber.kind='http_callback' with callback_url is valid."""
    sub = SubscriptionCreate(
        subscriber=SubscriberRef(kind="http_callback", callback_url="https://example.com/hook"),
        event_types=["com.github.push"],
    )
    assert sub.subscriber.kind == "http_callback"
    assert sub.subscriber.callback_url == "https://example.com/hook"


def test_subscription_sse_without_callback_url_valid():
    """subscriber.kind='sse' without callback_url is valid."""
    sub = SubscriptionCreate(
        subscriber=SubscriberRef(kind="sse"),
        event_types=["com.github.push"],
    )
    assert sub.subscriber.kind == "sse"
    assert sub.subscriber.callback_url is None


def test_events_capability_accepts_camel_case_aliases():
    """EventsCapability accepts camelCase aliases and maps to snake_case fields."""
    cap = EventsCapability(
        **{
            "webhooksSupported": True,
            "eventTypes": ["com.github.*"],
            "extraOAuthScopes": ["admin:repo_hook"],
        }
    )
    assert cap.webhooks_supported is True
    assert cap.event_types == ["com.github.*"]
    assert cap.extra_oauth_scopes == ["admin:repo_hook"]


def test_events_capability_accepts_snake_case():
    """EventsCapability accepts snake_case field names."""
    cap = EventsCapability(
        webhooks_supported=True,
        event_types=["com.github.*"],
        extra_oauth_scopes=["admin:repo_hook"],
    )
    assert cap.webhooks_supported is True
    assert cap.event_types == ["com.github.*"]
    assert cap.extra_oauth_scopes == ["admin:repo_hook"]


def test_event_envelope_round_trip():
    """EventEnvelope constructs from a dict and round-trips via model_dump."""
    payload = {
        "id": "evt-1",
        "source": "github",
        "type": "com.github.push",
        "subject": "repo/main",
        "data": {"ref": "refs/heads/main"},
    }
    env = EventEnvelope(**payload)
    dumped = env.model_dump()
    assert dumped["id"] == "evt-1"
    assert dumped["source"] == "github"
    assert dumped["type"] == "com.github.push"
    assert dumped["subject"] == "repo/main"
    assert dumped["data"] == {"ref": "refs/heads/main"}


def test_delivery_envelope_round_trip():
    """DeliveryEnvelope constructs from dicts and round-trips via model_dump."""
    env = DeliveryEnvelope(
        event={
            "id": "evt-1",
            "source": "github",
            "type": "com.github.push",
        },
        subscription={"id": "sub-1"},
    )
    dumped = env.model_dump()
    assert dumped["event"]["id"] == "evt-1"
    assert dumped["subscription"] == {"id": "sub-1"}


def test_gateway_create_with_events_capability():
    """GatewayCreate accepts an EventsCapability via the events field."""
    gw = GatewayCreate(name="g", url="http://x", events=EventsCapability(webhooks_supported=True))
    assert gw.events is not None
    assert gw.events.webhooks_supported is True
