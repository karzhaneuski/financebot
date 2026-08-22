from calendar import monthrange
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud

_MIN_DAYS_ELAPSED = 3


async def get_month_forecast(session: AsyncSession, user_id: int) -> dict:
    today = datetime.utcnow().date()
    month_start = date(today.year, today.month, 1)
    _, days_in_month = monthrange(today.year, today.month)
    days_elapsed = (today - month_start).days + 1

    spent_so_far = await crud.get_expenses_total_range(session, user_id, month_start, today)
    projected_total = (spent_so_far / days_elapsed) * days_in_month

    return {
        "spent_so_far": spent_so_far,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "projected_total": projected_total,
    }


def format_forecast_line(forecast: dict) -> str | None:
    """Return the forecast line, or None if there's too little data this month to project."""
    if forecast["days_elapsed"] < _MIN_DAYS_ELAPSED:
        return None
    amount = f"{round(forecast['projected_total']):,}".replace(",", " ")
    return f"📈 Прогноз на конец месяца: ~{amount} PLN (при текущем темпе трат)"
