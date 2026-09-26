import logging
import sys
import traceback
from datetime import date

from bot.services.llm import InvalidLLMResponse, LLMError, LLMUnavailableError, _extract_json, get_provider
from bot.utils.validators import validate_receipt

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a receipt parser. Extract all data from the receipt image and return ONLY valid JSON, no markdown, no explanation, no code fences.

Rules:
- currency: detect from symbols/context (PLN, EUR, USD, CZK, BYR). Default to PLN for Polish receipts.
- category: one of [groceries, cafe, pharmacy, transport, electronics, clothing, household, other]
- quantity: default is 1 if not shown. For LIDL/Polish format "4 x 5,99" means quantity=4, unit_price=5.99.
- Polish decimal separator is a comma — treat "5,99" as 5.99.
- total: use the "Suma" or "SUMA" line on Polish receipts. Ignore "Płatność" (payment method) lines.
- items: include purchased products AND discount/deposit lines as separate items — post-processing will handle them. Specifically:
    * INCLUDE discount lines ("Lidl Plus oferta", "Rabat grupowy", "Rabat", any line with a negative amount) as items
      with negative total_price. Set quantity=1, unit_price=same negative value.
    * INCLUDE deposit lines as items with category="other" and a "deposit_type" field:
        - Lines for deposits PAID (bottles/cans going out with purchase) such as
          "Opakowania zwrotne wydania", "Kaucja puszek", "Kaucja PET":
          set deposit_type="wydanie", total_price=POSITIVE amount.
        - Lines for deposits RETURNED (bottles brought back) such as
          "Opakowania zwrotne przyjęcia", "Zwrot kaucji":
          set deposit_type="przyjecie", total_price=POSITIVE amount (Python will negate it).
        - If you cannot distinguish the direction, set deposit_type="wydanie".
    * SKIP tax summary lines only (e.g. "A 23%", "B 8%", "PTU A", "PTU B" lines that are only a tax-rate breakdown).
    * SKIP payment method lines ("Płatność", "Karta płatnicza", "Gotówka", "Reszta").
    * SKIP subtotal / total lines ("Suma", "Suma PLN", "Do zapłaty").
- If you cannot read a value, use null.
- Do NOT apply discounts yourself — emit them as separate negative-price items in order.
- date / year_visible: set year_visible=true only if a year is printed next to the date. If only
  day+month is shown (e.g. "23 sie.", "23 авг."), set year_visible=false and still fill "date" with
  your best guess — the caller corrects the year from today's date. Never take a year from your
  training data.
- If the image is a bank/payment app screen rather than a shop receipt, the amount of a payment is
  often shown with a minus sign (e.g. "-13,04 €"): that sign only marks money going out — use the
  positive amount for total and for the item.

Biedronka receipt format (store name "Biedronka" or header "Jeronimo Martins Polska"):
- Item lines: "NAME  PTU  QUANTITY x  UNIT_PRICE  TOTAL", e.g. "PiwoCoronaExtra450n  A  10.000 x  5,99  59,90"
  means name="PiwoCoronaExtra450n", quantity=10, unit_price=5.99, total_price=59.90.
- Discount lines: "Rabat  -29,95" immediately after an item → emit as item with name="Rabat", total_price=-29.95.
- Total line: "Suma PLN  52,94" → total=52.94.
- No deposit (kaucja) lines in Biedronka format — ignore any such line if present."""

USER_PROMPT = """Parse this receipt and return JSON in this exact format (no markdown, no code fences):
{
  "store": "string or null",
  "date": "YYYY-MM-DD or null",
  "year_visible": true,
  "currency": "PLN",
  "total": 0.00,
  "items": [
    {
      "name": "string",
      "quantity": 1,
      "unit_price": 0.00,
      "total_price": 0.00,
      "category": "groceries",
      "deposit_type": null
    }
  ]
}

For deposit lines set deposit_type to "wydanie" (paid) or "przyjecie" (returned). For all other items set deposit_type to null."""


# Structured-output schemas mirroring the JSON the prompts ask for, so the
# rest of the pipeline sees the same shape from every provider. Enforced
# natively by Gemini; Claude follows the format described in the prompt.
_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_NUM = {"type": ["number", "null"]}

RECEIPT_CATEGORIES = ["groceries", "cafe", "pharmacy", "transport", "electronics", "clothing", "household", "other"]

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store": _NULLABLE_STR,
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "year_visible": {"type": ["boolean", "null"]},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. PLN"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": _NULLABLE_NUM,
                    "unit_price": _NULLABLE_NUM,
                    "total_price": _NULLABLE_NUM,
                    "category": {"type": "string", "enum": RECEIPT_CATEGORIES},
                    "deposit_type": {"type": ["string", "null"], "enum": ["wydanie", "przyjecie", None]},
                },
                "required": ["name", "quantity", "unit_price", "total_price", "category", "deposit_type"],
            },
        },
        # After "items" on purpose: Gemini emits keys in schema order, so the
        # total is written once the whole receipt has been read — it sits at
        # the bottom of the receipt.
        "total": {
            "type": ["number", "null"],
            "description": 'Grand total from the "Suma PLN" / "SUMA" line at the bottom of the receipt '
                           "(the amount paid), not a subtotal, tax or payment line. Null only if unreadable.",
        },
    },
    "required": ["store", "date", "year_visible", "currency", "items", "total"],
}

# The Claude prompt answers the literal `null` for "not a transaction screen";
# a schema-enforced answer can't have a null root, so it carries a flag instead
# (see _BANK_TX_SCHEMA_HINT) that parse_bank_transaction_screenshot() maps back.
BANK_TX_SCHEMA = {
    "type": "object",
    "properties": {
        "is_transaction_screen": {"type": "boolean"},
        "merchant": _NULLABLE_STR,
        "amount": _NULLABLE_NUM,
        "currency": _NULLABLE_STR,
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "year_visible": {"type": ["boolean", "null"]},
        "raw_title": _NULLABLE_STR,
    },
    "required": ["is_transaction_screen", "merchant", "amount", "currency", "date", "year_visible", "raw_title"],
}

_BANK_TX_SCHEMA_HINT = (
    "Output format note: instead of the literal null, answer "
    '{"is_transaction_screen": false} with every other field null when the image is not a '
    "bank/payment transaction screen; otherwise set is_transaction_screen to true."
)


_DEPOSIT_KEYWORDS = ("kaucja", "opakowania zwrotne", "zwrot kaucji")
_DISCOUNT_KEYWORDS = ("oferta", "rabat", "plus oferta")

# Fallback keyword detection when Claude didn't set deposit_type
_PRZYJECIE_KEYWORDS = ("przyjęcia", "przyjecia", "zwrot kaucji")


def _is_deposit(item: dict) -> bool:
    """True if this item is a deposit line (either via explicit field or name heuristic)."""
    if item.get("deposit_type") in ("wydanie", "przyjecie"):
        return True
    name = (item.get("name") or "").lower()
    return any(kw in name for kw in _DEPOSIT_KEYWORDS)


def _deposit_direction(item: dict) -> str:
    """Return 'wydanie' or 'przyjecie' for a deposit item."""
    dt = item.get("deposit_type")
    if dt in ("wydanie", "przyjecie"):
        return dt
    name = (item.get("name") or "").lower()
    if any(kw in name for kw in _PRZYJECIE_KEYWORDS):
        return "przyjecie"
    return "wydanie"


def _is_discount(name: str, total_price: float) -> bool:
    low = name.lower()
    return total_price < 0 and any(kw in low for kw in _DISCOUNT_KEYWORDS)


def _postprocess_items(items: list[dict]) -> list[dict]:
    """Fold deposit lines into a net synthetic item; fold discounts into preceding item."""
    # Pass 1 — separate deposits from regular items
    deposits: list[dict] = []
    non_deposits: list[dict] = []
    for it in items:
        (deposits if _is_deposit(it) else non_deposits).append(it)

    # Pass 2 — compute net deposit
    wydanie_total = sum(float(it.get("total_price") or 0) for it in deposits if _deposit_direction(it) == "wydanie")
    przyjecie_total = sum(float(it.get("total_price") or 0) for it in deposits if _deposit_direction(it) == "przyjecie")
    net_deposit = round(wydanie_total - przyjecie_total, 2)

    if deposits:
        logger.debug(
            "Deposits: wydanie=%.2f przyjecie=%.2f net=%.2f",
            wydanie_total, przyjecie_total, net_deposit,
        )

    # Pass 3 — fold discounts into the preceding regular item
    result: list[dict] = []
    for item in non_deposits:
        name = item.get("name") or ""
        price = float(item.get("total_price") or 0)
        if _is_discount(name, price) and result:
            prev = result[-1]
            prev["total_price"] = round(float(prev["total_price"] or 0) + price, 2)
            if prev.get("quantity") in (None, 1):
                prev["unit_price"] = prev["total_price"]
            logger.debug(
                "Applied discount %r (%.2f) to %r → new total %.2f",
                name, price, prev["name"], prev["total_price"],
            )
        else:
            result.append(item)

    # Pass 4 — append net deposit item if non-zero
    if net_deposit != 0:
        result.append({
            "name": "Kaucja (netto)",
            "quantity": 1,
            "unit_price": net_deposit,
            "total_price": net_deposit,
            "category": "other",
            "deposit_type": None,
        })

    return result


def _infer_recent_year(date_str: str | None, today: date) -> str | None:
    """Replace the year in a YYYY-MM-DD guess with the most recent occurrence
    of that month/day on or before `today`.

    Used when the screenshot only shows day+month — Claude's guessed year is
    unreliable (it tends to default to a year near its training cutoff
    instead of reasoning about the actual current date), so the year is
    recomputed here instead of trusted from the model output.
    """
    if not date_str:
        return None
    try:
        _, month_str, day_str = date_str.split("-")
        month, day = int(month_str), int(day_str)
    except (ValueError, AttributeError):
        return date_str

    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return date_str

    if candidate > today:
        year -= 1

    return f"{year:04d}-{month:02d}-{day:02d}"


_BANK_TX_SYSTEM_TEMPLATE = """You are a bank/payment-app transaction screenshot parser (Erste Bank, Revolut, mobile wallets, etc.).

Examine the image. If it shows the detail screen of ONE transaction in a banking or payment app,
in any language, extract the transaction data and return a JSON object. Typical signs: a single
merchant/recipient name, one amount (a payment is often shown with a minus sign), a date or
date+time, and status/card/account fields — e.g. Erste/Polish "Szczegóły transakcji",
"Data transakcji", "Odbiorca", "Nadawca"; Revolut/English "Completed", "Card payment",
"Statement"; Russian "Выполнено", "Оплата картой", "Выписка". A shop receipt (printed list of
products with a total) is NOT a transaction screen.

If the image is NOT a bank/payment transaction screen (shop receipt, other app), return: null

JSON format when it IS a transaction screen:
{
  "merchant": "string — cleaned merchant name; remove trailing store numbers/codes, e.g. 'GRANDE BISTRO 89045' → 'Grande Bistro'",
  "amount": 0.00,
  "currency": "PLN",
  "date": "YYYY-MM-DD",
  "year_visible": true,
  "raw_title": "string or null — full Tytuł/Title field content if visible"
}

Rules:
- merchant: prefer the Odbiorca field; if absent use the name at the top of the screen
- amount: absolute value (positive) of the amount shown, in its ORIGINAL currency — do NOT convert it
- currency: ISO 4217 code inferred from the currency symbol or explicit code shown on screen
  (€ → EUR, $ → USD, zł/PLN → PLN, Kč → CZK, Br/BYN → BYN). If no currency indicator is visible
  at all, default to PLN.
- date: convert Polish month names — Stycznia=01 Lutego=02 Marca=03 Kwietnia=04 Maja=05 Czerwca=06 Lipca=07 Sierpnia=08 Września=09 Października=10 Listopada=11 Grudnia=12
- year_visible: true only if an explicit year is printed on screen next to the date. If the
  screen shows only day+month (e.g. "23 sie."), set year_visible=false and still fill "date"
  with your best guess — the caller will correct the year deterministically.
- Today's date is __TODAY__ — do not use a year from your training data.
- Return ONLY valid JSON or the literal: null — no markdown, no code fences, no explanation"""


async def parse_bank_transaction_screenshot(image_bytes: bytes, today: date | None = None) -> dict | None:
    """Try to parse a bank/payment-app transaction detail screenshot.

    Returns a dict with keys (merchant, amount, currency, date, raw_title) if the
    image matches a transaction screen pattern, otherwise None. `amount` is the
    raw amount in its original `currency` — callers must convert to PLN
    themselves (see bot.services.currency.convert_to_pln).
    """
    today = today or date.today()
    system_prompt = _BANK_TX_SYSTEM_TEMPLATE.replace("__TODAY__", today.isoformat())

    try:
        data = await get_provider().generate_json(
            system=system_prompt,
            prompt="Parse this image.",
            schema=BANK_TX_SCHEMA,
            schema_hint=_BANK_TX_SCHEMA_HINT,
            tier="vision",
            image=image_bytes,
            max_tokens=512,
        )
    except InvalidLLMResponse:
        return None
    except LLMError as e:
        print(f"Vision API error (bank screenshot): {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise

    if data is None:
        return None

    if not isinstance(data, dict) or data.get("is_transaction_screen") is False:
        return None

    if not data.get("merchant") or data.get("amount") is None or not data.get("date"):
        logger.warning("Bank screenshot parser missing required fields: %s",
                       [k for k in ("merchant", "amount", "date") if not data.get(k) and data.get(k) != 0])
        return None

    date_str = data.get("date")
    if not data.get("year_visible", False):
        date_str = _infer_recent_year(date_str, today) or date_str

    return {
        "merchant": data["merchant"],
        "amount": data["amount"],
        "currency": (data.get("currency") or "PLN").upper(),
        "date": date_str,
        "raw_title": data.get("raw_title"),
    }


def _unsign_debit(data: dict) -> None:
    """A bank/payment screen parsed as a receipt shows a payment as "-13,04 €":
    negative total and every item negative. That minus only marks money going
    out — make it positive. Runs on the raw model output, before deposit
    returns are negated, so a real bottle-return receipt stays negative;
    receipts with any non-negative line (discounts under products) are left
    alone."""
    total = data.get("total")
    items = data.get("items") if isinstance(data.get("items"), list) else []
    prices = [it.get("total_price") for it in items if it.get("total_price") is not None]
    if not isinstance(total, (int, float)) or total >= 0 or any(p >= 0 for p in prices):
        return
    if any(it.get("deposit_type") for it in items):
        return
    data["total"] = -total
    for it in items:
        for key in ("total_price", "unit_price"):
            if isinstance(it.get(key), (int, float)):
                it[key] = -it[key]


async def parse_receipt(image_bytes: bytes | list[bytes], today: date | None = None) -> dict:
    """Parse a receipt photo into the validated receipt dict.

    Raises LLMUnavailableError when the provider is out of quota/balance or
    down, ValueError when the image couldn't be read, LLMError otherwise.
    """
    today = today or date.today()
    try:
        data = await get_provider().generate_json(
            # The model has no clock; without today's date it fills a missing
            # year from its training data.
            system=f"{SYSTEM_PROMPT}\n\nToday's date is {today.isoformat()}.",
            prompt=USER_PROMPT,
            schema=RECEIPT_SCHEMA,
            tier="vision",
            image=image_bytes,
            max_tokens=2048,
        )
    except LLMError as e:
        print(f"Vision API error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise

    if not isinstance(data, dict):
        raise ValueError("Receipt parser returned no JSON object")

    _unsign_debit(data)
    if isinstance(data.get("items"), list):
        data["items"] = _postprocess_items(data["items"])

    # Same rule as bank screenshots: a year that isn't printed is recomputed
    # (most recent occurrence of that day/month), and a future date means the
    # model picked the wrong year.
    date_str = data.get("date")
    if isinstance(date_str, str) and date_str:
        try:
            future = date.fromisoformat(date_str[:10]) > today
        except ValueError:
            future = False
        if data.get("year_visible") is False or future:
            data["date"] = _infer_recent_year(date_str[:10], today)
    data.pop("year_visible", None)

    return validate_receipt(data)
