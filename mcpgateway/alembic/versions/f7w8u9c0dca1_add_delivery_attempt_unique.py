# -*- coding: utf-8 -*-
"""add_delivery_attempt_unique

Add a composite unique constraint on ``delivery_attempts`` over
``(event_id, subscription_id, attempt_no)``. This backstops the
exactly-one-row-per-attempt invariant when concurrent delivery workers (or a
stale-claim reprocess) race to persist the same attempt.

Revision ID: f7w8u9c0dca1
Revises: e6v7t8b9tbl0
Create Date: 2026-05-30 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f7w8u9c0dca1"
down_revision: Union[str, Sequence[str], None] = "e6v7t8b9tbl0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "uq_delivery_attempt_event_sub_no"


def upgrade() -> None:
    """Add the composite unique constraint to ``delivery_attempts``."""
    inspector = sa.inspect(op.get_bind())
    if "delivery_attempts" not in inspector.get_table_names():
        print("delivery_attempts table not found. Skipping migration.")
        return

    existing = {uc["name"] for uc in inspector.get_unique_constraints("delivery_attempts")}
    if CONSTRAINT_NAME in existing:
        print(f"Constraint {CONSTRAINT_NAME} already exists; skipping create.")
        return

    with op.batch_alter_table("delivery_attempts", schema=None) as batch_op:
        batch_op.create_unique_constraint(CONSTRAINT_NAME, ["event_id", "subscription_id", "attempt_no"])
    print(f"Created {CONSTRAINT_NAME} on delivery_attempts.")


def downgrade() -> None:
    """Drop the composite unique constraint from ``delivery_attempts``."""
    inspector = sa.inspect(op.get_bind())
    if "delivery_attempts" not in inspector.get_table_names():
        print("delivery_attempts table not found. Skipping migration.")
        return

    with op.batch_alter_table("delivery_attempts", schema=None) as batch_op:
        try:
            batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
        except Exception as e:  # pragma: no cover - defensive idempotent re-run
            print(f"Constraint {CONSTRAINT_NAME} not found or already dropped: {e}")
