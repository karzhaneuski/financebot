import logging

import httpx
import redis.asyncio as aioredis

from bot.config import settings

logger = logging.getLogger(__name__)

REDIS_TTL = 3600


async def get_rate(from_currency: str, to_currency: str = "PLN", redis: aioredis.Redis = None) -> float:
    if from_currency == to_currency:
        return 1.0

    cache_key = f"rate:{from_currency}:{to_currency}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return float(cached)
    except Exception as e:
        logger.warning(f"Redis get failed for {cache_key}: {e}")

    url = f"https://v6.exchangerate-api.com/v6/{settings.EXCHANGE_API_KEY}/pair/{from_currency}/{to_currency}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        rate = float(data["conversion_rate"])
    except Exception as e:
        logger.warning(f"Currency rate fetch failed ({from_currency}->{to_currency}): {e}. Returning 1.0.")
        return 1.0

    try:
        await redis.setex(cache_key, REDIS_TTL, str(rate))
    except Exception as e:
        logger.warning(f"Redis setex failed for {cache_key}: {e}")

    return rate


async def convert_to_pln(amount: float, currency: str, redis: aioredis.Redis) -> float:
    if currency == "PLN":
        return round(amount, 2)
    rate = await get_rate(currency, "PLN", redis)
    return round(amount * rate, 2)
