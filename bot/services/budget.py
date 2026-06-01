import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.utils.formatters import format_category, format_month, format_pln

logger = logging.getLogger(__name__)

CATEGORY_EMOJI: dict[str, str] = {
    "groceries": "🛒",
    "housing": "🏠",
    "transport": "🚗",
    "cafe": "☕",
    "entertainment": "🎮",
    "health": "💊",
    "clothing": "👕",
    "subscriptions": "📱",
    "other": "❓",
    "pharmacy": "💊",
    "electronics": "📱",
    "household": "🏠",
}


def _progress_bar(pct: float) -> str:
    filled = min(10, round(pct / 100 * 10))
    return "▓" * filled + "░" * (10 - filled)


def _alert_icon(pct: float) -> str:
    if pct >= 100:
        return "  🚨"
    if pct >= 80:
        return "  ⚠️"
    return ""


async def get_budget_summary(session: AsyncSession, user_id: int) -> str:
    month = datetime.utcnow().strftime("%Y-%m")
    budgets = await crud.get_budgets(session, user_id, month)
    if not budgets:
        return "Бюджеты не установлены. Используй /budget чтобы добавить."

    spent_map = await crud.get_monthly_spending_by_category(session, user_id, month)

    lines = [f"📋 *Бюджеты на {format_month(month)}:*\n"]
    for b in budgets:
        cat = b.category.value
        limit = float(b.limit_pln)
        spent = spent_map.get(cat, 0.0)
        raw_pct = (spent / limit * 100) if limit else 0
        pct = max(1, round(raw_pct)) if spent > 0 else 0
        emoji = CATEGORY_EMOJI.get(cat, "")
        bar = _progress_bar(raw_pct)
        alert = _alert_icon(raw_pct)
        label = f"{emoji} {format_category(cat)}"
        lines.append(
            f"{label:<22} {spent:.2f} / {limit:.2f} PLN  {bar}  {pct}%{alert}"
        )

    return "\n".join(lines)


async def get_budget_status_text(session: AsyncSession, user_id: int, category: str) -> str:
    month = datetime.utcnow().strftime("%Y-%m")
    spent_map = await crud.get_monthly_spending_by_category(session, user_id, month)
    budgets = await crud.get_budgets(session, user_id, month)
    budget = next((b for b in budgets if b.category.value == category), None)
    if not budget:
        return ""

    limit = float(budget.limit_pln)
    spent = spent_map.get(category, 0.0)
    raw_pct = (spent / limit * 100) if limit else 0
    pct = max(1, round(raw_pct)) if spent > 0 else 0
    emoji = CATEGORY_EMOJI.get(category, "")

    lines = [
        f"✅ *Бюджет установлен!*",
        f"{emoji} {format_category(category)} — {limit:.2f} PLN/месяц",
        "",
        f"Потрачено в этом месяце: {spent:.2f} PLN ({pct}%)",
    ]
    if spent < limit:
        lines.append(f"Осталось: {limit - spent:.2f} PLN")
    else:
        lines.append(f"Превышение: {spent - limit:.2f} PLN 🚨")

    return "\n".join(lines)


async def check_and_notify_budgets(session: AsyncSession, user_id: int, bot: Bot) -> None:
    month = datetime.utcnow().strftime("%Y-%m")
    budgets = await crud.get_budgets(session, user_id, month)
    if not budgets:
        return

    spent_map = await crud.get_monthly_spending_by_category(session, user_id, month)

    for budget in budgets:
        cat = budget.category.value
        limit = float(budget.limit_pln)
        if not limit:
            continue
        spent = spent_map.get(cat, 0.0)
        pct = spent / limit * 100
        last_notified = budget.last_notified_pct or 0

        try:
            if pct >= 100 and last_notified < 100:
                overage = spent - limit
                await bot.send_message(
                    user_id,
                    f"🚨 *Бюджет на {format_category(cat)} превышен!*\n"
                    f"{spent:.2f} / {limit:.2f} PLN — превышение на {overage:.2f} PLN",
                    parse_mode="Markdown",
                )
                budget.last_notified_pct = 100
            elif pct >= 80 and last_notified < 80:
                remaining = limit - spent
                await bot.send_message(
                    user_id,
                    f"⚠️ *Бюджет на {format_category(cat)}: потрачено {pct:.0f}%!*\n"
                    f"{spent:.2f} / {limit:.2f} PLN — осталось {remaining:.2f} PLN",
                    parse_mode="Markdown",
                )
                budget.last_notified_pct = 80
        except Exception:
            logger.exception(f"Failed to send budget alert to user {user_id} for category {cat}")


# kept for backward compat
async def check_budget_alerts(session: AsyncSession, user_id: int) -> list[str]:
    month = datetime.utcnow().strftime("%Y-%m")
    budgets = await crud.get_budgets(session, user_id, month)
    if not budgets:
        return []

    spent_map = await crud.get_monthly_spending_by_category(session, user_id, month)

    warnings = []
    for b in budgets:
        cat = b.category.value
        limit = float(b.limit_pln)
        spent = spent_map.get(cat, 0.0)
        pct = (spent / limit * 100) if limit else 0

        if spent >= limit:
            warnings.append(
                f"🚨 Бюджет на '{format_category(cat)}' превышен ({spent:.2f} / {limit:.2f} PLN)"
            )
        elif spent >= limit * 0.8:
            warnings.append(
                f"⚠️ Бюджет на '{format_category(cat)}' использован на {pct:.0f}% ({spent:.2f} / {limit:.2f} PLN)"
            )

    return warnings


async def build_budget_text(session: AsyncSession, user_id: int) -> str:
    return await get_budget_summary(session, user_id)
