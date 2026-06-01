"""
Backfill script: set tx_type = 'cash_withdrawal' on receipts whose store name
matches any CASH_WITHDRAWAL_KEYWORDS but were saved before that column existed.

Targets rows where tx_type IS NULL or tx_type = 'purchase'.

Run inside the container:
    docker compose exec bot python bot/scripts/backfill_tx_type.py
"""
import asyncio
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sqlalchemy import or_, select

from bot.db.engine import AsyncSessionLocal, engine
from bot.db.models import Receipt
from bot.parsers.erste import CASH_WITHDRAWAL_KEYWORDS

_KEYWORDS_UPPER = [kw.upper() for kw in CASH_WITHDRAWAL_KEYWORDS]


def _is_cash_withdrawal(store: str | None) -> bool:
    if not store:
        return False
    upper = store.upper()
    return any(kw in upper for kw in _KEYWORDS_UPPER)


async def backfill() -> None:
    updated = 0
    matched_stores: dict[str, int] = {}

    async with AsyncSessionLocal() as session:
        stmt = select(Receipt).where(
            or_(Receipt.tx_type.is_(None), Receipt.tx_type == "purchase")
        )
        result = await session.execute(stmt)
        candidates = result.scalars().all()

        print(f"Scanning {len(candidates)} receipts with tx_type NULL or 'purchase'...")

        for receipt in candidates:
            if not _is_cash_withdrawal(receipt.store):
                continue
            receipt.tx_type = "cash_withdrawal"
            store_key = receipt.store or "(null)"
            matched_stores[store_key] = matched_stores.get(store_key, 0) + 1
            updated += 1

        await session.commit()

    print()
    print("=" * 50)
    print(f"Total scanned : {len(candidates)}")
    print(f"Updated       : {updated}")
    if matched_stores:
        print("\nMatched store names:")
        for store, count in sorted(matched_stores.items(), key=lambda x: -x[1]):
            print(f"  {store!r:35s}: {count}")
    print("=" * 50)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(backfill())
