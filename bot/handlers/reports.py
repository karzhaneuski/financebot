from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.crud import get_report_settings, set_report_setting
from bot.i18n import _, __

router = Router()

REPORT_LABELS = {
    "daily": __("Daily (02:00)"),
    "weekly": __("Weekly (Mon 02:00)"),
    "monthly": __("Monthly (1st, 02:00)"),
}


def _reports_keyboard(daily: bool, weekly: bool, monthly: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for field, label in REPORT_LABELS.items():
        enabled = {"daily": daily, "weekly": weekly, "monthly": monthly}[field]
        status = "✅" if enabled else "❌"
        action_label = _("Turn off") if enabled else _("Turn on")
        builder.row(
            InlineKeyboardButton(text=f"{status} {label}", callback_data=f"report_noop:{field}"),
            InlineKeyboardButton(text=action_label, callback_data=f"report_toggle:{field}"),
        )
    return builder.as_markup()


async def _show_settings(target: Message | CallbackQuery, session: AsyncSession, user_id: int) -> None:
    settings = await get_report_settings(session, user_id)
    keyboard = _reports_keyboard(settings.daily_enabled, settings.weekly_enabled, settings.monthly_enabled)
    text = _("⚙️ *Report settings:*")

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="Markdown", reply_markup=keyboard)


@router.message(Command("reports"))
async def cmd_reports(message: Message, session: AsyncSession) -> None:
    await _show_settings(message, session, message.from_user.id)


@router.callback_query(F.data.startswith("report_toggle:"))
async def handle_report_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    field_key = callback.data.split(":")[1]
    field_map = {
        "daily": "daily_enabled",
        "weekly": "weekly_enabled",
        "monthly": "monthly_enabled",
    }
    db_field = field_map.get(field_key)
    if db_field is None:
        await callback.answer()
        return

    settings = await get_report_settings(session, callback.from_user.id)
    current = getattr(settings, db_field)
    await set_report_setting(session, callback.from_user.id, db_field, not current)
    await session.flush()

    await callback.answer()
    await _show_settings(callback, session, callback.from_user.id)


@router.callback_query(F.data.startswith("report_noop:"))
async def handle_report_noop(callback: CallbackQuery) -> None:
    await callback.answer()
