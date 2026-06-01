import re
import logging

import fitz  # pymupdf

logger = logging.getLogger(__name__)

OP_TYPES = [
    "TRANSAKCJA KARTĄ",
    "PRZELEW EXPRESS ELIXIR",
    "OPŁATA/PROWIZJA",
    "OBCIĄŻENIE",
    "UZNANIE",
]

CASH_WITHDRAWAL_KEYWORDS = ["СНЯТИЕ НАЛИЧНЫХ", "WYPŁATA GOTÓWKI", "BANKOMAT", "ATM"]

# (keywords_upper, db_category_enum_value, display_name_ru)
CATEGORY_MAP = [
    (["KAUFLAND", "LIDL", "BIEDRONKA", "AUCHAN"], "groceries", "Продукты"),
    (["KEBAB", "MCDONALDS", "KFC", "DOMINOS", "WOLT", "PYSZNE"], "cafe", "Еда вне дома"),
    (["KOLEO", "FLIXBUS", "JAKDOJADE"], "transport", "Транспорт"),
    (["SPOTIFY", "YOUTUBE", "APPLE.COM", "CLAUDE.AI", "OPENAI"], "other", "Подписки"),
    (["REVOLUT"], "other", "Перевод на Revolut"),
    (["STYPENDIA", "STYPENDIUM"], "other", "Стипендия"),
    (["AKADEMIK", "АКАДЕМИК", "CZYNSZ", "NAJEM", "WYNAJEM"], "housing", "Жильё"),
    (["POLITECHNIKA"], "household", "Общежитие"),
    (["AMAZON", "ZALANDO", "OLX", "TEMU"], "other", "Онлайн шопинг"),
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Matches amounts like "-18,00", "2 000,00", "-2 000,00" optionally followed by PLN
_AMOUNT_RE = re.compile(r"(-?\s*[\d\s]+,\d{2})\s*(?:PLN)?$")
# Strips card-payment prefix: "DOP. VISA 421352******5046 PŁATNOŚĆ KARTĄ 25,00 PLN "
_CARD_PREFIX_RE = re.compile(
    r"^DOP\.\s+VISA\s+[\d*]+\s+"
    r"(?:PŁATNOŚĆ KARTĄ|WYPŁATA Z BANKOMATU KARTĄ|PRZELEW KARTĄ|WPŁATA WE WPŁATOMACIE KARTĄ)"
    r"\s+[\d,.]+\s+PLN\s*",
    re.IGNORECASE,
)
# Matches foreign-currency conversion prefix and captures the merchant name after it
# e.g. "149.01 EUR 1 EUR=4.4078 PLN efihotel.cz Brno" → group(3) = "efihotel.cz Brno"
_CURRENCY_CONV_RE = re.compile(
    r"(\d[\d\s,.]+)\s+(EUR|USD|CZK|TRY|BYN)\s+1\s+\w+=[\d.]+\s+PLN\s+(.+)",
    re.IGNORECASE,
)
# Matches foreign-currency card payment with NO merchant name after PLN
# e.g. "DOP. VISA 4213525046 PŁATNOŚĆ KARTĄ 149.01 EUR 1 EUR=4.4078 PLN"
_FOREIGN_CARD_NO_MERCHANT_RE = re.compile(
    r"PŁATNOŚĆ KARTĄ\s+([\d.]+)\s+(\w+)\s+1\s+\w+=[\d.]+\s+PLN\s*$",
    re.IGNORECASE,
)
# Matches Tytuł values that are pure transfer labels with no merchant context
_PRZELEW_ONLY_RE = re.compile(r"^przelew(?: na telefon)?$", re.IGNORECASE)


def categorize(description: str) -> tuple[str, str]:
    """Return (db_category, display_category_ru) for a transaction description."""
    upper = description.upper()
    for keywords, db_cat, display_cat in CATEGORY_MAP:
        if any(kw in upper for kw in keywords):
            return db_cat, display_cat
    return "other", "Другое"


def is_erste_bank_statement(pdf_bytes: bytes) -> bool:
    """Quick check of first 3000 chars of page 1 text."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = doc[0].get_text()[:3000]
        doc.close()
        return "Erste Bank Polska" in text or "HISTORIA RACHUNKU" in text
    except Exception:
        return False


def parse_erste_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    Parse an Erste Bank Polska PDF statement and return a list of transactions:
    {date, amount, currency, description, type, raw_type, category, category_display}
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        full_text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()

    return _extract_transactions(full_text)


def _extract_transactions(text: str) -> list[dict]:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    transactions: list[dict] = []
    i = 0
    while i < len(lines):
        if _DATE_RE.match(lines[i]):
            date_str = lines[i]
            # Collect subsequent lines until next standalone date
            block: list[str] = []
            j = i + 1
            while j < len(lines) and not _DATE_RE.match(lines[j]):
                block.append(lines[j])
                j += 1
            tx = _parse_block(date_str, block)
            if tx:
                transactions.append(tx)
            i = j
        else:
            i += 1

    return transactions


def _extract_tytul(lines: list[str]) -> str | None:
    """Return the value of the Tytuł: field from a transaction block."""
    for idx, line in enumerate(lines):
        upper = line.upper()
        pos = upper.find("TYTUŁ:")
        if pos == -1:
            pos = upper.find("TYTUL:")
        if pos != -1:
            value = line[pos + 6:].strip()
            if not value and idx + 1 < len(lines):
                value = lines[idx + 1].strip()
            return value
    return None


def _parse_block(date_str: str, lines: list[str]) -> dict | None:
    if not lines:
        return None

    # Identify operation type (longest match wins to avoid partial matches)
    raw_type: str | None = None
    type_idx: int | None = None
    for idx, line in enumerate(lines):
        line_upper = line.upper()
        for op in OP_TYPES:
            if op in line_upper:
                raw_type = op
                type_idx = idx
                break
        if raw_type:
            break

    if raw_type is None:
        return None

    # Find amount — scan from end of block
    amount: float | None = None
    amount_idx: int | None = None
    for idx in range(len(lines) - 1, -1, -1):
        m = _AMOUNT_RE.search(lines[idx])
        if m:
            try:
                amount = _parse_amount(m.group(1))
                amount_idx = idx
                break
            except ValueError:
                continue

    if amount is None:
        return None

    # Prefer Tytuł: field; strip card-payment prefix to get clean merchant name
    tytul = _extract_tytul(lines)
    foreign_card_no_merchant = False
    orig_amount: float | None = None
    orig_currency: str | None = None

    if tytul:
        tytul_upper = tytul.upper()
        if "BANKOMAT" in tytul_upper:
            description = "Снятие наличных"
        elif "WPŁATOMACIE" in tytul_upper:
            description = "Пополнение наличными"
        elif raw_type in ("OBCIĄŻENIE", "PRZELEW EXPRESS ELIXIR") and (
            "AKADEMIKU" in tytul_upper or "ZAMIESZKANIE" in tytul_upper
        ):
            description = "Академик"
        elif raw_type == "OBCIĄŻENIE" and _PRZELEW_ONLY_RE.match(tytul.strip()):
            description = "Перевод"
        else:
            conv_match = _CURRENCY_CONV_RE.search(tytul)
            if conv_match:
                description = conv_match.group(3).strip()
            else:
                no_merchant_match = _FOREIGN_CARD_NO_MERCHANT_RE.search(tytul)
                if no_merchant_match:
                    foreign_card_no_merchant = True
                    orig_amount = float(no_merchant_match.group(1))
                    orig_currency = no_merchant_match.group(2).upper()
                    description = ""
                else:
                    description = _CARD_PREFIX_RE.sub("", tytul).strip() or tytul.strip()
    else:
        skip = {type_idx, amount_idx}
        desc_parts = [
            lines[idx]
            for idx in range(len(lines))
            if idx not in skip and not _DATE_RE.match(lines[idx]) and not _AMOUNT_RE.search(lines[idx])
        ]
        description = " ".join(desc_parts).strip()
    description = description or raw_type

    # UZNANIE is always income; for everything else use the sign of the amount
    if raw_type == "UZNANIE":
        tx_type = "income"
    elif description == "Снятие наличных":
        tx_type = "cash_withdrawal"
    else:
        tx_type = "income" if amount > 0 else "expense"

    db_cat, display_cat = categorize(description)
    result: dict = {
        "date": date_str,
        "amount": amount,
        "currency": "PLN",
        "description": description,
        "type": tx_type,
        "raw_type": raw_type,
        "category": db_cat,
        "category_display": display_cat,
    }
    if foreign_card_no_merchant:
        result["foreign_card_no_merchant"] = True
        result["orig_amount"] = orig_amount
        result["orig_currency"] = orig_currency
    return result


def _parse_amount(raw: str) -> float:
    # Remove spaces used as thousands separators, replace comma decimal
    cleaned = raw.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    return float(cleaned)
