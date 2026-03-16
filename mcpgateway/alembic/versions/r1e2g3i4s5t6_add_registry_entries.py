# -*- coding: utf-8 -*-
"""add_registry_entries

Revision ID: r1e2g3i4s5t6
Revises: t1r2a3n4s5p6
Create Date: 2026-03-16 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "r1e2g3i4s5t6"
down_revision: Union[str, Sequence[str], None] = "t1r2a3n4s5p6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create registry_entries table and add registry_entry_id to servers."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Create registry_entries table
    if not inspector.has_table("registry_entries"):
        op.create_table(
            "registry_entries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("category", sa.String(100), nullable=False, server_default="Virtual Server"),
            sa.Column("tags", sa.JSON, nullable=False, server_default="[]"),
            sa.Column("icon", sa.String(767), nullable=True),
            sa.Column("tool_definitions", sa.JSON, nullable=False),
            sa.Column("tool_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("server_transport", sa.String(20), nullable=False, server_default="sse"),
            sa.Column("published_by", sa.String(255), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_server_id", sa.String(36), nullable=True),
            sa.Column("source_type", sa.String(50), nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("team_id", sa.String(36), nullable=True),
            sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        print("Created registry_entries table.")
    else:
        print("registry_entries table already exists.")

    # Add registry_entry_id to servers table
    if inspector.has_table("servers"):
        columns = [col["name"] for col in inspector.get_columns("servers")]
        if "registry_entry_id" not in columns:
            op.add_column("servers", sa.Column("registry_entry_id", sa.String(36), nullable=True))
            print("Added registry_entry_id column to servers table.")
        else:
            print("registry_entry_id column already exists in servers table.")


def downgrade() -> None:
    """Remove registry_entries table and registry_entry_id from servers."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("servers"):
        columns = [col["name"] for col in inspector.get_columns("servers")]
        if "registry_entry_id" in columns:
            op.drop_column("servers", "registry_entry_id")
            print("Removed registry_entry_id column from servers table.")

    if inspector.has_table("registry_entries"):
        op.drop_table("registry_entries")
        print("Dropped registry_entries table.")
