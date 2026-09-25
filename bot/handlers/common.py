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
from bot.i18n import _, ngettext

router = Router()

def welcome_text() -> str:
    return _(
        "👋 Hi! I'm *FinanceBot* — your personal expense tracker.\n\n"
        "*What I can do:*\n"
        "📸 Read receipts from photos\n"
        "📊 Build statistics and charts\n"
        "💰 Keep an eye on your budget\n"
        "📤 Export reports to Excel\n\n"
        "Just send me a photo of a receipt to get started!"
    )


def help_text() -> str:
    return _(
        "📋 *Commands:*\n\n"
        "📸 *Receipts*\n"
        "Just send a photo of a receipt — I'll read everything myself.\n\n"
        "📊 /stats — spending statistics (week / month / year)\n"
        "💰 /budget — budgets by category\n"
        "✏️ /add — add an expense manually\n"
        "📤 /export — export to an Excel file\n"
        "🔍 /search — find expenses by store name (e.g. “Kaufland”)\n"
        "🎁 /wrapped — your year in one picture\n"
        "👥 /split — split a receipt with other people\n"
        "🌐 /language — change language\n"
        "❌ /cancel — cancel the current action\n\n"
        "*Expense categories:*\n"
        "🛒 Groceries · ☕ Cafés · 💊 Pharmacy · 🚗 Transport\n"
        "📱 Electronics · 👕 Clothing · 🏠 Household · 📦 Other"
    )


# Callback buttons that open the section right away. (They used to be
# switch_inline_query_current_chat buttons, which only pasted
# "@bot /stats" into the input field — a text no handler matches.)
def _start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📊 Statistics"), callback_data="start:stats"),
        InlineKeyboardButton(text=_("💰 Budget"), callback_data="start:budget"),
    )
    builder.row(
        InlineKeyboardButton(text=_("➕ Add manually"), callback_data="start:add"),
    )
    return builder.as_markup()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(welcome_text(), parse_mode="Markdown", reply_markup=_start_keyboard())


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
    await message.answer(help_text(), parse_mode="Markdown")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(_("Nothing to cancel."))
        return
    await state.clear()
    await message.answer(_("❌ Action cancelled."))


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext, session: AsyncSession, redis: aioredis.Redis) -> None:
    args = (message.text or "").split(maxsplit=1)
    confirm = len(args) > 1 and args[1].strip().lower() == "confirm"

    if not confirm:
        await message.answer(
            _(
                "⚠️ This will delete ALL your data: transactions, receipts, settings.\n\n"
                "Send `/reset confirm` to confirm."
            ),
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
        _(
            "✅ All data deleted. You can start over — upload a statement.\n\n"
            "_Deleted: {receipts}, {items}, {budgets}._"
        ).format(
            receipts=ngettext("{n} receipt", "{n} receipts", counts["receipts"]).format(n=counts["receipts"]),
            items=ngettext("{n} item", "{n} items", counts["items"]).format(n=counts["items"]),
            budgets=ngettext("{n} budget", "{n} budgets", counts["budgets"]).format(n=counts["budgets"]),
        ),
        parse_mode="Markdown",
    )
