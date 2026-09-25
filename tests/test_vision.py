from datetime import date

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.utils.validators import validate_receipt
from bot.services.vision import _postprocess_items, parse_bank_transaction_screenshot, parse_receipt


VALID_RECEIPT = {
    "store": "Biedronka",
    "date": "2024-01-15",
    "currency": "PLN",
    "total": 25.50,
    "items": [
        {"name": "Mleko", "quantity": 2, "unit_price": 3.99, "total_price": 7.98, "category": "groceries"},
        {"name": "Chleb", "quantity": 1, "unit_price": 4.50, "total_price": 4.50, "category": "groceries"},
        {"name": "Maslo", "quantity": 1, "unit_price": 13.02, "total_price": 13.02, "category": "groceries"},
    ],
}


def test_validate_receipt_valid():
    result = validate_receipt(VALID_RECEIPT)
    assert result["store"] == "Biedronka"
    assert result["currency"] == "PLN"
    assert len(result["items"]) == 3
    assert result["total_mismatch"] is False


def test_validate_receipt_missing_keys():
    with pytest.raises(ValueError, match="обязательные поля"):
        validate_receipt({"store": "Test"})


def test_validate_receipt_empty_items():
    data = {**VALID_RECEIPT, "items": []}
    with pytest.raises(ValueError, match="непустым списком"):
        validate_receipt(data)


def test_validate_receipt_total_mismatch():
    data = {
        **VALID_RECEIPT,
        "total": 100.00,
    }
    result = validate_receipt(data)
    assert result["total_mismatch"] is True


def test_validate_receipt_unknown_category():
    data = {
        **VALID_RECEIPT,
        "items": [
            {"name": "X", "quantity": 1, "total_price": 25.50, "category": "unknown_cat"},
        ],
        "total": 25.50,
    }
    result = validate_receipt(data)
    assert result["items"][0]["category"] == "other"


def test_validate_receipt_fills_defaults():
    data = {
        "store": None,
        "date": None,
        "currency": None,
        "total": 10.0,
        "items": [{"name": "Item", "quantity": 1, "total_price": 10.0}],
    }
    result = validate_receipt(data)
    assert result["currency"] == "PLN"
    assert result["items"][0]["category"] == "other"


# --- Biedronka postprocessing tests ---

def test_biedronka_rabat_folded_into_item():
    """Biedronka 'Rabat -X' line is folded into the preceding item's total_price."""
    items = [
        {"name": "PiwoCoronaExtra450n", "quantity": 10, "unit_price": 5.99, "total_price": 59.90, "category": "groceries", "deposit_type": None},
        {"name": "Rabat", "quantity": 1, "unit_price": -29.95, "total_price": -29.95, "category": "groceries", "deposit_type": None},
    ]
    result = _postprocess_items(items)
    assert len(result) == 1
    assert result[0]["name"] == "PiwoCoronaExtra450n"
    assert result[0]["total_price"] == pytest.approx(29.95)


def test_biedronka_rabat_without_preceding_item_kept():
    """A Rabat line with no preceding item is kept as-is rather than discarded."""
    items = [
        {"name": "Rabat", "quantity": 1, "unit_price": -5.00, "total_price": -5.00, "category": "other", "deposit_type": None},
        {"name": "Chleb", "quantity": 1, "unit_price": 4.50, "total_price": 4.50, "category": "groceries", "deposit_type": None},
    ]
    result = _postprocess_items(items)
    # Rabat has no predecessor so it stays; Chleb follows normally
    assert len(result) == 2
    assert result[0]["name"] == "Rabat"
    assert result[1]["name"] == "Chleb"


def test_biedronka_multiple_items_with_one_discount():
    """Only the item directly above the Rabat line gets the discount applied."""
    items = [
        {"name": "Mleko", "quantity": 2, "unit_price": 3.99, "total_price": 7.98, "category": "groceries", "deposit_type": None},
        {"name": "PiwoCoronaExtra450n", "quantity": 10, "unit_price": 5.99, "total_price": 59.90, "category": "groceries", "deposit_type": None},
        {"name": "Rabat", "quantity": 1, "unit_price": -29.95, "total_price": -29.95, "category": "groceries", "deposit_type": None},
        {"name": "Maslo", "quantity": 1, "unit_price": 7.49, "total_price": 7.49, "category": "groceries", "deposit_type": None},
    ]
    result = _postprocess_items(items)
    assert len(result) == 3
    assert result[0]["name"] == "Mleko"
    assert result[0]["total_price"] == pytest.approx(7.98)
    assert result[1]["name"] == "PiwoCoronaExtra450n"
    assert result[1]["total_price"] == pytest.approx(29.95)
    assert result[2]["name"] == "Maslo"
    assert result[2]["total_price"] == pytest.approx(7.49)


@pytest.fixture(autouse=True)
def anthropic_provider(monkeypatch):
    """These tests exercise the Anthropic provider path with a mocked SDK."""
    from bot.config import settings
    from bot.services import llm

    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    llm.reset_provider()


def _mock_bank_tx_response(payload: str) -> MagicMock:
    mock_content = MagicMock()
    mock_content.text = payload
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    return mock_response


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_returns_dict():
    payload = (
        '{"merchant": "Grande Bistro", "amount": 15.00, "currency": "PLN", '
        '"date": "2026-05-29", "year_visible": true, "raw_title": "karta 421352******5046"}'
    )
    mock_response = _mock_bank_tx_response(payload)

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image")

    assert result is not None
    assert result["merchant"] == "Grande Bistro"
    assert result["amount"] == 15.00
    assert result["currency"] == "PLN"
    assert result["date"] == "2026-05-29"


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_eur_amount():
    """Revolut-style screen showing '-13,04 €' must be returned as EUR, not relabeled PLN."""
    payload = (
        '{"merchant": "Some Shop", "amount": 13.04, "currency": "EUR", '
        '"date": "2026-08-30", "year_visible": true, "raw_title": null}'
    )
    mock_response = _mock_bank_tx_response(payload)

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image")

    assert result is not None
    assert result["currency"] == "EUR"
    assert result["amount"] == 13.04


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_byn_amount():
    """Erste/Wallet screen showing '13,35 BYN' must be returned as BYN, not relabeled PLN."""
    payload = (
        '{"merchant": "Some Shop", "amount": 13.35, "currency": "BYN", '
        '"date": "2026-08-23", "year_visible": true, "raw_title": null}'
    )
    mock_response = _mock_bank_tx_response(payload)

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image")

    assert result is not None
    assert result["currency"] == "BYN"
    assert result["amount"] == 13.35


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_yearless_date_defaults_correctly():
    """A day+month-only date (no year visible) must resolve to the most recent
    past occurrence relative to `today`, not whatever year Claude guessed."""
    payload = (
        '{"merchant": "Some Shop", "amount": 13.35, "currency": "BYN", '
        '"date": "2025-08-23", "year_visible": false, "raw_title": null}'
    )
    mock_response = _mock_bank_tx_response(payload)

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image", today=date(2026, 9, 2))

    assert result is not None
    # Aug 23 already happened this year relative to Sep 2, so that's the most
    # recent past occurrence — not Claude's stale 2025 guess.
    assert result["date"] == "2026-08-23"


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_yearless_future_date_rolls_back():
    """A day+month combo that hasn't happened yet this year rolls back to last year."""
    payload = (
        '{"merchant": "Some Shop", "amount": 5.00, "currency": "PLN", '
        '"date": "2026-12-15", "year_visible": false, "raw_title": null}'
    )
    mock_response = _mock_bank_tx_response(payload)

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image", today=date(2026, 9, 2))

    assert result is not None
    assert result["date"] == "2025-12-15"


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_returns_none_for_receipt():
    mock_content = MagicMock()
    mock_content.text = "null"
    mock_response = MagicMock()
    mock_response.content = [mock_content]

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image")

    assert result is None


@pytest.mark.asyncio
async def test_parse_bank_transaction_screenshot_returns_none_for_bad_json():
    mock_content = MagicMock()
    mock_content.text = "not json at all"
    mock_response = MagicMock()
    mock_response.content = [mock_content]

    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.messages.create = AsyncMock(return_value=mock_response)
        result = await parse_bank_transaction_screenshot(b"fake image")

    assert result is None


@pytest.mark.asyncio
async def test_parse_receipt_invalid_json():
    mock_content = MagicMock()
    mock_content.text = "not a json at all {{{"

    mock_response = MagicMock()
    mock_response.content = [mock_content]

    with patch("anthropic.AsyncAnthropic") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError, match="invalid JSON"):
            await parse_receipt(b"fake image bytes")
