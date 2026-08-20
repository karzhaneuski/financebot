# (keywords_upper, db_category_enum_value, display_name_ru)
CATEGORY_MAP = [
    (["KAUFLAND", "LIDL", "BIEDRONKA", "AUCHAN"], "groceries", "Продукты"),
    (["KEBAB", "MCDONALDS", "MCDONALD'S", "KFC", "DOMINOS", "WOLT", "PYSZNE"], "cafe", "Еда вне дома"),
    (["KOLEO", "FLIXBUS", "JAKDOJADE", "DOPRAVNI", "DOPRAVNÍ"], "transport", "Транспорт"),
    (["SPOTIFY", "YOUTUBE", "APPLE.COM", "CLAUDE.AI", "CLAUDE", "OPENAI", "SCRIBD"], "other", "Подписки"),
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
