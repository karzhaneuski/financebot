from bot.categories import category_label

CATEGORY_EMOJI: dict[str, str] = {
    "groceries": "🛒",
    "cafe": "☕",
    "pharmacy": "💊",
    "transport": "🚌",
    "electronics": "📱",
    "clothing": "👗",
    "household": "🏡",
    "housing": "🏠",
    "entertainment": "🎭",
    "health": "🏥",
    "subscriptions": "📡",
    "other": "📦",
}


def category_meta(category: str, language: str) -> tuple[str, str]:
    """(localized name, emoji) for a category key."""
    return category_label(category, locale=language), CATEGORY_EMOJI.get(category, "📦")
