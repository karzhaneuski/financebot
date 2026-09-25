import logging
from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.i18n import _
from bot.utils.formatters import format_category, format_month, format_number, format_pln

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
        return _("No budgets set. Use /budget to add one.")

    spent_map = await crud.get_monthly_spending_by_category(session, user_id, month)

    lines = [_("📋 *Budgets for {month}:*").format(month=format_month(month)) + "\n"]
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
            f"{label:<22} {format_number(spent)} / {format_number(limit)} PLN  {bar}  {pct}%{alert}"
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
        _("✅ *Budget set!*"),
        f"{emoji} {format_category(category)} — " + _("{amount} PLN/month").format(amount=format_number(limit)),
        "",
        _("Spent this month: {amount} PLN ({pct})").format(amount=format_number(spent), pct=f"{pct}%"),
    ]
    if spent < limit:
        lines.append(_("Remaining: {amount} PLN").format(amount=format_number(limit - spent)))
    else:
        lines.append(_("Over by: {amount} PLN").format(amount=format_number(spent - limit)) + " 🚨")

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
                    _("🚨 *{category} budget exceeded!*\n"
                      "{spent} / {limit} PLN — over by {overage} PLN").format(
                        category=format_category(cat), spent=format_number(spent), limit=format_number(limit),
                        overage=format_number(overage),
                    ),
                    parse_mode="Markdown",
                )
                budget.last_notified_pct = 100
            elif pct >= 80 and last_notified < 80:
                remaining = limit - spent
                await bot.send_message(
                    user_id,
                    _("⚠️ *{category} budget: {pct} spent!*\n"
                      "{spent} / {limit} PLN — {remaining} PLN left").format(
                        category=format_category(cat), pct=f"{pct:.0f}%", spent=format_number(spent),
                        limit=format_number(limit), remaining=format_number(remaining),
                    ),
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
                _("🚨 '{category}' budget exceeded ({spent} / {limit} PLN)").format(
                    category=format_category(cat), spent=format_number(spent), limit=format_number(limit)
                )
            )
        elif spent >= limit * 0.8:
            warnings.append(
                _("⚠️ '{category}' budget {pct} used ({spent} / {limit} PLN)").format(
                    category=format_category(cat), pct=f"{pct:.0f}%", spent=format_number(spent), limit=format_number(limit)
                )
            )

    return warnings


async def build_budget_text(session: AsyncSession, user_id: int) -> str:
    return await get_budget_summary(session, user_id)
