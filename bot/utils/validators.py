import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"groceries", "cafe", "pharmacy", "transport", "electronics", "clothing", "household", "other"}
REQUIRED_KEYS = {"store", "date", "currency", "total", "items"}
REQUIRED_ITEM_KEYS = {"name", "quantity", "total_price"}


def validate_receipt(data: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"Отсутствуют обязательные поля: {missing}")

    if not isinstance(data.get("items"), list) or len(data["items"]) == 0:
        raise ValueError("Поле 'items' должно быть непустым списком")

    cleaned_items = []
    for i, item in enumerate(data["items"]):
        item_missing = REQUIRED_ITEM_KEYS - set(item.keys())
        if item_missing:
            raise ValueError(f"Позиция {i}: отсутствуют поля {item_missing}")

        cleaned_item = {
            "name": item["name"],
            "quantity": item.get("quantity", 1) or 1,
            "unit_price": item.get("unit_price"),
            "total_price": item.get("total_price", 0) or 0,
            "category": item.get("category", "other") if item.get("category") in VALID_CATEGORIES else "other",
        }
        cleaned_items.append(cleaned_item)

    result = {
        "store": data.get("store"),
        "date": data.get("date"),
        "currency": data.get("currency", "PLN") or "PLN",
        "total": data.get("total") or 0,
        "items": cleaned_items,
        "total_mismatch": False,
    }

    if result["total"] is not None and result["total"] != 0:
        items_sum = sum(float(it["total_price"]) for it in cleaned_items)
        declared_total = float(result["total"])
        if abs(items_sum - declared_total) > 0.02:
            # Amounts only at DEBUG: production logs must not carry spending data.
            logger.warning("Receipt total mismatch: declared total differs from items sum")
            logger.debug("Receipt total mismatch: declared %s, items sum %.2f", declared_total, items_sum)
            result["total_mismatch"] = True

    return result
