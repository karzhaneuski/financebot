from datetime import date

import pytest

from bot.db import crud
from tests.conftest import make_receipt


# ── parse_search_query (offline fallback path — no API key/network in tests) ──

@pytest.fixture
def no_llm(monkeypatch):
    """Force the regex fallback by making the Anthropic client unusable."""
    import bot.services.search_parser as sp
    class _Boom:
        def __init__(self, *a, **k):
            pass
        @property
        def messages(self):
            raise RuntimeError("no network")
    monkeypatch.setattr(sp.anthropic, "AsyncAnthropic", _Boom)


async def test_parse_amount_range(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, _ = await parse_search_query("покупки дороже 200 злотых в мае", today=date(2026, 8, 24))
    assert parsed["amount_min"] == 200.0
    assert parsed["date_from"] == date(2026, 5, 1)
    assert parsed["date_to"] == date(2026, 5, 31)


async def test_parse_last_month(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, _ = await parse_search_query("траты в прошлом месяце дешевле 50", today=date(2026, 8, 24))
    assert parsed["date_from"] == date(2026, 7, 1)
    assert parsed["date_to"] == date(2026, 7, 31)
    assert parsed["amount_max"] == 50.0


async def test_parse_category_keyword(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, _ = await parse_search_query("аптека в июне", today=date(2026, 8, 24))
    assert parsed["category"] == "pharmacy"
    assert parsed["date_from"].month == 6


async def test_parse_cash_flag(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, _ = await parse_search_query("снятие наличных за август", today=date(2026, 8, 24))
    assert parsed.get("include_cash") is True


# ── search_transactions ───────────────────────────────────────────────────────

async def test_search_fuzzy_merchant_and_dates(db_session):
    await make_receipt(db_session, user_id=7, store="Kaufland Srodmiescie",
                 date=date(2026, 6, 10), total_pln=120.0)
    await make_receipt(db_session, user_id=7, store="KAUFLAND OSIEDLE",
                 date=date(2026, 7, 2), total_pln=80.0)
    await make_receipt(db_session, user_id=7, store="Biedronka",
                 date=date(2026, 6, 20), total_pln=60.0)
    # other user must not leak
    await make_receipt(db_session, user_id=8, store="Kaufland X", date=date(2026, 6, 5))

    rows = await crud.search_transactions(db_session, 7, merchant="kaufland")
    assert len(rows) == 2
    assert all("kaufland" in r.store.lower() for r in rows)

    rows = await crud.search_transactions(
        db_session, 7, merchant="kaufland", date_from=date(2026, 7, 1), date_to=date(2026, 7, 30)
    )
    assert len(rows) == 1
    assert rows[0].store == "KAUFLAND OSIEDLE"


async def test_search_excludes_cash_unless_asked(db_session):
    await make_receipt(db_session, user_id=7, store="Bankomat", tx_type="cash_withdrawal", total_pln=500.0)
    await make_receipt(db_session, user_id=7, store="Zabka", tx_type="purchase", total_pln=20.0)

    rows = await crud.search_transactions(db_session, 7)
    assert [r.store for r in rows] == ["Zabka"]

    rows = await crud.search_transactions(db_session, 7, include_cash=True)
    assert len(rows) == 2


async def test_search_amount_filters_use_personal_total(db_session):
    await make_receipt(db_session, user_id=7, store="Big", total_pln=300.0, personal_total_pln=150.0)
    await make_receipt(db_session, user_id=7, store="Small", total_pln=100.0)

    rows = await crud.search_transactions(db_session, 7, amount_min=200.0)
    assert [r.store for r in rows] == []  # 300 counts as 150 after split

    rows = await crud.search_transactions(db_session, 7, amount_max=160.0)
    assert {r.store for r in rows} == {"Big", "Small"}


# ── wrapped ───────────────────────────────────────────────────────────────────

async def test_wrapped_collects_and_renders(db_session, tmp_path):
    year = date.today().year
    await make_receipt(db_session, user_id=9, store="Kaufland", date=date(year, 3, 5),
                 total=100.0, total_pln=100.0,
                 items=[{"name": "Coca-Cola", "total_price": 40.0, "category": "groceries"},
                        {"name": "Chleb", "total_price": 60.0, "category": "groceries"}])
    await make_receipt(db_session, user_id=9, store="Kaufland", date=date(year, 4, 5),
                 total=50.0, total_pln=50.0,
                 items=[{"name": "Coca-Cola", "total_price": 50.0, "category": "groceries"}])
    await make_receipt(db_session, user_id=9, store="Apteka", date=date(year, 8, 1),
                 total=30.0, total_pln=30.0,
                 items=[{"name": "Witamina C", "total_price": 30.0, "category": "pharmacy"}])

    from bot.services.wrapped import build_wrapped_caption, collect_wrapped_stats, render_wrapped_image
    stats = await collect_wrapped_stats(db_session, 9)
    assert stats is not None and stats["year"] == year
    assert stats["total"] == pytest.approx(180.0)
    assert stats["tx_count"] == 3
    assert stats["top_category"]["category"] == "groceries"
    assert stats["most_visited"]["store"] == "Kaufland"
    assert stats["priciest"].personal_amount() == pytest.approx(100.0)
    assert stats["products"][0]["normalized_name"] == "coca-cola"

    png = render_wrapped_image(stats)
    out = tmp_path / "wrapped.png"
    out.write_bytes(png)
    assert out.stat().st_size > 60000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    # Assert on rendered content: enough non-background pixels (text/cards/bars
    # actually painted), header present near the top, no dead band at the bottom.
    import io as _io

    from PIL import Image

    img = Image.open(_io.BytesIO(png)).convert("RGB")
    w_px, h_px = img.size
    bg = tuple(int(_BG[i:i + 2], 16) for _BG in ["#F7F5F0"] for i in (1, 3, 5))
    px = img.load()
    non_bg = sum(
        1 for y in range(0, h_px, 4) for x in range(0, w_px, 4) if px[x, y] != bg
    )
    sampled = len(range(0, h_px, 4)) * len(range(0, w_px, 4))
    assert non_bg / sampled > 0.10  # real content, not a blank canvas
    top_non_bg = sum(
        1 for y in range(0, h_px // 20) for x in range(w_px) if px[x, y] != bg
    )
    assert top_non_bg > 0           # header text visible near the top edge
    bottom_non_bg = sum(
        1 for y in range(h_px - h_px // 20, h_px) for x in range(w_px) if px[x, y] != bg
    )
    assert bottom_non_bg == 0       # dead band cropped (canvas trimmed)

    caption = build_wrapped_caption(stats)
    assert "180" in caption


async def test_wrapped_empty_returns_none(db_session):
    from bot.services.wrapped import collect_wrapped_stats
    assert await collect_wrapped_stats(db_session, 12345) is None


async def test_wrapped_excludes_income_and_cash(db_session):
    """Headline total must match the expense-only breakdowns (consistency fix)."""
    year = date.today().year
    await make_receipt(db_session, user_id=95, store="Zabka", date=date(year, 2, 1),
                       total=50.0, total_pln=50.0)
    await make_receipt(db_session, user_id=95, store="Salary", tx_type="income",
                       date=date(year, 2, 2), total=5000.0, total_pln=5000.0)
    await make_receipt(db_session, user_id=95, store="Bankomat", tx_type="cash_withdrawal",
                       date=date(year, 2, 3), total=200.0, total_pln=200.0)

    from bot.services.wrapped import collect_wrapped_stats
    stats = await collect_wrapped_stats(db_session, 95)
    assert stats is not None
    assert stats["tx_count"] == 1
    assert stats["total"] == pytest.approx(50.0)
    # monthly breakdown agrees with the headline
    assert sum(stats["monthly"]) == pytest.approx(stats["total"])


# ── split: personal flags + personal_total_pln propagation into aggregates ────

async def test_personal_total_propagates_into_totals(db_session):
    # Full receipt 200 PLN: item A (mine) 150, item B (not mine) 50.
    r, item_ids = await make_receipt(db_session, user_id=11, store="Lidl", date=date.today(),
                     total=200.0, total_pln=200.0,
                     items=[{"name": "A", "total_price": 150.0, "category": "groceries"},
                            {"name": "B", "total_price": 50.0, "category": "other"}])
    await crud.set_item_personal_flags(db_session, r.id, [item_ids[0]])
    r = await crud.get_receipt_by_id(db_session, r.id)  # eager reload with flags

    # Receipt-level totals
    today = date.today()
    month_start = today.replace(day=1)
    total, count = await crud.get_total_spending_range(db_session, 11, month_start, today)
    assert total == pytest.approx(150.0)
    expenses = await crud.get_expenses_total_range(db_session, 11, month_start, today)
    assert expenses == pytest.approx(150.0)

    # Category aggregate: items are all-or-nothing by is_personal flag.
    cats = await crud.get_spending_by_category_range(db_session, 11, month_start, today)
    cat_map = {c["category"]: c["total_pln"] for c in cats}
    assert cat_map.get("groceries") == pytest.approx(150.0)   # mine
    assert cat_map.get("other", 0.0) == pytest.approx(0)      # excluded (absent)

    # Store aggregate
    stores = await crud.get_spending_by_store_range(db_session, 11, month_start, today)
    assert stores[0]["total_pln"] == pytest.approx(150.0)

    # Monthly spending by category (budgets)
    monthly = await crud.get_monthly_spending_by_category(db_session, 11, today.strftime("%Y-%m"))
    assert monthly.get("groceries") == pytest.approx(150.0)
    assert "other" not in monthly

    # Flags persisted on items + clamped personal total stored on the receipt
    assert r.items[0].is_personal is True
    assert r.items[1].is_personal is False
    assert float(r.personal_total_pln) == pytest.approx(150.0)


async def test_split_mixed_categories_exact_regression(db_session):
    """Review regression: 50 groceries kept + 100 electronics excluded.

    groceries must show exactly 50 (kept item's full price), electronics must
    be absent/zero — NOT a proportional 37.5-style split of either category.
    """
    r, item_ids = await make_receipt(db_session, user_id=21, store="MediaMarkt", date=date.today(),
                     total=150.0, total_pln=150.0,
                     items=[{"name": "Chleb", "total_price": 50.0, "category": "groceries"},
                            {"name": "TV", "total_price": 100.0, "category": "electronics"}])
    await crud.set_item_personal_flags(db_session, r.id, [item_ids[0]])

    today = date.today()
    cats = await crud.get_spending_by_category_range(db_session, 21, today, today)
    cat_map = {c["category"]: c["total_pln"] for c in cats}
    assert cat_map.get("groceries") == pytest.approx(50.0)
    assert cat_map.get("electronics", 0.0) == pytest.approx(0.0)

    total, _ = await crud.get_total_spending_range(db_session, 21, today, today)
    assert total == pytest.approx(50.0)
    r2 = await crud.get_receipt_by_id(db_session, r.id)
    assert float(r2.personal_total_pln) == pytest.approx(50.0)


async def test_null_is_personal_counts_as_personal(db_session):
    """Pre-existing (never-split) items: is_personal IS NULL -> fully personal."""
    await make_receipt(db_session, user_id=31, store="Zabka", date=date.today(),
                       total=80.0, total_pln=80.0,
                       items=[{"name": "Mleko", "total_price": 80.0, "category": "groceries"}])
    today = date.today()
    total, _ = await crud.get_total_spending_range(db_session, 31, today, today)
    assert total == pytest.approx(80.0)

    monthly = await crud.get_monthly_spending_by_category(db_session, 31, today.strftime("%Y-%m"))
    assert monthly.get("groceries") == pytest.approx(80.0)


async def test_split_ratio_clamped_to_unit_range(db_session):
    """Discount/deposit lines can push my_native above the receipt total — clamp."""
    r, item_ids = await make_receipt(db_session, user_id=41, store="Lidl", date=date.today(),
                     total=100.0, total_pln=100.0,
                     items=[{"name": "A", "total_price": 120.0, "category": "other"},
                            {"name": "Rabat", "total_price": -20.0, "category": "other"}])
    mine = item_ids  # both marked mine; native sum (100) equals receipt total here
    await crud.set_item_personal_flags(db_session, r.id, mine)
    r2 = await crud.get_receipt_by_id(db_session, r.id)
    assert float(r2.personal_total_pln) == pytest.approx(100.0)

    # Over-1 case: mark everything mine on a receipt whose items sum ABOVE total
    r3, ids3 = await make_receipt(db_session, user_id=42, store="Lidl", date=date.today(),
                                  total=100.0, total_pln=100.0,
                                  items=[{"name": "X", "total_price": 130.0, "category": "other"}])
    await crud.set_item_personal_flags(db_session, r3.id, ids3)
    r3b = await crud.get_receipt_by_id(db_session, r3.id)
    assert float(r3b.personal_total_pln) == pytest.approx(100.0)  # clamped to 1.0


async def test_split_preloads_saved_flags_on_reopen(db_session):
    """Re-opening /split on an already-split receipt restores saved toggles."""
    r, item_ids = await make_receipt(db_session, user_id=51, store="Biedronka", date=date.today(),
                     total=60.0, total_pln=60.0,
                     items=[{"name": "A", "total_price": 40.0, "category": "groceries"},
                            {"name": "B", "total_price": 20.0, "category": "other"}])
    await crud.set_item_personal_flags(db_session, r.id, [item_ids[1]])  # only B mine

    r2 = await crud.get_receipt_by_id(db_session, r.id)
    mine = [it.id for it in r2.items if it.is_personal is not False]
    assert mine == [item_ids[1]]  # saved selection survives re-open


async def test_null_personal_total_behaves_as_before(db_session):
    await make_receipt(db_session, user_id=12, store="Zabka", date=date.today(), total=77.0, total_pln=77.0)
    today = date.today()
    total, _ = await crud.get_total_spending_range(db_session, 12, today, today)
    assert total == pytest.approx(77.0)


# ── search: category via EXISTS over items ────────────────────────────────────

async def test_search_category_matches_item_level_for_ocr_receipts(db_session):
    """OCR receipts carry category only on items — search must find them."""
    r, _ = await make_receipt(db_session, user_id=61, store="Apteka Nova",
                           date=date(2026, 6, 10), total=30.0, total_pln=30.0,
                           items=[{"name": "Witamina C", "total_price": 30.0,
                                   "category": "pharmacy"}])
    rows = await crud.search_transactions(db_session, 61, category="pharmacy")
    assert [x.id for x in rows] == [r.id]

    rows = await crud.search_transactions(db_session, 61, category="clothing")
    assert rows == []


async def test_search_limit_and_fallback_flag_parsing(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, used_fallback = await parse_search_query(
        "продукты за июнь дороже 100", today=date(2026, 8, 24)
    )
    assert used_fallback is True
    assert parsed["category"] == "groceries"
    assert parsed["amount_min"] == 100.0
    assert parsed["date_from"].month == 6


async def test_fallback_month_requires_preposition(no_llm):
    from bot.services.search_parser import parse_search_query
    # A month-like word without за/в/на before it must not create a date filter.
    parsed, _ = await parse_search_query("траты Marta", today=date(2026, 8, 24))
    assert "date_from" not in parsed
    parsed, _ = await parse_search_query("траты в марте", today=date(2026, 8, 24))
    assert parsed.get("date_from") == date(2026, 3, 1)


async def test_fallback_drops_future_date_ranges(no_llm):
    from bot.services.search_parser import parse_search_query
    parsed, _ = await parse_search_query("за декабрь дороже 10", today=date(2026, 8, 24))
    assert "date_from" not in parsed and "date_to" not in parsed
    assert parsed["amount_min"] == 10.0
