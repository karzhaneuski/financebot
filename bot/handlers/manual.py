import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from bot.db.crud import create_receipt
from bot.keyboards.inline import categories_keyboard
from bot.services import budget as budget_service
from bot.services.currency import convert_to_pln
from bot.utils.formatters import format_category, format_currency, format_pln

logger = logging.getLogger(__name__)
router = Router()

_AMOUNT_RE = re.compile(r"(\d+[\.,]?\d*)\s*([A-Z]{3})", re.IGNORECASE)


class ManualAddStates(StatesGroup):
    waiting_amount = State()
    waiting_store = State()
    waiting_category = State()
    waiting_date = State()


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(ManualAddStates.waiting_amount)
    await message.answer(
        "✏️ *Добавление расхода вручную*\n\n"
        "Введи сумму и валюту (например: `25.50 PLN` или `8 EUR`):",
        parse_mode="Markdown",
    )


@router.message(ManualAddStates.waiting_amount)
async def handle_manual_amount(message: Message, state: FSMContext) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return

    match = _AMOUNT_RE.search(message.text.upper())
    if not match:
        await message.answer("❌ Не понял формат. Введи как: `25.50 PLN`", parse_mode="Markdown")
        return

    amount = float(match.group(1).replace(",", "."))
    currency = match.group(2).upper()

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше нуля.")
        return

    await state.update_data(amount=amount, currency=currency)
    await state.set_state(ManualAddStates.waiting_store)
    await message.answer("Введи название магазина или места (или `/skip` чтобы пропустить):", parse_mode="Markdown")


@router.message(ManualAddStates.waiting_store)
async def handle_manual_store(message: Message, state: FSMContext) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return

    store = None if message.text.strip().lower() in ("/skip", "skip") else message.text.strip()
    await state.update_data(store=store)
    await state.set_state(ManualAddStates.waiting_category)
    await message.answer("Выбери категорию:", reply_markup=categories_keyboard())


@router.callback_query(ManualAddStates.waiting_category, F.data.startswith("cat:"))
async def handle_manual_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":")[1]
    await state.update_data(category=category)
    await state.set_state(ManualAddStates.waiting_date)
    await callback.answer()
    await callback.message.edit_text(
        "Введи дату в формате *ДД.ММ.ГГГГ* или `/skip` для сегодня:",
        parse_mode="Markdown",
    )


@router.message(ManualAddStates.waiting_date)
async def handle_manual_date(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, redis: aioredis.Redis) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return

    text = message.text.strip()
    if text.lower() in ("/skip", "skip"):
        expense_date = datetime.utcnow().strftime("%Y-%m-%d")
    else:
        try:
            parsed = datetime.strptime(text, "%d.%m.%Y")
            expense_date = parsed.strftime("%Y-%m-%d")
        except ValueError:
            await message.answer("❌ Неверный формат даты. Введи *ДД.ММ.ГГГГ* или `/skip`:", parse_mode="Markdown")
            return

    data = await state.get_data()
    amount = data["amount"]
    currency = data["currency"]
    store = data.get("store")
    category = data["category"]

    total_pln = await convert_to_pln(amount, currency, redis)

    receipt_data = {
        "store": store,
        "date": expense_date,
        "currency": currency,
        "total": amount,
        "items": [
            {
                "name": "Расход",
                "quantity": 1,
                "unit_price": amount,
                "total_price": amount,
                "category": category,
            }
        ],
    }

    await create_receipt(session, message.from_user.id, receipt_data, total_pln=total_pln)
    await state.clear()

    store_str = store or "без магазина"
    await message.answer(
        f"✅ Трата записана: *{format_currency(amount, currency)}* в {store_str}\n"
        f"🏷 {format_category(category)} · 📅 {expense_date}",
        parse_mode="Markdown",
    )

    await budget_service.check_and_notify_budgets(session, message.from_user.id, bot)
