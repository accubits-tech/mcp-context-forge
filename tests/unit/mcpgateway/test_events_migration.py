# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_migration.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the events Alembic migrations.

Tests verify:
- The two migration modules import and declare the correct revision chain
  (``e1v2t3a4col5`` on top of ``sec1scan2gate3``, then ``e6v7t8b9tbl0``).
- A real Alembic ``command.upgrade``/``command.downgrade`` round-trip against a
  fresh temporary SQLite database creates and drops the four event tables and
  the four new ``gateways`` columns.

The repo's earliest migrations are designed to run against a schema that was
bootstrapped via ``Base.metadata.create_all()`` and then stamped (the base
revision short-circuits on a fresh database), so the round-trip test seeds the
parent tables from ``Base.metadata``, removes the event objects, stamps the
prior head, and then exercises the two new migrations through the real Alembic
``command`` entry points.
"""

# Standard
import importlib

# Third-Party
from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

ALEMBIC_DIR = "/home/ubuntu/mcp-context-forge/.claude/worktrees/exciting-kirch-2f8916/mcpgateway/alembic"

MIGRATION_A = "mcpgateway.alembic.versions.e1v2t3a4col5_add_events_to_gateways"
MIGRATION_B = "mcpgateway.alembic.versions.e6v7t8b9tbl0_add_events_tables"

NEW_GATEWAY_COLUMNS = ("webhook_signing_secret", "webhook_secret_algo", "hook_state", "events_enabled")
EVENT_TABLES = ("event_subscriptions", "event_log", "delivery_attempts", "dead_letters")


def test_migration_a_revision_chain():
    """Migration A sits directly on top of the prior head."""
    mod = importlib.import_module(MIGRATION_A)
    assert mod.revision == "e1v2t3a4col5"
    assert mod.down_revision == "sec1scan2gate3"


def test_migration_b_revision_chain():
    """Migration B sits directly on top of migration A."""
    mod = importlib.import_module(MIGRATION_B)
    assert mod.revision == "e6v7t8b9tbl0"
    assert mod.down_revision == "e1v2t3a4col5"


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

    This reproduces a database that predates the events feature while keeping the
    parent tables (``gateways``, ``email_teams``, ...) that the new migrations
    reference as foreign-key targets.

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


def test_events_migration_round_trip(tmp_path, monkeypatch):
    """Real upgrade/downgrade round-trip on a fresh temp SQLite database."""
    db_path = tmp_path / "events_migration.db"
    url = f"sqlite:///{db_path}"

    # The Alembic env reads ``settings.database_url`` at run time; point it here.
    # First-Party
    from mcpgateway.config import settings

    monkeypatch.setattr(settings, "database_url", url)

    engine = sa.create_engine(url)
    _seed_pre_events_schema(engine)

    inspector = sa.inspect(engine)
    assert not any(inspector.has_table(t) for t in EVENT_TABLES), "event tables should be absent before upgrade"
    gateway_cols = {col["name"] for col in inspector.get_columns("gateways")}
    assert not (set(NEW_GATEWAY_COLUMNS) & gateway_cols), "new gateways columns should be absent before upgrade"

    cfg = _make_config(url)
    command.stamp(cfg, "sec1scan2gate3")

    # Upgrade through both new migrations.
    command.upgrade(cfg, "head")

    inspector = sa.inspect(engine)
    for table in EVENT_TABLES:
        assert inspector.has_table(table), f"{table} should exist after upgrade"
    gateway_cols = {col["name"] for col in inspector.get_columns("gateways")}
    for column in NEW_GATEWAY_COLUMNS:
        assert column in gateway_cols, f"{column} should exist on gateways after upgrade"

    # Verify the dedup unique constraint landed on event_log.
    unique_constraints = {uc["name"] for uc in inspector.get_unique_constraints("event_log")}
    assert "uq_event_log_source_id" in unique_constraints

    # Downgrade to migration A: event tables disappear, columns remain.
    command.downgrade(cfg, "e1v2t3a4col5")
    inspector = sa.inspect(engine)
    for table in EVENT_TABLES:
        assert not inspector.has_table(table), f"{table} should be dropped after downgrade to migration A"
    gateway_cols = {col["name"] for col in inspector.get_columns("gateways")}
    for column in NEW_GATEWAY_COLUMNS:
        assert column in gateway_cols, f"{column} should still exist after downgrade to migration A"

    # Downgrade to the prior head: the four gateways columns disappear too.
    command.downgrade(cfg, "sec1scan2gate3")
    inspector = sa.inspect(engine)
    gateway_cols = {col["name"] for col in inspector.get_columns("gateways")}
    for column in NEW_GATEWAY_COLUMNS:
        assert column not in gateway_cols, f"{column} should be dropped after downgrade to prior head"

    engine.dispose()
