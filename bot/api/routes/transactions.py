from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import get_current_user
from bot.api.categories import category_meta
from bot.api.deps import get_db, get_user_language
from bot.db import crud
from bot.i18n import i18n
from bot.markers import display_name

router = APIRouter()


def _display(value: str | None, language: str) -> str | None:
    with i18n.use_locale(language):
        return display_name(value)


@router.get("/recent")
async def recent_transactions(
    limit: int = Query(10, ge=1, le=100),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    language: str = Depends(get_user_language),
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

        name, emoji = category_meta(cat, language)
        source = r.source or ("receipt" if r.photo_file_id else "manual")

        result.append({
            "id": r.id,
            "date": r.date.isoformat() if r.date else None,
            "store": _display(r.store, language),
            "amount": round(r.personal_amount(), 2),
            "currency": r.currency,
            "original_amount": round(float(r.total), 2),
            "category": cat,
            "category_name": name,
            "emoji": emoji,
            "source": source,
        })

    return {"transactions": result}
