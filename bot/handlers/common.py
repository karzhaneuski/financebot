from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from bot.db.crud import delete_user_data
from bot.handlers.budget import _show_budget_list
from bot.handlers.manual import cmd_add
from bot.handlers.stats import cmd_stats

router = Router()

WELCOME_TEXT = (
    "👋 Привет! Я *FinanceBot* — твой личный трекер расходов.\n\n"
    "*Что я умею:*\n"
    "📸 Распознаю чеки по фото\n"
    "📊 Строю статистику и графики\n"
    "💰 Слежу за твоим бюджетом\n"
    "📤 Экспортирую отчёты в Excel\n\n"
    "Просто отправь мне фото чека, чтобы начать!"
)

HELP_TEXT = (
    "📋 *Список команд:*\n\n"
    "📸 *Чеки*\n"
    "Просто отправь фото чека — я всё распознаю сам.\n\n"
    "📊 /stats — статистика расходов (неделя / месяц / год)\n"
    "💰 /budget — управление бюджетами по категориям\n"
    "✏️ /add — добавить расход вручную\n"
    "📤 /export — экспорт в Excel-файл\n"
    "🔍 /search — поиск трат обычным текстом (например, «траты в Kaufland за июнь»)\n"
    "🎁 /wrapped — итоги года одной картинкой\n"
    "👥 /split — разделить чек с другими людьми\n"
    "❌ /cancel — отменить текущее действие\n\n"
    "*Категории расходов:*\n"
    "🛒 Продукты · ☕ Кафе · 💊 Аптека · 🚗 Транспорт\n"
    "📱 Электроника · 👕 Одежда · 🏠 Дом/Быт · 📦 Прочее"
)


# Callback buttons that open the section right away. (They used to be
# switch_inline_query_current_chat buttons, which only pasted
# "@bot /stats" into the input field — a text no handler matches.)
def _start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="start:stats"),
        InlineKeyboardButton(text="💰 Бюджет", callback_data="start:budget"),
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить вручную", callback_data="start:add"),
    )
    return builder.as_markup()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT, parse_mode="Markdown", reply_markup=_start_keyboard())


@router.callback_query(F.data.startswith("start:"))
async def start_menu_callback(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Same as sending /stats, /budget or /add. Replies with a new message so
    the welcome text stays; the user id comes from the callback (the message
    itself was sent by the bot)."""
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "stats":
        await cmd_stats(call.message)
    elif action == "budget":
        await _show_budget_list(call.message, session, call.from_user.id)
    elif action == "add":
        await state.clear()
        await cmd_add(call.message, state)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нет активного действия для отмены.")
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext, session: AsyncSession, redis: aioredis.Redis) -> None:
    args = (message.text or "").split(maxsplit=1)
    confirm = len(args) > 1 and args[1].strip().lower() == "confirm"

    if not confirm:
        await message.answer(
            "⚠️ Это удалит ВСЕ твои данные: транзакции, чеки, настройки.\n\n"
            "Отправь `/reset confirm` чтобы подтвердить.",
            parse_mode="Markdown",
        )
        return

    counts = await delete_user_data(session, message.from_user.id)
    await session.commit()

    # Clear FSM state and any pending Erste/foreign-merchant Redis keys
    await state.clear()
    for key_pattern in [
        f"erste_pending:{message.from_user.id}",
    ]:
        await redis.delete(key_pattern)

    await message.answer(
        "✅ Все данные удалены. Можешь начать заново — загрузи выписку.\n\n"
        f"_Удалено: {counts['receipts']} чеков, {counts['items']} позиций, "
        f"{counts['budgets']} бюджетов._",
        parse_mode="Markdown",
    )
