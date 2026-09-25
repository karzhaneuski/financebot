"""Language-neutral markers for store/item names the bot itself generates.

Earlier versions stored Russian text ("Снятие наличных" = cash withdrawal,
"Расход" = expense, ...) as store or item names. New records store a marker
instead and it is translated at display time. Old rows are left untouched: their Russian values are treated
as aliases of the matching marker, both for display and for grouping in SQL,
so old and new rows aggregate together.
"""
from sqlalchemy import case

from bot.i18n import __

CASH_WITHDRAWAL = "@cash_withdrawal"
CASH_DEPOSIT = "@cash_deposit"
# Rent for the "Akademik" dormitory. Keeps "AKADEMIK" in the marker so the
# keyword categorizer still maps it to housing.
AKADEMIK = "@akademik"
TRANSFER = "@transfer"
FOREIGN_CARD_PAYMENT = "@foreign_card_payment"
MANUAL_EXPENSE = "@manual_expense"

_LABELS = {
    CASH_WITHDRAWAL: __("Cash withdrawal"),
    CASH_DEPOSIT: __("Cash deposit"),
    AKADEMIK: __("Akademik"),
    TRANSFER: __("Transfer"),
    FOREIGN_CARD_PAYMENT: __("Card payment abroad"),
    MANUAL_EXPENSE: __("Expense"),
}

# Values written by pre-localization versions of the bot.
LEGACY_ALIASES = {
    "Снятие наличных": CASH_WITHDRAWAL,
    "Пополнение наличными": CASH_DEPOSIT,
    "Академик": AKADEMIK,
    "Перевод": TRANSFER,
    "Оплата картой за рубежом": FOREIGN_CARD_PAYMENT,
    "Расход": MANUAL_EXPENSE,
}


def canonical(value: str | None) -> str | None:
    """Marker for a legacy Russian value, otherwise the value itself."""
    if value is None:
        return None
    return LEGACY_ALIASES.get(value, value)


def is_marker(value: str | None) -> bool:
    return canonical(value) in _LABELS


def display_name(value: str | None) -> str | None:
    """Text to show for a stored store/item name in the current language."""
    key = canonical(value)
    label = _LABELS.get(key) if key is not None else None
    return str(label) if label is not None else value


def canonical_sql(column):
    """SQL expression mapping legacy values to markers, for GROUP BY / filters."""
    return case(LEGACY_ALIASES, value=column, else_=column)
