from datetime import date, datetime
from typing import Optional

CATEGORY_NAMES = {
    "groceries": "Продукты",
    "cafe": "Кафе/Рестораны",
    "pharmacy": "Аптека",
    "transport": "Транспорт",
    "electronics": "Электроника",
    "clothing": "Одежда",
    "household": "Дом/Быт",
    "housing": "Жильё",
    "entertainment": "Развлечения",
    "health": "Здоровье",
    "subscriptions": "Подписки",
    "other": "Прочее",
}

CURRENCY_SYMBOLS = {
    "PLN": "zł",
    "EUR": "€",
    "USD": "$",
    "CZK": "Kč",
    "BYR": "Br",
    "BYN": "Br",
}

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

MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MONTHS_NOMINATIVE = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def format_date_ru(date_str: str | None) -> str:
    if date_str is None:
        return "дата не определена"
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{d.day} {MONTHS_GENITIVE[d.month - 1]} {d.year}"
    except (ValueError, TypeError):
        return "дата не определена"


def format_currency(amount: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    return f"{amount:.2f} {symbol}"


def currency_flag(currency: str) -> str:
    return CURRENCY_FLAGS.get(currency, "💱")


def format_items_list(items: list[dict]) -> str:
    lines = []
    for item in items:
        name = item.get("name", "—")
        qty = item.get("quantity", 1)
        price = float(item.get("total_price", 0))
        qty_str = f" × {qty}" if qty != 1 else ""
        lines.append(f"  • {name}{qty_str} — {price:.2f} PLN")
    return "\n".join(lines)


# --- kept for backward compatibility with other modules ---

def format_amount(amount: float, currency: str = "PLN") -> str:
    return format_currency(amount, currency)


def format_pln(amount: float) -> str:
    return f"{amount:.2f} zł"


def format_date(d: Optional[date | datetime | str]) -> str:
    if d is None:
        return "неизвестно"
    if isinstance(d, str):
        return format_date_ru(d)
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]} {d.year}"


def format_category(category: str) -> str:
    return CATEGORY_NAMES.get(category, category)


def format_month(month: str) -> str:
    try:
        dt = datetime.strptime(month, "%Y-%m")
        return f"{MONTHS_NOMINATIVE[dt.month - 1]} {dt.year}"
    except ValueError:
        return month
