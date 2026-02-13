# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/i3j4k5l6m7n8_modify_gateway_url_constraint.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

modify gateway URL constraint to include slug

Revision ID: i3j4k5l6m7n8
Revises: h2b3c4d5e6f7
Create Date: 2025-11-05 14:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "i3j4k5l6m7n8"
down_revision: Union[str, Sequence[str], None] = "h2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Modify gateway URL constraint to allow same URL with different slugs."""
    # Check if we're dealing with a fresh database
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()

    if "gateways" not in tables:
        print("gateways table not found. Skipping migration.")
        return

    # Drop the old constraint
    with op.batch_alter_table("gateways", schema=None) as batch_op:
        # Drop old constraint (team_id, owner_email, url)
        try:
            batch_op.drop_constraint("uq_team_owner_url_gateway", type_="unique")
        except Exception as e:
            print(f"Constraint uq_team_owner_url_gateway not found or already dropped: {e}")

        # Create new constraint (team_id, owner_email, url, slug)
        batch_op.create_unique_constraint("uq_team_owner_url_slug_gateway", ["team_id", "owner_email", "url", "slug"])


def downgrade() -> None:
    """Revert gateway URL constraint to original form."""
    # Check if we're dealing with a fresh database
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()

    if "gateways" not in tables:
        print("gateways table not found. Skipping migration.")
        return

    with op.batch_alter_table("gateways", schema=None) as batch_op:
        # Drop new constraint (team_id, owner_email, url, slug)
        try:
            batch_op.drop_constraint("uq_team_owner_url_slug_gateway", type_="unique")
        except Exception as e:
            print(f"Constraint uq_team_owner_url_slug_gateway not found or already dropped: {e}")

        # Recreate old constraint (team_id, owner_email, url)
        batch_op.create_unique_constraint("uq_team_owner_url_gateway", ["team_id", "owner_email", "url"])
