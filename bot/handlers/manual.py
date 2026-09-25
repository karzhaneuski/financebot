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

from bot import markers
from bot.db.crud import create_receipt
from bot.i18n import _
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
        _(
            "✏️ *Adding an expense manually*\n\n"
            "Enter the amount and currency (e.g. `25.50 PLN` or `8 EUR`):"
        ),
        parse_mode="Markdown",
    )


@router.message(ManualAddStates.waiting_amount)
async def handle_manual_amount(message: Message, state: FSMContext) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer(_("Cancelled."))
        return

    match = _AMOUNT_RE.search(message.text.upper())
    if not match:
        await message.answer(_("❌ I didn't get the format. Enter it like: `25.50 PLN`"), parse_mode="Markdown")
        return

    amount = float(match.group(1).replace(",", "."))
    currency = match.group(2).upper()

    if amount <= 0:
        await message.answer(_("❌ The amount must be greater than zero."))
        return

    await state.update_data(amount=amount, currency=currency)
    await state.set_state(ManualAddStates.waiting_store)
    await message.answer(_("Enter the store or place name (or `/skip` to skip):"), parse_mode="Markdown")


@router.message(ManualAddStates.waiting_store)
async def handle_manual_store(message: Message, state: FSMContext) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer(_("Cancelled."))
        return

    store = None if message.text.strip().lower() in ("/skip", "skip") else message.text.strip()
    await state.update_data(store=store)
    await state.set_state(ManualAddStates.waiting_category)
    await message.answer(_("Choose a category:"), reply_markup=categories_keyboard())


@router.callback_query(ManualAddStates.waiting_category, F.data.startswith("cat:"))
async def handle_manual_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":")[1]
    await state.update_data(category=category)
    await state.set_state(ManualAddStates.waiting_date)
    await callback.answer()
    await callback.message.edit_text(
        _("Enter the date as *DD.MM.YYYY* or `/skip` for today:"),
        parse_mode="Markdown",
    )


@router.message(ManualAddStates.waiting_date)
async def handle_manual_date(message: Message, bot: Bot, state: FSMContext, session: AsyncSession, redis: aioredis.Redis) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer(_("Cancelled."))
        return

    text = message.text.strip()
    if text.lower() in ("/skip", "skip"):
        expense_date = datetime.utcnow().strftime("%Y-%m-%d")
    else:
        try:
            parsed = datetime.strptime(text, "%d.%m.%Y")
            expense_date = parsed.strftime("%Y-%m-%d")
        except ValueError:
            await message.answer(_("❌ Invalid date format. Enter *DD.MM.YYYY* or `/skip`:"), parse_mode="Markdown")
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
                "name": markers.MANUAL_EXPENSE,
                "quantity": 1,
                "unit_price": amount,
                "total_price": amount,
                "category": category,
            }
        ],
    }

    await create_receipt(session, message.from_user.id, receipt_data, total_pln=total_pln)
    await state.clear()

    if store:
        saved = _("✅ Expense saved: *{amount}* at {store}").format(
            amount=format_currency(amount, currency), store=store
        )
    else:
        saved = _("✅ Expense saved: *{amount}* (no store)").format(amount=format_currency(amount, currency))
    await message.answer(
        f"{saved}\n"
        f"🏷 {format_category(category)} · 📅 {expense_date}",
        parse_mode="Markdown",
    )

    await budget_service.check_and_notify_budgets(session, message.from_user.id, bot)
