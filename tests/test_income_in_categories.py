"""Income (salary, refunds) is not spending: its items must not count toward
category totals or budgets."""
import datetime as dt

from bot.db import crud
from tests.conftest import make_receipt

TODAY = dt.date.today()


async def _seed(session):
    await make_receipt(session, store="Lidl", date=TODAY, total=50.0, total_pln=50.0,
                       items=[{"name": "mleko", "total_price": 50.0, "category": "groceries"}])
    await make_receipt(session, store="ACME Sp. z o.o.", date=TODAY, total=8500.0, total_pln=8500.0,
                       tx_type="income", items=[{"name": "salary", "total_price": 8500.0, "category": "other"}])
    await make_receipt(session, store="Zwrot", date=TODAY, total=30.0, total_pln=30.0,
                       tx_type="income", items=[{"name": "refund", "total_price": 30.0, "category": "groceries"}])
    # Legacy rows without a tx_type count as purchases.
    await make_receipt(session, store="Żabka", date=TODAY, total=12.0, total_pln=12.0, tx_type=None,
                       items=[{"name": "woda", "total_price": 12.0, "category": "groceries"}])


EXPECTED = {"groceries": 62.0}


async def test_category_stats_skip_income(db_session):
    await _seed(db_session)
    rows = await crud.get_spending_by_category(db_session, 1, 30)
    assert {r["category"]: r["total_pln"] for r in rows} == EXPECTED
    rows = await crud.get_spending_by_category_range(db_session, 1, TODAY - dt.timedelta(days=1), TODAY)
    assert {r["category"]: r["total_pln"] for r in rows} == EXPECTED


async def test_budget_spending_skips_income(db_session):
    await _seed(db_session)
    assert await crud.get_monthly_spending_by_category(db_session, 1, TODAY.strftime("%Y-%m")) == EXPECTED
