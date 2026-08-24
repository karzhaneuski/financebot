import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.db.models import Item
from bot.services.search_parser import parse_search_query
from bot.utils.formatters import format_date

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10
SEARCH_RESULT_LIMIT = 200  # keep in sync with crud.search_transactions LIMIT
_SEARCH_RESULTS_KEY = "search:results:{uid}"
_SEARCH_TTL = 300

FALLBACK_NOTICE = (
    "⚠️ Поиск выполнен по упрощённым правилам — результаты могут быть неточными."
)

CATEGORY_EMOJI = {
    "groceries": "🛒", "cafe": "☕", "pharmacy": "💊", "transport": "🚗",
    "electronics": "📱", "clothing": "👕", "household": "🏠", "housing": "🏠",
    "entertainment": "🎮", "health": "💊", "subscriptions": "📱", "other": "❓",
}


def _results_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"srchpage:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶ Далее", callback_data=f"srchpage:{page + 1}"))
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def _receipt_emoji(receipt) -> str:
    if receipt.category:
        return CATEGORY_EMOJI.get(receipt.category.value, "❓")
    if receipt.items:
        return CATEGORY_EMOJI.get(receipt.items[0].category.value, "❓")
    return "🧾"


def _build_results_text(data: dict, page: int) -> str:
    ids: list[int] = data["ids"]
    total_sum: float = data["total_sum"]
    fallback: bool = data.get("fallback", False)

    total_pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_ids = ids[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = ["🔍 *Результаты поиска*\n"]
    for rid in page_ids:
        receipt = data["rows"].get(rid)
        if receipt is None:
            continue
        amount = receipt.personal_amount()
        store = receipt.store or "?"
        date_str = format_date(receipt.date) if receipt.date else "—"
        lines.append(f"• {date_str} — {store} — {amount:.2f} PLN {_receipt_emoji(receipt)}")

    count_word = "транзакция" if len(ids) % 10 == 1 and len(ids) % 100 != 11 else "транзакций"
    lines.append(f"\nВсего: {len(ids)} {count_word}, сумма {total_sum:.2f} PLN")
    if len(ids) >= SEARCH_RESULT_LIMIT:
        lines.append("(показаны первые 200 совпадений)")
    if fallback:
        lines.append("\n" + FALLBACK_NOTICE)
    return "\n".join(lines)


async def _render_results(
    target_message,
    session: AsyncSession,
    user_id: int,
    page: int,
    redis,
    *,
    edit: bool,
) -> None:
    raw = await redis.get(_SEARCH_RESULTS_KEY.format(uid=user_id))
    if not raw:
        await target_message.answer("⚠️ Сессия поиска устарела. Выполни /search заново.")
        return
    data = json.loads(raw)
    ids: list[int] = data["ids"]
    total_pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    # Load only the current page's rows with items eager-loaded.
    page_ids = ids[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    from sqlalchemy.orm import selectinload
    from bot.db.models import Receipt

    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id, Receipt.id.in_(page_ids))
        .options(selectinload(Receipt.items))
    )
    rows = {r.id: r for r in (await session.execute(stmt)).scalars().all()}
    data["rows"] = rows

    text = _build_results_text(data, page)
    markup = _results_keyboard(page, total_pages)
    try:
        if edit:
            await target_message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await target_message.answer(text, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        logger.exception("Failed to render search results page")


@router.message(Command("search"))
@router.message(Command("find"))
async def cmd_search(message: Message, session: AsyncSession, redis) -> None:
    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await message.answer(
            "Что искать? Например: 'траты в Kaufland за июнь' "
            "или 'покупки дороже 200 злотых в мае'"
        )
        return

    filters, used_fallback = await parse_search_query(query)
    receipts = await crud.search_transactions(
        session,
        message.from_user.id,
        merchant=filters.get("merchant"),
        category=filters.get("category"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        amount_min=filters.get("amount_min"),
        amount_max=filters.get("amount_max"),
        include_cash=filters.get("include_cash", False),
    )

    if not receipts:
        await message.answer("По этому запросу ничего не найдено 🤷")
        return

    ids = [r.id for r in receipts]
    await redis.set(
        _SEARCH_RESULTS_KEY.format(uid=message.from_user.id),
        json.dumps({
            "ids": ids[:SEARCH_RESULT_LIMIT],
            "total_sum": round(sum(r.personal_amount() for r in receipts[:SEARCH_RESULT_LIMIT]), 2),
            "fallback": used_fallback,
        }),
        ex=_SEARCH_TTL,
    )
    await _render_results(message, session, message.from_user.id, 0, redis=redis, edit=False)


@router.callback_query(F.data.startswith("srchpage:"))
async def search_page_callback(call: CallbackQuery, session: AsyncSession, redis) -> None:
    page = int(call.data.split(":")[1])
    # Answer the callback exactly once — the render path edits the message.
    await call.answer()
    await _render_results(call.message, session, call.from_user.id, page, redis=redis, edit=True)
