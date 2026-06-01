"""
Backfill script: populate volume_ml for items where the column is NULL
and the receipt has a photo_file_id (receipt scan origin).

Run inside the container:
    docker compose exec bot python bot/scripts/backfill_volume.py
"""
import asyncio
import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import traceback
from sqlalchemy import select

from bot.db.engine import AsyncSessionLocal, engine
from bot.db.models import Item, Receipt
from bot.services.normalization import normalize_item_names

_BATCH_SIZE = 50


async def backfill() -> None:
    updated = 0
    skipped = 0

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Item)
            .join(Receipt, Item.receipt_id == Receipt.id)
            .where(Item.volume_ml.is_(None), Receipt.photo_file_id.isnot(None))
        )
        result = await session.execute(stmt)
        items = result.scalars().all()

        print(f"Found {len(items)} items with volume_ml IS NULL from receipt scans")

        for batch_start in range(0, len(items), _BATCH_SIZE):
            batch = items[batch_start : batch_start + _BATCH_SIZE]
            batch_dicts = [{"name": item.name or ""} for item in batch]

            try:
                await normalize_item_names(batch_dicts)
            except Exception as e:
                print(f"  Error on batch {batch_start}: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                skipped += len(batch)
                continue

            if batch_start == 0:
                print(f"  [debug] first result dict: {batch_dicts[0]}")

            set_count = 0
            for item, d in zip(batch, batch_dicts):
                vol = d.get("volume_ml")
                item.volume_ml = vol if vol is not None else None
                if vol is not None:
                    set_count += 1

            updated += len(batch)
            print(f"  Processed {updated}/{len(items)} — {set_count}/{len(batch)} had volume in this batch")

        await session.commit()

    print(f"\nDone. Updated {updated} items ({skipped} skipped due to errors).")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(backfill())
