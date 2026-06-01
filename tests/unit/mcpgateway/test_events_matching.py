# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_matching.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.matching**.

These tests drive the L2 match stage (FRD section 8.6) against a real
(temporary, in-memory) database. Two ``Gateway`` connections are seeded in two
different teams, several ``EventSubscription`` rows are persisted (some with CEL
``filter_expr``, some with reverse-DNS ``event_types`` globs only, some
inactive, some expired, some cross-tenant), a canonical ``EventEnvelope`` is
built, and the match contract is asserted:

* :func:`envelope_to_ctx` builds the CEL activation dict
  (``{"event", "data", "type", "source", "subject"}``) from an envelope.
* :func:`matches` combines the reverse-DNS glob pre-filter with the CEL filter.
* :func:`find_candidate_subscriptions` is tenant-leading and enforces
  cross-tenant isolation:
  - TC-SUB-016: three subs matching one event yield three candidates.
  - TC-SUB-029 / SC-SEC-029: an event for team A returns ONLY team-A subs;
    team-B subs are structurally excluded.
  - glob + CEL combine correctly (a candidate must pass both gates).
  - inactive and expired (correlate) subs are excluded.
  - a CEL filter that fails closed yields no match.

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_matching.py -q
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timedelta, timezone
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, EventSubscription, Gateway
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events import matching as matching_mod
from mcpgateway.services.events.matching import (
    envelope_to_ctx,
    find_candidate_subscriptions,
    matches,
)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

TEAM_A = "team-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEAM_B = "team-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SOURCE_A = "https://github.com/acme/api"
EVENT_TYPE = "com.github.push"


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


def _make_gateway(db, *, team_id: str, source: str) -> Gateway:
    """Persist a connection in *team_id* whose canonical source is *source*."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url=source,
        capabilities={},
        team_id=team_id,
        events_enabled=True,
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _make_sub(
    db,
    *,
    team_id: str,
    gateway_id=None,
    source=None,
    event_types=None,
    filter_expr=None,
    active=True,
    mode="fanout",
    correlation_key=None,
    correlation_value=None,
    expires_at=None,
    subscriber_kind="sse",
) -> EventSubscription:
    """Persist an :class:`EventSubscription` with the given match criteria."""
    sub = EventSubscription(
        id=uuid.uuid4().hex,
        gateway_id=gateway_id,
        team_id=team_id,
        owner_email="finance@bud.studio",
        subscriber_kind=subscriber_kind,
        source=source,
        event_types=event_types if event_types is not None else [EVENT_TYPE],
        filter_expr=filter_expr,
        mode=mode,
        correlation_key=correlation_key,
        correlation_value=correlation_value,
        active=active,
        expires_at=expires_at,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _envelope(**overrides) -> EventEnvelope:
    """Build a canonical GitHub-push envelope, overridable per field."""
    payload = {
        "id": "deliv-1",
        "source": SOURCE_A,
        "type": EVENT_TYPE,
        "subject": "octo/repo",
        "time": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "data": {"ref": "refs/heads/main", "repository": {"full_name": "acme/api"}},
    }
    payload.update(overrides)
    return EventEnvelope(**payload)


# --------------------------------------------------------------------------- #
# envelope_to_ctx                                                              #
# --------------------------------------------------------------------------- #


def test_envelope_to_ctx_shape():
    env = _envelope()
    ctx = envelope_to_ctx(env)

    assert ctx["type"] == EVENT_TYPE
    assert ctx["source"] == SOURCE_A
    assert ctx["subject"] == "octo/repo"
    assert ctx["data"] == {"ref": "refs/heads/main", "repository": {"full_name": "acme/api"}}
    # The full envelope is exposed under "event" for dotted CEL access.
    assert isinstance(ctx["event"], dict)
    assert ctx["event"]["type"] == EVENT_TYPE
    assert ctx["event"]["data"]["ref"] == "refs/heads/main"


def test_envelope_to_ctx_accepts_dict_form():
    env = _envelope()
    ctx_from_obj = envelope_to_ctx(env)
    ctx_from_dict = envelope_to_ctx({"id": "deliv-1", "source": SOURCE_A, "type": EVENT_TYPE, "subject": "octo/repo", "data": ctx_from_obj["data"]})

    assert ctx_from_dict["type"] == ctx_from_obj["type"]
    assert ctx_from_dict["source"] == ctx_from_obj["source"]
    assert ctx_from_dict["data"] == ctx_from_obj["data"]


# --------------------------------------------------------------------------- #
# matches (glob + CEL combination)                                            #
# --------------------------------------------------------------------------- #


def test_matches_glob_only(session):
    sub = _make_sub(session, team_id=TEAM_A, source=SOURCE_A, event_types=["com.github.*"])
    ctx = envelope_to_ctx(_envelope())
    assert matches(sub, ctx) is True


def test_matches_glob_miss(session):
    sub = _make_sub(session, team_id=TEAM_A, source=SOURCE_A, event_types=["com.stripe.*"])
    ctx = envelope_to_ctx(_envelope())
    assert matches(sub, ctx) is False


def test_matches_glob_and_cel_both_pass(session):
    sub = _make_sub(
        session,
        team_id=TEAM_A,
        source=SOURCE_A,
        event_types=["com.github.*"],
        filter_expr='data.ref == "refs/heads/main"',
    )
    ctx = envelope_to_ctx(_envelope())
    assert matches(sub, ctx) is True


def test_matches_glob_pass_cel_false(session):
    sub = _make_sub(
        session,
        team_id=TEAM_A,
        source=SOURCE_A,
        event_types=["com.github.*"],
        filter_expr='data.ref == "refs/heads/release"',
    )
    ctx = envelope_to_ctx(_envelope())
    assert matches(sub, ctx) is False


def test_matches_cel_fail_closed(session):
    # Filter references a field absent from the payload -> runtime error -> no match.
    sub = _make_sub(
        session,
        team_id=TEAM_A,
        source=SOURCE_A,
        event_types=["com.github.*"],
        filter_expr='data.no_such_field == "x"',
    )
    ctx = envelope_to_ctx(_envelope())
    assert matches(sub, ctx) is False


# --------------------------------------------------------------------------- #
# find_candidate_subscriptions: fan-out (TC-SUB-016)                          #
# --------------------------------------------------------------------------- #


def test_three_subs_match_one_event(session):
    """TC-SUB-016: three subs matching one event yield three candidates."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    # (1) gateway-bound, glob match
    _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"])
    # (2) gateway-bound, exact type + CEL pass
    _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=[EVENT_TYPE], filter_expr='data.ref == "refs/heads/main"')
    # (3) cross-provider (gateway_id None) matched by source + team
    _make_sub(session, team_id=TEAM_A, gateway_id=None, source=SOURCE_A, event_types=["com.github.*"])

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert len(candidates) == 3


# --------------------------------------------------------------------------- #
# find_candidate_subscriptions: cross-tenant isolation (TC-SUB-029/SC-SEC-029)#
# --------------------------------------------------------------------------- #


def test_cross_tenant_isolation(session):
    """SC-SEC-029: an event for team A returns ONLY team-A subs; team B excluded."""
    gw_a = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    # team A: gateway-bound, matches.
    sub_a = _make_sub(session, team_id=TEAM_A, gateway_id=gw_a.id, event_types=["com.github.*"])
    # team B: identical match criteria, cross-provider on the SAME source -> must be excluded.
    _make_sub(session, team_id=TEAM_B, gateway_id=None, source=SOURCE_A, event_types=["com.github.*"])
    # team B: bound to a different (team-B) gateway with the same source -> excluded.
    gw_b = _make_gateway(session, team_id=TEAM_B, source=SOURCE_A)
    _make_sub(session, team_id=TEAM_B, gateway_id=gw_b.id, event_types=["com.github.*"])

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw_a)

    assert [c.id for c in candidates] == [sub_a.id]


def test_gateway_bound_sub_only_matches_its_connector(session):
    """A sub bound to gateway X must not match an event from gateway Y (same team)."""
    gw_x = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)
    gw_y = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    # Bound to gw_y; the event comes from gw_x -> excluded despite same team + source.
    _make_sub(session, team_id=TEAM_A, gateway_id=gw_y.id, event_types=["com.github.*"])
    sub_x = _make_sub(session, team_id=TEAM_A, gateway_id=gw_x.id, event_types=["com.github.*"])

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw_x)

    assert [c.id for c in candidates] == [sub_x.id]


def test_cross_provider_sub_matches_by_source(session):
    """A cross-provider sub (gateway_id None) matches only when source AND team align."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    same_source = _make_sub(session, team_id=TEAM_A, gateway_id=None, source=SOURCE_A, event_types=["com.github.*"])
    # Same team, DIFFERENT source -> excluded.
    _make_sub(session, team_id=TEAM_A, gateway_id=None, source="https://github.com/other/repo", event_types=["com.github.*"])

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert [c.id for c in candidates] == [same_source.id]


# --------------------------------------------------------------------------- #
# find_candidate_subscriptions: inactive / expired excluded                   #
# --------------------------------------------------------------------------- #


def test_inactive_sub_excluded(session):
    """TC-SUB-012: an inactive sub is never a candidate."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    active = _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"], active=True)
    _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"], active=False)

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert [c.id for c in candidates] == [active.id]


def test_expired_correlate_sub_excluded(session):
    """An expired correlate sub is skipped; a not-yet-expired one survives."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    # Distinct correlation_value per row: the partial unique backstop
    # (uq_event_sub_active_corr_value on team_id, correlation_value) forbids two
    # correlate rows sharing the same non-null value, independent of expiry.
    _make_sub(
        session,
        team_id=TEAM_A,
        gateway_id=gw.id,
        event_types=["com.github.*"],
        mode="correlate",
        correlation_key="$.subject",
        correlation_value="octo/repo-expired",
        expires_at=past,
    )
    live = _make_sub(
        session,
        team_id=TEAM_A,
        gateway_id=gw.id,
        event_types=["com.github.*"],
        mode="correlate",
        correlation_key="$.subject",
        correlation_value="octo/repo",
        expires_at=future,
    )

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert [c.id for c in candidates] == [live.id]


def test_glob_miss_sub_excluded_from_candidates(session):
    """A sub whose event_types glob does not match the event type is excluded."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    hit = _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"])
    _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.stripe.*"])

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert [c.id for c in candidates] == [hit.id]


def test_cel_fail_closed_sub_excluded_from_candidates(session):
    """A candidate whose CEL filter fails closed contributes no match."""
    gw = _make_gateway(session, team_id=TEAM_A, source=SOURCE_A)

    ok = _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"], filter_expr='data.ref == "refs/heads/main"')
    _make_sub(session, team_id=TEAM_A, gateway_id=gw.id, event_types=["com.github.*"], filter_expr='data.no_such_field == "x"')

    env = _envelope()
    candidates = find_candidate_subscriptions(session, envelope=env, gateway=gw)

    assert [c.id for c in candidates] == [ok.id]


def test_module_smoke_import():
    """The module exposes its public contract surface."""
    assert hasattr(matching_mod, "envelope_to_ctx")
    assert hasattr(matching_mod, "matches")
    assert hasattr(matching_mod, "find_candidate_subscriptions")
