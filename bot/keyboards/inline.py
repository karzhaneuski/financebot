from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Categories shown in the re-categorisation picker (subset of all categories).
# Order matches the 2-column layout described in the spec.
RECAT_CATEGORIES: list[tuple[str, str]] = [
    ("groceries",     "🛒 Продукты"),
    ("housing",       "🏠 Жильё"),
    ("transport",     "🚗 Транспорт"),
    ("cafe",          "☕ Кафе/Рестораны"),
    ("entertainment", "🎮 Развлечения"),
    ("health",        "💊 Здоровье"),
    ("clothing",      "👕 Одежда"),
    ("subscriptions", "📱 Подписки"),
    ("other",         "❓ Другое"),
]

CATEGORY_LABEL: dict[str, str] = {key: label for key, label in RECAT_CATEGORIES}


def stats_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📦 Продукты", callback_data="smenu:products"),
        InlineKeyboardButton(text="🏪 По магазинам", callback_data="smenu:stores"),
        InlineKeyboardButton(text="📊 По категориям", callback_data="smenu:cats"),
    )
    builder.row(
        InlineKeyboardButton(text="🔄 Сравнить периоды", callback_data="stats_compare"),
    )
    return builder.as_markup()


def compare_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Неделя", callback_data="stats_compare:week"),
        InlineKeyboardButton(text="Месяц", callback_data="stats_compare:month"),
        InlineKeyboardButton(text="Год", callback_data="stats_compare:year"),
    )
    return builder.as_markup()


def new_period_keyboard(screen: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="День", callback_data=f"speriod:{screen}:day"),
        InlineKeyboardButton(text="Неделя", callback_data=f"speriod:{screen}:week"),
        InlineKeyboardButton(text="Месяц", callback_data=f"speriod:{screen}:month"),
    )
    builder.row(
        InlineKeyboardButton(text="Год", callback_data=f"speriod:{screen}:year"),
        InlineKeyboardButton(text="Всё время", callback_data=f"speriod:{screen}:all"),
    )
    return builder.as_markup()


def products_action_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔍 Найти продукт", callback_data=f"sprod:{period}:search"),
        InlineKeyboardButton(text="📋 Все продукты", callback_data=f"sprod:{period}:list"),
    )
    return builder.as_markup()


def products_list_keyboard(
    page_items: list[dict],
    period: str,
    page: int,
    total_pages: int,
    start_idx: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, p in enumerate(page_items):
        global_idx = start_idx + i
        display = p["normalized_name"].title()[:32]
        builder.button(text=display, callback_data=f"sproddet:{period}:{global_idx}")
    builder.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"sprodlist:{period}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶ Далее", callback_data=f"sprodlist:{period}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="⬅ К выбору", callback_data=f"sprodback:{period}"))
    return builder.as_markup()


def product_detail_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅ К списку продуктов", callback_data=f"sprodback:{period}")
    return builder.as_markup()


def stores_list_keyboard(stores: list[dict], period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, s in enumerate(stores):
        builder.button(text=(s["store"] or "?")[:32], callback_data=f"sstoredet:{period}:{i}")
    builder.adjust(1)
    return builder.as_markup()


def store_detail_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅ К списку магазинов", callback_data=f"sstorelist:{period}")
    return builder.as_markup()


def fuzzy_matches_keyboard(matches: list[str], period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, name in enumerate(matches):
        builder.button(text=name.title()[:40], callback_data=f"sprod_pick:{period}:{i}")
    builder.adjust(1)
    return builder.as_markup()


def stats_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="За неделю", callback_data="stats:7"),
        InlineKeyboardButton(text="За месяц", callback_data="stats:30"),
        InlineKeyboardButton(text="За год", callback_data="stats:365"),
    )
    return builder.as_markup()


def stats_extra_keyboard(days: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📈 График по дням", callback_data=f"trend:{days}"),
        InlineKeyboardButton(text="🏪 По магазинам", callback_data=f"stores:{days}"),
    )
    return builder.as_markup()


def recat_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить категорию", callback_data=f"recat:{receipt_id}")
    return builder.as_markup()


def recat_categories_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in RECAT_CATEGORIES:
        builder.button(text=label, callback_data=f"recat_set:{receipt_id}:{key}")
    builder.adjust(2)
    return builder.as_markup()


BUDGET_CATEGORIES: list[tuple[str, str]] = [
    ("groceries",     "🛒 Продукты"),
    ("housing",       "🏠 Жильё"),
    ("transport",     "🚗 Транспорт"),
    ("cafe",          "☕ Кафе/Рестораны"),
    ("entertainment", "🎮 Развлечения"),
    ("health",        "💊 Здоровье"),
    ("clothing",      "👕 Одежда"),
    ("subscriptions", "📱 Подписки"),
    ("other",         "❓ Другое"),
]


def budget_category_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in BUDGET_CATEGORIES:
        builder.button(text=label, callback_data=f"budget_set_cat:{key}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="📋 Показать все бюджеты", callback_data="budget_show_all"))
    return builder.as_markup()


def budget_list_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    cat_label = {key: label for key, label in BUDGET_CATEGORIES}
    for cat in categories:
        label = cat_label.get(cat, cat)
        builder.row(
            InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"budget_set_cat:{cat}"),
            InlineKeyboardButton(text="🗑", callback_data=f"budget_del:{cat}"),
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить бюджет", callback_data="budget:set"))
    return builder.as_markup()


def budget_delete_confirm_keyboard(category: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data=f"budget_del_yes:{category}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="budget_del_no"),
    )
    return builder.as_markup()


def budget_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Показать бюджеты", callback_data="budget:show"),
        InlineKeyboardButton(text="➕ Установить бюджет", callback_data="budget:set"),
    )
    return builder.as_markup()


def categories_keyboard() -> InlineKeyboardMarkup:
    categories = [
        ("🛒 Продукты", "groceries"),
        ("☕ Кафе", "cafe"),
        ("💊 Аптека", "pharmacy"),
        ("🚗 Транспорт", "transport"),
        ("📱 Электроника", "electronics"),
        ("👕 Одежда", "clothing"),
        ("🏠 Дом/Быт", "household"),
        ("📦 Прочее", "other"),
    ]
    builder = InlineKeyboardBuilder()
    for label, cb in categories:
        builder.button(text=label, callback_data=f"cat:{cb}")
    builder.adjust(2)
    return builder.as_markup()


def erste_save_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Записать все транзакции", callback_data="erste:save")
    builder.button(text="❌ Отменить", callback_data="erste:cancel")
    builder.adjust(1)
    return builder.as_markup()


def revolut_save_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Записать все транзакции", callback_data="revolut:save")
    builder.button(text="❌ Отменить", callback_data="revolut:cancel")
    builder.adjust(1)
    return builder.as_markup()


def export_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Транзакции", callback_data="export_type:transactions"),
        InlineKeyboardButton(text="🧾 Товары из чеков", callback_data="export_type:items"),
    )
    builder.row(
        InlineKeyboardButton(text="📦 Всё вместе", callback_data="export_type:all"),
    )
    return builder.as_markup()


def export_period_keyboard(export_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Сегодня", callback_data=f"export_period:{export_type}:today"),
        InlineKeyboardButton(text="Эта неделя", callback_data=f"export_period:{export_type}:week"),
    )
    builder.row(
        InlineKeyboardButton(text="Этот месяц", callback_data=f"export_period:{export_type}:month"),
        InlineKeyboardButton(text="Этот год", callback_data=f"export_period:{export_type}:year"),
    )
    builder.row(
        InlineKeyboardButton(text="Всё время", callback_data=f"export_period:{export_type}:all"),
        InlineKeyboardButton(text="Указать даты", callback_data=f"export_period:{export_type}:custom"),
    )
    return builder.as_markup()


def export_done_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Экспортировать ещё", callback_data="export_done:again"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="export_done:menu"),
    )
    return builder.as_markup()


def confirm_keyboard(prefix: str = "confirm") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"{prefix}:no"),
    )
    return builder.as_markup()
