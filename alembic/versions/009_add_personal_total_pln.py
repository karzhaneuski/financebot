"""add personal_total_pln to receipts

Revision ID: 009_personal_total
Revises: 008_add_volume_ml
Create Date: 2026-08-24 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_personal_total"
down_revision: Union[str, None] = "008_add_volume_ml"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("receipts", sa.Column("personal_total_pln", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("receipts", "personal_total_pln")
