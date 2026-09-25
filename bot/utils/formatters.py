from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from babel.dates import format_date as _babel_format_date
from babel.numbers import format_decimal as _babel_format_decimal

from bot.categories import category_label
from bot.i18n import _, current_language
from bot.markers import display_name

CURRENCY_FLAGS = {
    "PLN": "🇵🇱",
    "USD": "🇺🇸",
    "EUR": "🇪🇺",
    "CZK": "🇨🇿",
    "BYN": "🇧🇾",
    "TRY": "🇹🇷",
    "JPY": "🇯🇵",
    "GBP": "🇬🇧",
}


# ── dates (month names come from CLDR via Babel, in the current language) ──

def format_day_month_year(d: date) -> str:
    """'5 June 2026' / '5 июня 2026' / '5 czerwca 2026'."""
    return _babel_format_date(d, "d MMMM y", locale=current_language())


def format_day_month(d: date) -> str:
    """'5 June' / '5 июня' / '5 czerwca'."""
    return _babel_format_date(d, "d MMMM", locale=current_language())


def month_name(month: int) -> str:
    """Standalone (nominative) month name, lowercase where the language does so."""
    return _babel_format_date(date(2000, month, 1), "LLLL", locale=current_language())


def format_month_year(year: int, month: int) -> str:
    """'June 2026' / 'июнь 2026' / 'czerwiec 2026'."""
    return _babel_format_date(date(year, month, 1), "LLLL y", locale=current_language())


def format_date_str(date_str: str | None) -> str:
    """Format an ISO 'YYYY-MM-DD' string; a friendly placeholder if missing/invalid."""
    if date_str is None:
        return _("date unknown")
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return _("date unknown")
    return format_day_month_year(d)


# CLDR minimumGroupingDigits, which Babel ignores: Polish leaves 4-digit
# numbers ungrouped ('1234,50' but '12 345,50'), as Intl does in the Mini App.
_MIN_GROUPING_DIGITS = {"pl": 2}


def format_number(amount: float, decimals: int = 2) -> str:
    """Locale-aware grouping and decimal separator (e.g. '1 234,50' in ru,
    '1234,50' in pl, '1,234.50' in en) for a plain number, no currency symbol."""
    lang = current_language()
    # Round half away from zero like Intl (Babel alone rounds half-even:
    # 1234.5 -> '1234'); str() so 0.005 is taken as written, not as its float.
    value = Decimal(str(amount)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    grouped = abs(value) >= 10 ** (2 + _MIN_GROUPING_DIGITS.get(lang, 1))
    pattern = ("#,##0" if grouped else "0") + ("." + "0" * decimals if decimals else "")
    return _babel_format_decimal(value, format=pattern, locale=lang)


def format_currency(amount: float, currency: str) -> str:
    """Amount followed by the ISO currency code ('1 234,50 PLN', '12,99 EUR'),
    the same shape the Mini App uses."""
    return f"{format_number(amount)} {currency}"


def currency_flag(currency: str) -> str:
    return CURRENCY_FLAGS.get(currency, "💱")


def format_receipt_amount(receipt) -> str:
    """Amount of a saved receipt: native sum, plus a PLN approximation when the
    receipt isn't already in PLN.

    The single place in the app that knows how to display one receipt's
    amount — photo, PDF and bank-screenshot confirmations plus the post-/recat
    edit all render through here. They used to format it separately and drifted
    apart, showing the native amount under a "PLN" label after a recat.
    """
    currency = receipt.currency or "PLN"
    text = format_currency(float(receipt.total), currency)
    if currency != "PLN":
        text += f" (≈ {format_pln(float(receipt.total_pln))})"
    return text


def format_items_list(items: list[dict], currency: str) -> str:
    """Item lines of a receipt. Prices are in the receipt's own currency —
    item dicts carry no currency of their own, so callers must pass the
    receipt's (`currency` is deliberately required: defaulting it to PLN is
    what made foreign-currency items render as "13.04 PLN").
    """
    lines = []
    for item in items:
        name = display_name(item.get("name")) or "—"
        qty = item.get("quantity", 1)
        price = float(item.get("total_price", 0))
        qty_str = f" × {qty}" if qty != 1 else ""
        lines.append(f"  • {name}{qty_str} — {format_currency(price, currency)}")
    return "\n".join(lines)


# --- kept for backward compatibility with other modules ---

def format_amount(amount: float, currency: str = "PLN") -> str:
    return format_currency(amount, currency)


def format_pln(amount: float) -> str:
    return format_currency(amount, "PLN")


def format_date(d: Optional[date | datetime | str]) -> str:
    if d is None:
        return _("unknown")
    if isinstance(d, str):
        return format_date_str(d)
    if isinstance(d, datetime):
        d = d.date()
    return format_day_month_year(d)


def format_category(category: str) -> str:
    return category_label(category)


def format_month(month: str) -> str:
    """'2026-06' -> 'June 2026' in the current language."""
    try:
        dt = datetime.strptime(month, "%Y-%m")
    except ValueError:
        return month
    return format_month_year(dt.year, dt.month)
