from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db.crud import _merchant_group_key, _strip_reference_suffix, get_subscriptions
from bot.db.models import Category, Item, Receipt


def test_strip_reference_suffix_removes_trailing_payment_code():
    assert _strip_reference_suffix("SPOTIFY P422DD162C") == "SPOTIFY"
    assert _strip_reference_suffix("SPOTIFY P3E28A7D47") == "SPOTIFY"


def test_strip_reference_suffix_removes_generic_word_suffix():
    assert _strip_reference_suffix("CLAUDE SUBSCRIPTION") == "CLAUDE"


def test_strip_reference_suffix_keeps_short_merchant_names_intact():
    # "PL" is too short to look like a reference code -> untouched.
    assert _strip_reference_suffix("MEDIAEXPERT PL") == "MEDIAEXPERT PL"


def test_strip_reference_suffix_guards_against_stripping_whole_name():
    # Would strip to "", guarded back to the original.
    assert _strip_reference_suffix("MEDIAEXPERT") == "MEDIAEXPERT"


def test_merchant_group_key_merges_spotify_variants():
    keys = {
        _merchant_group_key("Spotify P422DD162C"),
        _merchant_group_key("Spotify P3E28A7D47"),
        _merchant_group_key("Spotify P3F1B0839F"),
    }
    assert keys == {"SPOTIFY"}


def test_merchant_group_key_merges_claude_variants():
    assert _merchant_group_key("Claude") == _merchant_group_key("claude subscription")


# --- get_subscriptions integration-style tests (mocked session, no real DB) ---

FIXED_TODAY = date(2026, 8, 23)


def _receipt(id: int, when: date, amount: float, store: str, category: Category | None = None) -> Receipt:
    r = Receipt(id=id, user_id=1, store=store, date=when, currency="PLN", total=amount, total_pln=amount, category=category)
    r.items = []
    return r


async def _get_subscriptions(receipts: list[Receipt]) -> list[dict]:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = receipts
    session.execute = AsyncMock(return_value=result)
    with patch("bot.db.crud.datetime") as mock_dt:
        mock_dt.utcnow.return_value = datetime(FIXED_TODAY.year, FIXED_TODAY.month, FIXED_TODAY.day)
        return await get_subscriptions(session, user_id=1)


@pytest.mark.asyncio
async def test_stale_pattern_subscription_is_excluded():
    # ~30-day cadence but nothing charged in the last ~160 days -> likely cancelled.
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=220), 120.0, "SMART GYM"),
        _receipt(2, FIXED_TODAY - timedelta(days=190), 120.0, "SMART GYM"),
        _receipt(3, FIXED_TODAY - timedelta(days=160), 120.0, "SMART GYM"),
    ]
    assert await _get_subscriptions(receipts) == []


@pytest.mark.asyncio
async def test_stale_category_tagged_subscription_stays_with_unknown_next_expected():
    # Category-tagged AND pattern-matched, but hasn't recurred in a long time.
    # The category tag still means "this is a subscription" -> keep it, but stop
    # pretending we know when the next charge will land.
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=220), 27.14, "Spotify AB", category=Category.subscriptions),
        _receipt(2, FIXED_TODAY - timedelta(days=190), 27.14, "Spotify AB", category=Category.subscriptions),
        _receipt(3, FIXED_TODAY - timedelta(days=160), 27.14, "Spotify AB", category=Category.subscriptions),
    ]
    subs = await _get_subscriptions(receipts)
    assert len(subs) == 1
    assert subs[0]["detection"] == "both"
    assert subs[0]["next_expected"] is None


@pytest.mark.asyncio
async def test_recently_active_pattern_subscription_is_kept():
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=60), 120.0, "SMART GYM"),
        _receipt(2, FIXED_TODAY - timedelta(days=30), 120.0, "SMART GYM"),
        _receipt(3, FIXED_TODAY, 120.0, "SMART GYM"),
    ]
    subs = await _get_subscriptions(receipts)
    assert len(subs) == 1
    assert subs[0]["detection"] == "pattern"
    assert subs[0]["next_expected"] == FIXED_TODAY + timedelta(days=30)


@pytest.mark.asyncio
async def test_display_amount_uses_latest_occurrence_not_first():
    # Price dropped from 27.14 to 14.49 -> display should reflect the new price.
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=90), 27.14, "Spotify AB", category=Category.subscriptions),
        _receipt(2, FIXED_TODAY - timedelta(days=60), 27.14, "Spotify AB", category=Category.subscriptions),
        _receipt(3, FIXED_TODAY - timedelta(days=30), 14.49, "Spotify AB", category=Category.subscriptions),
    ]
    subs = await _get_subscriptions(receipts)
    assert len(subs) == 1
    assert subs[0]["amount"] == pytest.approx(14.49)
    assert subs[0]["monthly_total_pln"] == pytest.approx(14.49)
    assert subs[0]["last_charge"] == FIXED_TODAY - timedelta(days=30)


@pytest.mark.asyncio
async def test_two_occurrence_coincidence_is_not_flagged():
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=30), 8.60, "Zakup BLIK ASTARIUM"),
        _receipt(2, FIXED_TODAY, 8.60, "Zakup BLIK ASTARIUM"),
    ]
    assert await _get_subscriptions(receipts) == []


@pytest.mark.asyncio
async def test_three_occurrence_pattern_is_flagged():
    receipts = [
        _receipt(1, FIXED_TODAY - timedelta(days=60), 8.60, "Zakup BLIK ASTARIUM"),
        _receipt(2, FIXED_TODAY - timedelta(days=30), 8.60, "Zakup BLIK ASTARIUM"),
        _receipt(3, FIXED_TODAY, 8.60, "Zakup BLIK ASTARIUM"),
    ]
    subs = await _get_subscriptions(receipts)
    assert len(subs) == 1
    assert subs[0]["detection"] == "pattern"
