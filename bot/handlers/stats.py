import json
import logging

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
from bot.services.stats import format_stats_message, get_period_stats
from bot.utils.formatters import format_date

logger = logging.getLogger(__name__)
router = Router()

PERIOD_LABEL_MAP = {
    "day": "День",
    "week": "Неделя",
    "month": "Месяц",
    "year": "Год",
    "all": "Всё время",
}

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
        return f"{ml / 1000:.1f} L"
    return f"{ml} ml"


def _fmt_volume_long(ml: int) -> str:
    if ml >= 1000:
        return f"{ml:,} ml ({ml / 1000:.1f} L)".replace(",", " ")
    return f"{ml} ml"


def _visits_ru(n: int) -> str:
    if 11 <= (n % 100) <= 19:
        return "чеков"
    last = n % 10
    if last == 1:
        return "чек"
    if last in (2, 3, 4):
        return "чека"
    return "чеков"


def _build_product_detail_text(
    detail: dict,
    normalized_name: str,
    label: str,
    all_time: dict | None = None,
) -> str:
    display = normalized_name.title()

    if detail["total_spent"] == 0 and all_time:
        vol = all_time.get("total_volume_ml")
        print(f"[DEBUG] _build_product_detail_text fallback: all_time vol={vol}")
        vol_str = f" · {_fmt_volume(vol)}" if vol is not None else ""
        lines = [
            f"🧃 *{display} — {label}*",
            "За этот период покупок не было.",
            "",
            f"*За всё время:* {all_time['total_spent']:.2f} PLN, {_fmt_qty(all_time['total_qty'])} шт{vol_str}",
        ]
        return "\n".join(lines)

    total_vol = detail.get("total_volume_ml")
    print(f"[DEBUG] _build_product_detail_text normal: total_vol={total_vol}")
    lines = [
        f"🥤 *{display} — {label}*",
        f"Всего потрачено: *{detail['total_spent']:.2f} PLN*",
        f"Куплено: *{_fmt_qty(detail['total_qty'])} шт*",
    ]
    if total_vol is not None:
        lines.append(f"🧴 Объём: *{_fmt_volume_long(total_vol)}*")
    if detail["by_store"]:
        lines.append("\n*По магазинам:*")
        for s in detail["by_store"]:
            store_vol = s.get("total_volume_ml")
            vol_str = f", {_fmt_volume(store_vol)}" if store_vol is not None else ""
            lines.append(
                f"• {s['store'] or '?'} — {s['total_spent']:.2f} PLN ({_fmt_qty(s['total_qty'])} шт{vol_str})"
            )
    if detail["history"]:
        lines.append("\n*История покупок:*")
        for h in detail["history"]:
            lines.append(
                f"  — {format_date(h['date'])} — {h['store'] or '?'} — {_fmt_qty(h['qty'])}шт — {h['total']:.2f} PLN"
            )
    return "\n".join(lines)


# ── /stats entry point ────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer("📊 Статистика расходов:", reply_markup=stats_menu_keyboard())


# ── stats menu & period picker ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("smenu:"))
async def smenu_callback(call: CallbackQuery) -> None:
    screen = call.data.split(":")[1]
    await call.answer()
    await call.message.edit_text("📅 Выбери период:", reply_markup=new_period_keyboard(screen))


@router.callback_query(F.data.startswith("speriod:"))
async def speriod_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _, screen, period = call.data.split(":")
    await call.answer()

    if screen == "products":
        await call.message.edit_text(
            f"📦 Продукты — {PERIOD_LABEL_MAP[period]}",
            reply_markup=products_action_keyboard(period),
        )
    elif screen == "stores":
        await _show_stores_list(call, session, redis, period)
    elif screen == "cats":
        days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(period, 36500)
        label = PERIOD_LABEL_MAP[period]
        await call.message.edit_text("⏳ Формирую статистику...")
        stats = await get_period_stats(session, call.from_user.id, days)
        if stats["total_pln"] == 0:
            await call.message.edit_text(f"За период «{label}» трат не найдено 🙂")
            return
        text = await format_stats_message(stats, label)
        await call.message.edit_text(text, parse_mode="Markdown")
        if stats["by_category"]:
            chart = await build_pie_chart(stats["by_category"], f"Расходы — {label}")
            await call.message.answer_photo(
                BufferedInputFile(chart, filename="pie.png"),
                caption=f"Диаграмма за {label}",
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
        await call.message.answer("🔍 Введите название продукта:")


@router.callback_query(F.data.startswith("sprodlist:"))
async def products_list_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _, period, page_str = call.data.split(":")
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
            f"За период «{PERIOD_LABEL_MAP[period]}» продукты не найдены.",
            reply_markup=products_action_keyboard(period),
        )
        return

    names = [p["normalized_name"] for p in products]
    await redis.set(
        _PROD_LIST_KEY.format(uid=call.from_user.id),
        json.dumps(names, ensure_ascii=False),
        ex=_CACHE_TTL,
    )

    label = PERIOD_LABEL_MAP[period]
    page_size = 10
    total_pages = max(1, (len(products) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_items = products[start : start + page_size]

    lines = [f"📦 *Статистика продуктов — {label}*\n"]
    for i, p in enumerate(page_items, start=start + 1):
        display = p["normalized_name"].title()
        stores_str = f"{p['store_count']} маг."
        vol = p.get("total_volume_ml")
        vol_str = f" · {_fmt_volume(vol)}" if vol else ""
        lines.append(
            f"{i}. {display} — {p['total_spent']:.2f} PLN ({_fmt_qty(p['total_qty'])} шт{vol_str}, {stores_str})"
        )

    markup = products_list_keyboard(page_items, period, page, total_pages, start)
    await call.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=markup)


@router.callback_query(F.data.startswith("sproddet:"))
async def product_detail_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_PROD_LIST_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer("⚠️ Сессия устарела. Открой список заново.")
        return

    names: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(names):
        await call.message.answer("⚠️ Продукт не найден.")
        return

    await _show_product_detail_edit(call, session, period, names[idx])


async def _show_product_detail_edit(
    call: CallbackQuery,
    session: AsyncSession,
    period: str,
    normalized_name: str,
) -> None:
    detail = await crud.get_product_detail(session, call.from_user.id, normalized_name, period)
    logger.info("product_detail [%s/%s]: spent=%.2f qty=%.2f vol=%s",
                normalized_name, period, detail["total_spent"], detail["total_qty"], detail.get("total_volume_ml"))
    all_time = None
    if detail["total_spent"] == 0:
        all_time = await crud.get_product_detail(session, call.from_user.id, normalized_name, "all")
        logger.info("product_detail [%s/all]: spent=%.2f qty=%.2f vol=%s",
                    normalized_name, all_time["total_spent"], all_time["total_qty"], all_time.get("total_volume_ml"))
    text = _build_product_detail_text(detail, normalized_name, PERIOD_LABEL_MAP[period], all_time=all_time)
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
        f"📦 Продукты — {PERIOD_LABEL_MAP[period]}",
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
        await message.answer("Продукты не найдены.")
        return

    results = rfprocess.extract(
        query.lower(), all_names, scorer=fuzz.WRatio, score_cutoff=60, limit=5
    )
    matches = [name for name, _score, _idx in results]

    if not matches:
        await message.answer(f"Ничего не найдено по запросу «{query}». Попробуй другое название.")
        return

    if len(matches) == 1:
        detail = await crud.get_product_detail(session, message.from_user.id, matches[0], period)
        logger.info("search product_detail [%s/%s]: spent=%.2f qty=%.2f vol=%s",
                    matches[0], period, detail["total_spent"], detail["total_qty"], detail.get("total_volume_ml"))
        all_time = None
        if detail["total_spent"] == 0:
            all_time = await crud.get_product_detail(session, message.from_user.id, matches[0], "all")
            logger.info("search product_detail [%s/all]: spent=%.2f qty=%.2f vol=%s",
                        matches[0], all_time["total_spent"], all_time["total_qty"], all_time.get("total_volume_ml"))
        text = _build_product_detail_text(detail, matches[0], PERIOD_LABEL_MAP[period], all_time=all_time)
        await message.answer(text, parse_mode="Markdown", reply_markup=product_detail_keyboard(period))
        return

    await redis.set(
        _SEARCH_KEY.format(uid=message.from_user.id),
        json.dumps(matches, ensure_ascii=False),
        ex=120,
    )
    await message.answer(
        f"Найдено {len(matches)} совпадения для «{query}». Выбери продукт:",
        reply_markup=fuzzy_matches_keyboard(matches, period),
    )


@router.callback_query(F.data.startswith("sprod_pick:"))
async def fuzzy_pick_callback(
    call: CallbackQuery,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    _, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_SEARCH_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer("⚠️ Сессия устарела. Выполни поиск заново.")
        return

    matches: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(matches):
        await call.message.answer("⚠️ Ошибка индекса.")
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
        await call.message.edit_text(f"За период «{PERIOD_LABEL_MAP[period]}» магазины не найдены.")
        return

    store_names = [s["store"] for s in stores]
    await redis.set(
        _STORE_LIST_KEY.format(uid=call.from_user.id),
        json.dumps(store_names, ensure_ascii=False),
        ex=_CACHE_TTL,
    )

    label = PERIOD_LABEL_MAP[period]
    lines = [f"🏪 *По магазинам — {label}*\n"]
    for i, s in enumerate(stores, 1):
        v = s["visits"]
        lines.append(f"{i}. {s['store']} — {s['total_pln']:.2f} PLN ({v} {_visits_ru(v)})")

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
    _, period, idx_str = call.data.split(":")
    await call.answer()

    raw = await redis.get(_STORE_LIST_KEY.format(uid=call.from_user.id))
    if not raw:
        await call.message.answer("⚠️ Сессия устарела. Открой список заново.")
        return

    store_names: list[str] = json.loads(raw)
    idx = int(idx_str)
    if idx >= len(store_names):
        await call.message.answer("⚠️ Магазин не найден.")
        return

    store = store_names[idx]
    products = await crud.get_store_products(session, call.from_user.id, store, period)
    label = PERIOD_LABEL_MAP[period]

    lines = [f"🏪 *{store} — {label}*\n"]
    if not products:
        lines.append("Товары не найдены.")
    else:
        for p in products:
            display = (p["normalized_name"] or "?").title()
            lines.append(f"  • {display} — {p['total_spent']:.2f} PLN ({_fmt_qty(p['total_qty'])} шт)")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=store_detail_keyboard(period),
    )


# ── legacy callbacks (kept intact) ───────────────────────────────────────────

PERIOD_LABELS = {7: "неделю", 30: "месяц", 365: "год"}


@router.callback_query(F.data.startswith("stats:"))
async def stats_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = PERIOD_LABELS.get(days, f"{days} дней")
    await call.answer()
    await call.message.edit_text("⏳ Формирую статистику...")
    stats = await get_period_stats(session, call.from_user.id, days)
    if stats["total_pln"] == 0:
        await call.message.edit_text("За этот период трат не найдено 🙂")
        return
    text = await format_stats_message(stats, label)
    await call.message.edit_text(text, parse_mode="Markdown")
    if stats["by_category"]:
        chart = await build_pie_chart(stats["by_category"], f"Расходы за {label}")
        await call.message.answer_photo(
            BufferedInputFile(chart, filename="pie.png"),
            caption=f"Диаграмма расходов за {label}",
            reply_markup=stats_extra_keyboard(days),
        )


@router.callback_query(F.data.startswith("trend:"))
async def trend_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = PERIOD_LABELS.get(days, f"{days} дней")
    await call.answer()
    stats = await get_period_stats(session, call.from_user.id, days)
    non_zero = [d for d in stats["daily"] if d["total"] > 0]
    if not non_zero:
        await call.message.answer("Недостаточно данных для графика по дням.")
        return
    chart = await build_trend_chart(stats["daily"], f"Расходы по дням — {label}")
    await call.message.answer_photo(
        BufferedInputFile(chart, filename="trend.png"),
        caption=f"📈 Динамика расходов за {label}",
    )


@router.callback_query(F.data.startswith("stores:"))
async def stores_callback(call: CallbackQuery, session: AsyncSession) -> None:
    days = int(call.data.split(":")[1])
    label = PERIOD_LABELS.get(days, f"{days} дней")
    await call.answer()
    stats = await get_period_stats(session, call.from_user.id, days)
    if not stats["by_store"]:
        await call.message.answer("Нет данных по магазинам за этот период.")
        return
    chart = await build_bar_chart(stats["by_store"], f"Топ магазины — {label}", "store", "total")
    await call.message.answer_photo(
        BufferedInputFile(chart, filename="stores.png"),
        caption=f"🏪 Расходы по магазинам за {label}",
    )


# ── compare periods ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "stats_compare")
async def stats_compare_callback(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "🔄 Выбери период для сравнения с предыдущим:",
        reply_markup=compare_period_keyboard(),
    )


@router.callback_query(F.data.startswith("stats_compare:"))
async def stats_compare_period_callback(
    call: CallbackQuery,
    session: AsyncSession,
) -> None:
    period_key = call.data.split(":")[1]
    await call.answer()
    await call.message.edit_text("⏳ Формирую сравнение...")
    try:
        text = await build_comparison_report(session, call.from_user.id, period_key)
    except Exception:
        logger.exception("Ошибка при формировании сравнения")
        await call.message.edit_text("⚠️ Не удалось сформировать сравнение. Попробуй позже.")
        return
    await call.message.edit_text(text, parse_mode="Markdown")
