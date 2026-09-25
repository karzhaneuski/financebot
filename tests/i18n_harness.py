"""Harness that drives the real dispatcher with fake Telegram updates and
records every user-visible string the bot produces.

Used by the i18n snapshot tests: the recorded output of every scenario is
compared against a golden JSON file per language, so moving strings into
gettext catalogs cannot silently change what users see.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import itertools
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import matplotlib.figure
from aiogram import BaseMiddleware, Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User,
)
from matplotlib.text import Text

BOT_ID = 42
CHAT_TYPE = "private"

_current_session: contextvars.ContextVar = contextvars.ContextVar("test_db_session")
_update_ids = itertools.count(1)
_message_ids = itertools.count(1000)


# ── recording ────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def take(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        return out


RECORDER = Recorder()


def _keyboard_texts(markup: Any) -> list[list[str]] | None:
    if isinstance(markup, InlineKeyboardMarkup):
        return [[b.text for b in row] for row in markup.inline_keyboard]
    return None


def _record_method(method: TelegramMethod) -> None:
    name = type(method).__name__
    event: dict[str, Any] = {"m": name}
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str):
            event[field] = value
    if getattr(method, "show_alert", None):
        event["alert"] = True
    kb = _keyboard_texts(getattr(method, "reply_markup", None))
    if kb:
        event["kb"] = kb
    for attr in ("photo", "document"):
        f = getattr(method, attr, None)
        if f is not None and getattr(f, "filename", None):
            event["file"] = f.filename
    if name == "SetMyCommands":
        event["commands"] = [f"/{c.command} — {c.description}" for c in method.commands]
        event["language_code"] = method.language_code
        scope = method.scope
        event["scope"] = type(scope).__name__ if scope is not None else None
    if name in ("GetFile", "DeleteMessage", "AnswerCallbackQuery") and len(event) == 1:
        return  # plumbing without user-visible text
    RECORDER.add(event)


def _record_figure_texts(fig) -> None:
    texts = [t.get_text() for t in fig.findobj(Text) if t.get_text().strip()]
    RECORDER.add({"m": "figure", "texts": texts})


_orig_savefig = matplotlib.figure.Figure.savefig


def _savefig_spy(self, *args, **kwargs):
    _record_figure_texts(self)
    return _orig_savefig(self, *args, **kwargs)


def install_figure_spy(monkeypatch) -> None:
    monkeypatch.setattr(matplotlib.figure.Figure, "savefig", _savefig_spy)


# ── fake Telegram API ────────────────────────────────────────────────────────

def _chat(user_id: int) -> Chat:
    return Chat(id=user_id, type=CHAT_TYPE)


def _bot_message(chat_id: int, text: str | None = None) -> Message:
    return Message(
        message_id=next(_message_ids),
        date=dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.timezone.utc),
        chat=_chat(chat_id),
        from_user=User(id=BOT_ID, is_bot=True, first_name="FinanceBot"),
        text=text,
    )


class MockSession(BaseSession):
    """Answers every Bot API call locally and records what would be sent."""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        _record_method(method)
        name = type(method).__name__
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id="u", file_path="files/x")  # type: ignore[return-value]
        returning = getattr(method, "__returning__", None)
        if returning is Message or name.startswith("Send") or name.startswith("Edit"):
            chat_id = getattr(method, "chat_id", None) or 1
            return _bot_message(int(chat_id), getattr(method, "text", None)).as_(bot)  # type: ignore[return-value]
        return True  # type: ignore[return-value]

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
                             ) -> AsyncGenerator[bytes, None]:
        yield b"fake-file-bytes"

    async def close(self) -> None:
        pass


def make_bot() -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    return Bot(
        token="42:TEST",
        session=MockSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )


# ── dispatcher wiring ────────────────────────────────────────────────────────

class ContextSessionMiddleware(BaseMiddleware):
    """Injects the per-test in-memory session instead of the real engine."""

    async def __call__(self, handler, event, data):
        data["session"] = _current_session.get()
        return await handler(event, data)


_DISPATCHER = None


def get_dispatcher(redis_factory):
    """Routers are module singletons that can only be attached once, so the
    dispatcher (and the redis it is bound to) is built once per test process
    and reused. Returns (dispatcher, redis)."""
    global _DISPATCHER
    if _DISPATCHER is None:
        from bot.main import build_dispatcher

        redis = redis_factory()
        _DISPATCHER = (build_dispatcher(redis, session_middleware=ContextSessionMiddleware()), redis)
    return _DISPATCHER


@asynccontextmanager
async def use_session(session):
    token = _current_session.set(session)
    try:
        yield
    finally:
        _current_session.reset(token)


# ── update factories ─────────────────────────────────────────────────────────

def tg_user(user_id: int, language_code: str | None = "ru") -> User:
    return User(id=user_id, is_bot=False, first_name="Test", language_code=language_code)


def message_update(user: User, text: str | None = None, *, photo: bool = False,
                   document: dict | None = None) -> Update:
    kwargs: dict[str, Any] = {}
    if photo:
        kwargs["photo"] = [PhotoSize(file_id="photo-1", file_unique_id="p1", width=800, height=1200)]
    if document:
        kwargs["document"] = Document(file_id="doc-1", file_unique_id="d1", **document)
    msg = Message(
        message_id=next(_message_ids),
        date=dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.timezone.utc),
        chat=_chat(user.id),
        from_user=user,
        text=text,
        **kwargs,
    )
    return Update(update_id=next(_update_ids), message=msg)


def callback_update(user: User, data: str) -> Update:
    cq = CallbackQuery(
        id=str(next(_update_ids)),
        from_user=user,
        chat_instance="ci",
        data=data,
        message=_bot_message(user.id, "previous message"),
    )
    return Update(update_id=next(_update_ids), callback_query=cq)
