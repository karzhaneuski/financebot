"""Receipt totals: the "Suma PLN" total must be saved; a missing/zero model
total falls back to the items sum; a differing total shows the mismatch
warning. Gemini is mocked — no real API calls."""
import json
import logging
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import select

from bot.db.models import Item, Receipt
from bot.services.vision import RECEIPT_SCHEMA
from bot.utils.validators import validate_receipt
from tests.test_llm_providers import _answer, gemini  # noqa: F401  (fixture)

MISMATCH_WARNING = "⚠️ Итог чека не совпадает с суммой позиций."

ITEMS = [
    {"name": "ChipsyCrun130-140g", "quantity": 1, "unit_price": 6.99, "total_price": 6.99,
     "category": "groceries", "deposit_type": None},
    {"name": "MlekoUHT3,2%1L", "quantity": 2, "unit_price": 3.49, "total_price": 6.98,
     "category": "groceries", "deposit_type": None},
    {"name": "Chleb", "quantity": 1, "unit_price": 4.99, "total_price": 4.99,
     "category": "groceries", "deposit_type": None},
]
ITEMS_SUM = 18.96


def _receipt(total):
    return {"store": "Biedronka", "date": "2026-09-16", "currency": "PLN", "items": ITEMS, "total": total}


NOT_A_SCREEN = {"is_transaction_screen": False, "merchant": None, "amount": None, "currency": None,
                "date": None, "year_visible": None, "raw_title": None}


def _normalized(names):
    return [{"original": n, "normalized": n.title(), "volume_ml": None} for n in names]


# ── validator ────────────────────────────────────────────────────────────────

def _items(*prices):
    return [{"name": f"p{i}", "quantity": 1, "total_price": p, "category": "groceries"}
            for i, p in enumerate(prices)]


@pytest.mark.parametrize("total", [None, 0, 0.0, "0", ""])
def test_missing_or_zero_total_falls_back_to_items_sum(total, caplog):
    caplog.set_level(logging.DEBUG)
    result = validate_receipt({"store": "B", "date": None, "currency": "PLN", "total": total,
                               "items": _items(10.5, 2.25)})
    assert result["total"] == pytest.approx(12.75)
    assert result["total_from_items"] is True
    assert result["total_mismatch"] is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("sum of items" in w for w in warnings)
    assert not any("12.75" in w for w in warnings)  # no amounts above DEBUG


def test_matching_total_is_kept():
    result = validate_receipt({"store": "B", "date": None, "currency": "PLN", "total": 12.75,
                               "items": _items(10.5, 2.25)})
    assert (result["total"], result["total_from_items"], result["total_mismatch"]) == (12.75, False, False)


def test_differing_total_is_kept_and_flagged():
    result = validate_receipt({"store": "B", "date": None, "currency": "PLN", "total": 52.94,
                               "items": _items(10.5, 2.25)})
    assert result["total"] == 52.94 and result["total_mismatch"] is True
    assert result["total_from_items"] is False


def test_comma_decimal_string_total_is_parsed():
    result = validate_receipt({"store": "B", "date": None, "currency": "PLN", "total": "12,75",
                               "items": _items(10.5, 2.25)})
    assert result["total"] == 12.75 and not result["total_from_items"] and not result["total_mismatch"]


def test_no_total_and_zero_items_stays_zero():
    result = validate_receipt({"store": "B", "date": None, "currency": "PLN", "total": None,
                               "items": _items(0)})
    assert result["total"] == 0 and result["total_from_items"] is False


# ── schema ───────────────────────────────────────────────────────────────────

def test_schema_asks_for_suma_pln_after_the_items():
    props = list(RECEIPT_SCHEMA["properties"])
    assert props.index("total") > props.index("items")  # Gemini emits keys in schema order
    assert "Suma PLN" in RECEIPT_SCHEMA["properties"]["total"]["description"]


# ── handler plumbing ─────────────────────────────────────────────────────────

class _Status:
    def __init__(self):
        self.texts: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class _Message:
    def __init__(self, user_id):
        self.photo = [SimpleNamespace(file_id="photo-1")]
        self.from_user = SimpleNamespace(id=user_id)
        self.status = _Status()
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)
        return self.status


class _Bot:
    async def download(self, photo, destination):
        destination.write(b"jpeg")

    async def send_message(self, *a, **k):
        pass


async def _saved(session, user_id):
    receipt = (await session.execute(select(Receipt).where(Receipt.user_id == user_id))).scalar_one()
    items = (await session.execute(select(Item).where(Item.receipt_id == receipt.id))).scalars().all()
    return receipt, items


def _two_page_pdf() -> bytes:
    """Items on page 1, the "Suma PLN" line only on page 2."""
    doc = fitz.open()
    page1 = doc.new_page()
    page1.insert_text((72, 72), "Jeronimo Martins Polska - Biedronka")
    for i, it in enumerate(ITEMS):
        page1.insert_text((72, 110 + 20 * i), f"{it['name']}  A  {it['quantity']} x {it['unit_price']}")
    doc.new_page().insert_text((72, 72), f"Suma PLN  {ITEMS_SUM}")
    return doc.tobytes()


# ── PDF path ─────────────────────────────────────────────────────────────────

async def test_pdf_sends_every_page_and_saves_the_suma_total(gemini, db_session):  # noqa: F811
    import bot.handlers.receipt as rh
    gemini.side_effect = [_answer(_receipt(ITEMS_SUM)), _answer(_normalized([i["name"] for i in ITEMS]))]
    status = _Status()

    await rh._handle_pdf_receipt(_Message(101), status, _two_page_pdf(), db_session, redis=None, bot=_Bot())

    receipt_call = gemini.await_args_list[0].kwargs
    images = [p for p in receipt_call["contents"] if not isinstance(p, str)]
    assert len(images) == 2  # page 2 carries "Suma PLN"
    receipt, items = await _saved(db_session, 101)
    assert float(receipt.total) == ITEMS_SUM and float(receipt.total_pln) == ITEMS_SUM
    assert len(items) == 3
    assert f"Итого: {ITEMS_SUM:.2f} PLN".replace(".", ",") in status.texts[-1]
    assert MISMATCH_WARNING not in status.texts[-1]


async def test_pdf_zero_total_falls_back_to_items_sum(gemini, db_session):  # noqa: F811
    import bot.handlers.receipt as rh
    gemini.side_effect = [_answer(_receipt(0)), _answer(_normalized([i["name"] for i in ITEMS]))]
    status = _Status()

    await rh._handle_pdf_receipt(_Message(102), status, _two_page_pdf(), db_session, redis=None, bot=_Bot())

    receipt, _ = await _saved(db_session, 102)
    assert float(receipt.total) == ITEMS_SUM and float(receipt.total_pln) == ITEMS_SUM
    assert f"Итого: {ITEMS_SUM:.2f} PLN".replace(".", ",") in status.texts[-1]


def test_pdf_render_caps_pages():
    import bot.handlers.receipt as rh
    doc = fitz.open()
    for _ in range(7):
        doc.new_page()
    pages = rh._pdf_pages_to_jpeg(doc.tobytes())
    assert len(pages) == rh._PDF_MAX_PAGES
    assert all(p[:2] == b"\xff\xd8" for p in pages)  # JPEG


# ── photo path ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("user_id,model_total,saved_total,warns", [
    (201, ITEMS_SUM, ITEMS_SUM, False),     # normal: "Suma PLN" read
    (202, None, ITEMS_SUM, False),          # no total → items sum, logged only
    (203, 0, ITEMS_SUM, False),             # zero total → items sum
    (204, 52.94, 52.94, True),              # differs → keep total, warn the user
])
async def test_photo_totals(gemini, db_session, user_id, model_total, saved_total, warns):  # noqa: F811
    import bot.handlers.receipt as rh
    gemini.side_effect = [
        _answer(NOT_A_SCREEN),
        _answer(_receipt(model_total)),
        _answer(_normalized([i["name"] for i in ITEMS])),
    ]
    message = _Message(user_id)

    await rh.handle_receipt_photo(message, _Bot(), db_session, redis=None)

    receipt, items = await _saved(db_session, user_id)
    assert float(receipt.total) == pytest.approx(saved_total)
    assert float(receipt.total_pln) == pytest.approx(saved_total)
    preview = message.status.texts[-1]
    assert f"Итого: {saved_total:.2f} PLN".replace(".", ",") in preview
    assert (MISMATCH_WARNING in preview) is warns
    # Normalization applied: stored names normalized, preview shows raw names.
    assert {i.normalized_name for i in items} == {i["name"].title().lower() for i in ITEMS}
    assert "ChipsyCrun130-140g" in preview


async def test_photo_request_order_and_payloads(gemini, db_session):  # noqa: F811
    import bot.handlers.receipt as rh
    gemini.side_effect = [
        _answer(NOT_A_SCREEN),
        _answer(_receipt(ITEMS_SUM)),
        _answer(_normalized([i["name"] for i in ITEMS])),
    ]
    await rh.handle_receipt_photo(_Message(300), _Bot(), db_session, redis=None)
    calls = gemini.await_args_list
    assert len(calls) == 3
    assert "is_transaction_screen" in json.dumps(calls[0].kwargs["config"].response_json_schema)
    assert calls[1].kwargs["config"].response_json_schema == RECEIPT_SCHEMA
    assert json.loads(calls[2].kwargs["contents"][0]) == [i["name"] for i in ITEMS]
