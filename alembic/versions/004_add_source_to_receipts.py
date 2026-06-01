"""add source column to receipts

Revision ID: 004_add_source
Revises: 003_normalized_name
Create Date: 2026-05-31 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_add_source"
down_revision: Union[str, None] = "003_normalized_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("receipts", sa.Column("source", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("receipts", "source")
