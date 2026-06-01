# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_delivery_constraint.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the ``delivery_attempts`` uniqueness backstop (M2b).

Tests verify:
- The new migration sits directly on top of the prior events head
  (``e6v7t8b9tbl0``) and is itself the single unique head of the chain.
- On a temporary database built from ``Base.metadata``, two
  ``DeliveryAttempt`` rows sharing ``(event_id, subscription_id, attempt_no)``
  violate the ``uq_delivery_attempt_event_sub_no`` unique constraint while
  rows differing only in ``attempt_no`` insert cleanly.
- A real Alembic ``command.upgrade``/``command.downgrade`` round-trip adds and
  drops the unique constraint against a fresh temporary SQLite database, using
  the repo's seed-then-stamp migration approach.
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

PRIOR_HEAD = "e6v7t8b9tbl0"
NEW_REVISION = "f7w8u9c0dca1"
MIGRATION_C = "mcpgateway.alembic.versions.f7w8u9c0dca1_add_delivery_attempt_unique"

CONSTRAINT_NAME = "uq_delivery_attempt_event_sub_no"

# Tables seeded from the ORM but removed before the round-trip so the events
# migrations recreate them from scratch.
EVENT_TABLES = ("event_subscriptions", "event_log", "delivery_attempts", "dead_letters")
NEW_GATEWAY_COLUMNS = ("webhook_signing_secret", "webhook_secret_algo", "hook_state", "events_enabled")


def test_migration_c_revision_chain():
    """The M2b migration sits directly on top of the prior events head."""
    mod = importlib.import_module(MIGRATION_C)
    assert mod.revision == NEW_REVISION
    assert mod.down_revision == PRIOR_HEAD


def test_migration_c_is_not_branching():
    """The chain has exactly one head and the M2b revision is an ancestor of it.

    The M2b migration was the head when introduced; the M7 correlation-value
    migration now sits directly on top of it, so the single chain head moves
    forward to that descendant. This guards against accidental branching while
    confirming M2b remains in the linear chain.
    """
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"

    head = heads[0]
    ancestor_revisions = {rev.revision for rev in script.iterate_revisions(head, "base")}
    assert NEW_REVISION in ancestor_revisions


def test_delivery_attempt_unique_constraint_in_metadata():
    """The ORM model declares the composite unique constraint."""
    # First-Party
    from mcpgateway.db import DeliveryAttempt

    unique_names = {c.name for c in DeliveryAttempt.__table__.constraints if isinstance(c, sa.UniqueConstraint)}
    assert CONSTRAINT_NAME in unique_names

    constraint = next(c for c in DeliveryAttempt.__table__.constraints if getattr(c, "name", None) == CONSTRAINT_NAME)
    assert [col.name for col in constraint.columns] == ["event_id", "subscription_id", "attempt_no"]


def _make_minimal_attempt(**overrides):
    """Build a ``DeliveryAttempt`` kwargs dict with required NOT NULL fields.

    Args:
        **overrides: Field overrides merged on top of the defaults.

    Returns:
        dict: Keyword arguments for constructing a ``DeliveryAttempt``.
    """
    base = {
        "event_id": "evt-1",
        "subscription_id": "sub-1",
        "attempt_no": 1,
        "status": "pending",
        "idempotency_key": "idem-1",
    }
    base.update(overrides)
    return base


def test_duplicate_attempt_triggers_integrity_error(tmp_path):
    """Two rows with identical (event_id, subscription_id, attempt_no) collide."""
    # First-Party
    from mcpgateway.db import Base, DeliveryAttempt

    url = f"sqlite:///{tmp_path / 'constraint.db'}"
    engine = sa.create_engine(url)
    # Build just the delivery_attempts table (FKs are not enforced on SQLite by
    # default, so referenced parent rows are unnecessary for this assertion).
    DeliveryAttempt.__table__.create(engine)

    Session = sa.orm.sessionmaker(bind=engine)
    session = Session()
    try:
        session.add(DeliveryAttempt(id="a1", **_make_minimal_attempt()))
        session.add(DeliveryAttempt(id="a2", **_make_minimal_attempt()))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
        engine.dispose()
    # Silence unused import warning while keeping Base available for parity.
    assert Base is not None


def test_differing_attempt_no_inserts_cleanly(tmp_path):
    """Rows that differ only in ``attempt_no`` are accepted."""
    # First-Party
    from mcpgateway.db import DeliveryAttempt

    url = f"sqlite:///{tmp_path / 'constraint_ok.db'}"
    engine = sa.create_engine(url)
    DeliveryAttempt.__table__.create(engine)

    Session = sa.orm.sessionmaker(bind=engine)
    session = Session()
    try:
        session.add(DeliveryAttempt(id="a1", **_make_minimal_attempt(attempt_no=1)))
        session.add(DeliveryAttempt(id="a2", **_make_minimal_attempt(attempt_no=2)))
        session.commit()
        assert session.query(DeliveryAttempt).count() == 2
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


def _delivery_attempts_ddl(engine: sa.Engine) -> str:
    """Return the live ``CREATE TABLE`` DDL for ``delivery_attempts``.

    SQLite's reflection (``get_unique_constraints``) does not surface a *named*
    unique constraint when foreign keys are also present in the table, so the
    round-trip test inspects the stored DDL directly and additionally checks
    that the constraint is actually enforced.

    Args:
        engine: Engine bound to the temporary SQLite database.

    Returns:
        str: The ``sqlite_master`` DDL string for ``delivery_attempts``.
    """
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='delivery_attempts'").fetchone()
    return row[0] if row else ""


def _insert_attempt(engine: sa.Engine, row_id: str) -> None:
    """Insert a ``delivery_attempts`` row that collides on the unique key.

    Args:
        engine: Engine bound to the temporary SQLite database.
        row_id: Distinct primary key for the row.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO delivery_attempts " "(id, event_id, subscription_id, attempt_no, status, idempotency_key, created_at) " "VALUES (:id, 'e', 's', 1, 'pending', 'k', '2026-01-01')",
            {"id": row_id},
        )


def test_constraint_migration_round_trip(tmp_path, monkeypatch):
    """Real upgrade/downgrade round-trip adds and drops the unique constraint."""
    db_path = tmp_path / "delivery_constraint_migration.db"
    url = f"sqlite:///{db_path}"

    # First-Party
    from mcpgateway.config import settings

    monkeypatch.setattr(settings, "database_url", url)

    engine = sa.create_engine(url)
    _seed_pre_events_schema(engine)

    cfg = _make_config(url)
    command.stamp(cfg, "sec1scan2gate3")

    # Upgrade through every migration including the new M2b head.
    command.upgrade(cfg, "head")

    # The constraint lands in the table DDL and is enforced at the engine level.
    assert CONSTRAINT_NAME in _delivery_attempts_ddl(engine)
    _insert_attempt(engine, "a1")
    with pytest.raises(IntegrityError):
        _insert_attempt(engine, "a2")

    # Downgrade just the M2b migration: constraint disappears, table remains.
    command.downgrade(cfg, PRIOR_HEAD)
    inspector = sa.inspect(engine)
    assert inspector.has_table("delivery_attempts")
    assert CONSTRAINT_NAME not in _delivery_attempts_ddl(engine)

    engine.dispose()
