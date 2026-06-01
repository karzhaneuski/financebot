"""
Backfill script: populate normalized_name for items where the column is NULL.

The migration added normalized_name but did not backfill existing rows.
This script queries those items in batches of 50 and calls the Claude-based
normalizer to fill in the values.

Run inside the container:
    docker compose exec bot python bot/scripts/backfill_normalized_names.py
"""
import asyncio
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sqlalchemy import select

from bot.db.engine import AsyncSessionLocal, engine
from bot.db.models import Item
from bot.services.normalization import normalize_item_names

_BATCH_SIZE = 50


async def backfill() -> None:
    updated = 0

    async with AsyncSessionLocal() as session:
        stmt = select(Item).where(Item.normalized_name.is_(None))
        result = await session.execute(stmt)
        items = result.scalars().all()

        print(f"Found {len(items)} items with normalized_name IS NULL")

        for batch_start in range(0, len(items), _BATCH_SIZE):
            batch = items[batch_start : batch_start + _BATCH_SIZE]
            batch_dicts = [{"name": item.name or ""} for item in batch]

            await normalize_item_names(batch_dicts)

            for item, d in zip(batch, batch_dicts):
                item.normalized_name = d.get("normalized_name") or (item.name or "").lower().strip() or None

            updated += len(batch)
            print(f"  Processed {updated}/{len(items)}...")

        await session.commit()

    print(f"Updated {updated} items")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(backfill())
