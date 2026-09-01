"""The save confirmation and the post-/recat edit must show a receipt's amount
identically.

They used to be formatted by two separate pieces of code: the save path
converted to PLN correctly, while recat_set_callback printed the *native*
total under a hardcoded "PLN" label — so a 13.04 EUR transaction was
confirmed as "13.04 € (≈ 56.33 zł)" and then re-rendered as "13.04 PLN"
after changing its category.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.db.crud import get_receipt_by_id
from bot.handlers.receipt import handle_receipt_photo, recat_set_callback
from bot.utils.formatters import format_items_list, format_receipt_amount
from tests.conftest import make_receipt

EUR_RATE = 4.32


class _FakeMessage:
    """Captures what the handler sends/edits instead of talking to Telegram."""

    def __init__(self):
        self.photo = [SimpleNamespace(file_id="file-eur-1")]
        self.from_user = SimpleNamespace(id=1)
        self.sent: list[str] = []
        self.status = _FakeStatusMessage()

    async def answer(self, text, **kwargs):
        self.sent.append(text)
        return self.status


class _FakeStatusMessage:
    def __init__(self):
        self.text: str | None = None
        self.reply_markup = None

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")


class _FakeCall:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _FakeStatusMessage()
        self.answers: list[str] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


class _FakeRedis:
    """convert_to_pln() reads the FX rate straight out of the cache."""

    async def get(self, key):
        return str(EUR_RATE)

    async def setex(self, *args, **kwargs):
        return True


class _FakeBot:
    async def download(self, photo, destination):
        destination.write(b"fake image bytes")


def _amount_line(text: str) -> str:
    """The '🏪 ...' line of a confirmation message."""
    return next(line for line in text.splitlines() if line.startswith("🏪"))


@pytest.mark.asyncio
async def test_recat_message_matches_save_message_for_foreign_currency(db_session):
    """A EUR bank screenshot: saving it and then changing its category must
    show the same converted amount and the same currency label both times."""
    bank_tx = {
        "merchant": "Some Shop",
        "amount": 13.04,
        "currency": "EUR",
        "date": "2026-08-30",
        "raw_title": None,
    }
    message = _FakeMessage()

    with patch(
        "bot.handlers.receipt.parse_bank_transaction_screenshot",
        AsyncMock(return_value=bank_tx),
    ), patch("bot.handlers.receipt.check_anomaly", AsyncMock(return_value=None)):
        await handle_receipt_photo(
            message, bot=_FakeBot(), session=db_session, redis=_FakeRedis()
        )

    save_text = message.status.text
    assert save_text is not None
    save_amount = _amount_line(save_text)

    # The saved row carries the native amount and the converted PLN total.
    receipt = await get_receipt_by_id(db_session, 1)
    assert receipt.currency == "EUR"
    assert float(receipt.total) == pytest.approx(13.04)
    assert float(receipt.total_pln) == pytest.approx(round(13.04 * EUR_RATE, 2))

    call = _FakeCall(f"recat_set:{receipt.id}:cafe")
    await recat_set_callback(call, session=db_session)

    recat_text = call.message.text
    assert recat_text is not None
    recat_amount = _amount_line(recat_text)

    # The regression: recat used to print "13.04 PLN" here.
    assert recat_amount == save_amount
    assert "13.04 PLN" not in recat_text
    assert "€" in recat_amount
    assert f"{round(13.04 * EUR_RATE, 2):.2f} zł" in recat_amount


@pytest.mark.asyncio
async def test_receipt_photo_amount_matches_recat_amount(db_session):
    """The OCR receipt-photo card and the post-recat edit render the amount
    through the same helper, so a foreign-currency receipt reads identically in
    both (the '💰 Итого:' line and the '🏪 ...' line carry the same value)."""
    parsed = {
        "store": "Kaufland",
        "date": "2026-08-30",
        "currency": "EUR",
        "total": 13.04,
        "items": [
            {"name": "Kaffee", "quantity": 1, "unit_price": 13.04,
             "total_price": 13.04, "category": "groceries"},
        ],
    }
    message = _FakeMessage()

    with patch(
        "bot.handlers.receipt.parse_bank_transaction_screenshot",
        AsyncMock(return_value=None),
    ), patch(
        "bot.handlers.receipt.parse_receipt", AsyncMock(return_value=parsed)
    ), patch(
        "bot.handlers.receipt.normalize_item_names", AsyncMock(return_value=None)
    ), patch("bot.handlers.receipt.check_anomaly", AsyncMock(return_value=None)):
        await handle_receipt_photo(
            message, bot=_FakeBot(), session=db_session, redis=_FakeRedis()
        )

    save_text = message.status.text
    total_line = next(l for l in save_text.splitlines() if l.startswith("💰 Итого:"))
    expected = f"13.04 € (≈ {round(13.04 * EUR_RATE, 2):.2f} zł)"
    assert total_line == f"💰 Итого: {expected}"

    receipt = await get_receipt_by_id(db_session, 1)
    call = _FakeCall(f"recat_set:{receipt.id}:groceries")
    await recat_set_callback(call, session=db_session)

    assert _amount_line(call.message.text) == f"🏪 Kaufland — {expected}"


@pytest.mark.asyncio
async def test_recat_message_for_pln_receipt_has_no_conversion_suffix(db_session):
    """A plain PLN receipt shows just the złoty amount — no '(≈ ...)' tail."""
    receipt, _ = await make_receipt(
        db_session, store="Zabka", currency="PLN", total=25.5, total_pln=25.5
    )

    call = _FakeCall(f"recat_set:{receipt.id}:groceries")
    await recat_set_callback(call, session=db_session)

    amount = _amount_line(call.message.text)
    assert amount == "🏪 Zabka — 25.50 zł"
    assert "≈" not in amount


@pytest.mark.asyncio
async def test_receipt_photo_items_use_receipt_currency(db_session):
    """Item lines carry the receipt's own currency — a EUR receipt must not
    print its items' native prices under a "PLN" label."""
    parsed = {
        "store": "Kaufland",
        "date": "2026-08-30",
        "currency": "EUR",
        "total": 20.04,
        "items": [
            {"name": "Kaffee", "quantity": 1, "unit_price": 13.04,
             "total_price": 13.04, "category": "groceries"},
            {"name": "Brot", "quantity": 2, "unit_price": 3.50,
             "total_price": 7.00, "category": "groceries"},
        ],
    }
    message = _FakeMessage()

    with patch(
        "bot.handlers.receipt.parse_bank_transaction_screenshot",
        AsyncMock(return_value=None),
    ), patch(
        "bot.handlers.receipt.parse_receipt", AsyncMock(return_value=parsed)
    ), patch(
        "bot.handlers.receipt.normalize_item_names", AsyncMock(return_value=None)
    ), patch("bot.handlers.receipt.check_anomaly", AsyncMock(return_value=None)):
        await handle_receipt_photo(
            message, bot=_FakeBot(), session=db_session, redis=_FakeRedis()
        )

    text = message.status.text
    item_lines = [line for line in text.splitlines() if line.startswith("  • ")]

    assert item_lines == ["  • Kaffee — 13.04 €", "  • Brot × 2 — 7.00 €"]
    assert "PLN" not in text


def test_format_items_list_uses_given_currency():
    items = [
        {"name": "Kaffee", "quantity": 1, "total_price": 13.04},
        {"name": "Brot", "quantity": 2, "total_price": 7.00},
    ]

    assert format_items_list(items, "EUR") == (
        "  • Kaffee — 13.04 €\n  • Brot × 2 — 7.00 €"
    )
    assert format_items_list(items, "PLN") == (
        "  • Kaffee — 13.04 zł\n  • Brot × 2 — 7.00 zł"
    )
    # Unknown codes fall back to the bare code rather than a wrong symbol.
    assert "13.04 HUF" in format_items_list(items, "HUF")


def test_format_receipt_amount_shapes():
    pln = SimpleNamespace(currency="PLN", total=25.5, total_pln=25.5)
    eur = SimpleNamespace(currency="EUR", total=13.04, total_pln=56.33)
    byn = SimpleNamespace(currency="BYN", total=13.35, total_pln=17.72)

    assert format_receipt_amount(pln) == "25.50 zł"
    assert format_receipt_amount(eur) == "13.04 € (≈ 56.33 zł)"
    assert format_receipt_amount(byn) == "13.35 Br (≈ 17.72 zł)"
