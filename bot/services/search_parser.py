"""Natural-language search query parsing (Feature 1).

Calls the configured LLM provider (bot.services.llm, fast tier) to extract
structured filters from a free-text query in Russian, English or Polish.
Falls back to a lightweight regex parser when the API is unavailable so
/search degrades gracefully.
"""
import logging
import re
from calendar import monthrange
from datetime import date, timedelta

from bot.db.models import Category
from bot.services.llm import get_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a search-query parser for a personal finance tracker. The user writes a free-text query in Russian, English or Polish describing which transactions to find.

Extract filters and return ONLY valid JSON (no markdown, no code fences):
{
  "merchant": "string or null — store/merchant name as written",
  "category": "one of [groceries, cafe, pharmacy, transport, electronics, clothing, household, housing, entertainment, health, subscriptions, other] or null",
  "date_from": "YYYY-MM-DD or null",
  "date_to": "YYYY-MM-DD or null",
  "amount_min": number or null,
  "amount_max": number or null,
  "include_cash": true if the user explicitly asks about cash withdrawals (наличные, снятие / cash, ATM, withdrawal / gotówka, bankomat, wypłata z bankomatu), otherwise false
}

Rules:
- Month names in any case/inflection map to months: январь=1 ... декабрь=12; January=1 ... December=12; styczeń/stycznia/styczniu=1 ... grudzień/grudnia/grudniu=12. A month without a year ("за июнь", "in June", "w czerwcu") means the whole of that month of the CURRENT year.
- Relative terms:
  - current month: "в этом месяце" / "this month" / "w tym miesiącu";
  - previous month: "в прошлом месяце" / "last month" / "w zeszłym (poprzednim) miesiącu";
  - previous calendar week (Mon-Sun): "на прошлой неделе" / "last week" / "w zeszłym (poprzednim) tygodniu";
  - current week: "на этой неделе" / "this week" / "w tym tygodniu";
  - current year: "в этом году" / "this year" / "w tym roku".
- Amounts:
  - amount_min=N: "дороже N", "больше N" / "over N", "more than N", "above N" / "powyżej N", "ponad N", "więcej niż N", "droższe niż N";
  - amount_max=N: "дешевле N", "меньше N" / "under N", "less than N", "below N", "cheaper than N" / "poniżej N", "mniej niż N", "tańsze niż N";
  - both: "от A до B" / "between A and B", "from A to B" / "od A do B".
- category must be one of the listed enum values; pick the closest match in any language ("продукты" / "groceries" / "zakupy spożywcze"→groceries, "кафе/ресторан" / "cafe/restaurant" / "kawiarnia/restauracja"→cafe, "аптека" / "pharmacy" / "apteka"→pharmacy, etc.). If unsure, use null.
- Amounts are in PLN unless another currency is clearly stated (convert nothing — just use the number). "zł", "złoty", "злотых" mean PLN.
- Omitted filters stay null."""

# Same shape as the JSON described in SYSTEM_PROMPT (enforced by Gemini).
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "enum": [c.value for c in Category] + [None]},
        "date_from": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "date_to": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "amount_min": {"type": ["number", "null"]},
        "amount_max": {"type": ["number", "null"]},
        "include_cash": {"type": "boolean"},
    },
    "required": ["merchant", "category", "date_from", "date_to", "amount_min", "amount_max", "include_cash"],
}

_MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма[ейя]": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
# Polish month names in the cases that follow w/we/za/z ("w czerwcu",
# "za czerwiec", "z czerwca").
_MONTHS_PL = {
    r"stycz\w*": 1, r"lut\w*": 2, r"mar(?:zec|ca|cu)": 3, r"kwie\w*": 4, r"maj[au]?": 5,
    r"czerw\w*": 6, r"lip\w*": 7, r"sierp\w*": 8, r"wrze\w*": 9, r"pa[źz]dziernik\w*": 10,
    r"listopad\w*": 11, r"grud\w*": 12,
}

_NUM = r"(\d+(?:[.,]\d+)?)"
_AMOUNT_MIN_RE = re.compile(
    r"(?:дороже|больше|over|more than|above|powyżej|ponad|więcej niż|droższ\w* niż)\s+" + _NUM)
_AMOUNT_MAX_RE = re.compile(
    r"(?:дешевле|меньше|under|less than|below|cheaper than|poniżej|mniej niż|tańsz\w* niż)\s+" + _NUM)
_AMOUNT_RANGE_RE = re.compile(r"(?:^|\s)(?:от|between|from|od)\s+" + _NUM + r"\s+(?:до|and|to|do)\s+" + _NUM)

_PREV_MONTH_RE = re.compile(r"\b(?:last|previous) month\b|\b(?:zeszł|poprzedni)\w*\s+miesi\w*")
_THIS_MONTH_RE = re.compile(r"\bthis month\b|\b(?:tym|ten|bieżąc\w*)\s+miesi\w*")
_PREV_WEEK_RE = re.compile(r"\b(?:last|previous) week\b|\b(?:zeszł|poprzedni)\w*\s+(?:tydzie[ńn]|tygodni\w*)")
_THIS_WEEK_RE = re.compile(r"\bthis week\b|\b(?:tym|ten|bieżąc\w*)\s+(?:tydzie[ńn]|tygodni\w*)")
_THIS_YEAR_RE = re.compile(r"в этом году|этот год|\bthis year\b|\bw tym roku\b|\bten rok\b")

# Checked in order; the first match wins. Latin stems are matched at a word
# start ("rent" must not match "current").
_CATEGORY_KEYWORDS = [
    ("продукт", "groceries"), (r"\bgrocer", "groceries"), (r"\bspo[żz]yw", "groceries"),
    ("кафе", "cafe"), ("ресторан", "cafe"), (r"\bcaf[eé]", "cafe"), (r"\brestaura", "cafe"), (r"\bkawiar", "cafe"),
    ("аптек", "pharmacy"), (r"\bpharmac", "pharmacy"), (r"\baptek", "pharmacy"),
    ("транспорт", "transport"), (r"\btransport", "transport"),
    ("электроник", "electronics"), (r"\belectronic", "electronics"), (r"\belektronik", "electronics"),
    ("одежд", "clothing"), (r"\bcloth", "clothing"), (r"\bodzie[żz]", "clothing"), (r"\bubra[ńn]", "clothing"),
    ("жиль", "housing"), (r"\bhousing\b", "housing"), (r"\brent\b", "housing"), (r"\bmieszkan", "housing"), (r"\bczynsz", "housing"),
    ("развлечен", "entertainment"), (r"\bentertainment", "entertainment"), (r"\brozrywk", "entertainment"),
    ("здоровь", "health"), (r"\bhealth", "health"), (r"\bzdrow", "health"),
    ("подписк", "subscriptions"), (r"\bsubscription", "subscriptions"), (r"\bsubskrypc", "subscriptions"), (r"\babonament", "subscriptions"),
]
_CASH_RE = re.compile(r"наличн|снят|\bcash\b|\batm\b|withdraw|got[óo]wk|bankomat")


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _to_float(num: str) -> float:
    return float(num.replace(",", "."))


def _find_month(low: str) -> int | None:
    """Month names only count after a preposition ("за июнь", "in June",
    "w czerwcu") — avoids matching merchant names containing month stems."""
    for pattern, month in _MONTHS_RU.items():
        if re.search(rf"(?:^|\s)(?:за|в|на)\s+\w*{pattern}", low):
            return month
    for name, month in _MONTHS_EN.items():
        if re.search(rf"\b(?:in|for|during)\s+{name}\b", low):
            return month
    for pattern, month in _MONTHS_PL.items():
        if re.search(rf"(?:^|\s)(?:w|we|za|z)\s+{pattern}\b", low):
            return month
    return None


def _regex_parse(text: str, today: date) -> dict:
    """Offline fallback parser for common Russian, English and Polish patterns."""
    out: dict = {"include_cash": False}
    low = text.lower()

    m = _AMOUNT_RANGE_RE.search(low)
    if m:
        out["amount_min"], out["amount_max"] = _to_float(m.group(1)), _to_float(m.group(2))
    m = _AMOUNT_MIN_RE.search(low)
    if m:
        out["amount_min"] = _to_float(m.group(1))
    m = _AMOUNT_MAX_RE.search(low)
    if m:
        out["amount_max"] = _to_float(m.group(1))

    month = _find_month(low)
    if month:
        out["date_from"], out["date_to"] = _month_bounds(today.year, month)

    this_mon = today - timedelta(days=today.weekday())
    if ("прошл" in low and "месяц" in low) or _PREV_MONTH_RE.search(low):
        pm_year, pm_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        out["date_from"], out["date_to"] = _month_bounds(pm_year, pm_month)
    elif "этот месяц" in low or "в этом месяце" in low or _THIS_MONTH_RE.search(low):
        out["date_from"], out["date_to"] = _month_bounds(today.year, today.month)
    elif ("прошл" in low and "недел" in low) or _PREV_WEEK_RE.search(low):
        out["date_from"] = this_mon - timedelta(days=7)
        out["date_to"] = this_mon - timedelta(days=1)
    elif ("эт" in low and "недел" in low) or _THIS_WEEK_RE.search(low):
        out["date_from"] = this_mon
        out["date_to"] = this_mon + timedelta(days=6)
    elif _THIS_YEAR_RE.search(low):
        out["date_from"], out["date_to"] = date(today.year, 1, 1), date(today.year, 12, 31)

    # Drop date ranges that lie entirely in the future (parser noise).
    df, dt = out.get("date_from"), out.get("date_to")
    if df and dt and df > today:
        out.pop("date_from")
        out.pop("date_to")

    for pattern, cat in _CATEGORY_KEYWORDS:
        if re.search(pattern, low):
            out["category"] = cat
            break

    if _CASH_RE.search(low):
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
    """Parse a free-text query (Russian, English or Polish) into optional search filters.

    Returns (filters, used_fallback): used_fallback is True when the LLM call
    failed and the offline regex parser produced the result — callers should
    surface a "results may be imprecise" notice in that case.
    """
    today = today or date.today()
    try:
        data = await get_provider().generate_json(
            # The model has no clock: relative terms ("last month", "за июнь"
            # = June of the current year) need today's date.
            system=f"{SYSTEM_PROMPT}\n\nToday's date: {today.isoformat()} ({today:%A}).",
            prompt=text.strip(),
            schema=SEARCH_SCHEMA,
            tier="fast",
            max_tokens=512,
        )
        if not isinstance(data, dict):
            raise ValueError("search parser returned no JSON object")
        parsed = _validate(data)
        # Sanity-check LLM dates; fall back to regex parser on garbage.
        if parsed.get("date_from") and parsed.get("date_to") and parsed["date_from"] > parsed["date_to"]:
            raise ValueError("inverted range")
        logger.debug("search_query %r -> %s", text, parsed)
        return parsed, False
    except Exception:
        logger.warning("LLM search parse failed, using fallback", exc_info=True)
        return _regex_parse(text, today), True
