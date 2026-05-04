# -*- coding: utf-8 -*-
"""add_skills_tables

Revision ID: sk1ll2hub3v1
Revises: d1e2p3l4o5y6
Create Date: 2026-04-24 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "sk1ll2hub3v1"
down_revision: Union[str, Sequence[str], None] = "d1e2p3l4o5y6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create skills and server_skill_association tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("skills"):
        op.create_table(
            "skills",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("skill_path", sa.String(255), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.Text, nullable=False),
            sa.Column("content_md", sa.Text, nullable=False, server_default=""),
            sa.Column("license", sa.String(255), nullable=True),
            sa.Column("compatibility", sa.String(500), nullable=True),
            sa.Column("metadata_json", sa.JSON, nullable=False, server_default="{}"),
            sa.Column("allowed_tools", sa.Text, nullable=True),
            sa.Column("allowed_gateway_ids", sa.JSON, nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("tags", sa.JSON, nullable=False, server_default="[]"),
            sa.Column("created_by", sa.String(255), nullable=True),
            sa.Column("created_from_ip", sa.String(45), nullable=True),
            sa.Column("created_via", sa.String(100), nullable=True),
            sa.Column("created_user_agent", sa.Text, nullable=True),
            sa.Column("modified_by", sa.String(255), nullable=True),
            sa.Column("modified_from_ip", sa.String(45), nullable=True),
            sa.Column("modified_via", sa.String(100), nullable=True),
            sa.Column("modified_user_agent", sa.Text, nullable=True),
            sa.Column("import_batch_id", sa.String(36), nullable=True),
            sa.Column("federation_source", sa.String(255), nullable=True),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("gateway_id", sa.String(36), sa.ForeignKey("gateways.id"), nullable=True),
            sa.Column("team_id", sa.String(36), sa.ForeignKey("email_teams.id", ondelete="SET NULL"), nullable=True),
            sa.Column("owner_email", sa.String(255), nullable=True),
            sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
            sa.UniqueConstraint("team_id", "owner_email", "skill_path", name="uq_team_owner_path_skill"),
        )
        op.create_index("ix_skills_name", "skills", ["name"])
        op.create_index("ix_skills_team_owner", "skills", ["team_id", "owner_email"])
        print("Created skills table.")
    else:
        print("skills table already exists.")

    if not inspector.has_table("server_skill_association"):
        op.create_table(
            "server_skill_association",
            sa.Column("server_id", sa.String(36), sa.ForeignKey("servers.id"), primary_key=True),
            sa.Column("skill_id", sa.Integer, sa.ForeignKey("skills.id"), primary_key=True),
        )
        print("Created server_skill_association table.")
    else:
        print("server_skill_association table already exists.")


def downgrade() -> None:
    """Drop skills and server_skill_association tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("server_skill_association"):
        op.drop_table("server_skill_association")
        print("Dropped server_skill_association table.")

    if inspector.has_table("skills"):
        # Drop indexes first to avoid PostgreSQL dependency errors
        indexes = {idx["name"] for idx in inspector.get_indexes("skills")}
        if "ix_skills_team_owner" in indexes:
            op.drop_index("ix_skills_team_owner", table_name="skills")
        if "ix_skills_name" in indexes:
            op.drop_index("ix_skills_name", table_name="skills")
        op.drop_table("skills")
        print("Dropped skills table.")
