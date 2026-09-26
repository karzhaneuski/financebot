"""Product stats and the item export count real receipt lines whether or not
a photo is stored: PDF receipts are saved without photo_file_id and used to
be dropped (Biedronka PDF receipt missing from /stats → stores and the
"Items" sheet while a Lidl photo receipt of the same day showed up)."""
import datetime as dt

from bot import markers
from bot.db import crud
from tests.conftest import make_receipt

TODAY = dt.date.today()


async def _seed(session):
    await make_receipt(session, store="Lidl", date=TODAY, total=20.0, total_pln=20.0,
                       items=[{"name": "mleko", "total_price": 20.0, "category": "groceries"}])
    # PDF receipt: no photo.
    await make_receipt(session, store="Biedronka", date=TODAY, total=30.0, total_pln=30.0,
                       photo_file_id=None, tx_type=None,
                       items=[{"name": "chleb", "total_price": 10.0, "category": "groceries"},
                              {"name": "masło", "total_price": 20.0, "category": "groceries"}])
    # Not receipt lines: the one item is the whole transaction or a placeholder.
    await make_receipt(session, store="Biedronka", date=TODAY, total=40.0, total_pln=40.0,
                       source="screenshot", items=[{"name": markers.MANUAL_EXPENSE, "total_price": 40.0}])
    await make_receipt(session, store="Biedronka", date=TODAY, total=50.0, total_pln=50.0,
                       photo_file_id=None, source="revolut",
                       items=[{"name": "BIEDRONKA 1234", "total_price": 50.0}])
    await make_receipt(session, store="Biedronka", date=TODAY, total=60.0, total_pln=60.0,
                       photo_file_id=None, items=[{"name": markers.MANUAL_EXPENSE, "total_price": 60.0}])
    await make_receipt(session, store="Biedronka", date=TODAY, total=70.0, total_pln=70.0,
                       photo_file_id=None, tx_type=None, items=[{"name": "Расход", "total_price": 70.0}])


async def test_pdf_receipt_items_in_store_products(db_session):
    await _seed(db_session)
    products = await crud.get_store_products(db_session, 1, "Biedronka", "month")
    assert {p["normalized_name"] for p in products} == {"chleb", "masło"}


async def test_pdf_receipt_items_in_product_stats(db_session):
    await _seed(db_session)
    products = {p["normalized_name"] for p in await crud.get_products_stats(db_session, 1, "month")}
    assert products == {"mleko", "chleb", "masło"}
    assert set(await crud.get_all_normalized_names(db_session, 1)) == products
    detail = await crud.get_product_detail(db_session, 1, "chleb", "month")
    assert detail["total_spent"] == 10.0


async def test_pdf_receipt_items_in_export(db_session):
    await _seed(db_session)
    rows = await crud.get_items_for_export(db_session, 1, TODAY - dt.timedelta(days=1), TODAY)
    assert sorted(r.name for r in rows) == ["chleb", "masło", "mleko"]
