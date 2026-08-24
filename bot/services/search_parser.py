"""Natural-language search query parsing (Feature 1).

Calls the Anthropic API (Haiku-tier) to extract structured filters from a
free-text Russian query. Falls back to a lightweight regex parser when the
API is unavailable so /search degrades gracefully.
"""
import json
import logging
import re
from calendar import monthrange
from datetime import date, timedelta

import anthropic

from bot.config import settings
from bot.db.models import Category

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a search-query parser for a personal finance tracker. The user writes a free-text query in Russian describing which transactions to find.

Extract filters and return ONLY valid JSON (no markdown, no code fences):
{
  "merchant": "string or null — store/merchant name as written",
  "category": "one of [groceries, cafe, pharmacy, transport, electronics, clothing, household, housing, entertainment, health, subscriptions, other] or null",
  "date_from": "YYYY-MM-DD or null",
  "date_to": "YYYY-MM-DD or null",
  "amount_min": number or null,
  "amount_max": number or null,
  "include_cash": true if the user explicitly asks about cash withdrawals (наличные, снятие), otherwise false
}

Rules:
- Russian month names map to months: январь=1 ... декабрь=12. "за июнь" means the whole of June of the CURRENT year.
- Relative terms: "в этом месяце" = current month, "в прошлом месяце" = previous month, "на прошлой неделе" = previous calendar week (Mon-Sun), "на этой неделе" = current week, "в этом году" = current year.
- "дороже N" → amount_min=N; "дешевле N" / "меньше N" → amount_max=N; "от A до B" → both.
- category must be one of the listed enum values; pick the closest match ("продукты"→groceries, "кафе/ресторан"→cafe, "аптека"→pharmacy, etc.). If unsure, use null.
- Amounts are in PLN unless another currency is clearly stated (convert nothing — just use the number).
- Omitted filters stay null."""

_MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма[ейя]": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _regex_parse(text: str, today: date) -> dict:
    """Offline fallback parser for common Russian patterns."""
    out: dict = {"include_cash": False}
    low = text.lower()

    m = re.search(r"дороже\s+(\d+(?:[.,]\d+)?)", low)
    if m:
        out["amount_min"] = float(m.group(1).replace(",", "."))
    m = re.search(r"(?:дешевле|меньше)\s+(\d+(?:[.,]\d+)?)", low)
    if m:
        out["amount_max"] = float(m.group(1).replace(",", "."))

    # Month names only count when preceded by a preposition (за|в|на),
    # e.g. "за июнь" — avoids matching merchant names containing month stems.
    for pattern, month in _MONTHS_RU.items():
        m = re.search(rf"(?:^|\s)(?:за|в|на)\s+\w*{pattern}", low)
        if m:
            out["date_from"], out["date_to"] = _month_bounds(today.year, month)
            break

    if "прошл" in low and "месяц" in low:
        pm_year, pm_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        out["date_from"], out["date_to"] = _month_bounds(pm_year, pm_month)
    elif "этот месяц" in low or "в этом месяце" in low:
        out["date_from"], out["date_to"] = _month_bounds(today.year, today.month)
    elif "прошл" in low and "недел" in low:
        this_mon = today - timedelta(days=today.weekday())
        out["date_from"] = this_mon - timedelta(days=7)
        out["date_to"] = this_mon - timedelta(days=1)
    elif "эт" in low and "недел" in low:
        this_mon = today - timedelta(days=today.weekday())
        out["date_from"] = this_mon
        out["date_to"] = this_mon + timedelta(days=6)

    # Drop date ranges that lie entirely in the future (parser noise).
    df, dt = out.get("date_from"), out.get("date_to")
    if df and dt and df > today:
        out.pop("date_from")
        out.pop("date_to")

    keywords = {
        "продукт": "groceries",
        "кафе": "cafe", "ресторан": "cafe",
        "аптек": "pharmacy", "транспорт": "transport",
        "электроник": "electronics", "одежд": "clothing",
        "жиль": "housing", "развлечен": "entertainment",
        "здоровь": "health", "подписк": "subscriptions",
    }
    for kw, cat in keywords.items():
        if kw in low:
            out["category"] = cat
            break

    if "наличн" in low or "снят" in low or "сняти" in low:
        out["include_cash"] = True

    return {k: v for k, v in out.items() if v not in (None, False)} | (
        {"include_cash": True} if out.get("include_cash") else {}
    )


def _validate(data: dict) -> dict:
    out: dict = {}
    if isinstance(data.get("merchant"), str) and data["merchant"].strip():
        out["merchant"] = data["merchant"].strip()
    try:
        if data.get("category"):
            out["category"] = Category(data["category"]).value
    except ValueError:
        pass
    for key in ("date_from", "date_to"):
        val = data.get(key)
        if isinstance(val, str):
            try:
                out[key] = date.fromisoformat(val[:10])
            except ValueError:
                pass
    for key in ("amount_min", "amount_max"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            out[key] = float(val)
    out["include_cash"] = bool(data.get("include_cash"))
    return out


async def parse_search_query(text: str, today: date | None = None) -> tuple[dict, bool]:
    """Parse a Russian free-text query into optional search filters.

    Returns (filters, used_fallback): used_fallback is True when the LLM call
    failed and the offline regex parser produced the result — callers should
    surface a "results may be imprecise" notice in that case.
    """
    today = today or date.today()
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text.strip()}],
        )
        raw = response.content[0].text.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
        data = json.loads(match.group(1) if match else raw)
        parsed = _validate(data)
        # Sanity-check LLM dates; fall back to regex parser on garbage.
        if parsed.get("date_from") and parsed.get("date_to") and parsed["date_from"] > parsed["date_to"]:
            raise ValueError("inverted range")
        logger.info("search_query %r -> %s", text, parsed)
        return parsed, False
    except Exception:
        logger.warning("LLM search parse failed for %r, using fallback", text, exc_info=True)
        return _regex_parse(text, today), True
