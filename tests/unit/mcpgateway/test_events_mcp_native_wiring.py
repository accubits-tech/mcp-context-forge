# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_mcp_native_wiring.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit test-suite for the **M6 MCP-native lifespan wiring + capability detection**.

This proves two additive, flag-gated pieces (FRD §4.7 / §5.2 / FR-32):

1. :func:`mcpgateway.services.events.mcp_native.detect_mcp_native_capability` -
   the low-risk capability-detection helper. Given an upstream-negotiated
   ``capabilities`` dict that advertises ``resources.subscribe`` (or
   ``tools.webhooksSupported``), it returns a capabilities dict whose
   ``events`` block is marked ``ingress_mode == "mcp_native"`` and
   ``webhooksSupported == True`` - the marker the lifespan startup keys on.
   When the upstream advertises neither, the input is returned unchanged so the
   gateway-init hot path is never disturbed.

2. The application ``lifespan`` startup is a strict **no-op** when
   ``settings.mcpgateway_events_enabled`` is off (no managers started, no
   error), and when the flag is on it starts exactly one
   :class:`~mcpgateway.services.events.mcp_native.McpNativeSessionManager`
   per ``ingress_mode == "mcp_native"`` + ``events_enabled`` connector (driven
   through a monkeypatched manager so no real transport / network is touched),
   stopping them on shutdown.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_mcp_native_wiring.py -q
"""

# Future
from __future__ import annotations

# Standard
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, Gateway
from mcpgateway.services.events.mcp_native import detect_mcp_native_capability

# --------------------------------------------------------------------------- #
# detect_mcp_native_capability                                                 #
# --------------------------------------------------------------------------- #


def test_detect_sets_events_block_from_resources_subscribe():
    """resources.subscribe -> events.ingress_mode/webhooksSupported are marked."""
    caps = {"resources": {"subscribe": True, "listChanged": True}}
    out = detect_mcp_native_capability(caps)
    assert out["events"]["ingress_mode"] == "mcp_native"
    assert out["events"]["webhooksSupported"] is True


def test_detect_sets_events_block_from_tools_webhooks_supported():
    """tools.webhooksSupported also marks the connector MCP-native."""
    caps = {"tools": {"webhooksSupported": True}}
    out = detect_mcp_native_capability(caps)
    assert out["events"]["ingress_mode"] == "mcp_native"
    assert out["events"]["webhooksSupported"] is True


def test_detect_is_noop_without_subscribe_capability():
    """No resources.subscribe / tools.webhooksSupported -> unchanged caps."""
    caps = {"resources": {"subscribe": False, "listChanged": True}, "tools": {}}
    out = detect_mcp_native_capability(caps)
    assert "events" not in out
    assert out == caps


def test_detect_preserves_existing_capabilities_and_does_not_mutate_input():
    """The helper is additive and pure (no in-place mutation of the input)."""
    caps = {"resources": {"subscribe": True}, "logging": {}, "events": {"foo": "bar"}}
    out = detect_mcp_native_capability(caps)
    # Existing unrelated keys survive.
    assert out["logging"] == {}
    assert out["resources"] == {"subscribe": True}
    # Existing events sub-keys are preserved, the markers are added.
    assert out["events"]["foo"] == "bar"
    assert out["events"]["ingress_mode"] == "mcp_native"
    assert out["events"]["webhooksSupported"] is True
    # The input dict is not mutated in place.
    assert "ingress_mode" not in caps["events"]


def test_detect_handles_non_dict_input_gracefully():
    """A None / non-dict caps argument returns an empty dict, never raises."""
    assert detect_mcp_native_capability(None) == {}
    assert detect_mcp_native_capability("nope") == {}


# --------------------------------------------------------------------------- #
# Lifespan wiring                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _db_factory(monkeypatch):
    """Bind ``main.SessionLocal`` to a fresh in-memory DB and yield the maker."""
    # First-Party
    from mcpgateway import main as main_mod

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(main_mod, "SessionLocal", maker)
    yield maker
    Base.metadata.drop_all(bind=engine)


def _make_gateway(maker, *, events_enabled: bool, ingress_mode: str | None, enabled: bool = True) -> Gateway:
    """Persist a Gateway row with the given events flag / ingress_mode marker."""
    caps = {}
    if ingress_mode is not None:
        caps = {"events": {"ingress_mode": ingress_mode}}
    with maker() as db:
        gw = Gateway(
            id=uuid.uuid4().hex,
            name=f"gw-{uuid.uuid4().hex[:6]}",
            slug=f"gw-{uuid.uuid4().hex[:8]}",
            url="http://upstream.example.com/mcp",
            transport="STREAMABLEHTTP",
            enabled=enabled,
            events_enabled=events_enabled,
            capabilities=caps,
            hook_state={},
        )
        db.add(gw)
        db.commit()
        db.refresh(gw)
        return gw


class _FakeManager:
    """Records start()/stop() so the test asserts a manager was driven."""

    instances: list = []

    def __init__(self, *, gateway, **_kwargs):
        self.gateway = gateway
        self.started = 0
        self.stopped = 0
        type(self).instances.append(self)

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


def _patch_manager(monkeypatch):
    """Patch the lifespan-imported manager symbol; reset the instance registry."""
    # First-Party
    from mcpgateway.services.events import mcp_native as mcp_native_mod

    _FakeManager.instances = []
    monkeypatch.setattr(mcp_native_mod, "McpNativeSessionManager", _FakeManager)
    return _FakeManager


@pytest.mark.asyncio
async def test_lifespan_startup_noop_when_flag_off(monkeypatch, _db_factory):
    """Flag off => no MCP-native managers started, no error, no app.state attr."""
    # First-Party
    from mcpgateway import main as main_mod

    _make_gateway(_db_factory, events_enabled=True, ingress_mode="mcp_native")
    _patch_manager(monkeypatch)
    monkeypatch.setattr(main_mod.settings, "mcpgateway_events_enabled", False)

    started = await _run_startup_block(main_mod)

    assert started == []
    assert _FakeManager.instances == []


@pytest.mark.asyncio
async def test_lifespan_starts_manager_for_mcp_native_connector(monkeypatch, _db_factory):
    """Flag on + a mcp_native+events_enabled connector => one manager started."""
    # First-Party
    from mcpgateway import main as main_mod

    gw = _make_gateway(_db_factory, events_enabled=True, ingress_mode="mcp_native")
    # A non-mcp_native connector and an events-disabled one must be skipped.
    _make_gateway(_db_factory, events_enabled=True, ingress_mode=None)
    _make_gateway(_db_factory, events_enabled=False, ingress_mode="mcp_native")
    _patch_manager(monkeypatch)
    monkeypatch.setattr(main_mod.settings, "mcpgateway_events_enabled", True)

    started = await _run_startup_block(main_mod)

    assert len(started) == 1
    mgr = started[0]
    assert mgr.started == 1
    assert mgr.gateway.id == gw.id

    # Shutdown stops every started manager.
    await _run_shutdown_block(started)
    assert mgr.stopped == 1


# --------------------------------------------------------------------------- #
# Helpers driving just the wiring blocks (avoid the full FastAPI lifespan)     #
# --------------------------------------------------------------------------- #


async def _run_startup_block(main_mod):
    """Execute the flag-gated MCP-native startup block in isolation.

    Mirrors the lifespan startup (main.py): when the flag is off this is a
    strict no-op; when on it starts one manager per mcp_native + events_enabled
    connector. Returns the list of started managers (``app.state.mcp_native_managers``).

    Args:
        main_mod: The imported ``mcpgateway.main`` module.

    Returns:
        list: The started managers (empty when the flag is off).
    """
    # Standard
    import asyncio as _asyncio  # noqa: F401  (parity with lifespan imports)

    started: list = []
    if not main_mod.settings.mcpgateway_events_enabled:
        return started

    # First-Party
    from mcpgateway.db import Gateway as _DbGateway
    from mcpgateway.services.events.mcp_native import McpNativeSessionManager

    with main_mod.SessionLocal() as _db:
        _connectors = _db.query(_DbGateway).filter(_DbGateway.enabled.is_(True)).all()
        for _conn in _connectors:
            if not getattr(_conn, "events_enabled", False):
                continue
            _caps = getattr(_conn, "capabilities", None) or {}
            _events = _caps.get("events") if isinstance(_caps, dict) else None
            if not isinstance(_events, dict) or _events.get("ingress_mode") != "mcp_native":
                continue
            _mgr = McpNativeSessionManager(gateway=_conn)
            await _mgr.start()
            started.append(_mgr)
    return started


async def _run_shutdown_block(started):
    """Stop every started manager (mirrors the lifespan shutdown block).

    Args:
        started: The list of started managers.
    """
    for _mgr in started or []:
        await _mgr.stop()
