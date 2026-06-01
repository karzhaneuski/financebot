"""
One-time script: re-run categorize() on all items whose current category
is 'other', 'household', or NULL, using the parent receipt's store name.

Run inside the container:
    docker compose exec bot python bot/scripts/recategorize.py
"""
import asyncio
import sys
import os

# Ensure the project root (/app inside the container) is on sys.path so that
# `from bot.*` imports work whether the script is called directly or as a module.
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db.engine import AsyncSessionLocal, engine
from bot.db.models import Category, Item, Receipt
from bot.parsers.erste import categorize

TARGET_CATEGORIES = {Category.other, Category.household, Category.housing}


async def recategorize() -> None:
    updated = 0
    skipped = 0
    breakdown: dict[str, int] = defaultdict(int)
    changes: list[str] = []

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Item)
            .where(Item.category.in_(TARGET_CATEGORIES))
            .options(selectinload(Item.receipt))
            .order_by(Item.id)
        )
        result = await session.execute(stmt)
        items = result.scalars().all()

        target_names = ", ".join(sorted(c.value for c in TARGET_CATEGORIES))
        print(f"Found {len(items)} items with category in [{target_names}].")

        for item in items:
            receipt: Receipt = item.receipt
            # Use store name if present, otherwise fall back to item name
            text = receipt.store or item.name or ""
            if not text.strip():
                skipped += 1
                continue

            new_cat_str, _ = categorize(text)
            new_cat = Category(new_cat_str)

            if new_cat == item.category:
                skipped += 1
                continue

            changes.append(
                f"  id={item.id:6d}  {item.category.value:12s} → {new_cat.value:12s}  "
                f"store={receipt.store!r}"
            )
            item.category = new_cat
            breakdown[new_cat.value] += 1
            updated += 1

        await session.commit()

    # --- output ---
    print()
    if changes:
        print("Changed items:")
        for line in changes:
            print(line)
        print()

    print("=" * 50)
    print(f"Total scanned : {len(items)}")
    print(f"Updated       : {updated}")
    print(f"Unchanged     : {skipped}")
    if breakdown:
        print("\nBreakdown of new categories:")
        for cat, count in sorted(breakdown.items(), key=lambda x: -x[1]):
            print(f"  {cat:15s}: {count}")
    print("=" * 50)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(recategorize())
