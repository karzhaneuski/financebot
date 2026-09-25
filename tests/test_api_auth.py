"""API auth: the DEV_TOKEN shortcut must be off unless DEV_MODE is set."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import bot.config
from bot.api.auth import get_telegram_user


def _settings(**overrides):
    base = dict(DEV_MODE=False, DEV_TOKEN=None, DEV_USER_ID=42, BOT_TOKEN="123:abc")
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**overrides):
        monkeypatch.setattr(bot.config, "settings", _settings(**overrides))
    return _apply


async def test_dev_token_rejected_without_dev_mode(use_settings):
    use_settings(DEV_MODE=False, DEV_TOKEN="devsecret123")
    with pytest.raises(HTTPException) as exc:
        await get_telegram_user(authorization="Bearer devsecret123")
    assert exc.value.status_code == 401


async def test_dev_token_accepted_in_dev_mode(use_settings):
    use_settings(DEV_MODE=True, DEV_TOKEN="devsecret123")
    tg_user = await get_telegram_user(authorization="Bearer devsecret123")
    assert tg_user == {"id": 42, "language_code": None}


async def test_dev_mode_without_token_does_not_open_a_path(use_settings):
    use_settings(DEV_MODE=True, DEV_TOKEN=None)
    for header in ("Bearer None", "Bearer ", "Bearer"):
        with pytest.raises(HTTPException):
            await get_telegram_user(authorization=header)


async def test_wrong_dev_token_rejected_in_dev_mode(use_settings):
    use_settings(DEV_MODE=True, DEV_TOKEN="devsecret123")
    with pytest.raises(HTTPException):
        await get_telegram_user(authorization="Bearer nope")
