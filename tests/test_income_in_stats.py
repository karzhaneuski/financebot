"""Income (salary, refunds) is not spending: it must not show up as a store
or as a bought item/product."""
import datetime as dt

from bot.db import crud
from tests.conftest import make_receipt

TODAY = dt.date.today()


async def _seed(session):
    await make_receipt(session, store="Lidl", date=TODAY, total=50.0, total_pln=50.0,
                       items=[{"name": "mleko", "total_price": 50.0, "category": "groceries"}])
    await make_receipt(session, store="ACME Sp. z o.o.", date=TODAY, total=8500.0, total_pln=8500.0,
                       tx_type="income", items=[{"name": "salary", "total_price": 8500.0, "category": "other"}])
    await make_receipt(session, store="ATM", date=TODAY, total=300.0, total_pln=300.0,
                       tx_type="cash_withdrawal", photo_file_id=None,
                       items=[{"name": "cash", "total_price": 300.0, "category": "other"}])
    # Legacy rows without a tx_type count as purchases.
    await make_receipt(session, store="Żabka", date=TODAY, total=12.0, total_pln=12.0, tx_type=None,
                       items=[{"name": "woda", "total_price": 12.0, "category": "groceries"}])


async def test_store_stats_are_spending_only(db_session):
    await _seed(db_session)
    expected = {"Lidl", "Żabka"}
    assert {s["store"] for s in await crud.get_spending_by_store(db_session, 1, 30)} == expected
    assert {s["store"] for s in await crud.get_store_stats_by_period(db_session, 1, "month")} == expected
    by_range = await crud.get_spending_by_store_range(db_session, 1, TODAY - dt.timedelta(days=1), TODAY)
    assert {s["store"] for s in by_range} == expected


async def test_item_and_product_stats_skip_income(db_session):
    await _seed(db_session)
    items = {i["name"] for i in await crud.get_items_grouped(db_session, 1, 30)}
    assert "salary" not in items and {"mleko", "woda"} <= items
    products = {p["normalized_name"] for p in await crud.get_products_stats(db_session, 1, "all")}
    assert products == {"mleko", "woda"}
    assert await crud.get_store_products(db_session, 1, "ACME Sp. z o.o.", "all") == []
