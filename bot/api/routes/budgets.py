from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import get_current_user
from bot.api.categories import CATEGORY_META
from bot.api.deps import get_db
from bot.db import crud

router = APIRouter()


@router.get("/budgets")
async def get_budgets(
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    month = date.today().strftime("%Y-%m")
    budgets = await crud.get_budgets(db, user_id, month)
    spending = await crud.get_monthly_spending_by_category(db, user_id, month)

    result = []
    for b in budgets:
        cat = b.category.value
        name, emoji = CATEGORY_META.get(cat, (cat, "📦"))
        spent = spending.get(cat, 0.0)
        limit = float(b.limit_pln)
        pct = round(spent / limit * 100) if limit > 0 else 0

        if pct >= 100:
            status = "exceeded"
        elif pct >= 80:
            status = "warning"
        else:
            status = "ok"

        result.append({
            "category": cat,
            "name": name,
            "emoji": emoji,
            "limit": round(limit, 2),
            "spent": round(spent, 2),
            "percentage": pct,
            "status": status,
        })

    return {"budgets": result}
