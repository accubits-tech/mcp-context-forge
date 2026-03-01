# -*- coding: utf-8 -*-
"""add_transport_to_servers

Revision ID: t1r2a3n4s5p6
Revises: k5l6m7n8o9p0
Create Date: 2026-03-02 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "t1r2a3n4s5p6"
down_revision: Union[str, Sequence[str], None] = "k5l6m7n8o9p0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add transport column to servers table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("servers"):
        print("Servers table not found. Skipping transport migration.")
        return

    columns = [col["name"] for col in inspector.get_columns("servers")]
    if "transport" not in columns:
        op.add_column("servers", sa.Column("transport", sa.String(20), nullable=False, server_default="sse"))
        print("Added transport column to servers table.")
    else:
        print("transport column already exists in servers table.")


def downgrade() -> None:
    """Remove transport column from servers table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("servers"):
        columns = [col["name"] for col in inspector.get_columns("servers")]
        if "transport" in columns:
            op.drop_column("servers", "transport")
            print("Removed transport column from servers table.")
