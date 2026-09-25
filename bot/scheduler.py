import logging
from datetime import date, timedelta
from calendar import monthrange

import httpx
import redis.asyncio as aioredis
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.db.crud import get_all_users_with_reports, resolve_user_language
from bot.db.engine import get_session
from bot.i18n import i18n
from bot.services.currency import FX_CACHE_TTL, FX_KEY, _fetch_rate_from_provider
from bot.services.reports import build_daily_report, build_monthly_report, build_weekly_report

logger = logging.getLogger(__name__)
TIMEZONE = "Europe/Warsaw"

# Home currencies covered by the daily refresh. Any other currency (e.g.
# Revolut's TRY/JPY) is warmed on demand by currency.get_rate()'s cold-cache
# fallback instead — this list only needs currencies that show up often
# enough to be worth pre-fetching once a day.
_FRANKFURTER_CURRENCIES = ["USD", "EUR", "CZK"]  # frankfurter.dev (ECB) doesn't cover BYN
_FALLBACK_ONLY_CURRENCIES = ["BYN"]  # fetched via exchangerate-api.com instead


async def _refresh_fx_rates(redis: aioredis.Redis) -> None:
    """Pre-warm fx_rate:{CCY} so the bot never blocks a receipt/report on a live fetch.

    PLN/USD/EUR/CZK come from frankfurter.dev (free, ECB-sourced, no API key).
    BYN isn't published by the ECB, so it falls back to exchangerate-api.com,
    same as currency.py's cold-cache path.
    """
    await redis.setex(FX_KEY.format(ccy="PLN"), FX_CACHE_TTL, "1.0")

    refreshed: list[str] = ["PLN"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.frankfurter.dev/v1/latest",
                params={"base": "PLN", "symbols": ",".join(_FRANKFURTER_CURRENCIES)},
            )
            resp.raise_for_status()
            data = resp.json()
        for ccy, pln_to_ccy in data["rates"].items():
            rate_to_pln = 1 / float(pln_to_ccy)
            await redis.setex(FX_KEY.format(ccy=ccy), FX_CACHE_TTL, str(rate_to_pln))
            refreshed.append(ccy)
    except Exception:
        logger.exception("FX refresh: frankfurter.dev fetch failed")

    for ccy in _FALLBACK_ONLY_CURRENCIES:
        try:
            rate = await _fetch_rate_from_provider(ccy, "PLN")
            await redis.setex(FX_KEY.format(ccy=ccy), FX_CACHE_TTL, str(rate))
            refreshed.append(ccy)
        except Exception:
            logger.exception("FX refresh: exchangerate-api.com fetch failed for %s", ccy)

    logger.info("FX refresh done: %s", ", ".join(refreshed))


async def _send_daily(bot: Bot) -> None:
    yesterday = date.today() - timedelta(days=1)
    async with get_session() as session:
        user_ids = await get_all_users_with_reports(session, "daily")
    for user_id in user_ids:
        try:
            async with get_session() as session:
                language = await resolve_user_language(session, user_id)
                with i18n.use_locale(language):
                    text = await build_daily_report(session, user_id, yesterday)
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception:
            logger.exception("Daily report failed for user %s", user_id)


async def _send_weekly(bot: Bot) -> None:
    # Runs Monday 02:00 — covers the previous Mon–Sun
    week_end = date.today() - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    async with get_session() as session:
        user_ids = await get_all_users_with_reports(session, "weekly")
    for user_id in user_ids:
        try:
            async with get_session() as session:
                language = await resolve_user_language(session, user_id)
                with i18n.use_locale(language):
                    text = await build_weekly_report(session, user_id, week_start, week_end)
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception:
            logger.exception("Weekly report failed for user %s", user_id)


async def _send_monthly(bot: Bot) -> None:
    # Runs 1st of month 02:00 — covers the previous month
    first_of_this_month = date.today().replace(day=1)
    prev_month_end = first_of_this_month - timedelta(days=1)
    year, month = prev_month_end.year, prev_month_end.month
    async with get_session() as session:
        user_ids = await get_all_users_with_reports(session, "monthly")
    for user_id in user_ids:
        try:
            async with get_session() as session:
                language = await resolve_user_language(session, user_id)
                with i18n.use_locale(language):
                    text = await build_monthly_report(session, user_id, year, month)
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception:
            logger.exception("Monthly report failed for user %s", user_id)


def setup_scheduler(bot: Bot, redis: aioredis.Redis) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Must run before any job that converts currency — daily/weekly/monthly
    # reports below are all at 02:00, so this runs a full hour earlier.
    scheduler.add_job(
        _refresh_fx_rates,
        CronTrigger(hour=1, minute=30, timezone=TIMEZONE),
        args=[redis],
        id="fx_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_daily,
        CronTrigger(hour=2, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="daily_report",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_weekly,
        CronTrigger(day_of_week="mon", hour=2, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="weekly_report",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_monthly,
        CronTrigger(day=1, hour=2, minute=0, timezone=TIMEZONE),
        args=[bot],
        id="monthly_report",
        replace_existing=True,
    )

    return scheduler
