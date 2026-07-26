"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2025-05-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

category_enum = sa.Enum(
    "groceries", "cafe", "pharmacy", "transport",
    "electronics", "clothing", "household", "other",
    name="category",
)


def upgrade() -> None:
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("store", sa.String(255), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_pln", sa.Numeric(12, 2), nullable=False),
        sa.Column("photo_file_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        if_not_exists=True,
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_receipts_user_id ON receipts (user_id)")

    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.Integer(),
            sa.ForeignKey("receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Numeric(10, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("category", category_enum, nullable=False),
        if_not_exists=True,
    )

    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("category", category_enum, nullable=False),
        sa.Column("limit_pln", sa.Numeric(12, 2), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        if_not_exists=True,
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_budgets_user_id ON budgets (user_id)")


def downgrade() -> None:
    op.drop_table("budgets")
    op.drop_table("items")
    op.drop_table("receipts")
    category_enum.drop(op.get_bind(), checkfirst=True)
