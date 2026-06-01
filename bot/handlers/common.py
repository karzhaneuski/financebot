from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from bot.db.crud import delete_user_data

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
    "❌ /cancel — отменить текущее действие\n\n"
    "*Категории расходов:*\n"
    "🛒 Продукты · ☕ Кафе · 💊 Аптека · 🚗 Транспорт\n"
    "📱 Электроника · 👕 Одежда · 🏠 Дом/Быт · 📦 Прочее"
)


def _start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", switch_inline_query_current_chat="/stats"),
        InlineKeyboardButton(text="💰 Бюджет", switch_inline_query_current_chat="/budget"),
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить вручную", switch_inline_query_current_chat="/add"),
    )
    return builder.as_markup()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT, parse_mode="Markdown", reply_markup=_start_keyboard())


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
