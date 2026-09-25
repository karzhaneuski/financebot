"""Every inline button must do something when tapped: carry callback_data
(no switch_inline_query/url buttons that just paste text) and be routed to
a callback handler. Plus the /start menu buttons open their sections."""
import inspect
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, User

from bot.handlers import common, reports, search
from bot.handlers.manual import ManualAddStates
from bot.keyboards import inline

# Sample arguments for keyboard factories that need them.
_SAMPLE_ARGS = {
    "new_period_keyboard": ("cats",),
    "products_action_keyboard": ("month",),
    "products_list_keyboard": ([{"normalized_name": "mleko"}, {"normalized_name": "chleb"}], "month", 1, 3, 10),
    "product_detail_keyboard": ("month",),
    "stores_list_keyboard": ([{"store": "Lidl"}], "month"),
    "store_detail_keyboard": ("month",),
    "fuzzy_matches_keyboard": (["mleko", "mleko uht"], "month"),
    "stats_extra_keyboard": (30,),
    "recat_keyboard": (1,),
    "recat_categories_keyboard": (1,),
    "budget_list_keyboard": (["groceries", "cafe"],),
    "budget_delete_confirm_keyboard": ("cafe",),
    "export_period_keyboard": ("all",),
}


def _all_keyboards() -> dict[str, InlineKeyboardMarkup]:
    boards = {}
    for name, fn in inspect.getmembers(inline, inspect.isfunction):
        if fn.__module__ != inline.__name__ or not name.endswith("_keyboard"):
            continue
        boards[name] = fn(*_SAMPLE_ARGS.get(name, ()))
    boards["common._start_keyboard"] = common._start_keyboard()
    boards["reports._reports_keyboard"] = reports._reports_keyboard(True, False, True)
    boards["search._results_keyboard"] = search._results_keyboard(1, 3)
    return boards


def _buttons():
    for board_name, markup in _all_keyboards().items():
        for row in markup.inline_keyboard:
            for button in row:
                yield board_name, button


# Built in handlers without a factory function.
_DYNAMIC_CALLBACKS = ["splitpick:1", "splittog:1:2:on", "splittog:1:2:un", "splitdone:1"]


def test_every_button_is_a_callback_button():
    offenders = [
        (board, b.text) for board, b in _buttons()
        if not b.callback_data or b.switch_inline_query is not None
        or b.switch_inline_query_current_chat is not None or b.url or b.web_app
    ]
    assert offenders == []


_DISPATCHER = None


def _dispatcher():
    """Routers attach once per process, so build the dispatcher once."""
    global _DISPATCHER
    if _DISPATCHER is None:
        from bot.main import build_dispatcher
        _DISPATCHER = build_dispatcher(fakeredis.aioredis.FakeRedis())
    return _DISPATCHER


def _callback(data: str) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=User(id=1, is_bot=False, first_name="T"),
                         chat_instance="c", data=data)


async def _is_routed(data: str) -> bool:
    """True if some callback handler's filters accept this data. State filters
    count as satisfied: such buttons are only shown in that state."""
    event = _callback(data)
    for router in _dispatcher().chain_tail:
        for handler in router.callback_query.handlers:
            for flt in handler.filters or []:
                if isinstance(flt.callback, State):
                    continue
                if not await flt.call(event):
                    break
            else:
                return True
    return False


async def test_every_callback_is_handled():
    datas = sorted({b.callback_data for _, b in _buttons()} | set(_DYNAMIC_CALLBACKS))
    unrouted = [d for d in datas if not await _is_routed(d)]
    assert unrouted == []


async def test_unknown_callback_is_not_routed():
    """Guards the checker itself."""
    assert not await _is_routed("no_such_action:1")


# ── /start menu buttons ──────────────────────────────────────────────────────

def test_start_keyboard_buttons():
    rows = common._start_keyboard().inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [("📊 Статистика", "start:stats"), ("💰 Бюджет", "start:budget")],
        [("➕ Добавить вручную", "start:add")],
    ]


class _BotMessage:
    """call.message: sent by the bot, so its from_user is the bot."""

    def __init__(self):
        self.from_user = SimpleNamespace(id=999, is_bot=True)
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))


class _Call:
    def __init__(self, data, user_id=42):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _BotMessage()
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


def _state(user_id=42) -> FSMContext:
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def test_start_stats_opens_stats_menu():
    call = _Call("start:stats")
    await common.start_menu_callback(call, _state(), session=None)
    assert call.answered
    text, markup = call.message.answers[-1]
    assert text == "📊 Статистика расходов:"
    assert markup == inline.stats_menu_keyboard()


async def test_start_budget_shows_budgets_of_the_tapping_user(db_session, monkeypatch):
    import bot.handlers.budget as budget_handlers
    seen = {}

    async def _summary(session, user_id):
        seen["user_id"] = user_id
        return "budgets"
    monkeypatch.setattr(budget_handlers, "get_budget_summary", _summary)

    call = _Call("start:budget", user_id=42)
    await common.start_menu_callback(call, _state(), session=db_session)
    assert seen["user_id"] == 42  # not the bot's id from call.message
    text, markup = call.message.answers[-1]
    assert text == "budgets"
    assert markup == inline.budget_category_keyboard()  # no budgets yet


async def test_start_add_enters_manual_entry():
    call = _Call("start:add")
    state = _state()
    await common.start_menu_callback(call, state, session=None)
    assert await state.get_state() == ManualAddStates.waiting_amount.state
    assert call.message.answers[-1][0].startswith("✏️ *Добавление расхода вручную*")
