import json
import logging
import sys
import traceback

from bot.services.llm import LLMUnavailableError, get_provider

logger = logging.getLogger(__name__)

_SYSTEM = """You are a receipt product normalizer. Given a JSON array of raw product names from receipts, return a JSON array of objects (same length, same order).

Each object must have:
- "original": the input name (copy verbatim)
- "normalized": normalized product name (brand + product type, Title Case)
- "volume_ml": volume in millilitres as an integer, or null

Normalization rules:
- Strip volume/weight/size from name: "Coca-Cola 0,33L" → "Coca-Cola", "Mleko 1L" → "Mleko"
- Strip quantity prefixes: "2x Jogurt" → "Jogurt"
- Fix store-specific compact formatting: "CocaColaNapój0,33L" → "Coca-Cola"
- Normalize brand names to canonical form: "coca cola" → "Coca-Cola"
- Keep it short — brand + product type only

Volume extraction rules:
- "0,5L" / "0.5l" / "500ml" / "500mL" → 500
- "1L" / "1l" → 1000
- "1,5L" → 1500
- "0,33L" / "330ml" → 330
- "2x2l" → 4000 (pack_qty × volume: 2 × 2000)
- "2x0,5l" → 1000 (2 × 500)
- Concatenated brand+volume like "CocaColaNapój0,33L" → 330, "MonsterUltraParad0,5" → 500
- If unit is kg/g (solid food, not liquid) → null

Energy drink defaults (apply when no volume is stated in the name):
- Monster Energy (any variant: Ultra, Assault, Mango Loco, Violet, etc.) → 500
- Red Bull (any variant) → 250
- Tiger Energy (any variant) → 500
- "Napój energetyczny" / "energi" / "energy drink" with no volume → 500
- Abbreviated store names like "NapMonsUltra*", "Nap Ener*" refer to energy drinks → 500

Cola/soda defaults (no volume stated → null, ambiguous between can and bottle):
- Coca-Cola, Pepsi, Sprite, Fanta with no volume → null

Return ONLY a JSON array, no markdown, no explanation

Examples:
Input:  ["Monster Ultra Violet 0,5L", "NapMonsUltraViolet", "Napój energetyczny 0,5L", "energi 500ml", "CocaColaNapój0,33L", "Coca-Cola", "Chleb tostowy", "Mleko 1L"]
Output: [
  {"original": "Monster Ultra Violet 0,5L",  "normalized": "Monster Energy",    "volume_ml": 500},
  {"original": "NapMonsUltraViolet",          "normalized": "Monster Energy",    "volume_ml": 500},
  {"original": "Napój energetyczny 0,5L",     "normalized": "Napój Energetyczny","volume_ml": 500},
  {"original": "energi 500ml",                "normalized": "Napój Energetyczny","volume_ml": 500},
  {"original": "CocaColaNapój0,33L",          "normalized": "Coca-Cola",         "volume_ml": 330},
  {"original": "Coca-Cola",                   "normalized": "Coca-Cola",         "volume_ml": null},
  {"original": "Chleb tostowy",               "normalized": "Chleb Tostowy",     "volume_ml": null},
  {"original": "Mleko 1L",                    "normalized": "Mleko",             "volume_ml": 1000}
]"""


# Same shape as the array the prompt asks for (enforced by Gemini).
NORMALIZATION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "original": {"type": "string"},
            "normalized": {"type": "string"},
            "volume_ml": {"type": ["integer", "null"]},
        },
        "required": ["original", "normalized", "volume_ml"],
    },
}


async def normalize_item_names(items: list[dict]) -> list[dict]:
    """Enrich each item dict in-place with 'normalized_name' and 'volume_ml'.

    Falls back to the lowercased raw name when the LLM provider is
    unavailable or answers badly — the receipt is always saved.
    """
    if not items:
        return items

    names = [item.get("name", "") for item in items]

    try:
        parsed = await get_provider().generate_json(
            system=_SYSTEM,
            prompt=json.dumps(names, ensure_ascii=False),
            schema=NORMALIZATION_SCHEMA,
            tier="fast",
            max_tokens=2048,
        )
        if isinstance(parsed, list) and len(parsed) == len(items):
            for item, entry in zip(items, parsed):
                if isinstance(entry, dict):
                    item["normalized_name"] = str(entry.get("normalized", "")).lower().strip()
                    vol = entry.get("volume_ml")
                    item["volume_ml"] = int(vol) if vol is not None else None
                else:
                    # fallback: old string format
                    item["normalized_name"] = str(entry).lower().strip()
                    item["volume_ml"] = None
            return items
        logger.warning("Normalization returned unexpected shape: %s (%d items expected)",
                       type(parsed).__name__, len(items))
    except LLMUnavailableError as e:
        # Expected while quota/balance is exhausted — no traceback per receipt.
        logger.warning("Normalization skipped, LLM provider unavailable: %s", e)
    except Exception as e:
        print(f"Normalization error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    for item in items:
        item["normalized_name"] = (item.get("name") or "").lower().strip()
        item.setdefault("volume_ml", None)
    return items
