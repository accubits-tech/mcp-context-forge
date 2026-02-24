"""merge gateway url constraint migrations

Revision ID: 9a79c73f2b78
Revises: f3a3a3d901b8, i3j4k5l6m7n8
Create Date: 2025-11-19 11:47:58.507673

"""

# Standard
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "9a79c73f2b78"
down_revision: Union[str, Sequence[str], None] = ("f3a3a3d901b8", "i3j4k5l6m7n8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
