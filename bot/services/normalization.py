import json
import logging
import sys
import traceback

import anthropic

from bot.config import settings

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


async def normalize_item_names(items: list[dict]) -> list[dict]:
    """Enrich each item dict in-place with 'normalized_name' and 'volume_ml'. Falls back to lowercased name on error."""
    if not items:
        return items

    names = [item.get("name", "") for item in items]

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(names, ensure_ascii=False)}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
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
        logger.warning("Normalization returned unexpected length: %d vs %d", len(parsed), len(items))
    except Exception as e:
        print(f"Normalization error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    for item in items:
        item["normalized_name"] = (item.get("name") or "").lower().strip()
        item.setdefault("volume_ml", None)
    return items
