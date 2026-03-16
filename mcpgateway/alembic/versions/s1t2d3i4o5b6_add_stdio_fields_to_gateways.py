# -*- coding: utf-8 -*-
"""add_stdio_fields_to_gateways

Revision ID: s1t2d3i4o5b6
Revises: r1e2g3i4s5t6
Create Date: 2026-03-16 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "s1t2d3i4o5b6"
down_revision: Union[str, Sequence[str], None] = "r1e2g3i4s5t6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add stdio fields to gateways table and make url nullable."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gateways"):
        print("gateways table does not exist, skipping.")
        return

    columns = [col["name"] for col in inspector.get_columns("gateways")]

    # Add stdio columns
    if "stdio_command" not in columns:
        op.add_column("gateways", sa.Column("stdio_command", sa.String(500), nullable=True))
        print("Added stdio_command column to gateways table.")

    if "stdio_args" not in columns:
        op.add_column("gateways", sa.Column("stdio_args", sa.JSON, nullable=True))
        print("Added stdio_args column to gateways table.")

    if "stdio_env" not in columns:
        op.add_column("gateways", sa.Column("stdio_env", sa.Text, nullable=True))
        print("Added stdio_env column to gateways table.")

    if "stdio_cwd" not in columns:
        op.add_column("gateways", sa.Column("stdio_cwd", sa.String(1000), nullable=True))
        print("Added stdio_cwd column to gateways table.")

    if "stdio_timeout" not in columns:
        op.add_column("gateways", sa.Column("stdio_timeout", sa.Integer, nullable=True, server_default="60"))
        print("Added stdio_timeout column to gateways table.")

    if "stdio_bridge_port" not in columns:
        op.add_column("gateways", sa.Column("stdio_bridge_port", sa.Integer, nullable=True))
        print("Added stdio_bridge_port column to gateways table.")

    # Make url nullable for stdio gateways (which auto-generate bridge URLs)
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # SQLite doesn't support ALTER COLUMN; url is already effectively nullable via batch
        print("SQLite: url column nullable change skipped (SQLite handles NULLs flexibly).")
    else:
        op.alter_column("gateways", "url", existing_type=sa.String(767), nullable=True)
        print("Made url column nullable in gateways table.")


def downgrade() -> None:
    """Remove stdio fields from gateways table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gateways"):
        return

    columns = [col["name"] for col in inspector.get_columns("gateways")]

    for col_name in ["stdio_command", "stdio_args", "stdio_env", "stdio_cwd", "stdio_timeout", "stdio_bridge_port"]:
        if col_name in columns:
            op.drop_column("gateways", col_name)
            print(f"Removed {col_name} column from gateways table.")

    # Restore url to non-nullable
    dialect = bind.dialect.name
    if dialect != "sqlite":
        op.alter_column("gateways", "url", existing_type=sa.String(767), nullable=False)
        print("Restored url column to non-nullable in gateways table.")
