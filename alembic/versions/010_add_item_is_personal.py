"""add is_personal to items

Revision ID: 010_item_is_personal
Revises: 009_personal_total
Create Date: 2026-08-24 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_item_is_personal"
down_revision: Union[str, None] = "009_personal_total"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL means "counts as personal" (pre-split items keep old behaviour).
    op.add_column("items", sa.Column("is_personal", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "is_personal")
