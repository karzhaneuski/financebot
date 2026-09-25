"""Item-name normalization through the LLM provider layer. SDKs are mocked."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.db.models import Item, Receipt
from bot.services import llm
from bot.services.normalization import NORMALIZATION_SCHEMA, _SYSTEM, normalize_item_names
from tests.test_llm_providers import _answer, _gemini_error, gemini  # noqa: F401  (fixture)


def _items(*names):
    return [{"name": n, "quantity": 1, "total_price": 1.0, "category": "groceries"} for n in names]


GEMINI_NORMALIZED = [
    {"original": "NapMonsUltraViolet", "normalized": "Monster Energy", "volume_ml": 500},
    {"original": "Mleko 1L", "normalized": "Mleko", "volume_ml": 1000},
    {"original": "Chleb tostowy", "normalized": "Chleb Tostowy", "volume_ml": None},
]


async def test_gemini_normalizes_items_in_place(gemini):  # noqa: F811
    gemini.return_value = _answer(GEMINI_NORMALIZED)
    items = _items("NapMonsUltraViolet", "Mleko 1L", "Chleb tostowy")

    result = await normalize_item_names(items)

    assert result is items
    assert [(i["normalized_name"], i["volume_ml"]) for i in items] == [
        ("monster energy", 500), ("mleko", 1000), ("chleb tostowy", None),
    ]
    kwargs = gemini.await_args.kwargs
    assert kwargs["config"].response_json_schema == NORMALIZATION_SCHEMA
    assert kwargs["config"].system_instruction == _SYSTEM
    assert json.loads(kwargs["contents"][0]) == ["NapMonsUltraViolet", "Mleko 1L", "Chleb tostowy"]


async def test_anthropic_path_still_works(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    llm.reset_provider()
    fenced = "```json\n" + json.dumps(GEMINI_NORMALIZED[:1]) + "\n```"
    response = MagicMock()
    response.content = [SimpleNamespace(text=fenced)]
    with patch("anthropic.AsyncAnthropic") as client_cls:
        client_cls.return_value.messages.create = AsyncMock(return_value=response)
        items = await normalize_item_names(_items("NapMonsUltraViolet"))
    assert items[0]["normalized_name"] == "monster energy" and items[0]["volume_ml"] == 500
    assert client_cls.return_value.messages.create.await_args.kwargs["model"] == "claude-haiku-4-5-20251001"


def _assert_raw_fallback(items, names):
    assert [(i["normalized_name"], i["volume_ml"]) for i in items] == [(n.lower(), None) for n in names]


async def test_unavailable_provider_falls_back_quietly(gemini, caplog, capsys):  # noqa: F811
    gemini.side_effect = _gemini_error(429, "RESOURCE_EXHAUSTED", "quota exceeded")
    caplog.set_level(logging.WARNING)
    names = ["Mleko 1L", "Chleb Tostowy"]

    items = await normalize_item_names(_items(*names))

    _assert_raw_fallback(items, names)
    assert "provider unavailable" in caplog.text
    assert capsys.readouterr().err == ""  # no traceback spam for an expected condition


async def test_missing_key_falls_back():
    # conftest's offline_llm: Gemini without GEMINI_API_KEY
    items = await normalize_item_names(_items("Woda 1,5L"))
    _assert_raw_fallback(items, ["Woda 1,5L"])


@pytest.mark.parametrize("answer", [
    "{not json",                                    # invalid JSON
    json.dumps(GEMINI_NORMALIZED[:1]),               # wrong length
    json.dumps({"normalized": "x"}),                 # not an array
])
async def test_bad_answers_fall_back(gemini, answer):  # noqa: F811
    gemini.return_value = _answer(answer)
    names = ["Mleko 1L", "Chleb"]
    items = await normalize_item_names(_items(*names))
    _assert_raw_fallback(items, names)


async def test_empty_list_makes_no_call(gemini):  # noqa: F811
    assert await normalize_item_names([]) == []
    gemini.assert_not_awaited()


# ── end to end: the receipt is saved even when normalization is unavailable ──

class _Status:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class _Message:
    def __init__(self):
        self.photo = [SimpleNamespace(file_id="photo-1")]
        self.from_user = SimpleNamespace(id=77)
        self.status = _Status()

    async def answer(self, text, **kwargs):
        return self.status


class _Bot:
    async def download(self, photo, destination):
        destination.write(b"jpeg")


async def test_receipt_saved_with_raw_names_when_normalization_unavailable(db_session, monkeypatch):
    import bot.handlers.receipt as rh

    async def _no_screenshot(*a, **k):
        return None

    async def _receipt(*a, **k):
        return {"store": "Lidl", "date": "2026-09-20", "currency": "PLN", "total": 5.0,
                "items": _items("Mleko 1L"), "total_mismatch": False}

    monkeypatch.setattr(rh, "parse_bank_transaction_screenshot", _no_screenshot)
    monkeypatch.setattr(rh, "parse_receipt", _receipt)
    # normalize_item_names is the real one; the provider is unavailable
    # (conftest: Gemini without a key).
    message = _Message()
    await rh.handle_receipt_photo(message, _Bot(), db_session, redis=None)

    assert message.status.texts[-1].startswith("✅ *Чек сохранён!*")
    receipt = (await db_session.execute(select(Receipt).where(Receipt.user_id == 77))).scalar_one()
    item = (await db_session.execute(select(Item).where(Item.receipt_id == receipt.id))).scalar_one()
    assert (item.name, item.normalized_name, item.volume_ml) == ("Mleko 1L", "mleko 1l", None)
