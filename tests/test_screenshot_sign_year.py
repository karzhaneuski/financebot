"""A bank/payment screen that reaches the receipt parser (e.g. a Revolut
screen the bank-screenshot detector didn't recognise) must not be saved as a
negative expense or with a year guessed from the model's training data."""
from datetime import date

import pytest

import bot.services.vision as vision

TODAY = date(2026, 9, 1)


class _FakeProvider:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls: list[dict] = []

    async def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


@pytest.fixture
def model(monkeypatch):
    def _set(answer: dict) -> _FakeProvider:
        provider = _FakeProvider(answer)
        monkeypatch.setattr(vision, "get_provider", lambda: provider)
        return provider
    return _set


def _item(name, price, deposit_type=None):
    return {"name": name, "quantity": 1, "unit_price": price, "total_price": price,
            "category": "other", "deposit_type": deposit_type}


def _receipt(total, items, day="2026-08-23", year_visible=True, currency="PLN"):
    return {"store": "Shop", "date": day, "year_visible": year_visible, "currency": currency,
            "total": total, "items": items}


async def test_revolut_screen_parsed_as_receipt_is_positive_with_current_year(model):
    model({"store": "Revolut - План Metal", "date": "2025-08-23", "year_visible": False,
           "currency": "EUR", "total": -13.04, "items": [_item("План Metal", -13.04)]})
    result = await vision.parse_receipt(b"img", today=TODAY)
    assert result["total"] == 13.04
    assert result["date"] == "2026-08-23"
    assert [it["total_price"] for it in result["items"]] == [13.04]
    assert result["items"][0]["unit_price"] == 13.04


async def test_printed_year_is_kept(model):
    model(_receipt(10.0, [_item("Chleb", 10.0)], day="2025-08-23", year_visible=True))
    assert (await vision.parse_receipt(b"img", today=TODAY))["date"] == "2025-08-23"


async def test_future_date_moves_to_previous_year(model):
    model(_receipt(10.0, [_item("Chleb", 10.0)], day="2026-12-01", year_visible=True))
    assert (await vision.parse_receipt(b"img", today=TODAY))["date"] == "2025-12-01"


async def test_bottle_return_receipt_stays_negative(model):
    model(_receipt(-2.0, [_item("Zwrot kaucji", 2.0, deposit_type="przyjecie")]))
    result = await vision.parse_receipt(b"img", today=TODAY)
    assert result["total"] == -2.0
    assert [it["total_price"] for it in result["items"]] == [-2.0]


async def test_receipt_with_discount_line_is_untouched(model):
    model(_receipt(8.0, [_item("Kawa", 10.0), _item("Rabat", -2.0)]))
    result = await vision.parse_receipt(b"img", today=TODAY)
    assert result["total"] == 8.0
    assert [it["total_price"] for it in result["items"]] == [8.0]  # discount merged into its product


async def test_receipt_prompt_gets_todays_date(model):
    provider = model(_receipt(10.0, [_item("Chleb", 10.0)]))
    await vision.parse_receipt(b"img", today=TODAY)
    assert provider.calls[0]["system"].endswith("Today's date is 2026-09-01.")


def test_bank_screen_detection_is_not_erste_only():
    prompt = vision._BANK_TX_SYSTEM_TEMPLATE
    for marker in ("Revolut", "Completed", "Выполнено", "Szczegóły transakcji"):
        assert marker in prompt
