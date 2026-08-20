"""Revolut consolidated statement (Сводные данные) CSV parser.

The export is not a flat transaction table — it's a sequence of blocks
separated by "---------" divider lines: first one account-summary block per
account (balances only, skipped), then one transaction-statement block per
account ("Личный счет (CCY)" → "Выписка по операциям" → header → rows →
"Итого"). Column layout varies per block: an account whose native currency
is PLN gets 8 columns (no duplicate PLN-equivalent pair, since native ==
PLN already); every foreign-currency account gets the full 13 columns
(native/PLN pairs for amount, balance, tax, other-tax, fee). The header row
itself is used to locate the amount column(s) rather than hardcoding an
offset, so both shapes parse correctly.
"""
import csv
import io
import logging
import re

from bot.services.categorization import categorize

logger = logging.getLogger(__name__)

MONTHS_RU = {
    "янв": 1, "февр": 2, "мар": 3, "апр": 4, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сент": 9, "окт": 10, "нояб": 11, "дек": 12,
}

_CURRENCY_SYMBOL_MAP = {"€": "EUR", "¥": "JPY", "$": "USD"}

_ACCOUNT_RE = re.compile(r"^Личный счет \(([A-Z]{3})\)$")
_AMOUNT_HEADER = "Поступления / Списания"
_NUM_CLEAN_RE = re.compile(r"[^\d,.\-]")


def is_revolut_statement(text: str) -> bool:
    return "Revolut Bank UAB" in text and "Выписка по операциям" in text


def _parse_ru_date(date_str: str) -> str:
    """'24 февр. 2026 г.' -> '2026-02-24'."""
    parts = date_str.strip().split()
    if len(parts) < 3:
        raise ValueError(f"Unrecognized date: {date_str!r}")
    day = int(parts[0])
    month_token = parts[1].rstrip(".").lower()
    month = MONTHS_RU.get(month_token)
    if month is None:
        raise ValueError(f"Unknown month token: {parts[1]!r}")
    year = int(parts[2])
    return f"{year:04d}-{month:02d}-{day:02d}"


def _clean_number(raw: str) -> float:
    """Strip currency symbols/ISO codes and parse European number format.

    '1 829,67' -> 1829.67, '-1 050,00' -> -1050.0, '8,00$' -> 8.0,
    '-225,00 TRY' -> -225.0, '20¥' -> 20.0.
    """
    s = raw.strip()
    for sym in _CURRENCY_SYMBOL_MAP:
        s = s.replace(sym, "")
    s = s.replace("\xa0", " ")
    # Drop a trailing 2-4 letter ISO code (space-separated or not).
    s = re.sub(r"\s*[A-Za-z]{2,4}$", "", s)
    s = _NUM_CLEAN_RE.sub("", s.replace(" ", ""))
    s = s.replace(",", ".")
    return float(s)


def _split_blocks(rows: list[list[str]]) -> list[list[list[str]]]:
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in rows:
        first = (row[0] if row else "").strip()
        if first == "---------":
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(row)
    if current:
        blocks.append(current)
    return blocks


def _parse_transaction_block(block: list[list[str]]) -> list[dict]:
    currency: str | None = None
    header_idx: int | None = None
    for i, row in enumerate(block):
        first = (row[0] if row else "").strip()
        m = _ACCOUNT_RE.match(first)
        if m:
            currency = m.group(1)
        if first == "Дата":
            header_idx = i
            break

    if currency is None or header_idx is None:
        return []  # account-summary block (no statement section) — skip entirely

    header = block[header_idx]
    amount_indices = [j for j, cell in enumerate(header) if cell.strip() == _AMOUNT_HEADER]
    if not amount_indices:
        return []
    amount_native_idx = amount_indices[0]
    amount_pln_idx = amount_indices[1] if len(amount_indices) > 1 else None

    transactions: list[dict] = []
    for row in block[header_idx + 1:]:
        first = (row[0] if row else "").strip()
        if not first or first == "Итого":
            continue
        if len(row) <= amount_native_idx:
            continue

        description = row[1].strip() if len(row) > 1 else ""
        category = row[2].strip() if len(row) > 2 else ""

        # "Обмен валюты" = moving money between the person's own currency
        # pockets — not a real expense. "Пополнение" (top-ups) and anything
        # else that isn't a POS purchase is skipped the same way by falling
        # through the next check.
        if category != "Торговая точка":
            continue

        try:
            native_amount = _clean_number(row[amount_native_idx])
        except ValueError:
            continue

        if native_amount >= 0:
            # Refund/cashback on a card purchase — known edge case, not
            # handled in v1 (would need matching against the original
            # purchase to net out correctly).
            continue

        try:
            parsed_date = _parse_ru_date(first)
        except ValueError:
            continue

        if amount_pln_idx is not None and len(row) > amount_pln_idx:
            try:
                pln_amount = _clean_number(row[amount_pln_idx])
            except ValueError:
                pln_amount = native_amount
        else:
            pln_amount = native_amount  # native currency IS PLN, no separate column

        db_cat, display_cat = categorize(description)
        transactions.append({
            "date": parsed_date,
            "amount": abs(native_amount),
            "currency": currency,
            "description": description,
            "type": "purchase",
            "category": db_cat,
            "category_display": display_cat,
            "total_pln": round(abs(pln_amount), 2),
        })

    return transactions


def parse_revolut_csv(csv_bytes: bytes) -> list[dict]:
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    transactions: list[dict] = []
    for block in _split_blocks(rows):
        transactions.extend(_parse_transaction_block(block))
    return transactions
