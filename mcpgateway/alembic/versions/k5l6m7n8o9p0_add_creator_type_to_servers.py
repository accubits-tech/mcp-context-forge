# -*- coding: utf-8 -*-
"""add creator_type to servers

Revision ID: k5l6m7n8o9p0
Revises: 9a79c73f2b78
Create Date: 2026-01-14 13:00:00.000000

This migration adds the creator_type column to the servers table
to track whether a server was created manually, via API, or via federation.
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "k5l6m7n8o9p0"
down_revision: Union[str, Sequence[str], None] = "9a79c73f2b78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add creator_type column to servers table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Check if column already exists (idempotent migration)
    columns = [col["name"] for col in inspector.get_columns("servers")]
    if "creator_type" not in columns:
        # Use batch mode for SQLite compatibility
        with op.batch_alter_table("servers", schema=None) as batch_op:
            batch_op.add_column(sa.Column("creator_type", sa.String(length=50), nullable=True))
        print("Added creator_type column to servers table")
    else:
        print("creator_type column already exists in servers table")


def downgrade() -> None:
    """Remove creator_type column from servers table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Check if column exists before dropping
    columns = [col["name"] for col in inspector.get_columns("servers")]
    if "creator_type" in columns:
        with op.batch_alter_table("servers", schema=None) as batch_op:
            batch_op.drop_column("creator_type")
        print("Removed creator_type column from servers table")
    else:
        print("creator_type column does not exist in servers table")
