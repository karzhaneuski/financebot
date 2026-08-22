# (keywords_upper, db_category_enum_value, display_name_ru)
CATEGORY_MAP = [
    (["KAUFLAND", "LIDL", "BIEDRONKA", "AUCHAN", "CARREFOUR", "ŻABKA", "TESCO", "ALCAMPO", "A 101"], "groceries", "Продукты"),
    (
        [
            "KEBAB", "MCDONALDS", "MCDONALD'S", "KFC", "DOMINOS", "WOLT", "PYSZNE",
            "STARBUCKS", "BISTRO", "DÜRÜM", "PIVN", "RISTORANTE", "REBELBEAN",
            "RESTAURACE", "POPEYES", "ASIA STAR", "ASIA GRAND",
        ],
        "cafe",
        "Еда вне дома",
    ),
    (["KOLEO", "FLIXBUS", "JAKDOJADE", "DOPRAVNI", "DOPRAVNÍ", "ISTANBULKART", "BELBIM", "RYANAIR"], "transport", "Транспорт"),
    (
        [
            "SPOTIFY", "YOUTUBE", "APPLE.COM", "CLAUDE.AI", "CLAUDE", "ANTHROPIC", "OPENAI", "SCRIBD",
            "КОМИССИЯ ПО ПЛАНУ", "REVOLUT METAL", "ПЛАН METAL",
        ],
        "subscriptions",
        "Подписки",
    ),
    (["ECZANESI"], "pharmacy", "Аптека"),
    (["SPORTISIMO"], "clothing", "Одежда"),
    (["REVOLUT"], "other", "Перевод на Revolut"),
    (["STYPENDIA", "STYPENDIUM"], "other", "Стипендия"),
    (["AKADEMIK", "АКАДЕМИК", "CZYNSZ", "NAJEM", "WYNAJEM"], "housing", "Жильё"),
    (["POLITECHNIKA"], "household", "Общежитие"),
    (["AMAZON", "ZALANDO", "OLX", "TEMU", "UNIQLO"], "other", "Онлайн шопинг"),
]


def categorize(description: str) -> tuple[str, str]:
    """Return (db_category, display_category_ru) for a transaction description."""
    upper = description.upper()
    for keywords, db_cat, display_cat in CATEGORY_MAP:
        if any(kw in upper for kw in keywords):
            return db_cat, display_cat
    return "other", "Другое"
