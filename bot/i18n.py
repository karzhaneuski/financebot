"""Localization core: gettext catalogs (bot/locales/<lang>/LC_MESSAGES/messages.po)
served through aiogram's I18n.

Message ids are the English texts. The active language lives in a context
variable: UserLocaleMiddleware sets it for every Telegram update, and code
running outside an update (scheduled reports, API requests) wraps its work in
``i18n.use_locale(lang)``.

Catalog workflow (run from the repo root):
    pybabel extract -F babel.cfg -k __ -k lazy_ngettext:1,2 -o bot/locales/messages.pot .
    pybabel update -i bot/locales/messages.pot -d bot/locales -D messages
    # edit the .po files, then:
    pybabel compile -d bot/locales -D messages
"""
from pathlib import Path
from typing import Any

from aiogram.types import TelegramObject
from aiogram.utils.i18n import I18n, I18nMiddleware
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiogram.utils.i18n import ngettext

LOCALES_DIR = Path(__file__).parent / "locales"

SUPPORTED_LANGUAGES: tuple[str, ...] = ("ru", "en", "pl")
# Users who used the bot before localization existed keep Russian.
LEGACY_LANGUAGE = "ru"
# New users whose Telegram language isn't supported.
FALLBACK_LANGUAGE = "en"

LANGUAGE_NAMES: dict[str, str] = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "pl": "🇵🇱 Polski",
}

i18n = I18n(path=LOCALES_DIR, default_locale=FALLBACK_LANGUAGE, domain="messages")
I18n.set_current(i18n)

__all__ = [
    "_", "__", "ngettext", "i18n", "detect_language", "current_language",
    "SUPPORTED_LANGUAGES", "LEGACY_LANGUAGE", "FALLBACK_LANGUAGE", "LANGUAGE_NAMES",
    "UserLocaleMiddleware",
]


def detect_language(language_code: str | None) -> str:
    """Map a Telegram language_code ("pl", "en-US", ...) to a supported language."""
    code = (language_code or "").split("-")[0].lower()
    return code if code in SUPPORTED_LANGUAGES else FALLBACK_LANGUAGE


def current_language() -> str:
    return i18n.current_locale


class UserLocaleMiddleware(I18nMiddleware):
    """Resolves the language stored for the user (creating the user row on
    first contact). Must be registered after the DB session middleware."""

    async def get_locale(self, event: TelegramObject, data: dict[str, Any]) -> str:
        from bot.db import crud

        tg_user = data.get("event_from_user")
        session = data.get("session")
        if tg_user is None or session is None:
            return self.i18n.default_locale
        language = await crud.get_or_create_user_language(session, tg_user.id, tg_user.language_code)
        data["user_language"] = language
        return language
