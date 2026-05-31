# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_correlate_constraint.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the correlation-value uniqueness backstop (M7, migration D).

Tests verify:
- The new migration sits directly on top of the prior head
  (``f7w8u9c0dca1``) and is itself the single unique head of the chain.
- On a temporary database built from ``Base.metadata``, two *active*
  correlate subscriptions sharing the same non-null ``(team_id,
  correlation_value)`` violate the ``uq_event_sub_active_corr_value`` partial
  unique index, while many fanout rows with NULL ``correlation_value`` insert
  cleanly (the partial ``WHERE correlation_value IS NOT NULL`` clause exempts
  them).
- A real Alembic ``command.upgrade``/``command.downgrade`` round-trip adds and
  drops the partial unique index against a fresh temporary SQLite database,
  using the repo's seed-then-stamp migration approach.
"""

# Standard
import importlib

# Third-Party
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

ALEMBIC_DIR = "/home/ubuntu/mcp-context-forge/.claude/worktrees/exciting-kirch-2f8916/mcpgateway/alembic"

PRIOR_HEAD = "f7w8u9c0dca1"
NEW_REVISION = "c0r3l8d0idx1"
MIGRATION_D = "mcpgateway.alembic.versions.c0r3l8d0idx1_add_correlation_value_unique"

INDEX_NAME = "uq_event_sub_active_corr_value"

# Tables seeded from the ORM but removed before the round-trip so the events
# migrations recreate them from scratch.
EVENT_TABLES = ("event_subscriptions", "event_log", "delivery_attempts", "dead_letters")
NEW_GATEWAY_COLUMNS = ("webhook_signing_secret", "webhook_secret_algo", "hook_state", "events_enabled")


def test_migration_d_revision_chain():
    """The M7 migration sits directly on top of the prior head."""
    mod = importlib.import_module(MIGRATION_D)
    assert mod.revision == NEW_REVISION
    assert mod.down_revision == PRIOR_HEAD


def test_migration_d_is_unique_head():
    """The chain has exactly one head and it is the new M7 revision."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert list(heads) == [NEW_REVISION]


def test_partial_unique_index_in_metadata():
    """The ORM model declares the partial unique index on (team_id, correlation_value)."""
    # First-Party
    from mcpgateway.db import EventSubscription

    index = next((ix for ix in EventSubscription.__table__.indexes if ix.name == INDEX_NAME), None)
    assert index is not None, f"{INDEX_NAME} not declared in EventSubscription.__table__.indexes"
    assert index.unique is True
    assert [col.name for col in index.columns] == ["team_id", "correlation_value"]


def _make_minimal_sub(**overrides):
    """Build an ``EventSubscription`` kwargs dict with required NOT NULL fields.

    Args:
        **overrides: Field overrides merged on top of the defaults.

    Returns:
        dict: Keyword arguments for constructing an ``EventSubscription``.
    """
    base = {
        "subscriber_kind": "http_callback",
        "event_types": ["com.example.task.completed"],
        "mode": "correlate",
        "active": True,
    }
    base.update(overrides)
    return base


def test_duplicate_correlate_value_triggers_integrity_error(tmp_path):
    """Two active subs with identical non-null (team_id, correlation_value) collide."""
    # First-Party
    from mcpgateway.db import EventSubscription

    url = f"sqlite:///{tmp_path / 'corr.db'}"
    engine = sa.create_engine(url)
    EventSubscription.__table__.create(engine)

    Session = sa.orm.sessionmaker(bind=engine)
    session = Session()
    try:
        session.add(EventSubscription(id="s1", **_make_minimal_sub(team_id="team-1", correlation_value="task-abc")))
        session.add(EventSubscription(id="s2", **_make_minimal_sub(team_id="team-1", correlation_value="task-abc")))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_null_correlation_value_rows_unaffected(tmp_path):
    """Many fanout subs with NULL correlation_value insert cleanly (partial index exempts them)."""
    # First-Party
    from mcpgateway.db import EventSubscription

    url = f"sqlite:///{tmp_path / 'corr_null.db'}"
    engine = sa.create_engine(url)
    EventSubscription.__table__.create(engine)

    Session = sa.orm.sessionmaker(bind=engine)
    session = Session()
    try:
        # Several fanout rows with the same team and NULL correlation_value.
        session.add(EventSubscription(id="f1", **_make_minimal_sub(team_id="team-1", mode="fanout", correlation_value=None)))
        session.add(EventSubscription(id="f2", **_make_minimal_sub(team_id="team-1", mode="fanout", correlation_value=None)))
        session.add(EventSubscription(id="f3", **_make_minimal_sub(team_id="team-1", mode="fanout", correlation_value=None)))
        session.commit()
        assert session.query(EventSubscription).count() == 3
    finally:
        session.close()
        engine.dispose()


def test_different_team_same_value_inserts_cleanly(tmp_path):
    """Same correlation_value across different teams is allowed (team_id is part of the key)."""
    # First-Party
    from mcpgateway.db import EventSubscription

    url = f"sqlite:///{tmp_path / 'corr_team.db'}"
    engine = sa.create_engine(url)
    EventSubscription.__table__.create(engine)

    Session = sa.orm.sessionmaker(bind=engine)
    session = Session()
    try:
        session.add(EventSubscription(id="s1", **_make_minimal_sub(team_id="team-1", correlation_value="task-abc")))
        session.add(EventSubscription(id="s2", **_make_minimal_sub(team_id="team-2", correlation_value="task-abc")))
        session.commit()
        assert session.query(EventSubscription).count() == 2
    finally:
        session.close()
        engine.dispose()


def _make_config(url: str) -> Config:
    """Build an Alembic ``Config`` pointed at the temp database.

    Args:
        url: SQLAlchemy URL for the temporary SQLite database.

    Returns:
        Config: An Alembic configuration ready for ``command`` calls.
    """
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _seed_pre_events_schema(engine: sa.Engine) -> None:
    """Create the full ORM schema, then remove the event objects.

    Reproduces a database that predates the events feature while keeping the
    parent tables the events migrations reference as foreign-key targets.

    Args:
        engine: Engine bound to the temporary SQLite database.
    """
    # First-Party
    from mcpgateway.db import Base

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        for table in EVENT_TABLES:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        for column in NEW_GATEWAY_COLUMNS:
            conn.exec_driver_sql(f"ALTER TABLE gateways DROP COLUMN {column}")


def _event_subscriptions_index_ddl(engine: sa.Engine) -> str:
    """Return the live DDL for the ``uq_event_sub_active_corr_value`` index.

    SQLite reflection (``get_indexes``) does not reliably surface a *partial*
    unique index on an engine whose connection previously dropped and recreated
    the table during the migration round-trip, so the round-trip test inspects
    the stored DDL directly and additionally checks that the index is actually
    enforced.

    Args:
        engine: Engine bound to the temporary SQLite database.

    Returns:
        str: The ``sqlite_master`` DDL string for the partial unique index (empty if absent).
    """
    with engine.connect() as conn:
        row = conn.exec_driver_sql(f"SELECT sql FROM sqlite_master WHERE type='index' AND name='{INDEX_NAME}'").fetchone()
    return row[0] if row else ""


def _insert_correlate_sub(engine: sa.Engine, row_id: str, *, team_id: str, correlation_value) -> None:
    """Insert a correlate ``event_subscriptions`` row.

    Args:
        engine: Engine bound to the temporary SQLite database.
        row_id: Distinct primary key for the row.
        team_id: Tenant id stored in ``team_id``.
        correlation_value: Value stored in ``correlation_value`` (may be ``None``).
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO event_subscriptions "
            "(id, team_id, subscriber_kind, event_types, mode, correlation_value, active, created_at) "
            "VALUES (:id, :team_id, 'http_callback', '[]', 'correlate', :cv, 1, '2026-01-01')",
            {"id": row_id, "team_id": team_id, "cv": correlation_value},
        )


def test_constraint_migration_round_trip(tmp_path, monkeypatch):
    """Real upgrade/downgrade round-trip adds and drops the partial unique index."""
    db_path = tmp_path / "correlate_constraint_migration.db"
    url = f"sqlite:///{db_path}"

    # First-Party
    from mcpgateway.config import settings

    monkeypatch.setattr(settings, "database_url", url)

    engine = sa.create_engine(url)
    _seed_pre_events_schema(engine)

    cfg = _make_config(url)
    command.stamp(cfg, "sec1scan2gate3")

    # Upgrade through every migration including the new M7 head.
    command.upgrade(cfg, "head")

    # The partial unique index lands in the table DDL (with the WHERE clause)
    # and is enforced at the engine level.
    ddl = _event_subscriptions_index_ddl(engine)
    assert INDEX_NAME in ddl
    assert "correlation_value IS NOT NULL" in ddl

    _insert_correlate_sub(engine, "s1", team_id="team-1", correlation_value="task-abc")
    with pytest.raises(IntegrityError):
        _insert_correlate_sub(engine, "s2", team_id="team-1", correlation_value="task-abc")

    # NULL correlation_value rows remain exempt from the partial unique index.
    _insert_correlate_sub(engine, "n1", team_id="team-1", correlation_value=None)
    _insert_correlate_sub(engine, "n2", team_id="team-1", correlation_value=None)

    # Downgrade just the M7 migration: index disappears, table remains.
    command.downgrade(cfg, PRIOR_HEAD)
    assert sa.inspect(engine).has_table("event_subscriptions")
    assert INDEX_NAME not in _event_subscriptions_index_ddl(engine)

    engine.dispose()
