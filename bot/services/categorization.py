from bot.categories import category_label
from bot.i18n import __

# (keywords_upper, db_category_enum_value, display label — None means the
# category's own name)
CATEGORY_MAP = [
    (["KAUFLAND", "LIDL", "BIEDRONKA", "AUCHAN", "CARREFOUR", "ŻABKA", "TESCO", "ALCAMPO", "A 101"], "groceries", None),
    (
        [
            "KEBAB", "MCDONALDS", "MCDONALD'S", "KFC", "DOMINOS", "WOLT", "PYSZNE",
            "STARBUCKS", "BISTRO", "DÜRÜM", "PIVN", "RISTORANTE", "REBELBEAN",
            "RESTAURACE", "POPEYES", "ASIA STAR", "ASIA GRAND",
        ],
        "cafe",
        __("Eating out"),
    ),
    (["KOLEO", "FLIXBUS", "JAKDOJADE", "DOPRAVNI", "DOPRAVNÍ", "ISTANBULKART", "BELBIM", "RYANAIR"], "transport", None),
    (
        [
            "SPOTIFY", "YOUTUBE", "APPLE.COM", "CLAUDE.AI", "CLAUDE", "ANTHROPIC", "OPENAI", "SCRIBD",
            "КОМИССИЯ ПО ПЛАНУ", "REVOLUT METAL", "ПЛАН METAL",
        ],
        "subscriptions",
        None,
    ),
    (["ECZANESI"], "pharmacy", None),
    (["SPORTISIMO"], "clothing", None),
    (["REVOLUT"], "other", __("Transfer to Revolut")),
    (["STYPENDIA", "STYPENDIUM"], "other", __("Scholarship")),
    (["AKADEMIK", "АКАДЕМИК", "CZYNSZ", "NAJEM", "WYNAJEM"], "housing", None),
    (["POLITECHNIKA"], "household", __("Dormitory")),
    (["AMAZON", "ZALANDO", "OLX", "TEMU", "UNIQLO"], "other", __("Online shopping")),
]


def categorize(description: str) -> tuple[str, str]:
    """Return (db_category, localized display label) for a transaction description."""
    upper = description.upper()
    for keywords, db_cat, display_cat in CATEGORY_MAP:
        if any(kw in upper for kw in keywords):
            return db_cat, str(display_cat) if display_cat is not None else category_label(db_cat)
    return "other", category_label("other")
