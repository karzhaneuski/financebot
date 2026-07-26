"""add report_settings table

Revision ID: 007_report_settings
Revises: 006_budget_notifications
Create Date: 2026-06-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_report_settings"
down_revision: Union[str, None] = "006_budget_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("daily_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("weekly_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("monthly_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_report_settings_user_id", "report_settings", ["user_id"], if_not_exists=True
    )


def downgrade() -> None:
    op.drop_index("ix_report_settings_user_id", table_name="report_settings")
    op.drop_table("report_settings")
