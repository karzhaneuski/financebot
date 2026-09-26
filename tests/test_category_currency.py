"""Item prices live in the receipt's currency; every item-based sum
(categories, budgets, products, Wrapped, API, export) converts them to PLN
with the receipt's own rate (total_pln / total)."""
import datetime
import importlib.util
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db import crud
from bot.db.models import Base, Item

ROOT = Path(__file__).resolve().parent.parent


# ── Revolut imports store items natively ─────────────────────────────────────

async def test_revolut_items_saved_in_receipt_currency(db_session):
    saved, _dups, _pending = await crud.create_bank_transactions(
        db_session, 1,
        [{"date": "2026-06-10", "amount": 10.0, "currency": "EUR", "total_pln": 43.0,
          "description": "Cafe Wien", "category": "cafe"}],
        source="revolut",
    )
    assert saved == 1
    item = (await db_session.execute(select(Item))).scalar_one()
    assert float(item.total_price) == 10.0
    assert float(item.unit_price) == 10.0


# ── migration 012 ─────────────────────────────────────────────────────────────

def _load_migration():
    path = ROOT / "alembic" / "versions" / "012_item_prices_in_receipt_currency.py"
    spec = importlib.util.spec_from_file_location("m012", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(conn, fn_name):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()

    def _apply(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, fn_name)()
    await conn.run_sync(_apply)


async def test_migration_012_rewrites_only_revolut_pln_items_and_reverts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "INSERT INTO receipts (id, user_id, currency, total, total_pln, source) VALUES "
            "(1, 1, 'EUR', 10, 43, 'revolut'),"   # item in PLN -> rewritten
            "(2, 1, 'EUR', 10, 43, 'revolut'),"   # two items -> untouched
            "(3, 1, 'EUR', 10, 43, NULL),"        # photo receipt, already native
            "(4, 1, 'PLN', 50, 50, 'revolut'),"   # PLN -> untouched
            "(5, 1, 'EUR', 20, 86, 'revolut')"    # item already native -> untouched
        ))
        await conn.execute(text(
            "INSERT INTO items (id, receipt_id, name, quantity, unit_price, total_price, category) VALUES "
            "(1, 1, 'a', 1, 43, 43, 'cafe'),"
            "(2, 2, 'b', 1, 21.5, 21.5, 'cafe'), (3, 2, 'c', 1, 21.5, 21.5, 'cafe'),"
            "(4, 3, 'd', 1, 10, 10, 'cafe'),"
            "(5, 4, 'e', 1, 50, 50, 'cafe'),"
            "(6, 5, 'f', 1, 20, 20, 'cafe')"
        ))
        prices = "SELECT id, unit_price, total_price FROM items ORDER BY id"
        before = (await conn.execute(text(prices))).all()

        await _run(conn, "upgrade")
        after = {r.id: (float(r.unit_price), float(r.total_price)) for r in await conn.execute(text(prices))}
        assert after[1] == (10.0, 10.0)
        assert after[2] == (21.5, 21.5) and after[3] == (21.5, 21.5)
        assert after[4] == (10.0, 10.0)
        assert after[5] == (50.0, 50.0)
        assert after[6] == (20.0, 20.0)

        await _run(conn, "downgrade")
        reverted = {r.id: (float(r.unit_price), float(r.total_price)) for r in await conn.execute(text(prices))}
        # Downgrade restores the old convention for every Revolut row, so the
        # one saved natively by the new code (receipt 5) goes to PLN too.
        assert reverted == {r.id: (float(r.unit_price), float(r.total_price)) for r in before} | {6: (86.0, 86.0)}
    await engine.dispose()


# ── item sums are converted to PLN ────────────────────────────────────────────
# A 10 EUR receipt at 4.30 → 43 PLN: 6 EUR cafe + 4 EUR groceries.

JUNE = datetime.date(2026, 6, 15)
EUR_ITEMS = [{"name": "Kaffee", "total_price": 6.0, "category": "cafe"},
             {"name": "Brot", "total_price": 4.0, "category": "groceries"}]


async def _eur_receipt(session, user_id=1, **kw):
    from tests.conftest import make_receipt
    return await make_receipt(session, user_id=user_id, currency="EUR", total=10.0, total_pln=43.0,
                              date=kw.pop("date", JUNE), items=kw.pop("items", EUR_ITEMS), **kw)


def _approx(d: dict) -> dict:
    return {k: round(v, 2) for k, v in d.items()}


async def test_eur_receipt_counts_in_pln_in_category_sums(db_session):
    await _eur_receipt(db_session)

    by_range = await crud.get_spending_by_category_range(db_session, 1, JUNE, JUNE)  # Wrapped, reports, API
    assert _approx({r["category"]: r["total_pln"] for r in by_range}) == {"cafe": 25.8, "groceries": 17.2}

    by_month = await crud.get_monthly_spending_by_category(db_session, 1, "2026-06")  # budgets
    assert _approx(by_month) == {"cafe": 25.8, "groceries": 17.2}


async def test_eur_receipt_triggers_budget_in_pln(db_session):
    from bot.services.budget import check_budget_alerts
    today = datetime.datetime.utcnow().date()
    await crud.set_budget(db_session, 1, "cafe", 20.0, today.strftime("%Y-%m"))
    await _eur_receipt(db_session, date=today)
    # 6 EUR counted as 6 PLN stayed silently under the 20 PLN limit.
    alerts = await check_budget_alerts(db_session, 1)
    assert len(alerts) == 1 and "🚨" in alerts[0] and "25,80 / 20,00 PLN" in alerts[0]


async def test_pln_receipt_is_unchanged(db_session):
    from tests.conftest import make_receipt
    await make_receipt(db_session, total=10.0, total_pln=10.0, date=JUNE, items=EUR_ITEMS)
    by_month = await crud.get_monthly_spending_by_category(db_session, 1, "2026-06")
    assert _approx(by_month) == {"cafe": 6.0, "groceries": 4.0}


async def test_products_and_items_grouped_in_pln(db_session):
    await _eur_receipt(db_session, items=[{"name": "kaffee", "total_price": 6.0, "category": "cafe"}])
    products = await crud.get_products_stats(db_session, 1, "all")
    assert round(products[0]["total_spent"], 2) == 25.8
    detail = await crud.get_product_detail(db_session, 1, "kaffee", "all")
    assert round(detail["total_spent"], 2) == 25.8
    assert round(detail["by_store"][0]["total_spent"], 2) == 25.8
    assert round(detail["history"][0]["total"], 2) == 25.8
    store = await crud.get_store_products(db_session, 1, "Test Store", "all")
    assert round(store[0]["total_spent"], 2) == 25.8


async def test_split_eur_receipt_counts_only_my_items_in_pln(db_session):
    receipt, (cafe_id, _bread_id) = await _eur_receipt(db_session)
    await crud.set_item_personal_flags(db_session, receipt.id, [cafe_id])
    await db_session.flush()

    reloaded = await crud.get_receipt_by_id(db_session, receipt.id)
    assert float(reloaded.personal_total_pln) == 25.8  # 6/10 of 43 PLN
    by_month = await crud.get_monthly_spending_by_category(db_session, 1, "2026-06")
    assert _approx(by_month) == {"cafe": 25.8}  # not-mine bread excluded


async def test_revolut_eur_import_is_converted_once(db_session):
    await crud.create_bank_transactions(
        db_session, 1,
        [{"date": "2026-06-15", "amount": 10.0, "currency": "EUR", "total_pln": 43.0,
          "description": "Cafe Wien", "category": "cafe"}],
        source="revolut",
    )
    await db_session.flush()
    by_month = await crud.get_monthly_spending_by_category(db_session, 1, "2026-06")
    assert _approx(by_month) == {"cafe": 43.0}


async def test_exports_show_native_price_and_pln(db_session):
    from bot.handlers.export import _build_all_csv, _build_items_csv
    await _eur_receipt(db_session)
    rows = await crud.get_items_for_export(db_session, 1, None, None)

    items_csv = _build_items_csv(rows).splitlines()
    assert items_csv[0].endswith("total_price,currency,total_pln,category")
    assert "6.0,EUR,25.8,cafe" in items_csv[1]

    all_csv = _build_all_csv([], rows).splitlines()
    assert all_csv[1].split(",")[4] == "25.8"  # amount_pln column
