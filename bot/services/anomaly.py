from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.db.models import Category, Receipt

ANOMALY_MULTIPLIER = 3.0
_MIN_PRIOR_RECEIPTS = 3
_LOOKBACK_DAYS = 90


async def _primary_category(session: AsyncSession, receipt: Receipt) -> Category | None:
    """Receipt.category if set, else its first item's category (mirrors reports.py display logic)."""
    if receipt.category is not None:
        return receipt.category
    await session.refresh(receipt, attribute_names=["items"])
    if receipt.items:
        return receipt.items[0].category
    return None


async def check_anomaly(session: AsyncSession, user_id: int, receipt: Receipt) -> dict | None:
    """Flag `receipt` if its total is a statistical outlier for its category.

    Returns {"category_avg": float, "multiplier": float} when total_pln exceeds
    ANOMALY_MULTIPLIER times the category's recent average, else None.
    """
    category = await _primary_category(session, receipt)
    if category is None:
        return None

    avg, count = await crud.get_category_average(
        session, user_id, category, days=_LOOKBACK_DAYS, exclude_receipt_id=receipt.id
    )
    if count < _MIN_PRIOR_RECEIPTS or avg <= 0:
        return None

    multiplier = float(receipt.total_pln) / avg
    if multiplier <= ANOMALY_MULTIPLIER:
        return None

    return {"category_avg": avg, "multiplier": multiplier}
