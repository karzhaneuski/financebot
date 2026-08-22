from unittest.mock import AsyncMock, patch

import pytest

from bot.db.models import Category, Item, Receipt
from bot.services.anomaly import ANOMALY_MULTIPLIER, check_anomaly


def _receipt(category: Category | None, total_pln: float, items: list[Item] | None = None) -> Receipt:
    r = Receipt(id=1, user_id=1, currency="PLN", total=total_pln, total_pln=total_pln, category=category)
    if items is not None:
        r.items = items
    return r


@pytest.mark.asyncio
async def test_check_anomaly_triggers_above_multiplier():
    receipt = _receipt(Category.electronics, total_pln=500.0)
    with patch("bot.services.anomaly.crud.get_category_average", AsyncMock(return_value=(119.0, 5))):
        result = await check_anomaly(AsyncMock(), user_id=1, receipt=receipt)

    assert result is not None
    assert result["category_avg"] == 119.0
    assert result["multiplier"] == pytest.approx(500.0 / 119.0)
    assert result["multiplier"] > ANOMALY_MULTIPLIER


@pytest.mark.asyncio
async def test_check_anomaly_not_triggered_below_multiplier():
    receipt = _receipt(Category.groceries, total_pln=150.0)
    with patch("bot.services.anomaly.crud.get_category_average", AsyncMock(return_value=(100.0, 5))):
        result = await check_anomaly(AsyncMock(), user_id=1, receipt=receipt)

    assert result is None


@pytest.mark.asyncio
async def test_check_anomaly_skipped_with_too_few_prior_receipts():
    receipt = _receipt(Category.electronics, total_pln=1000.0)
    with patch("bot.services.anomaly.crud.get_category_average", AsyncMock(return_value=(50.0, 2))):
        result = await check_anomaly(AsyncMock(), user_id=1, receipt=receipt)

    assert result is None


@pytest.mark.asyncio
async def test_check_anomaly_falls_back_to_first_item_category():
    item = Item(id=1, receipt_id=1, name="TV", quantity=1, total_price=500.0, category=Category.electronics)
    receipt = _receipt(None, total_pln=500.0, items=[item])
    session = AsyncMock()
    with patch("bot.services.anomaly.crud.get_category_average", AsyncMock(return_value=(119.0, 5))) as mock_avg:
        result = await check_anomaly(session, user_id=1, receipt=receipt)

    mock_avg.assert_awaited_once_with(session, 1, Category.electronics, days=90, exclude_receipt_id=1)
    assert result is not None


@pytest.mark.asyncio
async def test_check_anomaly_no_category_no_items_skips():
    receipt = _receipt(None, total_pln=500.0)
    session = AsyncMock()

    async def _fake_refresh(obj, attribute_names=None):
        obj.items = []

    session.refresh.side_effect = _fake_refresh
    result = await check_anomaly(session, user_id=1, receipt=receipt)

    assert result is None
