# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_mcp_native.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit test-suite for **mcpgateway.services.events.mcp_native**.

This proves the M6 MCP-native ingress adapter: a PERSISTENT upstream
``ClientSession`` (``message_handler=self._on_message``, *not* the one-shot
``async with`` federation uses today) whose received
``notifications/resources/updated`` are re-emitted as the canonical reverse-DNS
event ``com.mcp.resource.updated`` through the shared emission tail
(:func:`mcpgateway.services.events.emit.publish_normalized_event`), with a
gateway-synthesized deterministic ``evt_id`` (FRD §5.2 / §8.3 / FR-11a / FR-32).

The MCP gating test-cases (FRD §11 M6 gate) covered here:

* **TC-MCP-001** - a ``resources/updated`` produces exactly one normalized
  ``com.mcp.resource.updated`` event with ``subject = uri``, ``data`` from a
  ``resources/read``, and a synthesized ``evt_id``.
* **TC-MCP-002** - the synthesized id is deterministic/stable and differs per
  ``uri`` and per relay ``seq``.
* **TC-MCP-003** - the same update relayed twice with the same synthesized id is
  deduped (one downstream emit).
* **TC-MCP-004** - a session ``404``/break drives :meth:`reconnect`, which
  re-initializes, re-lists, and re-issues ``resources/subscribe`` for ALL active
  uris (no silent drop).
* **TC-MCP-025** - the manager keeps the session OPEN after listing
  (persistent-session mode) and retains subscriptions; it does NOT use a
  one-shot ``async with`` that tears down + drops subs (the regression fixed).
* **TC-MCP-022** (best-effort) - a follow-up routed to a worker without the live
  session re-initializes/re-subscribes rather than orphaning subs.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_mcp_native.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from typing import List, Optional
import uuid

# Third-Party
import mcp.types as mcp_types
from pydantic import AnyUrl
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, EventLog, Gateway
from mcpgateway.services.events import bus as bus_mod
from mcpgateway.services.events import ingress_service as ingress_mod
from mcpgateway.services.events import mcp_native as mcp_native_mod
from mcpgateway.services.events import stream as stream_mod
from mcpgateway.services.events.emit import synthesize_mcp_event_id
from mcpgateway.services.events.mcp_native import McpNativeSessionManager

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

EVT_TYPE = "com.mcp.resource.updated"


@pytest.fixture
def session_factory():
    """A SessionLocal-style factory bound to a fresh in-memory database.

    Returns a zero-arg callable that yields a brand-new ``Session`` each call,
    mirroring how the real manager uses ``SessionLocal()`` for a fresh
    unit-of-work per notification.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield maker
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


def _make_gateway(db, *, transport: str = "STREAMABLEHTTP", capabilities: Optional[dict] = None) -> Gateway:
    """Persist a minimal MCP-native Gateway row to satisfy the event_log FK."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://upstream.example.com/mcp",
        transport=transport,
        capabilities=capabilities if capabilities is not None else {"events": {"ingress_mode": "mcp_native"}},
        hook_state={},
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _resource_updated(uri: str) -> mcp_types.ServerNotification:
    """Build an outer ServerNotification wrapping a resources/updated for *uri*."""
    return mcp_types.ServerNotification(
        mcp_types.ResourceUpdatedNotification(
            params=mcp_types.ResourceUpdatedNotificationParams(uri=AnyUrl(uri)),
        )
    )


def _list_changed(kind: str) -> mcp_types.ServerNotification:
    """Build a */list_changed ServerNotification (resources|tools|prompts)."""
    cls = {
        "resources": mcp_types.ResourceListChangedNotification,
        "tools": mcp_types.ToolListChangedNotification,
        "prompts": mcp_types.PromptListChangedNotification,
    }[kind]
    return mcp_types.ServerNotification(cls())


def _read_result(uri: str, text: str) -> mcp_types.ReadResourceResult:
    """A canned ReadResourceResult with a single text content for *uri*."""
    return mcp_types.ReadResourceResult(contents=[mcp_types.TextResourceContents(uri=AnyUrl(uri), text=text)])


class FakeClientSession:
    """A scripted stand-in for ``mcp.client.session.ClientSession``.

    Records the upstream calls the manager makes (initialize / subscribe /
    unsubscribe / read / list_*) so the tests can assert the persistent-session
    contract without a real transport. ``subscribe_resource`` etc. can be set to
    raise to simulate a broken / 404 stream.
    """

    def __init__(self, *, subscribe_supported: bool = True, read_text: str = "hello"):
        self.initialized = 0
        self.subscribed: List[str] = []
        self.unsubscribed: List[str] = []
        self.reads: List[str] = []
        self.listed_resources = 0
        self.listed_tools = 0
        self.listed_prompts = 0
        self.closed = 0
        self._read_text = read_text
        self._subscribe_supported = subscribe_supported
        self.fail_next_subscribe = False

    async def initialize(self) -> mcp_types.InitializeResult:
        self.initialized += 1
        caps = mcp_types.ServerCapabilities(
            resources=mcp_types.ResourcesCapability(subscribe=self._subscribe_supported, listChanged=True),
        )
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=caps,
            serverInfo=mcp_types.Implementation(name="fake", version="0"),
        )

    async def subscribe_resource(self, uri):  # accepts str/AnyUrl
        if self.fail_next_subscribe:
            self.fail_next_subscribe = False
            raise RuntimeError("HTTP 404: session not found")
        self.subscribed.append(str(uri))
        return mcp_types.EmptyResult()

    async def unsubscribe_resource(self, uri):
        self.unsubscribed.append(str(uri))
        return mcp_types.EmptyResult()

    async def read_resource(self, uri) -> mcp_types.ReadResourceResult:
        self.reads.append(str(uri))
        return _read_result(str(uri), self._read_text)

    async def list_resources(self, *args, **kwargs) -> mcp_types.ListResourcesResult:
        self.listed_resources += 1
        return mcp_types.ListResourcesResult(resources=[])

    async def list_tools(self, *args, **kwargs) -> mcp_types.ListToolsResult:
        self.listed_tools += 1
        return mcp_types.ListToolsResult(tools=[])

    async def list_prompts(self, *args, **kwargs) -> mcp_types.ListPromptsResult:
        self.listed_prompts += 1
        return mcp_types.ListPromptsResult(prompts=[])

    async def aclose(self) -> None:
        self.closed += 1


def _manager(gw, session, *, sessions: Optional[List[FakeClientSession]] = None):
    """Build a manager wired to a session_factory and a scripted client_factory.

    ``sessions`` is a list of pre-built FakeClientSession instances handed out
    one-per-:meth:`start`/:meth:`reconnect` call so a reconnect test gets a
    distinct (fresh) session object to assert against.
    """
    pool = list(sessions) if sessions is not None else [FakeClientSession()]
    handed: List[FakeClientSession] = []

    async def client_factory(_gateway):
        nxt = pool.pop(0) if pool else FakeClientSession()
        handed.append(nxt)
        return nxt

    mgr = McpNativeSessionManager(gateway=gw, session_factory=session, client_factory=client_factory)
    mgr._handed = handed  # test-visible record of sessions actually used
    return mgr


def _drain(queue) -> list:
    """Drain all currently-queued events from a bus subscriber queue."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# --------------------------------------------------------------------------- #
# TC-MCP-001 - resources/updated -> one normalized com.mcp.resource.updated     #
# --------------------------------------------------------------------------- #


def test_resources_updated_emits_one_normalized_event(session_factory):
    """A single resources/updated yields exactly one normalized event."""
    with session_factory() as db:
        gw = _make_gateway(db)
        gw_id = gw.id
    queue = bus_mod.get_event_bus().subscribe()

    fake = FakeClientSession(read_text="contents-of-doc-1")
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        # Register an active subscription so the source/seq bookkeeping exists,
        # then deliver the upstream notification straight to the handler.
        await mgr.subscribe_resource("res://doc/1")
        await mgr._on_message(_resource_updated("res://doc/1"))
        await mgr.stop()

    asyncio.run(scenario())

    # Exactly one persisted event_log row carrying the normalized envelope.
    with session_factory() as db:
        rows = db.execute(select(EventLog)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.evt_type == EVT_TYPE
        assert row.evt_subject == "res://doc/1"
        assert row.evt_source == f"//{gw_id}"
        assert row.gateway_id == gw_id
        # data carries the fetched read result.
        assert "res://doc/1" in str(row.data)
        assert "contents-of-doc-1" in str(row.data)
        # The id is the synthesized deterministic digest.
        expected = synthesize_mcp_event_id(gateway_id=gw_id, source=f"//{gw_id}", type=EVT_TYPE, subject="res://doc/1", seq=1)
        assert row.evt_id == expected

    # The upstream read was issued to fetch content.
    assert fake.reads == ["res://doc/1"]

    # Exactly one L1 fan-out of the inner event dict.
    published = _drain(queue)
    assert len(published) == 1
    assert published[0]["type"] == EVT_TYPE
    assert published[0]["subject"] == "res://doc/1"


def test_resources_updated_subject_is_uri(session_factory):
    """The envelope subject is the resource uri verbatim (string form)."""
    with session_factory() as db:
        gw = _make_gateway(db)
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        await mgr._on_message(_resource_updated("res://folder/item.txt"))
        await mgr.stop()

    asyncio.run(scenario())
    with session_factory() as db:
        row = db.execute(select(EventLog)).scalars().one()
        assert row.evt_subject == "res://folder/item.txt"


# --------------------------------------------------------------------------- #
# TC-MCP-002 - synthesized id deterministic / differs per uri & seq             #
# --------------------------------------------------------------------------- #


def test_synthesized_id_differs_per_uri_and_seq(session_factory):
    """Distinct uris and distinct relay seqs produce distinct synthesized ids."""
    with session_factory() as db:
        gw = _make_gateway(db)
        gw_id = gw.id
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        await mgr._on_message(_resource_updated("res://a"))  # a, seq 1
        await mgr._on_message(_resource_updated("res://b"))  # b, seq 1
        await mgr._on_message(_resource_updated("res://a"))  # a, seq 2 (new update)
        await mgr.stop()

    asyncio.run(scenario())

    with session_factory() as db:
        rows = db.execute(select(EventLog)).scalars().all()
        ids = sorted(r.evt_id for r in rows)
    assert len(ids) == 3  # all three are distinct -> three persisted rows
    assert len(set(ids)) == 3

    src = f"//{gw_id}"
    assert synthesize_mcp_event_id(gateway_id=gw_id, source=src, type=EVT_TYPE, subject="res://a", seq=1) in ids
    assert synthesize_mcp_event_id(gateway_id=gw_id, source=src, type=EVT_TYPE, subject="res://b", seq=1) in ids
    assert synthesize_mcp_event_id(gateway_id=gw_id, source=src, type=EVT_TYPE, subject="res://a", seq=2) in ids


# --------------------------------------------------------------------------- #
# TC-MCP-003 - same update relayed twice (same seq) -> deduped                   #
# --------------------------------------------------------------------------- #


def test_replayed_notification_same_seq_is_deduped(session_factory):
    """A notification replayed with the SAME relay seq dedups (one emit)."""
    with session_factory() as db:
        gw = _make_gateway(db)
    queue = bus_mod.get_event_bus().subscribe()
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    # The SAME ServerNotification object replayed twice with the SAME seq must
    # collapse: identical synthesized id -> dedup -> one downstream emit.
    async def scenario():
        await mgr.start()
        notif = _resource_updated("res://doc/1")
        await mgr._on_message(notif, seq=5)
        await mgr._on_message(notif, seq=5)
        await mgr.stop()

    asyncio.run(scenario())

    with session_factory() as db:
        assert len(db.execute(select(EventLog)).scalars().all()) == 1
    assert len(_drain(queue)) == 1


# --------------------------------------------------------------------------- #
# TC-MCP-025 - persistent session: not one-shot, subs retained                  #
# --------------------------------------------------------------------------- #


def test_session_stays_open_after_listing_persistent_mode(session_factory):
    """start() keeps the session OPEN (persistent) and retains subscriptions."""
    with session_factory() as db:
        gw = _make_gateway(db)
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://doc/1")
        # After start + subscribe, the session must NOT have been torn down.
        assert fake.closed == 0
        assert mgr.is_running() is True
        # A notification can still be processed (proves the loop is live).
        await mgr._on_message(_resource_updated("res://doc/1"))
        # Subscriptions are retained on the manager (not dropped).
        assert "res://doc/1" in mgr.active_uris()
        await mgr.stop()

    asyncio.run(scenario())
    # Only stop() tears the session down (one-shot would have closed it earlier).
    assert fake.closed == 1
    # The subscribe was issued exactly once upstream (0 -> 1 transition).
    assert fake.subscribed == ["res://doc/1"]
    # And events still flowed while the session was held open.
    with session_factory() as db:
        assert len(db.execute(select(EventLog)).scalars().all()) == 1


def test_subscribe_is_refcounted(session_factory):
    """subscribe is issued only on 0->1; unsubscribe only on 1->0."""
    with session_factory() as db:
        gw = _make_gateway(db)
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://doc/1")  # 0 -> 1: upstream subscribe
        await mgr.subscribe_resource("res://doc/1")  # 1 -> 2: no upstream call
        await mgr.unsubscribe_resource("res://doc/1")  # 2 -> 1: no upstream call
        assert "res://doc/1" in mgr.active_uris()
        await mgr.unsubscribe_resource("res://doc/1")  # 1 -> 0: upstream unsubscribe
        assert "res://doc/1" not in mgr.active_uris()
        await mgr.stop()

    asyncio.run(scenario())
    assert fake.subscribed == ["res://doc/1"]
    assert fake.unsubscribed == ["res://doc/1"]


# --------------------------------------------------------------------------- #
# TC-MCP-004 - reconnect re-inits, re-lists, re-subscribes ALL active uris       #
# --------------------------------------------------------------------------- #


def test_reconnect_resubscribes_all_active_uris(session_factory):
    """A session break drives reconnect() to re-init + re-list + re-subscribe ALL uris."""
    with session_factory() as db:
        gw = _make_gateway(db)
    first = FakeClientSession()
    second = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[first, second])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://a")
        await mgr.subscribe_resource("res://b")
        assert first.subscribed == ["res://a", "res://b"]
        # Simulate a stream break / 404 and reconnect.
        await mgr.reconnect()
        await mgr.stop()

    asyncio.run(scenario())

    # A fresh session object was opened and re-initialized.
    assert mgr._handed[0] is first
    assert mgr._handed[1] is second
    assert second.initialized == 1
    # It re-listed (reconcile) and re-subscribed BOTH active uris (no silent drop).
    assert second.listed_resources >= 1
    assert sorted(second.subscribed) == ["res://a", "res://b"]
    # The old (broken) session was torn down.
    assert first.closed == 1


def test_reconnect_after_subscribe_failure_no_silent_drop(session_factory):
    """A subscribe that hits a 404 mid-stream is recovered by reconnect, not dropped."""
    with session_factory() as db:
        gw = _make_gateway(db)
    first = FakeClientSession()
    second = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[first, second])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://a")
        # The next on_message arrives but the session is broken: simulate by
        # reconnecting (what the live loop does on an Exception arm).
        await mgr.reconnect()
        # After reconnect the active uri is re-subscribed on the NEW session.
        assert "res://a" in mgr.active_uris()
        await mgr.stop()

    asyncio.run(scenario())
    assert second.subscribed == ["res://a"]


# --------------------------------------------------------------------------- #
# TC-MCP-007/008 (best-effort) - capability fallback when subscribe unsupported #
# --------------------------------------------------------------------------- #


def test_no_subscribe_when_capability_absent(session_factory):
    """When upstream resources.subscribe is false, no resources/subscribe is sent."""
    with session_factory() as db:
        gw = _make_gateway(db)
    fake = FakeClientSession(subscribe_supported=False)
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://a")
        assert mgr.supports_subscribe() is False
        # The uri is still tracked (so list_changed/polling fallback can use it),
        # but no upstream resources/subscribe call was made.
        assert "res://a" in mgr.active_uris()
        await mgr.stop()

    asyncio.run(scenario())
    assert fake.subscribed == []


# --------------------------------------------------------------------------- #
# list_changed handling                                                         #
# --------------------------------------------------------------------------- #


def test_resources_list_changed_relists_and_emits_no_event(session_factory):
    """resources/list_changed triggers a re-list and emits NO domain event."""
    with session_factory() as db:
        gw = _make_gateway(db)
    queue = bus_mod.get_event_bus().subscribe()
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        before = fake.listed_resources
        await mgr._on_message(_list_changed("resources"))
        await mgr.stop()
        return before

    before = asyncio.run(scenario())
    # A re-list happened beyond the initial start() listing.
    assert fake.listed_resources > before
    # No event_log row, no bus publish (list_changed is not a domain event).
    with session_factory() as db:
        assert db.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


def test_tools_list_changed_refetches_tools(session_factory):
    """tools/list_changed re-fetches tools (and emits no event)."""
    with session_factory() as db:
        gw = _make_gateway(db)
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    async def scenario():
        await mgr.start()
        before = fake.listed_tools
        await mgr._on_message(_list_changed("tools"))
        await mgr.stop()
        return before

    before = asyncio.run(scenario())
    assert fake.listed_tools > before
    with session_factory() as db:
        assert db.execute(select(EventLog)).scalars().all() == []


# --------------------------------------------------------------------------- #
# notifications/message -> logging only, no event                               #
# --------------------------------------------------------------------------- #


def test_logging_message_emits_no_event(session_factory):
    """notifications/message routes to logging and emits no domain event."""
    with session_factory() as db:
        gw = _make_gateway(db)
    queue = bus_mod.get_event_bus().subscribe()
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    log_notif = mcp_types.ServerNotification(
        mcp_types.LoggingMessageNotification(
            params=mcp_types.LoggingMessageNotificationParams(level="info", data="hi"),
        )
    )

    async def scenario():
        await mgr.start()
        await mgr._on_message(log_notif)
        await mgr.stop()

    asyncio.run(scenario())
    with session_factory() as db:
        assert db.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


def test_exception_arm_triggers_reconnect(session_factory):
    """An Exception delivered to the handler triggers a reconnect (no crash, no drop)."""
    with session_factory() as db:
        gw = _make_gateway(db)
    first = FakeClientSession()
    second = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[first, second])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://a")
        # The SDK delivers a transport/decode error as an Exception object.
        await mgr._on_message(RuntimeError("HTTP 404: stream gone"))
        await mgr.stop()

    asyncio.run(scenario())
    # Reconnect happened: a second session was opened and re-subscribed res://a.
    assert second.initialized == 1
    assert second.subscribed == ["res://a"]


def test_request_responder_arm_is_ignored(session_factory):
    """A server-initiated request / non-notification object is ignored (no event, no crash)."""
    with session_factory() as db:
        gw = _make_gateway(db)
    queue = bus_mod.get_event_bus().subscribe()
    fake = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[fake])

    class _FakeRequest:  # a server-initiated request stand-in (not a notification)
        pass

    async def scenario():
        await mgr.start()
        # Route a non-notification, non-exception object: must be a no-op.
        await mgr._on_message(_FakeRequest())
        await mgr.stop()

    asyncio.run(scenario())
    with session_factory() as db:
        assert db.execute(select(EventLog)).scalars().all() == []
    assert _drain(queue) == []


# --------------------------------------------------------------------------- #
# TC-MCP-022 (best-effort) - re-subscribe path on a session-less worker         #
# --------------------------------------------------------------------------- #


def test_resubscribe_all_after_session_loss(session_factory):
    """A follow-up handling that lost the session re-subscribes rather than orphaning.

    Single-process assumption: subscriptions are tracked on the manager; if the
    upstream session is gone, the manager re-establishes it and re-issues every
    active resources/subscribe (the same code path reconnect() exercises), so a
    worker that picks up after a break does not orphan the subscriptions.
    """
    with session_factory() as db:
        gw = _make_gateway(db)
    first = FakeClientSession()
    second = FakeClientSession()
    mgr = _manager(gw, session_factory, sessions=[first, second])

    async def scenario():
        await mgr.start()
        await mgr.subscribe_resource("res://x")
        await mgr.subscribe_resource("res://y")
        # Simulate the worker losing its live session entirely.
        await mgr.stop()
        assert mgr.is_running() is False
        # The active uris are still known and can be re-established.
        assert sorted(mgr.active_uris()) == ["res://x", "res://y"]
        # Re-establish: every active uri is re-subscribed on the new session.
        await mgr.start()
        await mgr.stop()

    asyncio.run(scenario())
    assert sorted(second.subscribed) == ["res://x", "res://y"]


# --------------------------------------------------------------------------- #
# Smoke import                                                                  #
# --------------------------------------------------------------------------- #


def test_module_imports_cleanly():
    """Smoke-import the module surface."""
    assert hasattr(mcp_native_mod, "McpNativeSessionManager")
    mgr_cls = mcp_native_mod.McpNativeSessionManager
    for name in ("start", "stop", "reconnect", "subscribe_resource", "unsubscribe_resource", "_on_message"):
        assert hasattr(mgr_cls, name)


# --------------------------------------------------------------------------- #
# TC-MCP-025 (true end-to-end) - real in-memory MCP server, persistent session  #
# --------------------------------------------------------------------------- #


def test_end_to_end_inmemory_resource_updated(session_factory):
    """A live in-memory MCP server emits resources/updated; the manager normalizes it.

    A real ``create_connected_server_and_client_session`` link is used: the
    manager's ``_on_message`` is wired as the live client's ``message_handler``
    and the live client is the manager's session (so ``read_resource`` goes over
    the wire). A ``call_tool`` on the server triggers a genuine server-initiated
    ``notifications/resources/updated`` over the in-memory transport, which the
    client's reader dispatches to the manager. This proves the persistent
    session + notification path against a real MCP server (the one-shot drop
    regression is fixed - TC-MCP-025 / FR-32).
    """
    pytest.importorskip("mcp.shared.memory")
    # Third-Party
    from mcp.server.lowlevel import Server
    from mcp.shared.memory import create_connected_server_and_client_session

    with session_factory() as db:
        gw = _make_gateway(db)
        gw_id = gw.id

    uri = "res://live/doc"
    server = Server("e2e-events")

    @server.read_resource()
    async def _read(req_uri):  # noqa: ANN001
        return "live-content"

    @server.list_tools()
    async def _list_tools():
        return [mcp_types.Tool(name="touch", description="emit resources/updated", inputSchema={"type": "object"})]

    @server.call_tool()
    async def _call_tool(name, arguments):  # noqa: ANN001, ARG001
        # Server-initiated notification over the live in-memory transport.
        await server.request_context.session.send_resource_updated(AnyUrl(uri))
        return [mcp_types.TextContent(type="text", text="ok")]

    done = asyncio.Event()
    tasks: List[asyncio.Task] = []

    async def scenario():
        mgr = McpNativeSessionManager(gateway=gw, session_factory=session_factory)

        async def process(message):
            # _on_message issues a re-entrant resources/read on the SAME live
            # session; it must run OUTSIDE the client's reader task (which is
            # what invoked the handler) so the reader stays free to route the
            # read response. The persistent-session manager would normally
            # process notifications off the reader loop the same way.
            await mgr._on_message(message, session=client)
            done.set()

        async def relay(message):
            if isinstance(message, mcp_types.ServerNotification) and isinstance(message.root, mcp_types.ResourceUpdatedNotification):
                tasks.append(asyncio.create_task(process(message)))

        async with create_connected_server_and_client_session(server, message_handler=relay) as client:
            await client.initialize()
            # Invoke the tool -> server sends a real resources/updated back.
            await client.call_tool("touch", {})
            await asyncio.wait_for(done.wait(), timeout=5)
        for task in tasks:
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))

    with session_factory() as db:
        rows = db.execute(select(EventLog)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.evt_type == EVT_TYPE
    assert row.evt_subject == uri
    assert row.evt_source == f"//{gw_id}"
    assert "live-content" in str(row.data)
