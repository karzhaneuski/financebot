"""add users table with per-user language

Revision ID: 011_users_language
Revises: 010_item_is_personal
Create Date: 2026-09-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_users_language"
down_revision: Union[str, None] = "010_item_is_personal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    # Everyone who used the bot before localization keeps Russian. Existing
    # tables are only read, never modified.
    op.execute(
        """
        INSERT INTO users (user_id, language)
        SELECT user_id, 'ru' FROM (
            SELECT user_id FROM receipts
            UNION SELECT user_id FROM budgets
            UNION SELECT user_id FROM report_settings
        ) AS existing_users
        """
    )


def downgrade() -> None:
    op.drop_table("users")
