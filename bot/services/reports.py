from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.services.forecast import format_forecast_line, get_month_forecast
from bot.categories import category_label
from bot.i18n import _
from bot.markers import display_name
from bot.utils.formatters import format_day_month, format_day_month_year, format_month_year, format_number

CATEGORY_EMOJI = {
    "groceries": "🛒",
    "cafe": "☕",
    "pharmacy": "💊",
    "transport": "🚗",
    "electronics": "📱",
    "clothing": "👕",
    "household": "🏠",
    "housing": "🏠",
    "entertainment": "🎭",
    "health": "💚",
    "subscriptions": "📡",
    "other": "❓",
}


def _progress_bar(current: float, limit: float, width: int = 10) -> str:
    if limit <= 0:
        return "░" * width
    filled = round(min(current / limit, 1.0) * width)
    return "▓" * filled + "░" * (width - filled)


def _fmt(amount: float) -> str:
    return format_number(amount)


def _receipt_primary_emoji(receipt) -> str:
    if receipt.category:
        return CATEGORY_EMOJI.get(receipt.category.value, "❓")
    if receipt.items:
        return CATEGORY_EMOJI.get(receipt.items[0].category.value, "❓")
    return "🧾"


async def build_daily_report(session: AsyncSession, user_id: int, target_date: date) -> str:
    receipts = await crud.get_receipts_by_date_range(session, user_id, target_date, target_date)

    day_str = format_day_month_year(target_date)
    header = _("📅 *Daily report — {date}*").format(date=day_str) + "\n"

    if not receipts:
        body = header + "\n" + _("No spending yesterday 🎉")
    else:
        total = sum(r.personal_amount() for r in receipts)
        lines = [
            header,
            _("💸 Spent: *{amount} PLN*").format(amount=_fmt(total)),
            _("🧾 Transactions: *{count}*").format(count=len(receipts)),
            "",
            _("Expenses:"),
        ]
        for r in receipts:
            store = display_name(r.store) or _("No name")
            emoji = _receipt_primary_emoji(r)
            lines.append(f"• {store} — {_fmt(r.personal_amount())} PLN  {emoji}")
        body = "\n".join(lines)

    forecast_line = format_forecast_line(await get_month_forecast(session, user_id))
    if forecast_line:
        body += "\n\n" + forecast_line
    return body


async def build_weekly_report(
    session: AsyncSession, user_id: int, week_start: date, week_end: date
) -> str:
    total, tx_count = await crud.get_total_spending_range(session, user_id, week_start, week_end)

    start_str = format_day_month(week_start)
    end_str = format_day_month_year(week_end)
    header = _("📊 *Weekly report — {start} – {end}*").format(start=start_str, end=end_str) + "\n"

    if total == 0:
        body = header + "\n" + _("No spending this week 🎉")
        forecast_line = format_forecast_line(await get_month_forecast(session, user_id))
        if forecast_line:
            body += "\n\n" + forecast_line
        return body

    lines = [header, _("💸 Total: *{amount} PLN*").format(amount=_fmt(total)), ""]

    by_cat = await crud.get_spending_by_category_range(session, user_id, week_start, week_end)
    if by_cat:
        lines.append(_("By category:"))
        for row in by_cat:
            pct = round(row["total_pln"] / total * 100) if total else 0
            emoji = CATEGORY_EMOJI.get(row["category"], "❓")
            name = category_label(row["category"])
            lines.append(f"{emoji} {name:<14} — {_fmt(row['total_pln'])} PLN  ({pct}%)")
        lines.append("")

    by_store = await crud.get_spending_by_store_range(session, user_id, week_start, week_end)
    if by_store:
        lines.append(_("🏪 *Top stores:*"))
        for i, row in enumerate(by_store[:3], 1):
            lines.append(f"{i}. {display_name(row['store'])} — {_fmt(row['total_pln'])} PLN")
        lines.append("")

    month_str = week_end.strftime("%Y-%m")
    budgets = await crud.get_budgets(session, user_id, month_str)
    if budgets:
        spending_by_cat = await crud.get_monthly_spending_by_category(session, user_id, month_str)
        budget_lines = []
        for b in budgets:
            cat = b.category.value
            spent = spending_by_cat.get(cat, 0.0)
            limit = float(b.limit_pln)
            pct = round(spent / limit * 100) if limit else 0
            bar = _progress_bar(spent, limit)
            emoji = CATEGORY_EMOJI.get(cat, "❓")
            name = category_label(cat)
            warn = " ⚠️" if pct >= 80 else ""
            budget_lines.append(f"{emoji} {name:<12} {round(spent):>4} / {round(limit)} PLN  {bar}  {pct}%{warn}")
        if budget_lines:
            lines.append(_("📋 *Budgets:*"))
            lines.extend(budget_lines)
            lines.append("")

    prev_start = week_start - timedelta(weeks=1)
    prev_end = week_end - timedelta(weeks=1)
    prev_total, _prev_count = await crud.get_total_spending_range(session, user_id, prev_start, prev_end)
    if prev_total > 0:
        diff_pct = round((total - prev_total) / prev_total * 100)
        arrow = "📈" if diff_pct >= 0 else "📉"
        sign = "+" if diff_pct >= 0 else ""
        lines.append(_("vs last week: {diff} {arrow}  (was {amount} PLN)").format(diff=f"{sign}{diff_pct}%", arrow=arrow, amount=_fmt(prev_total)))

    forecast_line = format_forecast_line(await get_month_forecast(session, user_id))
    if forecast_line:
        lines.append("")
        lines.append(forecast_line)

    return "\n".join(lines)


async def build_monthly_report(session: AsyncSession, user_id: int, year: int, month: int) -> str:
    last_day = monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    total, tx_count = await crud.get_total_spending_range(session, user_id, month_start, month_end)
    month_str = f"{year}-{month:02d}"
    month_name = format_month_year(year, month).capitalize()
    header = _("📈 *Monthly report — {month}*").format(month=month_name) + "\n"

    if total == 0:
        return header + "\n" + _("No spending this month 🎉")

    lines = [
        header,
        _("💸 Total spent: *{amount} PLN*").format(amount=_fmt(total)),
        _("🧾 Transactions: *{count}*").format(count=tx_count),
        "",
    ]

    by_cat = await crud.get_spending_by_category_range(session, user_id, month_start, month_end)
    if by_cat:
        lines.append(_("By category:"))
        for row in by_cat:
            pct = round(row["total_pln"] / total * 100) if total else 0
            emoji = CATEGORY_EMOJI.get(row["category"], "❓")
            name = category_label(row["category"])
            lines.append(f"{emoji} {name:<14} — {_fmt(row['total_pln'])} PLN  ({pct}%)")
        lines.append("")

    by_store = await crud.get_spending_by_store_range(session, user_id, month_start, month_end)
    if by_store:
        lines.append(_("🏪 *Top 5 stores:*"))
        for i, row in enumerate(by_store[:5], 1):
            lines.append(f"{i}. {display_name(row['store'])} — {_fmt(row['total_pln'])} PLN")
        lines.append("")

    budgets = await crud.get_budgets(session, user_id, month_str)
    if budgets:
        spending_by_cat = await crud.get_monthly_spending_by_category(session, user_id, month_str)
        budget_lines = []
        for b in budgets:
            cat = b.category.value
            spent = spending_by_cat.get(cat, 0.0)
            limit = float(b.limit_pln)
            pct = round(spent / limit * 100) if limit else 0
            bar = _progress_bar(spent, limit)
            emoji = CATEGORY_EMOJI.get(cat, "❓")
            name = category_label(cat)
            warn = " 🚨" if pct > 100 else (" ⚠️" if pct >= 80 else "")
            budget_lines.append(f"{emoji} {name:<12} {round(spent):>4} / {round(limit)} PLN  {bar}  {pct}%{warn}")
        if budget_lines:
            lines.append(_("📋 *Budget status:*"))
            lines.extend(budget_lines)
            lines.append("")

    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    prev_last_day = monthrange(prev_year, prev_month)[1]
    prev_total, _prev_count = await crud.get_total_spending_range(
        session, user_id, date(prev_year, prev_month, 1), date(prev_year, prev_month, prev_last_day)
    )
    if prev_total > 0:
        diff_pct = round((total - prev_total) / prev_total * 100)
        arrow = "📈" if diff_pct >= 0 else "📉"
        sign = "+" if diff_pct >= 0 else ""
        lines.append(_("vs last month: {diff} {arrow}  (was {amount} PLN)").format(diff=f"{sign}{diff_pct}%", arrow=arrow, amount=_fmt(prev_total)))

    return "\n".join(lines)
