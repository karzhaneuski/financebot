import logging
from datetime import date, timedelta
from calendar import monthrange

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.db.crud import get_all_users_with_reports
from bot.db.engine import get_session
from bot.services.reports import build_daily_report, build_monthly_report, build_weekly_report

logger = logging.getLogger(__name__)
TIMEZONE = "Europe/Warsaw"


async def _send_daily(bot: Bot) -> None:
    yesterday = date.today() - timedelta(days=1)
    async with get_session() as session:
        user_ids = await get_all_users_with_reports(session, "daily")
    for user_id in user_ids:
        try:
            async with get_session() as session:
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
                text = await build_monthly_report(session, user_id, year, month)
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception:
            logger.exception("Monthly report failed for user %s", user_id)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

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
