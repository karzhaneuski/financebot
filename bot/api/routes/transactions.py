from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import get_current_user
from bot.api.categories import CATEGORY_META
from bot.api.deps import get_db
from bot.db import crud

router = APIRouter()


@router.get("/recent")
async def recent_transactions(
    limit: int = Query(10, ge=1, le=100),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    receipts = await crud.get_recent_receipts(db, user_id, limit)

    result = []
    for r in receipts:
        if r.category:
            cat = r.category.value
        elif r.items:
            cat = r.items[0].category.value
        else:
            cat = "other"

        name, emoji = CATEGORY_META.get(cat, ("Другое", "📦"))
        source = r.source or ("receipt" if r.photo_file_id else "manual")

        result.append({
            "id": r.id,
            "date": r.date.isoformat() if r.date else None,
            "store": r.store,
            "amount": round(r.personal_amount(), 2),
            "currency": r.currency,
            "original_amount": round(float(r.total), 2),
            "category": cat,
            "category_name": name,
            "emoji": emoji,
            "source": source,
        })

    return {"transactions": result}
