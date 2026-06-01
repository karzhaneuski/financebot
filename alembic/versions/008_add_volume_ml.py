"""add volume_ml to items

Revision ID: 008_add_volume_ml
Revises: 007_report_settings
Create Date: 2026-06-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_add_volume_ml"
down_revision: Union[str, None] = "007_report_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("volume_ml", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "volume_ml")
