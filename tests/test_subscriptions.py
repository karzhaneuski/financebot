from datetime import date

from bot.handlers.stats import _build_subscriptions_text, _fmt_next_expected, _subscription_emoji


def test_subscription_emoji_known_keywords():
    assert _subscription_emoji("Spotify AB") == "🎵"
    assert _subscription_emoji("CLAUDE.AI SUBSCRIPTION") == "🤖"
    assert _subscription_emoji("Revolut Metal Plan") == "💳"


def test_subscription_emoji_unknown_falls_back():
    assert _subscription_emoji("Random Gym Membership") == "🔁"


def test_fmt_next_expected_none():
    assert _fmt_next_expected(None) == "неизвестно (одно списание)"


def test_fmt_next_expected_formats_genitive_date():
    assert _fmt_next_expected(date(2026, 9, 15)) == "~15 сентября"


def test_build_subscriptions_text_empty():
    text = _build_subscriptions_text([])
    assert "не найдено" in text


def test_build_subscriptions_text_lists_sorted_and_totals():
    subs = [
        {
            "store": "Spotify",
            "amount": 27.14,
            "currency": "PLN",
            "last_charge": date(2026, 8, 15),
            "next_expected": date(2026, 9, 15),
            "monthly_total_pln": 27.14,
            "detection": "both",
        },
        {
            "store": "Claude",
            "amount": 95.50,
            "currency": "USD",
            "last_charge": date(2026, 8, 1),
            "next_expected": None,
            "monthly_total_pln": 95.50,
            "detection": "category",
        },
    ]
    text = _build_subscriptions_text(subs)

    assert "🎵 Spotify — 27,14 PLN/мес" in text
    assert "Следующее списание: ~15 сентября" in text
    assert "🤖 Claude — 95,50 PLN/мес" in text
    assert "Следующее списание: неизвестно (одно списание)" in text
    assert "Итого в месяц: ~122,64 PLN" in text
