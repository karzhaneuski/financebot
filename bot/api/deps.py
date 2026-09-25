from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import get_telegram_user
from bot.db import crud
from bot.db.engine import AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_user_language(
    tg_user: dict = Depends(get_telegram_user),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Language for API responses — the same one the bot uses for this user."""
    return await crud.get_or_create_user_language(db, tg_user["id"], tg_user.get("language_code"))
