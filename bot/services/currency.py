import logging

import httpx
import redis.asyncio as aioredis

from bot.config import settings

logger = logging.getLogger(__name__)

# Daily scheduler job (bot/scheduler.py::_refresh_fx_rates) pre-warms this key
# for PLN/USD/EUR/BYN/CZK once/day. TTL outlives the refresh interval so a
# late/failed run doesn't drop the rate before the next one succeeds.
FX_CACHE_TTL = 25 * 3600
FX_KEY = "fx_rate:{ccy}"


async def _fetch_rate_from_provider(from_currency: str, to_currency: str = "PLN") -> float:
    """One-off pair fetch from exchangerate-api.com.

    Used to lazily warm the cache for currencies the daily frankfurter.dev
    job doesn't cover (Revolut's TRY/JPY, or BYN which ECB doesn't publish),
    and as the cold-start fallback before the scheduler's first run.
    """
    url = f"https://v6.exchangerate-api.com/v6/{settings.EXCHANGE_API_KEY}/pair/{from_currency}/{to_currency}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    return float(data["conversion_rate"])


async def get_rate(from_currency: str, to_currency: str = "PLN", redis: aioredis.Redis = None) -> float:
    """Rate to convert 1 unit of from_currency into to_currency.

    Reads only from the fx_rate:{CCY} cache. On a cold/empty cache entry it
    fetches once synchronously and populates the cache, so callers never
    hard-fail on a missing rate.
    """
    if from_currency == to_currency:
        return 1.0

    cache_key = FX_KEY.format(ccy=from_currency)
    try:
        cached = await redis.get(cache_key)
        if cached:
            return float(cached)
    except Exception as e:
        logger.warning(f"Redis get failed for {cache_key}: {e}")

    try:
        rate = await _fetch_rate_from_provider(from_currency, to_currency)
    except Exception as e:
        logger.warning(f"Currency rate fetch failed ({from_currency}->{to_currency}): {e}. Returning 1.0.")
        return 1.0

    try:
        await redis.setex(cache_key, FX_CACHE_TTL, str(rate))
    except Exception as e:
        logger.warning(f"Redis setex failed for {cache_key}: {e}")

    return rate


async def convert_to_pln(amount: float, currency: str, redis: aioredis.Redis) -> float:
    if currency == "PLN":
        return round(amount, 2)
    rate = await get_rate(currency, "PLN", redis)
    return round(amount * rate, 2)
