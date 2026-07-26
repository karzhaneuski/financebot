"""add normalized_name to items

Revision ID: 003_normalized_name
Revises: 002_housing_tx_type
Create Date: 2026-05-30 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_normalized_name"
down_revision: Union[str, None] = "002_housing_tx_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("normalized_name", sa.String(255), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_items_normalized_name ON items (normalized_name)"
    )


def downgrade() -> None:
    op.drop_index("ix_items_normalized_name", table_name="items")
    op.drop_column("items", "normalized_name")
