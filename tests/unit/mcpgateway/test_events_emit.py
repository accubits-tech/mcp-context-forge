# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_emit.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit test-suite for **mcpgateway.services.events.emit**.

This module proves the two reusable primitives that the M6 MCP-native ingress
path and the existing M1 webhook ingress share:

* :func:`synthesize_mcp_event_id` - the deterministic ``evt_id`` synthesizer for
  MCP-native notifications (which carry no provider id). The digest is a stable
  sha256 hex over ``(gateway_id, source, type, subject, seq)`` (FRD §5.2). It is
  deterministic for fixed inputs, stable across calls, and differs for a
  different resource ``uri`` (subject) or relay ``seq`` so a replayed
  notification (same seq) collapses while a genuinely new update differs
  (TC-MCP-002).
* :func:`publish_normalized_event` - the extracted ingress publish tail. Given a
  built :class:`EventEnvelope`, it dedups on the connection-scoped
  ``(source, id)`` key, persists exactly one ``event_log`` row, ``XADD``s the
  accepted event onto the L2 stream, and fans it out once onto the L1 bus,
  returning ``(published=True, event_log_id)``. A duplicate (same
  ``source`` + ``id``) is deduped with no second persist, no second XADD, and
  no second bus publish: the fast-path TTL hit returns
  ``(published=False, None)``, while a cache-miss for a true duplicate falls
  through to the ``(evt_source, evt_id)`` unique-constraint backstop and returns
  ``(published=False, existing_id)`` (TC-MCP-003 dedup mechanics).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_emit.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, EventLog, Gateway
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import emit as emit_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.services.events.emit import publish_normalized_event, synthesize_mcp_event_id

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def session():
    """Create a fresh in-memory database session with all tables built."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """Reset the process-wide bus, stream, and ingress dedup-cache singletons."""
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)
    yield
    monkeypatch.setattr(bus_mod, "_event_bus", None)
    monkeypatch.setattr(stream_mod, "_event_stream", None)
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)


def _make_gateway(db) -> Gateway:
    """Persist a minimal Gateway row to satisfy the event_log FK."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities={},
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _drain(queue) -> list:
    """Drain all currently-queued events from a bus subscriber queue."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# --------------------------------------------------------------------------- #
# synthesize_mcp_event_id (FRD §5.2 / TC-MCP-002)                              #
# --------------------------------------------------------------------------- #


def test_synthesize_mcp_event_id_is_deterministic_and_stable():
    """Fixed inputs always yield the SAME 64-char sha256 hex (across calls)."""
    kwargs = dict(
        gateway_id="gw-1",
        source="//conn-1",
        type="com.mcp.resource.updated",
        subject="res://doc/1",
        seq=7,
    )
    a = synthesize_mcp_event_id(**kwargs)
    b = synthesize_mcp_event_id(**kwargs)

    assert a == b
    assert isinstance(a, str)
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_synthesize_mcp_event_id_differs_for_different_subject():
    """A different resource uri (subject) yields a different id."""
    base = dict(gateway_id="gw-1", source="//conn-1", type="com.mcp.resource.updated", seq=1)
    a = synthesize_mcp_event_id(subject="res://doc/1", **base)
    b = synthesize_mcp_event_id(subject="res://doc/2", **base)

    assert a != b


def test_synthesize_mcp_event_id_differs_for_different_seq():
    """A different relay sequence yields a different id (distinct updates differ)."""
    base = dict(gateway_id="gw-1", source="//conn-1", type="com.mcp.resource.updated", subject="res://doc/1")
    a = synthesize_mcp_event_id(seq=1, **base)
    b = synthesize_mcp_event_id(seq=2, **base)

    assert a != b


def test_synthesize_mcp_event_id_same_seq_collapses():
    """A replayed notification with the SAME seq collapses to the SAME id."""
    base = dict(gateway_id="gw-1", source="//conn-1", type="com.mcp.resource.updated", subject="res://doc/1", seq=42)

    first = synthesize_mcp_event_id(**base)
    replay = synthesize_mcp_event_id(**base)

    assert first == replay


def test_synthesize_mcp_event_id_differs_for_different_gateway():
    """The same logical notification under a different gateway is a different id."""
    base = dict(source="//conn-1", type="com.mcp.resource.updated", subject="res://doc/1", seq=1)
    a = synthesize_mcp_event_id(gateway_id="gw-1", **base)
    b = synthesize_mcp_event_id(gateway_id="gw-2", **base)

    assert a != b


# --------------------------------------------------------------------------- #
# publish_normalized_event: persist + XADD + publish once (TC-MCP-003)         #
# --------------------------------------------------------------------------- #


def _envelope(*, source: str, evt_id: str) -> EventEnvelope:
    """Build a canonical MCP-native envelope for the emit tests."""
    return EventEnvelope(
        id=evt_id,
        source=source,
        type="com.mcp.resource.updated",
        subject="res://doc/1",
        time=datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
        data={"contents": [{"uri": "res://doc/1", "text": "hello"}]},
    )


def test_publish_persists_xadds_and_publishes_once(session):
    gw = _make_gateway(session)
    queue = bus_mod.get_event_bus().subscribe()
    source = f"//{gw.id}"
    envelope = _envelope(source=source, evt_id=synthesize_mcp_event_id(gateway_id=gw.id, source=source, type="com.mcp.resource.updated", subject="res://doc/1", seq=1))

    published, event_log_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=envelope))

    assert published is True
    assert isinstance(event_log_id, str) and event_log_id

    # Exactly one persisted event_log row carrying the envelope fields.
    rows = session.execute(select(EventLog)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == event_log_id
    assert row.evt_id == envelope.id
    assert row.evt_source == source
    assert row.evt_type == "com.mcp.resource.updated"
    assert row.evt_subject == "res://doc/1"
    assert row.gateway_id == gw.id
    assert row.data == {"contents": [{"uri": "res://doc/1", "text": "hello"}]}

    # Exactly one L1 bus publish carrying the inner event dict.
    published_events = _drain(queue)
    assert len(published_events) == 1
    assert published_events[0]["id"] == envelope.id
    assert published_events[0]["type"] == "com.mcp.resource.updated"
    assert published_events[0]["subject"] == "res://doc/1"

    # Exactly one L2 stream entry with the contract shape.
    pending = asyncio.run(stream_mod.get_event_stream().read_group("w1"))
    assert len(pending) == 1
    _, msg = pending[0]
    assert msg["event_log_id"] == event_log_id
    assert msg["gateway_id"] == gw.id
    assert msg["envelope"]["id"] == envelope.id


def test_publish_duplicate_is_deduped_no_second_side_effect(session):
    gw = _make_gateway(session)
    queue = bus_mod.get_event_bus().subscribe()
    source = f"//{gw.id}"
    evt_id = synthesize_mcp_event_id(gateway_id=gw.id, source=source, type="com.mcp.resource.updated", subject="res://doc/1", seq=99)
    envelope = _envelope(source=source, evt_id=evt_id)

    first_pub, first_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=envelope))
    # Re-emit the SAME (source, id) - a replayed notification.
    second_pub, second_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=_envelope(source=source, evt_id=evt_id)))

    assert first_pub is True
    assert second_pub is False

    # No second persist: still exactly one row.
    assert len(session.execute(select(EventLog)).scalars().all()) == 1
    # The fast-path TTL dedup short-circuits before touching the DB, so it
    # reports no event_log id (the DB backstop is what surfaces the existing id;
    # see test_publish_db_backstop_dedupes_when_cache_misses).
    assert first_id is not None
    assert second_id is None

    # No second bus publish: exactly one event fanned out.
    assert len(_drain(queue)) == 1

    # No second XADD: exactly one stream entry.
    assert len(asyncio.run(stream_mod.get_event_stream().read_group("w1"))) == 1


def test_publish_db_backstop_dedupes_when_cache_misses(session, monkeypatch):
    """A cache miss for a true duplicate still dedups via the (evt_source, evt_id) unique constraint."""
    gw = _make_gateway(session)
    queue = bus_mod.get_event_bus().subscribe()
    source = f"//{gw.id}"
    evt_id = "fixed-dup-id"
    envelope = _envelope(source=source, evt_id=evt_id)

    first_pub, first_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=envelope))

    # Force the fast-path TTL cache to miss so the DB unique constraint is the backstop.
    monkeypatch.setattr(ingress_mod, "_DEDUP_CACHE", None)

    second_pub, second_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=_envelope(source=source, evt_id=evt_id)))

    assert first_pub is True
    assert second_pub is False
    assert second_id == first_id
    assert len(session.execute(select(EventLog)).scalars().all()) == 1
    assert len(_drain(queue)) == 1


def test_publish_distinct_ids_both_persist_and_publish(session):
    gw = _make_gateway(session)
    queue = bus_mod.get_event_bus().subscribe()
    source = f"//{gw.id}"

    a_pub, a_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=_envelope(source=source, evt_id="id-a")))
    b_pub, b_id = asyncio.run(publish_normalized_event(session, gateway=gw, envelope=_envelope(source=source, evt_id="id-b")))

    assert a_pub is True and b_pub is True
    assert a_id != b_id
    assert len(session.execute(select(EventLog)).scalars().all()) == 2
    assert len(_drain(queue)) == 2


def test_publish_persists_raw_headers_when_given(session):
    """The optional raw_headers passthrough is stored on the row (webhook parity)."""
    gw = _make_gateway(session)
    source = f"//{gw.id}"
    envelope = _envelope(source=source, evt_id="hdr-id")

    asyncio.run(publish_normalized_event(session, gateway=gw, envelope=envelope, raw_headers={"X-Foo": "bar"}))

    row = session.execute(select(EventLog)).scalars().one()
    assert row.raw_headers == {"X-Foo": "bar"}


def test_emit_module_imports_cleanly():
    """Smoke-import the module surface."""
    assert hasattr(emit_mod, "synthesize_mcp_event_id")
    assert hasattr(emit_mod, "publish_normalized_event")
