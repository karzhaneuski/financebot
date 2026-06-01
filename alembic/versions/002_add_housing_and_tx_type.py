"""add housing category and tx_type column

Revision ID: 002_housing_tx_type
Revises: 001_initial
Create Date: 2026-05-30 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_housing_tx_type"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'housing' to the category enum (PostgreSQL-specific)
    op.execute("ALTER TYPE category ADD VALUE IF NOT EXISTS 'housing'")

    # Add tx_type column to receipts; backfill existing rows as 'purchase'
    op.add_column("receipts", sa.Column("tx_type", sa.String(20), nullable=True))
    op.execute("UPDATE receipts SET tx_type = 'purchase' WHERE tx_type IS NULL")


def downgrade() -> None:
    op.drop_column("receipts", "tx_type")
    # PostgreSQL does not support removing enum values; housing stays in the type
