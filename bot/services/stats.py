from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.utils.formatters import format_category

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


def _times_ru(n: int) -> str:
    if 11 <= (n % 100) <= 19:
        return "раз"
    last = n % 10
    if last == 1:
        return "раз"
    if last in (2, 3, 4):
        return "раза"
    return "раз"


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
        f"📊 *Статистика за {period_label}*\n",
        f"💰 Итого потрачено: *{total:.2f} PLN*",
        f"🧾 Количество чеков: *{stats['receipt_count']}*",
    ]

    if stats["by_category"]:
        lines.append("\n📦 *По категориям:*")
        for row in stats["by_category"]:
            pct = round(row["total"] / total * 100) if total else 0
            emoji = CATEGORY_EMOJI.get(row["category"], "📦")
            name = format_category(row["category"])
            lines.append(f"  {emoji} {name} — {row['total']:.2f} PLN ({pct}%)")

    if stats["by_store"]:
        lines.append("\n🏪 *Топ магазины:*")
        for i, row in enumerate(stats["by_store"], 1):
            lines.append(f"  {i}. {row['store']} — {row['total']:.2f} PLN")

    if stats["top_items"]:
        lines.append("\n🔁 *Часто покупаемое:*")
        for row in stats["top_items"]:
            n = row["count"]
            lines.append(
                f"  • {row['name']} — куплено {n} {_times_ru(n)}, потрачено {row['total']:.2f} PLN"
            )

    cash = stats.get("cash_withdrawals_pln", 0.0)
    if cash:
        lines.append(f"\n💵 Снятия наличных: *{cash:.2f} PLN*")

    return "\n".join(lines)
