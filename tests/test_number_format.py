"""format_number() must render amounts exactly like the Mini App's
Intl.NumberFormat, so the bot and the dashboard show the same figures.
Expected values were produced by Node's Intl (ru-RU / en-GB / pl-PL)."""
import pytest

from bot.i18n import i18n
from bot.utils.formatters import format_number

NBSP = " "


@pytest.mark.parametrize(
    "lang, amount, decimals, expected",
    [
        ("ru", 1234.5, 2, f"1{NBSP}234,50"),
        ("ru", 999.995, 2, f"1{NBSP}000,00"),
        ("en", 1234567.891, 2, "1,234,567.89"),
        ("en", 1234.5, 0, "1,235"),  # half away from zero, not half-even
        # Polish CLDR minimumGroupingDigits=2: 4-digit numbers stay ungrouped.
        ("pl", 1234.5, 2, "1234,50"),
        ("pl", 9999.994, 2, "9999,99"),
        ("pl", 9999.995, 2, f"10{NBSP}000,00"),
        ("pl", 12345.5, 0, f"12{NBSP}346"),
        ("pl", -1234.5, 2, "-1234,50"),
        ("ru", 0.005, 2, "0,01"),
        ("ru", 1500, 1, f"1{NBSP}500,0"),
    ],
)
def test_format_number_matches_intl(lang, amount, decimals, expected):
    with i18n.use_locale(lang):
        assert format_number(amount, decimals) == expected
