"""LLM provider layer: Gemini structured output parsing, error classification
and the user-facing "temporarily unavailable" path. All SDKs are mocked —
no real API calls."""
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.config import settings
from bot.services import llm
from bot.services.llm import InvalidLLMResponse, LLMError, LLMUnavailableError
from bot.services.vision import (
    BANK_TX_SCHEMA,
    RECEIPT_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT,
    parse_bank_transaction_screenshot,
    parse_receipt,
)

GEMINI_RECEIPT = {
    "store": "Lidl",
    "date": "2026-09-20",
    "currency": "PLN",
    "total": 17.97,
    "items": [
        {"name": "Mleko 1L", "quantity": 2, "unit_price": 3.99, "total_price": 7.98,
         "category": "groceries", "deposit_type": None},
        {"name": "Lidl Plus oferta", "quantity": 1, "unit_price": -1.00, "total_price": -1.00,
         "category": "groceries", "deposit_type": None},
        {"name": "Woda 1,5L", "quantity": 1, "unit_price": 2.49, "total_price": 2.49,
         "category": "groceries", "deposit_type": None},
        {"name": "Kaucja PET", "quantity": 1, "unit_price": 0.50, "total_price": 0.50,
         "category": "other", "deposit_type": "wydanie"},
        {"name": "Chleb", "quantity": 1, "unit_price": 8.00, "total_price": 8.00,
         "category": "groceries", "deposit_type": None},
    ],
}


@pytest.fixture
def gemini(monkeypatch):
    """Configure the Gemini provider with a mocked google-genai client.
    Returns the AsyncMock standing in for client.aio.models.generate_content."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-test-flash")
    llm.reset_provider()
    generate = AsyncMock()
    client = MagicMock()
    client.aio.models.generate_content = generate
    with patch("google.genai.Client", return_value=client) as client_cls:
        generate.client_cls = client_cls
        yield generate


def _answer(payload) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(text=text)


def _gemini_error(code: int, status: str, message: str):
    from google.genai import errors
    body = {"error": {"code": code, "message": message, "status": status}}
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, body)


# ── Gemini: receipts ─────────────────────────────────────────────────────────

async def test_gemini_receipt_parsed_into_same_structure(gemini):
    gemini.return_value = _answer(GEMINI_RECEIPT)
    result = await parse_receipt(b"jpeg-bytes")

    assert result["store"] == "Lidl" and result["currency"] == "PLN"
    names = [it["name"] for it in result["items"]]
    # Same post-processing as with Claude: discount folded into the previous
    # item, deposit turned into the net "Kaucja (netto)" line.
    assert names == ["Mleko 1L", "Woda 1,5L", "Chleb", "Kaucja (netto)"]
    assert result["items"][0]["total_price"] == pytest.approx(6.98)
    assert result["items"][-1]["total_price"] == pytest.approx(0.50)
    assert result["total_mismatch"] is False


async def test_gemini_request_uses_prompt_schema_image_and_model(gemini):
    gemini.return_value = _answer(GEMINI_RECEIPT)
    await parse_receipt(b"jpeg-bytes")

    gemini.client_cls.assert_called_once_with(api_key="test-gemini-key")
    kwargs = gemini.await_args.kwargs
    assert kwargs["model"] == "gemini-test-flash"
    config = kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == RECEIPT_SCHEMA
    assert config.system_instruction == SYSTEM_PROMPT
    image, prompt = kwargs["contents"]
    assert image.inline_data.data == b"jpeg-bytes"
    assert image.inline_data.mime_type == "image/jpeg"
    assert prompt == USER_PROMPT


def test_receipt_schema_matches_the_prompt_structure():
    """The schema must describe exactly the JSON the prompt asks for, so the
    rest of the pipeline sees the same shape from every provider."""
    example = json.loads(re.search(r"\{[\s\S]*\}", USER_PROMPT).group(0))
    assert set(RECEIPT_SCHEMA["properties"]) == set(example)
    item_example = example["items"][0]
    assert set(RECEIPT_SCHEMA["properties"]["items"]["items"]["properties"]) == set(item_example)


async def test_gemini_invalid_json_is_an_unreadable_receipt(gemini):
    gemini.return_value = _answer("{not json")
    with pytest.raises(ValueError):
        await parse_receipt(b"jpeg-bytes")


async def test_gemini_empty_answer_is_an_unreadable_receipt(gemini):
    gemini.return_value = SimpleNamespace(text=None)  # e.g. blocked by safety filters
    with pytest.raises(ValueError):
        await parse_receipt(b"jpeg-bytes")


# ── Gemini: bank screenshots ─────────────────────────────────────────────────

async def test_gemini_bank_screenshot_parsed(gemini):
    gemini.return_value = _answer({
        "is_transaction_screen": True, "merchant": "Grande Bistro", "amount": 15.0,
        "currency": "eur", "date": "2026-08-23", "year_visible": False, "raw_title": None,
    })
    from datetime import date
    result = await parse_bank_transaction_screenshot(b"png", today=date(2026, 9, 2))
    assert result == {"merchant": "Grande Bistro", "amount": 15.0, "currency": "EUR",
                      "date": "2026-08-23", "raw_title": None}
    config = gemini.await_args.kwargs["config"]
    assert config.response_json_schema == BANK_TX_SCHEMA
    assert "is_transaction_screen" in config.system_instruction  # the null-root hint


async def test_gemini_not_a_bank_screen_returns_none(gemini):
    gemini.return_value = _answer({
        "is_transaction_screen": False, "merchant": None, "amount": None, "currency": None,
        "date": None, "year_visible": None, "raw_title": None,
    })
    assert await parse_bank_transaction_screenshot(b"receipt photo") is None


# ── error classification ─────────────────────────────────────────────────────

@pytest.mark.parametrize("code,status,message", [
    (429, "RESOURCE_EXHAUSTED", "You exceeded your current quota"),
    (403, "PERMISSION_DENIED", "Billing account disabled"),
    (503, "UNAVAILABLE", "The model is overloaded"),
])
async def test_gemini_quota_and_outages_are_unavailable(gemini, code, status, message):
    gemini.side_effect = _gemini_error(code, status, message)
    with pytest.raises(LLMUnavailableError):
        await parse_receipt(b"jpeg-bytes")
    with pytest.raises(LLMUnavailableError):
        await parse_bank_transaction_screenshot(b"jpeg-bytes")


async def test_gemini_bad_request_is_not_unavailable(gemini):
    gemini.side_effect = _gemini_error(400, "INVALID_ARGUMENT", "Unsupported image")
    with pytest.raises(LLMError) as exc:
        await parse_receipt(b"jpeg-bytes")
    assert not isinstance(exc.value, LLMUnavailableError)


async def test_gemini_network_error_is_unavailable(gemini):
    gemini.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(LLMUnavailableError):
        await parse_receipt(b"jpeg-bytes")


def _anthropic_error(status: int, message: str):
    import anthropic
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"message": message}})
    cls = {400: anthropic.BadRequestError, 429: anthropic.RateLimitError}[status]
    return cls(message, response=response, body={"error": {"message": message}})


@pytest.mark.parametrize("status,message", [
    (400, "Your credit balance is too low to access the Anthropic API."),
    (429, "Rate limit exceeded"),
])
async def test_anthropic_balance_and_rate_limit_are_unavailable(monkeypatch, status, message):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    llm.reset_provider()
    with patch("anthropic.AsyncAnthropic") as client_cls:
        client_cls.return_value.messages.create = AsyncMock(side_effect=_anthropic_error(status, message))
        with pytest.raises(LLMUnavailableError):
            await parse_receipt(b"jpeg-bytes")


async def test_missing_key_is_unavailable():
    # conftest's offline_llm: Gemini selected, no GEMINI_API_KEY
    with pytest.raises(LLMUnavailableError):
        await parse_receipt(b"jpeg-bytes")


def test_invalid_response_is_a_value_error():
    assert issubclass(InvalidLLMResponse, ValueError)


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    with pytest.raises(LLMError):
        llm.get_provider()


# ── search parser via Gemini ─────────────────────────────────────────────────

async def test_gemini_search_query_parsed(gemini):
    from datetime import date

    from bot.services.search_parser import SEARCH_SCHEMA, parse_search_query
    gemini.return_value = _answer({
        "merchant": "Kaufland", "category": None, "date_from": "2026-06-01", "date_to": "2026-06-30",
        "amount_min": 200, "amount_max": None, "include_cash": False,
    })
    filters, used_fallback = await parse_search_query("траты в Kaufland дороже 200 за июнь")
    assert used_fallback is False
    assert filters == {"merchant": "Kaufland", "date_from": date(2026, 6, 1),
                       "date_to": date(2026, 6, 30), "amount_min": 200.0, "include_cash": False}
    kwargs = gemini.await_args.kwargs
    assert kwargs["config"].response_json_schema == SEARCH_SCHEMA
    assert kwargs["contents"] == ["траты в Kaufland дороже 200 за июнь"]  # no image


async def test_search_falls_back_when_quota_exhausted(gemini):
    from bot.services.search_parser import parse_search_query
    gemini.side_effect = _gemini_error(429, "RESOURCE_EXHAUSTED", "quota")
    _filters, used_fallback = await parse_search_query("дороже 100")
    assert used_fallback is True


# ── user-facing message ──────────────────────────────────────────────────────

class _Status:
    def __init__(self):
        self.texts: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class _Message:
    def __init__(self):
        self.photo = [SimpleNamespace(file_id="f1")]
        self.from_user = SimpleNamespace(id=1)
        self.status = _Status()

    async def answer(self, text, **kwargs):
        return self.status


class _Bot:
    async def download(self, photo, destination):
        destination.write(b"jpeg")


@pytest.mark.parametrize("screenshot_fails", [True, False])
async def test_photo_shows_temporarily_unavailable_message(monkeypatch, screenshot_fails):
    import bot.handlers.receipt as rh

    async def _unavailable(*a, **k):
        raise LLMUnavailableError("gemini: HTTP 429")

    async def _not_a_screenshot(*a, **k):
        return None

    monkeypatch.setattr(rh, "parse_bank_transaction_screenshot",
                        _unavailable if screenshot_fails else _not_a_screenshot)
    monkeypatch.setattr(rh, "parse_receipt", _unavailable)
    message = _Message()
    await rh.handle_receipt_photo(message, _Bot(), session=None, redis=None)

    assert message.status.texts[-1] == rh.VISION_UNAVAILABLE_TEXT
    assert "временно недоступно" in rh.VISION_UNAVAILABLE_TEXT
    assert "/add" in rh.VISION_UNAVAILABLE_TEXT and "выписк" in rh.VISION_UNAVAILABLE_TEXT


async def test_pdf_receipt_shows_temporarily_unavailable_message(monkeypatch):
    import bot.handlers.receipt as rh

    async def _unavailable(*a, **k):
        raise LLMUnavailableError("gemini: HTTP 429")
    monkeypatch.setattr(rh, "parse_receipt", _unavailable)
    monkeypatch.setattr(rh, "_pdf_pages_to_jpeg", lambda b: [b"jpeg"])
    status = _Status()
    await rh._handle_pdf_receipt(_Message(), status, b"%PDF", session=None, redis=None, bot=_Bot())
    assert status.texts[-1] == rh.VISION_UNAVAILABLE_TEXT
