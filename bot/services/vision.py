import base64
import json
import logging
import re
import sys
import traceback

import anthropic

from bot.config import settings
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


def _extract_json(raw: str) -> str:
    """Strip markdown code fences if Claude wrapped the JSON despite instructions."""
    # Remove ```json ... ``` or ``` ... ``` wrappers
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


_BANK_TX_SYSTEM = """You are an Erste Bank mobile app transaction screenshot parser.

Examine the image. If it shows an Erste Bank transaction detail screen — look for any of:
  "Szczegóły transakcji", "Data transakcji", or both "Odbiorca" and "Nadawca" fields —
extract the transaction data and return a JSON object.

If the image is NOT an Erste Bank transaction screen (shop receipt, other bank, other app), return: null

JSON format when it IS a transaction screen:
{
  "merchant": "string — cleaned merchant name; remove trailing store numbers/codes, e.g. 'GRANDE BISTRO 89045' → 'Grande Bistro'",
  "amount_pln": 0.00,
  "date": "YYYY-MM-DD",
  "raw_title": "string or null — full Tytuł/Title field content if visible"
}

Rules:
- merchant: prefer the Odbiorca field; if absent use the name at the top of the screen
- amount_pln: absolute value (positive) of the PLN amount shown
- date: convert Polish month names — Stycznia=01 Lutego=02 Marca=03 Kwietnia=04 Maja=05 Czerwca=06 Lipca=07 Sierpnia=08 Września=09 Października=10 Listopada=11 Grudnia=12
- Return ONLY valid JSON or the literal: null — no markdown, no code fences, no explanation"""


async def parse_bank_transaction_screenshot(image_bytes: bytes) -> dict | None:
    """Try to parse an Erste Bank transaction detail screenshot.

    Returns a dict with keys (merchant, amount_pln, date, raw_title) if the image
    matches the Erste Bank transaction screen pattern, otherwise None.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=_BANK_TX_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": "Parse this image."},
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        print(f"Claude Vision API error (bank screenshot): {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise

    raw_text = response.content[0].text.strip()
    logger.debug("Bank screenshot Claude response: %r", raw_text)

    json_text = _extract_json(raw_text)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        logger.warning("Bank screenshot parser returned invalid JSON: %r", raw_text)
        return None

    if data is None:
        return None

    if not isinstance(data, dict):
        return None

    if not data.get("merchant") or data.get("amount_pln") is None or not data.get("date"):
        logger.warning("Bank screenshot parser missing required fields: %r", data)
        return None

    return data


async def parse_receipt(image_bytes: bytes) -> dict:
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        print(f"Claude Vision API error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise RuntimeError(f"Ошибка API Claude: {e}") from e

    raw_text = response.content[0].text.strip()
    logger.debug("Claude raw response: %r", raw_text)

    json_text = _extract_json(raw_text)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON. Raw output: %r", raw_text)
        print(f"Claude returned invalid JSON: {raw_text!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise ValueError("Claude returned invalid JSON") from e

    if isinstance(data.get("items"), list):
        data["items"] = _postprocess_items(data["items"])

    return validate_receipt(data)
