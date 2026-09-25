from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.categories import category_label
from bot.i18n import _
from bot.markers import display_name

# Categories shown in the re-categorisation and budget pickers (subset of all
# categories). Order matches the 2-column layout described in the spec.
PICKER_CATEGORIES: list[tuple[str, str]] = [
    ("groceries",     "🛒"),
    ("housing",       "🏠"),
    ("transport",     "🚗"),
    ("cafe",          "☕"),
    ("entertainment", "🎮"),
    ("health",        "💊"),
    ("clothing",      "👕"),
    ("subscriptions", "📱"),
    ("other",         "❓"),
]
_PICKER_EMOJI = dict(PICKER_CATEGORIES)


def picker_label(category: str) -> str:
    """Emoji + localized name, as shown on the category picker buttons."""
    emoji = _PICKER_EMOJI.get(category)
    return f"{emoji} {category_label(category)}" if emoji else category_label(category)


def stats_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📦 Products"), callback_data="smenu:products"),
        InlineKeyboardButton(text=_("🏪 By store"), callback_data="smenu:stores"),
        InlineKeyboardButton(text=_("📊 By category"), callback_data="smenu:cats"),
    )
    builder.row(
        InlineKeyboardButton(text=_("🔄 Compare periods"), callback_data="stats_compare"),
        InlineKeyboardButton(text=_("📅 Subscriptions"), callback_data="subs:show"),
    )
    return builder.as_markup()


def compare_period_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("Week"), callback_data="stats_compare:week"),
        InlineKeyboardButton(text=_("Month"), callback_data="stats_compare:month"),
        InlineKeyboardButton(text=_("Year"), callback_data="stats_compare:year"),
    )
    return builder.as_markup()


def new_period_keyboard(screen: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("Day"), callback_data=f"speriod:{screen}:day"),
        InlineKeyboardButton(text=_("Week"), callback_data=f"speriod:{screen}:week"),
        InlineKeyboardButton(text=_("Month"), callback_data=f"speriod:{screen}:month"),
    )
    builder.row(
        InlineKeyboardButton(text=_("Year"), callback_data=f"speriod:{screen}:year"),
        InlineKeyboardButton(text=_("All time"), callback_data=f"speriod:{screen}:all"),
    )
    return builder.as_markup()


def products_action_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("🔍 Find a product"), callback_data=f"sprod:{period}:search"),
        InlineKeyboardButton(text=_("📋 All products"), callback_data=f"sprod:{period}:list"),
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
        nav.append(InlineKeyboardButton(text=_("◀ Back"), callback_data=f"sprodlist:{period}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text=_("▶ Next"), callback_data=f"sprodlist:{period}:{page + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text=_("⬅ Back to options"), callback_data=f"sprodback:{period}"))
    return builder.as_markup()


def product_detail_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("⬅ Back to products"), callback_data=f"sprodback:{period}")
    return builder.as_markup()


def stores_list_keyboard(stores: list[dict], period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, s in enumerate(stores):
        builder.button(text=(display_name(s["store"]) or "?")[:32], callback_data=f"sstoredet:{period}:{i}")
    builder.adjust(1)
    return builder.as_markup()


def store_detail_keyboard(period: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("⬅ Back to stores"), callback_data=f"sstorelist:{period}")
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
        InlineKeyboardButton(text=_("7 days"), callback_data="stats:7"),
        InlineKeyboardButton(text=_("30 days"), callback_data="stats:30"),
        InlineKeyboardButton(text=_("365 days"), callback_data="stats:365"),
    )
    return builder.as_markup()


def stats_extra_keyboard(days: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📈 Daily chart"), callback_data=f"trend:{days}"),
        InlineKeyboardButton(text=_("🏪 By store"), callback_data=f"stores:{days}"),
    )
    return builder.as_markup()


def recat_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("✏️ Change category"), callback_data=f"recat:{receipt_id}")
    return builder.as_markup()


def recat_categories_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, _emoji in PICKER_CATEGORIES:
        builder.button(text=picker_label(key), callback_data=f"recat_set:{receipt_id}:{key}")
    builder.adjust(2)
    return builder.as_markup()


def budget_category_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, _emoji in PICKER_CATEGORIES:
        builder.button(text=picker_label(key), callback_data=f"budget_set_cat:{key}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text=_("📋 Show all budgets"), callback_data="budget_show_all"))
    return builder.as_markup()


def budget_list_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat in categories:
        label = picker_label(cat)
        builder.row(
            InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"budget_set_cat:{cat}"),
            InlineKeyboardButton(text="🗑", callback_data=f"budget_del:{cat}"),
        )
    builder.row(InlineKeyboardButton(text=_("➕ Add a budget"), callback_data="budget:set"))
    return builder.as_markup()


def budget_delete_confirm_keyboard(category: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("✅ Yes"), callback_data=f"budget_del_yes:{category}"),
        InlineKeyboardButton(text=_("❌ No"), callback_data="budget_del_no"),
    )
    return builder.as_markup()


def budget_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📋 Show budgets"), callback_data="budget:show"),
        InlineKeyboardButton(text=_("➕ Set a budget"), callback_data="budget:set"),
    )
    return builder.as_markup()


# Categories offered by the manual /add flow.
_MANUAL_CATEGORIES: list[tuple[str, str]] = [
    ("groceries", "🛒"),
    ("cafe", "☕"),
    ("pharmacy", "💊"),
    ("transport", "🚗"),
    ("electronics", "📱"),
    ("clothing", "👕"),
    ("household", "🏠"),
    ("other", "📦"),
]


def categories_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, emoji in _MANUAL_CATEGORIES:
        builder.button(text=f"{emoji} {category_label(key)}", callback_data=f"cat:{key}")
    builder.adjust(2)
    return builder.as_markup()


def erste_save_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("✅ Save all transactions"), callback_data="erste:save")
    builder.button(text=_("❌ Cancel"), callback_data="erste:cancel")
    builder.adjust(1)
    return builder.as_markup()


def revolut_save_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=_("✅ Save all transactions"), callback_data="revolut:save")
    builder.button(text=_("❌ Cancel"), callback_data="revolut:cancel")
    builder.adjust(1)
    return builder.as_markup()


def export_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📊 Transactions"), callback_data="export_type:transactions"),
        InlineKeyboardButton(text=_("🧾 Receipt items"), callback_data="export_type:items"),
    )
    builder.row(
        InlineKeyboardButton(text=_("📦 Everything"), callback_data="export_type:all"),
    )
    return builder.as_markup()


def export_period_keyboard(export_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("Today"), callback_data=f"export_period:{export_type}:today"),
        InlineKeyboardButton(text=_("This week"), callback_data=f"export_period:{export_type}:week"),
    )
    builder.row(
        InlineKeyboardButton(text=_("This month"), callback_data=f"export_period:{export_type}:month"),
        InlineKeyboardButton(text=_("This year"), callback_data=f"export_period:{export_type}:year"),
    )
    builder.row(
        InlineKeyboardButton(text=_("All time"), callback_data=f"export_period:{export_type}:all"),
        InlineKeyboardButton(text=_("Custom dates"), callback_data=f"export_period:{export_type}:custom"),
    )
    return builder.as_markup()


def export_done_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=_("📊 Export more"), callback_data="export_done:again"),
        InlineKeyboardButton(text=_("🏠 Main menu"), callback_data="export_done:menu"),
    )
    return builder.as_markup()
