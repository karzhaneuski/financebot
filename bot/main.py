import asyncio
import logging
import subprocess
import sys

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


async def main() -> None:
    _run_migrations()

    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=False)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
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

    scheduler = setup_scheduler(bot, redis)
    scheduler.start()

    api_server = uvicorn.Server(
        uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    )

    try:
        logger.info("Бот запущен")
        await asyncio.gather(api_server.serve(), dp.start_polling(bot))
    finally:
        logger.info("Завершение работы...")
        scheduler.shutdown(wait=False)
        await redis.aclose()
        await engine.dispose()
        await bot.session.close()
        logger.info("Бот остановлен корректно")


if __name__ == "__main__":
    asyncio.run(main())
