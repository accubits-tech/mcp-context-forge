# -*- coding: utf-8 -*-
"""add_tool_generation_jobs

Revision ID: j0b1s2t3a4t5
Revises: t1r2a3n4s5p6
Create Date: 2026-03-22 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "j0b1s2t3a4t5"
down_revision: Union[str, Sequence[str], None] = "s1t2d3i4o5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create tool_generation_jobs table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("tool_generation_jobs"):
        print("tool_generation_jobs table already exists. Skipping.")
        return

    op.create_table(
        "tool_generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("progress_message", sa.String(255), nullable=True),
        sa.Column("params", sa.JSON, nullable=True),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_tool_gen_jobs_status", "tool_generation_jobs", ["status"])
    op.create_index("ix_tool_gen_jobs_created_by", "tool_generation_jobs", ["created_by"])
    op.create_index("ix_tool_gen_jobs_created_at", "tool_generation_jobs", ["created_at"])
    print("Created tool_generation_jobs table with indexes.")


def downgrade() -> None:
    """Drop tool_generation_jobs table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("tool_generation_jobs"):
        op.drop_index("ix_tool_gen_jobs_created_at", table_name="tool_generation_jobs")
        op.drop_index("ix_tool_gen_jobs_created_by", table_name="tool_generation_jobs")
        op.drop_index("ix_tool_gen_jobs_status", table_name="tool_generation_jobs")
        op.drop_table("tool_generation_jobs")
        print("Dropped tool_generation_jobs table.")
