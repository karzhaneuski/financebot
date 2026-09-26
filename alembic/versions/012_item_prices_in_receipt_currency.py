"""store every item price in its receipt's currency (data only)

Revision ID: 012_item_currency
Revises: 011_users_language
Create Date: 2026-09-26 00:00:00.000000

items.total_price is meant to be in receipts.currency — photo, PDF and
manual receipts always stored it that way, and category/budget sums convert
it to PLN with the receipt's own rate (total_pln / total). Revolut imports
were the exception: their single item was written in PLN (= total_pln) while
the receipt kept the foreign currency, so a PLN conversion would apply the
rate twice. Rewrite those items to the native amount.

Only single-item Revolut receipts whose item still equals total_pln are
touched; anything else is left as is. No schema change.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "012_item_currency"
down_revision: Union[str, None] = "011_users_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Portable SQL (PostgreSQL in production, SQLite in tests): correlated
# subqueries instead of UPDATE ... FROM.
_REVOLUT_FOREIGN_SINGLE_ITEM = """
    SELECT r.id FROM receipts r
    WHERE r.source = 'revolut' AND r.currency <> 'PLN'
      AND (SELECT count(*) FROM items i2 WHERE i2.receipt_id = r.id) = 1
"""


def _rewrite_revolut_items(from_col: str, to_col: str) -> None:
    op.execute(f"""
        UPDATE items
        SET total_price = (SELECT r.{to_col} FROM receipts r WHERE r.id = items.receipt_id),
            unit_price  = (SELECT r.{to_col} FROM receipts r WHERE r.id = items.receipt_id)
        WHERE receipt_id IN ({_REVOLUT_FOREIGN_SINGLE_ITEM})
          AND abs(total_price - (SELECT r.{from_col} FROM receipts r WHERE r.id = items.receipt_id)) <= 0.01
    """)


def upgrade() -> None:
    _rewrite_revolut_items(from_col="total_pln", to_col="total")


def downgrade() -> None:
    _rewrite_revolut_items(from_col="total", to_col="total_pln")
