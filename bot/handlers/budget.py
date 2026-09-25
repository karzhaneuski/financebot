import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.crud import delete_budget, get_budgets, set_budget
from bot.i18n import _
from bot.keyboards.inline import (
    budget_category_keyboard,
    budget_delete_confirm_keyboard,
    budget_list_keyboard,
)
from bot.services.budget import get_budget_status_text, get_budget_summary
from bot.utils.formatters import format_category, format_pln

logger = logging.getLogger(__name__)
router = Router()


class BudgetSetStates(StatesGroup):
    waiting_amount = State()


async def _show_budget_list(target: Message | CallbackQuery, session: AsyncSession, user_id: int) -> None:
    month = datetime.utcnow().strftime("%Y-%m")
    budgets = await get_budgets(session, user_id, month)
    text = await get_budget_summary(session, user_id)
    keyboard = budget_list_keyboard([b.category.value for b in budgets]) if budgets else budget_category_keyboard()

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@router.message(Command("budget"))
async def cmd_budget(message: Message, session: AsyncSession) -> None:
    await _show_budget_list(message, session, message.from_user.id)


@router.callback_query(F.data.startswith("budget_set_cat:"))
async def handle_budget_set_cat(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":")[1]
    await state.update_data(category=category)
    await state.set_state(BudgetSetStates.waiting_amount)
    await callback.answer()
    await callback.message.edit_text(
        _("Enter the monthly limit for *{category}* (PLN):").format(category=format_category(category)),
        parse_mode="Markdown",
    )


@router.message(BudgetSetStates.waiting_amount)
async def handle_budget_amount(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer(_("Cancelled."))
        return

    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(_("❌ Enter a valid amount, e.g. `500` or `1500.50`"), parse_mode="Markdown")
        return

    data = await state.get_data()
    category = data["category"]
    month = datetime.utcnow().strftime("%Y-%m")

    await set_budget(session, message.from_user.id, category, amount, month)
    await session.flush()
    await state.clear()

    status = await get_budget_status_text(session, message.from_user.id, category)
    await message.answer(status, parse_mode="Markdown")


@router.callback_query(F.data == "budget_show_all")
async def handle_budget_show_all(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await _show_budget_list(callback, session, callback.from_user.id)


@router.callback_query(F.data.startswith("budget_del:"))
async def handle_budget_del(callback: CallbackQuery, session: AsyncSession) -> None:
    category = callback.data.split(":")[1]
    month = datetime.utcnow().strftime("%Y-%m")
    budgets = await get_budgets(session, callback.from_user.id, month)
    budget = next((b for b in budgets if b.category.value == category), None)

    await callback.answer()
    if not budget:
        await callback.message.edit_text(_("Budget not found."))
        return

    limit = float(budget.limit_pln)
    await callback.message.edit_text(
        _("Delete the *{category}* budget ({limit}/month)?").format(
            category=format_category(category), limit=format_pln(limit)
        ),
        parse_mode="Markdown",
        reply_markup=budget_delete_confirm_keyboard(category),
    )


@router.callback_query(F.data.startswith("budget_del_yes:"))
async def handle_budget_del_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    category = callback.data.split(":")[1]
    month = datetime.utcnow().strftime("%Y-%m")
    await delete_budget(session, callback.from_user.id, category, month)
    await session.flush()
    await callback.answer(_("Budget deleted"), show_alert=False)
    await _show_budget_list(callback, session, callback.from_user.id)


@router.callback_query(F.data == "budget_del_no")
async def handle_budget_del_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await _show_budget_list(callback, session, callback.from_user.id)


# Legacy handlers kept for backward compatibility
@router.callback_query(F.data == "budget:show")
async def handle_budget_show(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await _show_budget_list(callback, session, callback.from_user.id)


@router.callback_query(F.data == "budget:set")
async def handle_budget_set_start(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        _("💰 Choose a category to set a budget for:"),
        reply_markup=budget_category_keyboard(),
    )


@router.callback_query(BudgetSetStates.waiting_amount, F.data.startswith("cat:"))
async def handle_budget_category_legacy(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":")[1]
    await state.update_data(category=category)
    await state.set_state(BudgetSetStates.waiting_amount)
    await callback.answer()
    await callback.message.edit_text(
        _("Enter the limit for *{category}* in złoty (PLN):").format(category=format_category(category)),
        parse_mode="Markdown",
    )
