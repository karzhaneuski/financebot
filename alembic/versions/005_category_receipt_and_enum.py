"""add entertainment/health/subscriptions to category enum; add category to receipts

Revision ID: 005_category_receipt_and_enum
Revises: 004_add_source
Create Date: 2026-05-31 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "005_category_receipt_and_enum"
down_revision: Union[str, None] = "004_add_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extend the existing enum type (PostgreSQL 9.3+ supports IF NOT EXISTS)
    op.execute("ALTER TYPE category ADD VALUE IF NOT EXISTS 'entertainment'")
    op.execute("ALTER TYPE category ADD VALUE IF NOT EXISTS 'health'")
    op.execute("ALTER TYPE category ADD VALUE IF NOT EXISTS 'subscriptions'")

    # Add nullable category column to receipts referencing the existing enum type
    op.add_column(
        "receipts",
        sa.Column(
            "category",
            PgEnum(name="category", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("receipts", "category")
    # Note: PostgreSQL does not support removing enum values — the three new
    # values (entertainment, health, subscriptions) remain in the type.
