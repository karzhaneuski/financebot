from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.utils.formatters import MONTHS_NOMINATIVE

CATEGORY_EMOJI = {
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

CATEGORY_NAME_RU = {
    "groceries": "Продукты",
    "housing": "Жильё",
    "transport": "Транспорт",
    "cafe": "Кафе/Рестораны",
    "entertainment": "Развлечения",
    "health": "Здоровье",
    "clothing": "Одежда",
    "subscriptions": "Подписки",
    "other": "Другое",
    "pharmacy": "Аптека",
    "electronics": "Электроника",
    "household": "Дом/Быт",
}


def _period_dates(
    period_key: str,
) -> tuple[tuple[date, date], tuple[date, date], str, str]:
    today = date.today()

    if period_key == "week":
        curr_mon = today - timedelta(days=today.weekday())
        curr_sun = curr_mon + timedelta(days=6)
        prev_mon = curr_mon - timedelta(days=7)
        prev_sun = prev_mon + timedelta(days=6)
        fmt = lambda d: d.strftime("%d.%m")
        curr_label = f"{fmt(curr_mon)}–{fmt(curr_sun)}"
        prev_label = f"{fmt(prev_mon)}–{fmt(prev_sun)}"
        return (curr_mon, curr_sun), (prev_mon, prev_sun), curr_label, prev_label

    if period_key == "month":
        y, m = today.year, today.month
        curr_start = date(y, m, 1)
        curr_end = date(y, m, monthrange(y, m)[1])
        if m == 1:
            py, pm = y - 1, 12
        else:
            py, pm = y, m - 1
        prev_start = date(py, pm, 1)
        prev_end = date(py, pm, monthrange(py, pm)[1])
        curr_label = MONTHS_NOMINATIVE[m - 1].capitalize()
        prev_label = MONTHS_NOMINATIVE[pm - 1].capitalize()
        if y != py:
            curr_label = f"{curr_label} {y}"
            prev_label = f"{prev_label} {py}"
        return (curr_start, curr_end), (prev_start, prev_end), curr_label, prev_label

    if period_key == "year":
        y = today.year
        return (
            (date(y, 1, 1), date(y, 12, 31)),
            (date(y - 1, 1, 1), date(y - 1, 12, 31)),
            str(y),
            str(y - 1),
        )

    raise ValueError(f"Unknown period: {period_key}")


def _arrow(curr: float, prev: float) -> str:
    if prev == 0:
        return "▲" if curr > 0 else "→"
    pct = (curr - prev) / abs(prev) * 100
    if abs(pct) < 1:
        return "→"
    return "▲" if pct > 0 else "▼"


def _diff_line(curr: float, prev: float) -> str:
    diff = curr - prev
    sign = "+" if diff >= 0 else ""
    if prev == 0:
        pct_str = ""
    else:
        pct = (curr - prev) / abs(prev) * 100
        pct_sign = "+" if pct >= 0 else ""
        pct_str = f" ({pct_sign}{pct:.0f}%)"
    arrow = _arrow(curr, prev)
    return f"   Разница: {sign}{diff:.2f} PLN{pct_str} {arrow}"


def _budget_bar(spent: float, limit: float) -> str:
    pct = spent / limit if limit > 0 else 0
    filled = min(10, int(pct * 10))
    return "▓" * filled + "░" * (10 - filled)


async def build_comparison_report(
    session: AsyncSession, user_id: int, period_key: str
) -> str:
    curr_range, prev_range, curr_label, prev_label = _period_dates(period_key)
    cf, ct = curr_range
    pf, pt = prev_range

    curr_exp = await crud.get_expenses_total_range(session, user_id, cf, ct)
    prev_exp = await crud.get_expenses_total_range(session, user_id, pf, pt)
    curr_inc = await crud.get_income_by_range(session, user_id, cf, ct)
    prev_inc = await crud.get_income_by_range(session, user_id, pf, pt)

    curr_cats_raw = await crud.get_spending_by_category_range(session, user_id, cf, ct)
    prev_cats_raw = await crud.get_spending_by_category_range(session, user_id, pf, pt)
    curr_cats = {r["category"]: r["total_pln"] for r in curr_cats_raw}
    prev_cats = {r["category"]: r["total_pln"] for r in prev_cats_raw}

    curr_stores = await crud.get_spending_by_store_range(session, user_id, cf, ct)
    prev_stores = await crud.get_spending_by_store_range(session, user_id, pf, pt)

    today = date.today()
    curr_month_str = today.strftime("%Y-%m")
    budgets = await crud.get_budgets(session, user_id, curr_month_str)
    budget_spending: dict[str, float] = {}
    if budgets:
        budget_spending = await crud.get_monthly_spending_by_category(session, user_id, curr_month_str)

    lines = [f"🔄 *Сравнение — {curr_label} vs {prev_label}*\n"]

    lines.append("💸 *Расходы:*")
    lines.append(f"   {curr_label}:  {curr_exp:.2f} PLN")
    lines.append(f"   {prev_label}: {prev_exp:.2f} PLN")
    lines.append(_diff_line(curr_exp, prev_exp))

    if curr_inc > 0 or prev_inc > 0:
        lines.append("")
        lines.append("💰 *Доходы:*")
        lines.append(f"   {curr_label}:  {curr_inc:.2f} PLN")
        lines.append(f"   {prev_label}: {prev_inc:.2f} PLN")
        lines.append(_diff_line(curr_inc, prev_inc))

    all_cats = sorted(
        set(curr_cats) | set(prev_cats),
        key=lambda c: curr_cats.get(c, 0.0),
        reverse=True,
    )
    if all_cats:
        lines.append("")
        lines.append("📂 *По категориям:*")
        for cat in all_cats:
            cv = curr_cats.get(cat, 0.0)
            pv = prev_cats.get(cat, 0.0)
            emoji = CATEGORY_EMOJI.get(cat, "📦")
            name = CATEGORY_NAME_RU.get(cat, cat)
            arrow = _arrow(cv, pv)
            if pv == 0:
                pct_str = ""
            else:
                pct = (cv - pv) / abs(pv) * 100
                s = "+" if pct >= 0 else ""
                pct_str = f"  ({s}{pct:.0f}%)"
            lines.append(f"   {emoji} *{name}*")
            lines.append(f"      {curr_label}: {cv:.2f} PLN  →  {prev_label}: {pv:.2f} PLN{pct_str} {arrow}")

    top_n = 3
    curr_top = curr_stores[:top_n]
    prev_top = prev_stores[:top_n]
    max_rows = max(len(curr_top), len(prev_top), 0)
    if max_rows > 0:
        lines.append("")
        lines.append("🏪 *Топ магазинов:*")
        lines.append(f"   *{curr_label}* | *{prev_label}*")
        for i in range(max_rows):
            c = curr_top[i] if i < len(curr_top) else None
            p = prev_top[i] if i < len(prev_top) else None
            c_str = f"{i+1}. {c['store'][:14]} {c['total_pln']:.0f} PLN" if c else ""
            p_str = f"{i+1}. {p['store'][:14]} {p['total_pln']:.0f} PLN" if p else ""
            lines.append(f"   {c_str}  |  {p_str}")

    if budgets:
        lines.append("")
        lines.append("📋 *Бюджеты (текущий месяц):*")
        for b in budgets:
            cat = b.category.value
            spent = budget_spending.get(cat, 0.0)
            limit = float(b.limit_pln)
            pct = spent / limit * 100 if limit > 0 else 0
            bar = _budget_bar(spent, limit)
            emoji = CATEGORY_EMOJI.get(cat, "📦")
            name = CATEGORY_NAME_RU.get(cat, cat)
            status = "🚨" if pct >= 100 else "⚠️" if pct >= 80 else ""
            lines.append(f"   {emoji} {name}  {spent:.0f} / {limit:.0f} PLN  {bar}  {pct:.0f}%{' ' + status if status else ''}")

    return "\n".join(lines)
