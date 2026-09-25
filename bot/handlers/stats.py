import json
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from rapidfuzz import fuzz
from rapidfuzz import process as rfprocess
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from bot.db import crud
from bot.keyboards.inline import (
    compare_period_keyboard,
    fuzzy_matches_keyboard,
    new_period_keyboard,
    product_detail_keyboard,
    products_action_keyboard,
    products_list_keyboard,
    stats_extra_keyboard,
    stats_menu_keyboard,
    store_detail_keyboard,
    stores_list_keyboard,
)
from bot.services.charts import build_bar_chart, build_pie_chart, build_trend_chart
from bot.services.comparison import build_comparison_report
from bot.services.forecast import format_forecast_line, get_month_forecast
from bot.services.stats import format_stats_message, get_period_stats
from bot.i18n import _, __, ngettext
from bot.markers import display_name
from bot.utils.formatters import currency_flag, format_date, format_day_month, format_number

logger = logging.getLogger(__name__)
router = Router()

_PERIOD_LABELS = {
    "day": __("Day"),
    "week": __("Week"),
    "month": __("Month"),
    "year": __("Year"),
    "all": __("All time"),
}


def period_label(period: str) -> str:
    return str(_PERIOD_LABELS[period])

_PROD_LIST_KEY = "stats:pl:{uid}"
_STORE_LIST_KEY = "stats:sl:{uid}"
_SEARCH_KEY = "stats:psearch:{uid}"
_CACHE_TTL = 300


class ProductSearchState(StatesGroup):
    waiting_query = State()


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_qty(qty) -> str:
    q = float(qty)
    return str(int(q)) if q == int(q) else f"{q:.3g}"


def _fmt_volume(ml: int) -> str:
    if ml >= 1000:
        return f"{format_number(ml / 1000, 1)} L"
    return f"{ml} ml"


def _fmt_volume_long(ml: int) -> str:
    if ml >= 1000:
        return f"{format_number(ml, 0)} ml ({format_number(ml / 1000, 1)} L)"
    return f"{ml} ml"


_SUBSCRIPTION_EMOJI: list[tuple[list[str], str]] = [
    (["SPOTIFY"], "🎵"),
    (["NETFLIX"], "🎬"),
    (["YOUTUBE"], "▶️"),
    (["CLAUDE", "ANTHROPIC", "OPENAI", "CHATGPT"], "🤖"),
    (["REVOLUT", "METAL"], "💳"),
    (["APPLE", "ICLOUD"], "🍎"),
    (["GITHUB"], "🐙"),
]


def _subscription_emoji(store: str) -> str:
    upper = store.upper()
    for keywords, emoji in _SUBSCRIPTION_EMOJI:
        if any(kw in upper for kw in keywords):
            return emoji
    return "🔁"


def _fmt_next_expected(next_expected) -> str:
    if next_expected is None:
        return _("unknown (single charge)")
    return f"~{format_day_month(next_expected)}"


def _build_subscriptions_text(subs: list[dict]) -> str:
    if not subs:
        return _("📅 *Subscriptions*") + "\n\n" + _("No subscriptions found yet.")

    lines = [_("📅 *Subscriptions*") + "\n"]
    total = 0.0
    for s in subs:
        emoji = _subscription_emoji(s["store"])
        lines.append(f" {emoji} {display_name(s['store'])} — "
                     + _("{amount} PLN/mo").format(amount=format_number(s['monthly_total_pln'])))
        lines.append("    " + _("Next charge: {date}").format(date=_fmt_next_expected(s["next_expected"])))
        lines.append("")
        total += s["monthly_total_pln"]

    lines.append(" " + _("Total per month: ~{amount} PLN").format(amount=format_number(total)))
    return "\n".join(lines)


def _build_product_detail_text(
    detail: dict,
    normalized_name: str,
    label: str,
    all_time: dict | None = None,
) -> str:
    display = normalized_name.title()

    if detail["total_spent"] == 0 and all_time:
        vol = all_time.get("total_volume_ml")
        vol_str = f" · {_fmt_volume(vol)}" if vol is not None else ""
        lines = [
            f"🧃 *{display} — {label}*",
            _("No purchases in this period."),
            "",
            _("*All time:* {amount} PLN, {qty} pcs").format(
                amount=format_number(all_time['total_spent']), qty=_fmt_qty(all_time["total_qty"])
            ) + vol_str,
        ]
        return "\n".join(lines)

    total_vol = detail.get("total_volume_ml")
    lines = [
        f"🥤 *{display} — {label}*",
        _("Total spent: *{amount} PLN*").format(amount=format_number(detail['total_spent'])),
        _("Bought: *{qty} pcs*").format(qty=_fmt_qty(detail["total_qty"])),
    ]
    if total_vol is not None:
        lines.append(_("🧴 Volume: *{volume}*").format(volume=_fmt_volume_long(total_vol)))
    if detail["by_store"]:
        lines.append("\n" + _("*By store:*"))
        for s in detail["by_store"]:
            store_vol = s.get("total_volume_ml")
            vol_str = f", {_fmt_volume(store_vol)}" if store_vol is not None else ""
            lines.append(
                f"• {display_name(s['store']) or '?'} — {format_number(s['total_spent'])} PLN "
                + "(" + _("{qty} pcs").format(qty=_fmt_qty(s["total_qty"])) + vol_str + ")"
            )
    if detail["history"]:
        lines.append("\n" + _("*Purchase history:*"))
        for h in detail["history"]:
            lines.append(
                f"  — {format_date(h['date'])} — {display_name(h['store']) or '?'} — "
                + _("{qty}pcs").format(qty=_fmt_qty(h["qty"])) + f" — {format_number(h['total'])} PLN"
            )
    return "\n".join(lines)


async def _append_currency_breakdown(text: str, session: AsyncSession, user_id: int, days: int) -> str:
    """Append a per-currency breakdown block if 2+ currencies appear in the period."""
    date_to = datetime.utcnow().date()
    date_from = date_to - timedelta(days=days)
    rows = await crud.get_spending_by_currency(session, user_id, date_from, date_to)
    if len(rows) < 2:
        return text

    lines = [text, "", _("💱 *By currency:*")]
    for r in rows:
        ccy = r["currency"]
        flag = currency_flag(ccy)
        original = format_number(r['total_original'])
        if ccy == "PLN":
            lines.append(f"{flag} PLN — {original} " + _("(home currency)"))
        else:
            pln = format_number(r['total_pln'])
            lines.append(f"{flag} {ccy} — {original} (≈{pln} PLN)")
    return "\n".join(lines)


async def _append_forecast(text: str, session: AsyncSession, user_id: int) -> str:
    """Append the end-of-month forecast line. Caller restricts this to month-period views."""
    line = format_forecast_line(await get_month_forecast(session, user_id))
    if not line:
        return text
    return f"{text}\n\n{line}"


# ── /stats entry point ────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer(_("📊 Spending statistics:"), reply_markup=stats_menu_keyboard())


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message, session: AsyncSession) -> None:
    subs = await crud.get_subscriptions(session, message.from_user.id)
    await message.answer(_build_subscriptions_text(subs), parse_mode="Markdown")


@router.callback_query(F.data == "subs:show")
async def subs_show_callback(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    subs = await crud.get_subscriptions(session, call.from_user.id)
    await call.message.edit_text(_build_subscriptions_text(subs), parse_mode="Markdown")


# ── stats menu & period picker ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("smenu:"))
async def smenu_callback(call: CallbackQuery) -> None:
    screen = call.data.split(":")[1]
    await call.answer()
    await call.message.edit_text(_("📅 Choose a period:"), reply_markup=new_period_keyboard(screen))


@router.callback_query(F.data.startswith("speriod:"))
async def speriod_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _prefix, screen, period = call.data.split(":")
    await call.answer()

    if screen == "products":
        await call.message.edit_text(
            _("📦 Products — {period}").format(period=period_label(period)),
            reply_markup=products_action_keyboard(period),
        )
    elif screen == "stores":
        await _show_stores_list(call, session, redis, period)
    elif screen == "cats":
        days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(period, 36500)
        label = period_label(period)
        await call.message.edit_text(_("⏳ Building statistics..."))
        stats = await get_period_stats(session, call.from_user.id, days)
        if stats["total_pln"] == 0:
            await call.message.edit_text(_("No spending found for “{period}” 🙂").format(period=label))
            return
        text = await format_stats_message(stats, label)
        text = await _append_currency_breakdown(text, session, call.from_user.id, days)
        if period == "month":
            text = await _append_forecast(text, session, call.from_user.id)
        await call.message.edit_text(text, parse_mode="Markdown")
        if stats["by_category"]:
            chart = await build_pie_chart(stats["by_category"], _("Spending — {period}").format(period=label))
            await call.message.answer_photo(
                BufferedInputFile(chart, filename="pie.png"),
                caption=_("Chart for {period}").format(period=label),
                reply_markup=stats_extra_keyboard(days),
            )


# ── product analytics ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sprod:"))
async def sprod_action_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
    state: FSMContext,
) -> None:
    parts = call.data.split(":")
    period, action = parts[1], parts[2]
    await call.answer()

    if action == "list":
        await _show_products_list(call, session, redis, period, page=0)
    elif action == "search":
        await state.set_state(ProductSearchState.waiting_query)
        await state.update_data(period=period)
        await call.message.answer(_("🔍 Enter a product name:"))


@router.callback_query(F.data.startswith("sprodlist:"))
async def products_list_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _prefix, period, page_str = call.data.split(":")
    await call.answer()
    await _show_products_list(call, session, redis, period, page=int(page_str))


async def _show_products_list(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
    period: str,
    page: int,
) -> None:
    products = await crud.get_products_stats(session, call.from_user.id, period)
    if not products:
        await call.message.edit_text(
            _("No products found for “{period}”.").format(period=period_label(period)),
            reply_markup=products_action_keyboard(period),
        )
        return

    names = [p["normalized_name"] for p in products]
    await redis.set(
        _PROD_LIST_KEY.format(uid=call.from_user.id),
        json.dumps(names, ensure_ascii=False),
        ex=_CACHE_TTL,
    )

    label = period_label(period)
    page_size = 10
    total_pages = max(1, (len(products) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_items = products[start : start + page_size]

    lines = [_("📦 *Product statistics — {period}*").format(period=label) + "\n"]
    for i, p in enumerate(page_items, start=start + 1):
        display = p["normalized_name"].title()
        stores_str = ngettext("{n} store", "{n} stores", p["store_count"]).format(n=p["store_count"])
        vol = p.get("total_volume_ml")
        vol_str = f" · {_fmt_volume(vol)}" if vol else ""
        lines.append(
            f"{i}. {display} — {format_number(p['total_spent'])} PLN "
            + "(" + _("{qty} pcs").format(qty=_fmt_qty(p["total_qty"])) + f"{vol_str}, {stores_str})"
        )

    markup = products_list_keyboard(page_items, period, page, total_pages, start)
    await call.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=markup)


@router.callback_query(F.data.startswith("sproddet:"))
async def product_detail_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _prefix, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_PROD_LIST_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer(_("⚠️ The session has expired. Open the list again."))
        return

    names: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(names):
        await call.message.answer(_("⚠️ Product not found."))
        return

    await _show_product_detail_edit(call, session, period, names[idx])


async def _show_product_detail_edit(
    call: CallbackQuery,
    session: AsyncSession,
    period: str,
    normalized_name: str,
) -> None:
    detail = await crud.get_product_detail(session, call.from_user.id, normalized_name, period)
    logger.debug("product_detail [%s/%s]: spent=%.2f qty=%.2f vol=%s",
                normalized_name, period, detail["total_spent"], detail["total_qty"], detail.get("total_volume_ml"))
    all_time = None
    if detail["total_spent"] == 0:
        all_time = await crud.get_product_detail(session, call.from_user.id, normalized_name, "all")
        logger.debug("product_detail [%s/all]: spent=%.2f qty=%.2f vol=%s",
                    normalized_name, all_time["total_spent"], all_time["total_qty"], all_time.get("total_volume_ml"))
    text = _build_product_detail_text(detail, normalized_name, period_label(period), all_time=all_time)
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=product_detail_keyboard(period))
    except Exception:
        logger.exception("edit_text failed for %r/%s, falling back to answer", normalized_name, period)
        await call.message.answer(text, parse_mode="Markdown", reply_markup=product_detail_keyboard(period))


@router.callback_query(F.data.startswith("sprodback:"))
async def product_back_callback(call: CallbackQuery) -> None:
    period = call.data.split(":")[1]
    await call.answer()
    await call.message.edit_text(
        _("📦 Products — {period}").format(period=period_label(period)),
        reply_markup=products_action_keyboard(period),
    )


# ── product fuzzy search ──────────────────────────────────────────────────────

@router.message(ProductSearchState.waiting_query)
async def product_search_handler(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    redis: aioredis.Redis,
) -> None:
    data = await state.get_data()
    period = data.get("period", "month")
    query = (message.text or "").strip()
    await state.clear()

    all_names = await crud.get_all_normalized_names(session, message.from_user.id)
    if not all_names:
        await message.answer(_("No products found."))
        return

    results = rfprocess.extract(
        query.lower(), all_names, scorer=fuzz.WRatio, score_cutoff=60, limit=5
    )
    matches = [name for name, _score, _idx in results]

    if not matches:
        await message.answer(_("Nothing found for “{query}”. Try another name.").format(query=query))
        return

    if len(matches) == 1:
        detail = await crud.get_product_detail(session, message.from_user.id, matches[0], period)
        logger.debug("search product_detail [%s/%s]: spent=%.2f qty=%.2f vol=%s",
                    matches[0], period, detail["total_spent"], detail["total_qty"], detail.get("total_volume_ml"))
        all_time = None
        if detail["total_spent"] == 0:
            all_time = await crud.get_product_detail(session, message.from_user.id, matches[0], "all")
            logger.debug("search product_detail [%s/all]: spent=%.2f qty=%.2f vol=%s",
                        matches[0], all_time["total_spent"], all_time["total_qty"], all_time.get("total_volume_ml"))
        text = _build_product_detail_text(detail, matches[0], period_label(period), all_time=all_time)
        await message.answer(text, parse_mode="Markdown", reply_markup=product_detail_keyboard(period))
        return

    await redis.set(
        _SEARCH_KEY.format(uid=message.from_user.id),
        json.dumps(matches, ensure_ascii=False),
        ex=120,
    )
    await message.answer(
        ngettext("Found {n} match for “{query}”. Choose a product:",
                 "Found {n} matches for “{query}”. Choose a product:", len(matches)).format(n=len(matches), query=query),
        reply_markup=fuzzy_matches_keyboard(matches, period),
    )


@router.callback_query(F.data.startswith("sprod_pick:"))
async def fuzzy_pick_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _prefix, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_SEARCH_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer(_("⚠️ The session has expired. Run the search again."))
        return

    matches: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(matches):
        await call.message.answer(_("⚠️ Index error."))
        return

    await _show_product_detail_edit(call, session, period, matches[idx])


# ── store analytics ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sstorelist:"))
async def stores_list_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    period = call.data.split(":")[1]
    await call.answer()
    await _show_stores_list(call, session, redis, period)


async def _show_stores_list(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
    period: str,
) -> None:
    stores = await crud.get_store_stats_by_period(session, call.from_user.id, period)
    if not stores:
        await call.message.edit_text(_("No stores found for “{period}”.").format(period=period_label(period)))
        return

    store_names = [s["store"] for s in stores]
    await redis.set(
        _STORE_LIST_KEY.format(uid=call.from_user.id),
        json.dumps(store_names, ensure_ascii=False),
        ex=_CACHE_TTL,
    )

    label = period_label(period)
    lines = [_("🏪 *By store — {period}*").format(period=label) + "\n"]
    for i, s in enumerate(stores, 1):
        v = s["visits"]
        visits = ngettext("{n} receipt", "{n} receipts", v).format(n=v)
        lines.append(f"{i}. {display_name(s['store'])} — {format_number(s['total_pln'])} PLN ({visits})")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=stores_list_keyboard(stores, period),
    )


@router.callback_query(F.data.startswith("sstoredet:"))
async def store_detail_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _prefix, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_STORE_LIST_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer(_("⚠️ The session has expired. Open the list again."))
        return

    store_names: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(store_names):
        await call.message.answer(_("⚠️ Store not found."))
        return

    store = store_names[idx]
    products = await crud.get_store_products(session, call.from_user.id, store, period)
    label = period_label(period)

    lines = [f"🏪 *{display_name(store)} — {label}*\n"]
    if not products:
        lines.append(_("No items found."))
    else:
        for p in products:
            display = (p["normalized_name"] or "?").title()
            lines.append(f"  • {display} — {format_number(p['total_spent'])} PLN ("
                         + _("{qty} pcs").format(qty=_fmt_qty(p["total_qty"])) + ")")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=store_detail_keyboard(period),
    )


# ── legacy callbacks (kept intact) ───────────────────────────────────────────

_LEGACY_PERIOD_LABELS = {7: __("the week"), 30: __("the month"), 365: __("the year")}


def _legacy_period_label(days: int) -> str:
    label = _LEGACY_PERIOD_LABELS.get(days)
    if label is not None:
        return str(label)
    return ngettext("{n} day", "{n} days", days).format(n=days)


@router.callback_query(F.data.startswith("stats:"))
async def stats_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = _legacy_period_label(days)
    await call.answer()
    await call.message.edit_text(_("⏳ Building statistics..."))
    stats = await get_period_stats(session, call.from_user.id, days)
    if stats["total_pln"] == 0:
        await call.message.edit_text(_("No spending found for this period 🙂"))
        return
    text = await format_stats_message(stats, label)
    text = await _append_currency_breakdown(text, session, call.from_user.id, days)
    if days == 30:
        text = await _append_forecast(text, session, call.from_user.id)
    await call.message.edit_text(text, parse_mode="Markdown")
    if stats["by_category"]:
        chart = await build_pie_chart(stats["by_category"], _("Spending for {period}").format(period=label))
        await call.message.answer_photo(
            BufferedInputFile(chart, filename="pie.png"),
            caption=_("Spending chart for {period}").format(period=label),
            reply_markup=stats_extra_keyboard(days),
        )


@router.callback_query(F.data.startswith("trend:"))
async def trend_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = _legacy_period_label(days)
    await call.answer()
    stats = await get_period_stats(session, call.from_user.id, days)
    non_zero = [d for d in stats["daily"] if d["total"] > 0]
    if not non_zero:
        await call.message.answer(_("Not enough data for a daily chart."))
        return
    chart = await build_trend_chart(stats["daily"], _("Daily spending — {period}").format(period=label))
    await call.message.answer_photo(
        BufferedInputFile(chart, filename="trend.png"),
        caption=_("📈 Spending trend for {period}").format(period=label),
    )


@router.callback_query(F.data.startswith("stores:"))
async def stores_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = _legacy_period_label(days)
    await call.answer()
    stats = await get_period_stats(session, call.from_user.id, days)
    if not stats["by_store"]:
        await call.message.answer(_("No store data for this period."))
        return
    chart = await build_bar_chart(stats["by_store"], _("Top stores — {period}").format(period=label), "store", "total")
    await call.message.answer_photo(
        BufferedInputFile(chart, filename="stores.png"),
        caption=_("🏪 Spending by store for {period}").format(period=label),
    )


# ── compare periods ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "stats_compare")
async def stats_compare_callback(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        _("🔄 Choose a period to compare with the previous one:"),
        reply_markup=compare_period_keyboard(),
    )


@router.callback_query(F.data.startswith("stats_compare:"))
async def stats_compare_period_callback(
    call: CallbackQuery,
    session: AsyncSession,
) -> None:
    period_key = call.data.split(":")[1]
    await call.answer()
    await call.message.edit_text(_("⏳ Building the comparison..."))
    try:
        text = await build_comparison_report(session, call.from_user.id, period_key)
    except Exception:
        logger.exception("Failed to build the comparison")
        await call.message.edit_text(_("⚠️ Couldn't build the comparison. Please try later."))
        return
    await call.message.edit_text(text, parse_mode="Markdown")
