from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.i18n import _, ngettext
from bot.markers import display_name
from bot.utils.formatters import format_category, format_number

CATEGORY_EMOJI = {
    "groceries": "🛒",
    "cafe": "☕",
    "pharmacy": "💊",
    "transport": "🚗",
    "electronics": "📱",
    "clothing": "👕",
    "household": "🏠",
    "housing": "🏠",
    "other": "📦",
}


async def get_period_stats(session: AsyncSession, user_id: int, days: int) -> dict:
    receipts = await crud.get_receipts(session, user_id, days)
    total_pln = sum(r.personal_amount() for r in receipts)
    receipt_count = len(receipts)

    by_category_raw = await crud.get_spending_by_category(session, user_id, days)
    by_category = [{"category": r["category"], "total": r["total_pln"]} for r in by_category_raw]

    by_store_raw = await crud.get_spending_by_store(session, user_id, days)
    by_store = [{"store": r["store"], "total": r["total_pln"]} for r in by_store_raw[:5]]

    top_items_raw = await crud.get_items_grouped(session, user_id, days)
    top_items = [
        {"name": r["name"], "count": int(r["total_quantity"]), "total": r["total_pln"]}
        for r in top_items_raw[:5]
    ]

    daily = await crud.get_daily_spending(session, user_id, days)
    cash_withdrawals_pln = await crud.get_cash_withdrawal_total(session, user_id, days)

    return {
        "total_pln": total_pln,
        "receipt_count": receipt_count,
        "by_category": by_category,
        "by_store": by_store,
        "top_items": top_items,
        "daily": daily,
        "cash_withdrawals_pln": cash_withdrawals_pln,
    }


async def format_stats_message(stats: dict, period_label: str) -> str:
    total = stats["total_pln"]
    lines = [
        _("📊 *Statistics for {period}*").format(period=period_label) + "\n",
        _("💰 Total spent: *{amount} PLN*").format(amount=format_number(total)),
        _("🧾 Number of receipts: *{count}*").format(count=stats["receipt_count"]),
    ]

    if stats["by_category"]:
        lines.append("\n" + _("📦 *By category:*"))
        for row in stats["by_category"]:
            pct = round(row["total"] / total * 100) if total else 0
            emoji = CATEGORY_EMOJI.get(row["category"], "📦")
            name = format_category(row["category"])
            lines.append(f"  {emoji} {name} — {format_number(row['total'])} PLN ({pct}%)")

    if stats["by_store"]:
        lines.append("\n" + _("🏪 *Top stores:*"))
        for i, row in enumerate(stats["by_store"], 1):
            lines.append(f"  {i}. {display_name(row['store'])} — {format_number(row['total'])} PLN")

    if stats["top_items"]:
        lines.append("\n" + _("🔁 *Frequently bought:*"))
        for row in stats["top_items"]:
            n = row["count"]
            bought = ngettext("bought {n} time", "bought {n} times", n).format(n=n)
            lines.append(
                f"  • {display_name(row['name'])} — "
                + _("{bought}, spent {amount} PLN").format(bought=bought, amount=format_number(row['total']))
            )

    cash = stats.get("cash_withdrawals_pln", 0.0)
    if cash:
        lines.append("\n" + _("💵 Cash withdrawals: *{amount} PLN*").format(amount=format_number(cash)))

    return "\n".join(lines)
