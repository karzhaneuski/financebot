from calendar import monthrange
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import get_current_user
from bot.api.categories import CATEGORY_META
from bot.api.deps import get_db
from bot.db import crud

router = APIRouter()


def _period_range(period: str) -> tuple[date, date]:
    today = date.today()
    if period == "week":
        return today - timedelta(days=6), today
    elif period == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    else:
        _, last_day = monthrange(today.year, today.month)
        return date(today.year, today.month, 1), date(today.year, today.month, last_day)


@router.get("/summary")
async def stats_summary(
    period: str = Query("month"),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    date_from, date_to = _period_range(period)
    total_spent = await crud.get_expenses_total_range(db, user_id, date_from, date_to)
    total_income = await crud.get_income_by_range(db, user_id, date_from, date_to)
    _, tx_count = await crud.get_total_spending_range(db, user_id, date_from, date_to)

    return {
        "period": period,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total_spent": round(total_spent, 2),
        "total_income": round(total_income, 2),
        "balance": round(total_income - total_spent, 2),
        "transaction_count": tx_count,
    }


@router.get("/by-category")
async def stats_by_category(
    period: str = Query("month"),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    date_from, date_to = _period_range(period)
    rows = await crud.get_spending_by_category_range(db, user_id, date_from, date_to)

    total = sum(r["total_pln"] for r in rows) or 1
    categories = []
    for r in rows:
        cat = r["category"]
        name, emoji = CATEGORY_META.get(cat, (cat, "📦"))
        amount = round(r["total_pln"], 2)
        categories.append({
            "category": cat,
            "name": name,
            "emoji": emoji,
            "amount": amount,
            "percentage": round(amount / total * 100),
        })

    return {"categories": categories}


@router.get("/by-day")
async def stats_by_day(
    period: str = Query("month"),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    date_from, date_to = _period_range(period)
    days = await crud.get_daily_spending_and_income(db, user_id, date_from, date_to)
    return {"days": days}


@router.get("/top-stores")
async def stats_top_stores(
    period: str = Query("month"),
    limit: int = Query(5, ge=1, le=50),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    date_from, date_to = _period_range(period)
    rows = await crud.get_spending_by_store_range(db, user_id, date_from, date_to)

    stores = [
        {"store": r["store"], "amount": round(r["total_pln"], 2), "count": r["visits"]}
        for r in rows[:limit]
    ]
    return {"stores": stores}
