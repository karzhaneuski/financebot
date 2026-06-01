"""add last_notified_pct to budgets

Revision ID: 006_budget_notifications
Revises: 005_category_receipt_and_enum
Create Date: 2026-06-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_budget_notifications"
down_revision: Union[str, None] = "005_category_receipt_and_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "budgets",
        sa.Column("last_notified_pct", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("budgets", "last_notified_pct")
