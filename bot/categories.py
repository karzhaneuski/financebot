"""Single source of category display names.

Categories are stored as enum keys (bot.db.models.Category) and translated
only when shown, so switching language never touches analytics.
"""
from bot.i18n import __, i18n

_LABELS = {
    "groceries": __("Groceries"),
    "cafe": __("Cafés/Restaurants"),
    "pharmacy": __("Pharmacy"),
    "transport": __("Transport"),
    "electronics": __("Electronics"),
    "clothing": __("Clothing"),
    "household": __("Household"),
    "housing": __("Housing"),
    "entertainment": __("Entertainment"),
    "health": __("Health"),
    "subscriptions": __("Subscriptions"),
    "other": __("Other"),
}

CATEGORY_KEYS: tuple[str, ...] = tuple(_LABELS)


def category_label(key: str, locale: str | None = None) -> str:
    """Localized name of a category key; unknown keys are returned as-is."""
    label = _LABELS.get(key)
    if label is None:
        return key
    if locale is None:
        return str(label)
    with i18n.use_locale(locale):
        return str(label)
