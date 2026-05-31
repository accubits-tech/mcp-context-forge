# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_config.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the Triggers & Events configuration block on Settings.
"""

# Third-Party
import pytest

# First-Party
from mcpgateway.config import Settings


def test_events_disabled_by_default():
    """The master triggers/events switch defaults to False (gated off)."""
    settings = Settings(_env_file=None)
    assert settings.mcpgateway_events_enabled is False


def test_events_enabled_via_env_case_insensitive(monkeypatch):
    """MCPGATEWAY_EVENTS_ENABLED env var (case-insensitive) toggles the flag on."""
    monkeypatch.setenv("MCPGATEWAY_EVENTS_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.mcpgateway_events_enabled is True


def test_events_numeric_defaults():
    """Numeric event defaults match the documented FRD values."""
    settings = Settings(_env_file=None)
    assert settings.mcpgateway_events_redis_stream_prefix == "mcpgw:events"
    assert settings.mcpgateway_events_dedup_ttl_seconds == 86400
    assert settings.mcpgateway_events_max_delivery_attempts == 8
    assert settings.mcpgateway_events_max_body_bytes == 26214400
    assert settings.mcpgateway_events_signature_tolerance_seconds == 300
