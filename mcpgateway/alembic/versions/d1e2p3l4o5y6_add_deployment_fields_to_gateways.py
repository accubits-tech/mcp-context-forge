# -*- coding: utf-8 -*-
"""add_deployment_fields_to_gateways

Revision ID: d1e2p3l4o5y6
Revises: j0b1s2t3a4t5
Create Date: 2026-04-22 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d1e2p3l4o5y6"
down_revision: Union[str, Sequence[str], None] = "j0b1s2t3a4t5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("deployment_source", sa.String(16)),
    ("deployment_source_ref", sa.String(1024)),
    ("deployment_source_sha256", sa.String(64)),
    ("deployment_runtime", sa.String(16)),
    ("deployment_entry_mode", sa.String(16)),
    ("deployment_entry_command", sa.String(1024)),
    ("deployment_image_tag", sa.String(255)),
    ("deployment_container_id", sa.String(128)),
    ("deployment_host_port", sa.Integer),
    ("deployment_build_status", sa.String(16)),
    ("deployment_build_log_ref", sa.String(512)),
    ("deployment_resource_limits", sa.JSON),
    ("deployment_egress_allowlist", sa.JSON),
    ("deployment_env_encrypted", sa.Text),
    ("deployment_last_built_at", sa.DateTime(timezone=True)),
    ("deployment_last_started_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    """Add deployment fields to gateways table for user-supplied Python/Node MCP servers."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gateways"):
        print("gateways table does not exist, skipping.")
        return

    existing = {col["name"] for col in inspector.get_columns("gateways")}

    for name, col_type in _COLUMNS:
        if name not in existing:
            op.add_column("gateways", sa.Column(name, col_type, nullable=True))
            print(f"Added {name} column to gateways table.")


def downgrade() -> None:
    """Remove deployment fields from gateways table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gateways"):
        return

    existing = {col["name"] for col in inspector.get_columns("gateways")}

    for name, _col_type in _COLUMNS:
        if name in existing:
            op.drop_column("gateways", name)
            print(f"Removed {name} column from gateways table.")
