# -*- coding: utf-8 -*-
"""add_events_to_gateways

Revision ID: e1v2t3a4col5
Revises: sec1scan2gate3
Create Date: 2026-05-30 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e1v2t3a4col5"
down_revision: Union[str, Sequence[str], None] = "sec1scan2gate3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EVENT_COLUMNS = (
    ("webhook_signing_secret", sa.Text(), True, None),
    ("webhook_secret_algo", sa.String(32), True, None),
    ("hook_state", sa.JSON(), True, None),
    ("events_enabled", sa.Boolean(), False, sa.false()),
)


def upgrade() -> None:
    """Add the events feature columns to the gateways table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gateways"):
        print("gateways table does not exist; skipping events column adds.")
        return

    existing = {col["name"] for col in inspector.get_columns("gateways")}
    for name, col_type, nullable, server_default in _EVENT_COLUMNS:
        if name not in existing:
            op.add_column("gateways", sa.Column(name, col_type, nullable=nullable, server_default=server_default))
            print(f"Added {name} column to gateways table.")
        else:
            print(f"{name} column already exists in gateways table.")


def downgrade() -> None:
    """Remove the events feature columns from the gateways table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("gateways"):
        existing = {col["name"] for col in inspector.get_columns("gateways")}
        for name, _col_type, _nullable, _server_default in reversed(_EVENT_COLUMNS):
            if name in existing:
                op.drop_column("gateways", name)
                print(f"Removed {name} column from gateways table.")
