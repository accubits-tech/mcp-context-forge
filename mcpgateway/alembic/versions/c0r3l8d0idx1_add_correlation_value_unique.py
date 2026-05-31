# -*- coding: utf-8 -*-
"""add_correlation_value_unique

Add a PARTIAL unique index ``uq_event_sub_active_corr_value`` on
``event_subscriptions`` over ``(team_id, correlation_value)`` restricted to
rows where ``correlation_value IS NOT NULL``. This backstops the
one-live-correlate-waiter-per-(team_id, correlation_value) invariant so that a
race cannot bind two concurrent correlate subscriptions to the same upstream
task/correlation carrier. Fanout subscriptions (NULL ``correlation_value``) are
exempt via the partial ``WHERE`` clause.

Portable across SQLite (>=3.8.0) and PostgreSQL via the per-dialect
``sqlite_where`` / ``postgresql_where`` kwargs, which emit a partial UNIQUE
INDEX (a UNIQUE CONSTRAINT cannot be partial).

Revision ID: c0r3l8d0idx1
Revises: f7w8u9c0dca1
Create Date: 2026-05-31 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c0r3l8d0idx1"
down_revision: Union[str, Sequence[str], None] = "f7w8u9c0dca1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_event_sub_active_corr_value"


def upgrade() -> None:
    """Create the partial unique index on ``event_subscriptions``."""
    inspector = sa.inspect(op.get_bind())
    if "event_subscriptions" not in inspector.get_table_names():
        print("event_subscriptions table not found. Skipping migration.")
        return

    existing = {ix["name"] for ix in inspector.get_indexes("event_subscriptions")}
    if INDEX_NAME in existing:
        print(f"Index {INDEX_NAME} already exists; skipping create.")
        return

    op.create_index(
        INDEX_NAME,
        "event_subscriptions",
        ["team_id", "correlation_value"],
        unique=True,
        sqlite_where=sa.text("correlation_value IS NOT NULL"),
        postgresql_where=sa.text("correlation_value IS NOT NULL"),
    )
    print(f"Created partial unique index {INDEX_NAME} on event_subscriptions.")


def downgrade() -> None:
    """Drop the partial unique index from ``event_subscriptions``."""
    inspector = sa.inspect(op.get_bind())
    if "event_subscriptions" not in inspector.get_table_names():
        print("event_subscriptions table not found. Skipping migration.")
        return

    existing = {ix["name"] for ix in inspector.get_indexes("event_subscriptions")}
    if INDEX_NAME not in existing:
        print(f"Index {INDEX_NAME} not found; skipping drop.")
        return

    op.drop_index(INDEX_NAME, table_name="event_subscriptions")
    print(f"Dropped partial unique index {INDEX_NAME} from event_subscriptions.")
