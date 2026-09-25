import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"groceries", "cafe", "pharmacy", "transport", "electronics", "clothing", "household", "other"}
REQUIRED_KEYS = {"store", "date", "currency", "total", "items"}
REQUIRED_ITEM_KEYS = {"name", "quantity", "total_price"}


def validate_receipt(data: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not isinstance(data.get("items"), list) or len(data["items"]) == 0:
        raise ValueError("Field 'items' must be a non-empty list")

    cleaned_items = []
    for i, item in enumerate(data["items"]):
        item_missing = REQUIRED_ITEM_KEYS - set(item.keys())
        if item_missing:
            raise ValueError(f"Item {i}: missing fields {item_missing}")

        cleaned_item = {
            "name": item["name"],
            "quantity": item.get("quantity", 1) or 1,
            "unit_price": item.get("unit_price"),
            "total_price": item.get("total_price", 0) or 0,
            "category": item.get("category", "other") if item.get("category") in VALID_CATEGORIES else "other",
        }
        cleaned_items.append(cleaned_item)

    items_sum = round(sum(float(it["total_price"]) for it in cleaned_items), 2)
    declared_total = _to_float(data.get("total"))

    result = {
        "store": data.get("store"),
        "date": data.get("date"),
        "currency": data.get("currency", "PLN") or "PLN",
        "total": declared_total or 0,
        "items": cleaned_items,
        "total_mismatch": False,
        "total_from_items": False,
    }

    if not declared_total and items_sum > 0:
        # The model gave no usable total (null/0 — e.g. it didn't see the
        # "Suma PLN" line). Saving 0 would hide the purchase from every
        # statistic, so fall back to the items sum.
        # Amounts only at DEBUG: production logs must not carry spending data.
        logger.warning("Receipt total missing or zero, using the sum of items instead")
        logger.debug("Receipt total fallback: declared %r, items sum %.2f", data.get("total"), items_sum)
        result["total"] = items_sum
        result["total_from_items"] = True
    elif declared_total and abs(items_sum - declared_total) > 0.02:
        logger.warning("Receipt total mismatch: declared total differs from items sum")
        logger.debug("Receipt total mismatch: declared %s, items sum %.2f", declared_total, items_sum)
        result["total_mismatch"] = True

    return result


def _to_float(value: Any) -> float | None:
    """Model output as a number; tolerates "52,94"-style strings. None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None
