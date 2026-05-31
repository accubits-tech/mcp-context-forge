# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_models.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the events/triggers ORM models (M0).

Covers the four new columns added to :class:`Gateway` and the four new
tables: ``event_subscriptions``, ``event_log``, ``delivery_attempts`` and
``dead_letters``.
"""

# Standard
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import (
    Base,
    DeadLetter,
    DeliveryAttempt,
    EventLog,
    EventSubscription,
    Gateway,
)


@pytest.fixture
def session():
    """Create a fresh in-memory database session with all tables built."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_gateway() -> Gateway:
    """Build a minimal valid Gateway row."""
    return Gateway(
        id=uuid.uuid4().hex,
        name="gw-test",
        slug="gw-test",
        url="http://example.com",
        capabilities={},
    )


# ---------------------------------------------------------------------------
# (a) New Gateway columns
# ---------------------------------------------------------------------------
def test_gateway_new_columns_exist_with_nullability():
    """The four new event columns exist on Gateway with correct nullability."""
    cols = Gateway.__table__.columns

    assert "webhook_signing_secret" in cols
    assert cols["webhook_signing_secret"].nullable is True

    assert "webhook_secret_algo" in cols
    assert cols["webhook_secret_algo"].nullable is True

    assert "hook_state" in cols
    assert cols["hook_state"].nullable is True

    assert "events_enabled" in cols
    assert cols["events_enabled"].nullable is False


def test_gateway_events_enabled_default_is_false(session):
    """events_enabled defaults to False on persistence."""
    gw = _make_gateway()
    session.add(gw)
    session.commit()
    session.refresh(gw)
    assert gw.events_enabled is False


# ---------------------------------------------------------------------------
# (b) New tables present
# ---------------------------------------------------------------------------
def test_new_tables_present_in_metadata():
    """All four new tables are registered in Base.metadata."""
    tables = Base.metadata.tables
    for name in ("event_subscriptions", "event_log", "delivery_attempts", "dead_letters"):
        assert name in tables, f"missing table: {name}"


def test_event_subscriptions_columns():
    """event_subscriptions has the expected columns."""
    cols = set(EventSubscription.__table__.columns.keys())
    expected = {
        "id",
        "gateway_id",
        "team_id",
        "owner_email",
        "subscriber_kind",
        "callback_url",
        "subscriber_target_ref",
        "target",
        "source",
        "event_types",
        "filter_expr",
        "mode",
        "correlation_key",
        "correlation_value",
        "delivery",
        "active",
        "expires_at",
        "created_at",
    }
    assert expected <= cols


def test_event_log_columns():
    """event_log has the expected columns."""
    cols = set(EventLog.__table__.columns.keys())
    expected = {
        "id",
        "evt_id",
        "evt_source",
        "evt_type",
        "evt_subject",
        "evt_time",
        "gateway_id",
        "provider_id",
        "data",
        "raw_headers",
        "received_at",
    }
    assert expected <= cols


def test_delivery_attempts_columns():
    """delivery_attempts has the expected columns."""
    cols = set(DeliveryAttempt.__table__.columns.keys())
    expected = {
        "id",
        "event_id",
        "subscription_id",
        "attempt_no",
        "status",
        "http_status",
        "idempotency_key",
        "error",
        "next_retry_at",
        "created_at",
    }
    assert expected <= cols


def test_dead_letters_columns():
    """dead_letters has the expected columns."""
    cols = set(DeadLetter.__table__.columns.keys())
    expected = {
        "id",
        "event_id",
        "subscription_id",
        "attempts",
        "last_error",
        "payload_snapshot",
        "created_at",
    }
    assert expected <= cols


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------
def test_persistence_round_trip(session):
    """Create a full chain of rows referencing valid FKs and read them back."""
    gw = _make_gateway()
    session.add(gw)
    session.commit()

    event = EventLog(
        evt_id="evt-1",
        evt_source="//example.com/source",
        evt_type="com.example.thing.created",
        gateway_id=gw.id,
    )
    sub = EventSubscription(
        gateway_id=gw.id,
        subscriber_kind="http_callback",
        callback_url="http://example.com/hook",
        event_types=["com.example.*"],
    )
    session.add_all([event, sub])
    session.commit()

    attempt = DeliveryAttempt(
        event_id=event.id,
        subscription_id=sub.id,
        attempt_no=1,
        status="pending",
        idempotency_key="idem-1",
    )
    dead = DeadLetter(
        event_id=event.id,
        subscription_id=sub.id,
        attempts=5,
        last_error="boom",
    )
    session.add_all([attempt, dead])
    session.commit()

    # Read back and assert defaults.
    fetched_sub = session.get(EventSubscription, sub.id)
    assert fetched_sub.active is True
    assert fetched_sub.mode == "fanout"
    assert fetched_sub.event_types == ["com.example.*"]
    assert fetched_sub.created_at is not None

    fetched_event = session.get(EventLog, event.id)
    assert fetched_event.received_at is not None
    assert fetched_event.gateway_id == gw.id

    fetched_attempt = session.get(DeliveryAttempt, attempt.id)
    assert fetched_attempt.event_id == event.id
    assert fetched_attempt.subscription_id == sub.id

    fetched_dead = session.get(DeadLetter, dead.id)
    assert fetched_dead.event_id == event.id
    assert fetched_dead.attempts == 5


# ---------------------------------------------------------------------------
# Unique constraint
# ---------------------------------------------------------------------------
def test_event_log_unique_source_id(session):
    """Two EventLog rows with the same (evt_source, evt_id) raise IntegrityError."""
    e1 = EventLog(evt_id="dup", evt_source="//same/source", evt_type="t")
    session.add(e1)
    session.commit()

    e2 = EventLog(evt_id="dup", evt_source="//same/source", evt_type="t")
    session.add(e2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# Named indexes
# ---------------------------------------------------------------------------
def test_expected_named_indexes_present():
    """The expected named indexes exist on the new tables."""
    sub_indexes = {ix.name for ix in EventSubscription.__table__.indexes}
    assert {
        "ix_event_subs_tenant_source_active",
        "ix_event_subscriptions_gw_mode",
        "ix_event_subscriptions_corr_value",
    } <= sub_indexes

    log_indexes = {ix.name for ix in EventLog.__table__.indexes}
    assert "ix_event_log_gw_type_time" in log_indexes

    attempt_indexes = {ix.name for ix in DeliveryAttempt.__table__.indexes}
    assert {
        "ix_delivery_attempts_event_sub",
        "ix_delivery_attempts_retry",
    } <= attempt_indexes


def test_event_log_unique_constraint_named(session):
    """The named unique constraint on event_log is reflected by the inspector."""
    insp = inspect(session.get_bind())
    uniques = {uc["name"] for uc in insp.get_unique_constraints("event_log")}
    # On SQLite a UniqueConstraint may surface as a unique index instead.
    indexes = {ix["name"] for ix in insp.get_indexes("event_log")}
    assert "uq_event_log_source_id" in (uniques | indexes)
