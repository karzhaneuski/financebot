"""Tests for the three Erste Bank parser fixes."""

from bot import markers
import pytest

from bot.parsers.erste import (
    CASH_WITHDRAWAL_KEYWORDS,
    categorize,
    _parse_block,
    _extract_transactions,
)


# ---------------------------------------------------------------------------
# Fix 1: "Akademik" → housing category
# ---------------------------------------------------------------------------

class TestHousingCategory:
    def test_akademik_maps_to_housing(self):
        db_cat, display = categorize("Akademik")
        assert db_cat == "housing"
        assert display == "Жильё"

    def test_akademik_case_insensitive(self):
        db_cat, _ = categorize("AKADEMIK")
        assert db_cat == "housing"

        db_cat, _ = categorize("akademik")
        assert db_cat == "housing"

    def test_akademik_katowice(self):
        db_cat, _ = categorize("Akademik Katowice")
        assert db_cat == "housing"

    def test_czynsz_maps_to_housing(self):
        db_cat, display = categorize("czynsz za lokal")
        assert db_cat == "housing"
        assert display == "Жильё"

    def test_najem_maps_to_housing(self):
        db_cat, _ = categorize("Najem mieszkania")
        assert db_cat == "housing"

    def test_wynajem_maps_to_housing(self):
        db_cat, _ = categorize("Wynajem apartamentu")
        assert db_cat == "housing"

    def test_groceries_still_works(self):
        db_cat, _ = categorize("Biedronka Katowice")
        assert db_cat == "groceries"

    def test_unknown_falls_back_to_other(self):
        db_cat, display = categorize("SomeRandomStore")
        assert db_cat == "other"
        assert display == "Прочее"


# ---------------------------------------------------------------------------
# Fix 2: Foreign-currency card payment with no merchant name
# ---------------------------------------------------------------------------

def _make_akademik_block():
    """Helper: minimal block lines for an Erste dorm charge."""
    return [
        "OBCIĄŻENIE",
        "Tytuł: przelew akademiku 2025-05",
        "-800,00 PLN",
    ]


def _make_foreign_no_merchant_block():
    """Block representing a foreign-currency card payment with no merchant."""
    return [
        "TRANSAKCJA KARTĄ",
        "Tytuł: DOP. VISA 4213525046 PŁATNOŚĆ KARTĄ 149.01 EUR 1 EUR=4.4078 PLN",
        "-657,16 PLN",
    ]


def _make_foreign_with_merchant_block():
    """Block with a merchant name after the PLN rate."""
    return [
        "TRANSAKCJA KARTĄ",
        "Tytuł: DOP. VISA 4213525046 PŁATNOŚĆ KARTĄ 29.99 EUR 1 EUR=4.2100 PLN efihotel.cz Brno",
        "-126,29 PLN",
    ]


class TestForeignCardNoMerchant:
    def test_no_merchant_sets_flag(self):
        tx = _parse_block("2025-05-10", _make_foreign_no_merchant_block())
        assert tx is not None
        assert tx.get("foreign_card_no_merchant") is True

    def test_no_merchant_captures_orig_amount_and_currency(self):
        tx = _parse_block("2025-05-10", _make_foreign_no_merchant_block())
        assert tx["orig_amount"] == pytest.approx(149.01)
        assert tx["orig_currency"] == "EUR"

    def test_no_merchant_description_is_empty_or_fallback(self):
        tx = _parse_block("2025-05-10", _make_foreign_no_merchant_block())
        # description should be empty string or raw_type fallback (not the full garbage string)
        assert "VISA" not in tx["description"]
        assert "PŁATNOŚĆ KARTĄ" not in tx["description"].upper()

    def test_with_merchant_does_not_set_flag(self):
        tx = _parse_block("2025-05-10", _make_foreign_with_merchant_block())
        assert tx is not None
        assert not tx.get("foreign_card_no_merchant")
        assert "efihotel" in tx["description"].lower()


# ---------------------------------------------------------------------------
# Fix 3: Cash withdrawals get type="cash_withdrawal"
# ---------------------------------------------------------------------------

def _make_cash_withdrawal_block():
    return [
        "TRANSAKCJA KARTĄ",
        "Tytuł: DOP. VISA 4213525046 WYPŁATA Z BANKOMATU KARTĄ 500,00 PLN",
        "-500,00 PLN",
    ]


def _make_regular_expense_block():
    return [
        "TRANSAKCJA KARTĄ",
        "Tytuł: DOP. VISA 4213525046 PŁATNOŚĆ KARTĄ 45,00 PLN Kaufland",
        "-45,00 PLN",
    ]


class TestCashWithdrawal:
    def test_bankomat_tytul_sets_cash_withdrawal_type(self):
        tx = _parse_block("2025-05-15", _make_cash_withdrawal_block())
        assert tx is not None
        assert tx["type"] == "cash_withdrawal"

    def test_cash_withdrawal_description(self):
        tx = _parse_block("2025-05-15", _make_cash_withdrawal_block())
        assert tx["description"] == markers.CASH_WITHDRAWAL

    def test_regular_expense_is_not_cash_withdrawal(self):
        tx = _parse_block("2025-05-15", _make_regular_expense_block())
        assert tx is not None
        assert tx["type"] == "expense"

    def test_cash_withdrawal_keywords_list_present(self):
        upper_kws = [kw.upper() for kw in CASH_WITHDRAWAL_KEYWORDS]
        assert "BANKOMAT" in upper_kws
        assert "ATM" in upper_kws
