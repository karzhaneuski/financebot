from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.crud import set_user_language
from bot.i18n import LANGUAGE_NAMES, SUPPORTED_LANGUAGES, _, __, i18n

router = Router()

# Order matches the command list in /help.
_COMMANDS = [
    ("start", __("Start and short intro")),
    ("help", __("List of commands")),
    ("stats", __("Spending statistics")),
    ("subscriptions", __("Recurring subscriptions")),
    ("budget", __("Budgets by category")),
    ("add", __("Add an expense manually")),
    ("export", __("Export data")),
    ("reports", __("Scheduled report settings")),
    ("search", __("Search transactions in plain text")),
    ("wrapped", __("Year in review")),
    ("split", __("Split a receipt with other people")),
    ("language", __("Change language")),
    ("cancel", __("Cancel the current action")),
]


def bot_commands(language: str) -> list[BotCommand]:
    with i18n.use_locale(language):
        return [BotCommand(command=cmd, description=str(desc)) for cmd, desc in _COMMANDS]


async def setup_bot_commands(bot: Bot) -> None:
    """Command menu per Telegram client language; English for all others."""
    for language in SUPPORTED_LANGUAGES:
        await bot.set_my_commands(bot_commands(language), language_code=language)
    await bot.set_my_commands(bot_commands("en"))


def _language_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code in SUPPORTED_LANGUAGES:
        mark = " ✓" if code == current else ""
        builder.button(text=f"{LANGUAGE_NAMES[code]}{mark}", callback_data=f"lang:{code}")
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    await message.answer(
        _("🌐 Choose the bot language:"),
        reply_markup=_language_keyboard(i18n.current_locale),
    )


@router.callback_query(F.data.startswith("lang:"))
async def language_callback(call: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    language = call.data.split(":", 1)[1]
    if language not in SUPPORTED_LANGUAGES:
        await call.answer()
        return

    await set_user_language(session, call.from_user.id, language)
    await session.commit()
    await call.answer()

    # The update started in the old language; answer in the new one.
    with i18n.use_locale(language):
        await call.message.edit_text(
            _("✅ Language changed: {language}").format(language=LANGUAGE_NAMES[language]),
            reply_markup=_language_keyboard(language),
        )
        # Per-chat command menu so it follows the chosen language rather
        # than the Telegram client language.
        await bot.set_my_commands(bot_commands(language), scope=BotCommandScopeChat(chat_id=call.from_user.id))
