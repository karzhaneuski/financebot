import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import redis.asyncio as aioredis
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

from bot.api.app import app as fastapi_app
from bot.config import settings
from bot.db.engine import engine
from bot.handlers import budget, common, export, manual, receipt, reports, search, split, stats
from bot.scheduler import setup_scheduler
from bot.middleware import DbSessionMiddleware, RedisMiddleware

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
# httpx logs every request URL at INFO; the exchangerate-api.com URL
# contains the API key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


_ALREADY_APPLIED_MARKERS = ("DuplicateTableError", "DuplicateColumnError", "ProgrammingError")


def _run_migrations() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if any(marker in result.stderr for marker in _ALREADY_APPLIED_MARKERS):
            logger.warning(
                f"Alembic migration conflict, likely already applied by another "
                f"machine; continuing:\n{result.stderr}"
            )
            return
        logger.error(f"Alembic failed:\n{result.stderr}")
        raise RuntimeError("Migration failed, shutting down")
    logger.info("Alembic migrations applied")


ROLES = ("all", "bot", "api")
HEARTBEAT_FILE = Path("/tmp/financebot-bot.heartbeat")
HEARTBEAT_INTERVAL = 30  # seconds; the container healthcheck allows 120


def role_components(role: str) -> frozenset[str]:
    """What a process started with APP_ROLE=<role> runs.

    The scheduler belongs to the bot role only: running it in the API
    container too would send every scheduled report twice. Migrations run
    in "all" (single-process/local mode); in the split production layout a
    one-shot `migrate` service applies them before bot and api start.
    """
    if role == "bot":
        return frozenset({"polling", "scheduler", "heartbeat"})
    if role == "api":
        return frozenset({"api"})
    if role == "all":
        return frozenset({"migrations", "polling", "scheduler", "api"})
    raise ValueError(f"Unknown APP_ROLE {role!r}; expected one of {', '.join(ROLES)}")


def build_dispatcher(redis: aioredis.Redis) -> Dispatcher:
    dp = Dispatcher()

    dp.update.middleware(DbSessionMiddleware())
    dp.update.middleware(RedisMiddleware(redis))

    dp.include_router(common.router)
    dp.include_router(receipt.router)
    dp.include_router(stats.router)
    dp.include_router(budget.router)
    dp.include_router(manual.router)
    dp.include_router(export.router)
    dp.include_router(reports.router)
    dp.include_router(search.router)
    dp.include_router(split.router)

    @dp.errors()
    async def global_error_handler(event: ErrorEvent) -> bool:
        logger.exception(f"Unhandled error: {event.exception}")
        try:
            await event.update.message.answer(
                "❌ Произошла непредвиденная ошибка. Попробуй ещё раз или напиши /help"
            )
        except Exception:
            pass
        return True

    return dp


async def _heartbeat() -> None:
    """Touch a file while the event loop is alive (bot container healthcheck)."""
    while True:
        HEARTBEAT_FILE.touch()
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def main(role: str | None = None) -> None:
    role = role or os.environ.get("APP_ROLE", "all")
    components = role_components(role)
    logger.info("Starting FinanceBot, role=%s: %s", role, ", ".join(sorted(components)))

    if "migrations" in components:
        _run_migrations()

    tasks = []
    redis = bot = scheduler = None

    if "polling" in components:
        redis = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        dp = build_dispatcher(redis)
        tasks.append(dp.start_polling(bot))

    if "scheduler" in components:
        scheduler = setup_scheduler(bot, redis)
        scheduler.start()

    if "heartbeat" in components:
        tasks.append(_heartbeat())

    if "api" in components:
        api_server = uvicorn.Server(
            uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
        )
        tasks.append(api_server.serve())

    try:
        await asyncio.gather(*tasks)
    finally:
        logger.info("Shutting down...")
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if redis is not None:
            await redis.aclose()
        await engine.dispose()
        if bot is not None:
            await bot.session.close()
        logger.info("Stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
