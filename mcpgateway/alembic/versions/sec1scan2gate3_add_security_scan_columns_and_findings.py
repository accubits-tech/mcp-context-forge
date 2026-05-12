# -*- coding: utf-8 -*-
"""add_security_scan_columns_and_findings

Revision ID: sec1scan2gate3
Revises: sk1ll2hub3v1
Create Date: 2026-05-05 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "sec1scan2gate3"
down_revision: Union[str, Sequence[str], None] = "sk1ll2hub3v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCAN_COLUMNS = (
    ("deployment_security_scan_status", sa.String(16)),
    ("deployment_security_scan_run_id", sa.String(36)),
    ("deployment_security_scan_report_ref", sa.String(512)),
    ("deployment_security_scan_summary", sa.JSON),
    ("deployment_security_scan_started_at", sa.DateTime(timezone=True)),
    ("deployment_security_scan_completed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    """Add security-scan columns to gateways and create the findings table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("gateways"):
        existing = {col["name"] for col in inspector.get_columns("gateways")}
        for name, col_type in _SCAN_COLUMNS:
            if name not in existing:
                op.add_column("gateways", sa.Column(name, col_type, nullable=True))
                print(f"Added {name} column to gateways table.")
    else:
        print("gateways table does not exist; skipping column adds.")

    if not inspector.has_table("gateway_security_scan_findings"):
        op.create_table(
            "gateway_security_scan_findings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("gateway_id", sa.String(36), sa.ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False),
            sa.Column("scan_run_id", sa.String(36), nullable=False),
            sa.Column("scanner", sa.String(32), nullable=False),
            sa.Column("stage", sa.String(32), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("rule_id", sa.String(128), nullable=False),
            sa.Column("file", sa.String(512), nullable=True),
            sa.Column("line", sa.Integer, nullable=True),
            sa.Column("message", sa.Text, nullable=False),
            sa.Column("cwe", sa.String(32), nullable=True),
            sa.Column("raw_excerpt", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_security_findings_gw_sev", "gateway_security_scan_findings", ["gateway_id", "severity"])
        op.create_index("ix_security_findings_gw_run", "gateway_security_scan_findings", ["gateway_id", "scan_run_id"])
        op.create_index("ix_gateway_security_scan_findings_gateway_id", "gateway_security_scan_findings", ["gateway_id"])
        op.create_index("ix_gateway_security_scan_findings_scan_run_id", "gateway_security_scan_findings", ["scan_run_id"])
        print("Created gateway_security_scan_findings table.")
    else:
        print("gateway_security_scan_findings already exists; skipping create.")


def downgrade() -> None:
    """Drop the findings table and security-scan columns."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("gateway_security_scan_findings"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("gateway_security_scan_findings")}
        for idx_name in (
            "ix_gateway_security_scan_findings_scan_run_id",
            "ix_gateway_security_scan_findings_gateway_id",
            "ix_security_findings_gw_run",
            "ix_security_findings_gw_sev",
        ):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="gateway_security_scan_findings")
        op.drop_table("gateway_security_scan_findings")
        print("Dropped gateway_security_scan_findings table.")

    if inspector.has_table("gateways"):
        existing = {col["name"] for col in inspector.get_columns("gateways")}
        for name, _col_type in _SCAN_COLUMNS:
            if name in existing:
                op.drop_column("gateways", name)
                print(f"Removed {name} column from gateways table.")
