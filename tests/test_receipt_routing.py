"""Bug 4: a stuck ForeignMerchantStates.waiting_merchant FSM state must not
swallow commands like /stats, since receipt.router is registered before
stats.router (see bot/main.py) and aiogram stops propagation at the first
router whose handler fully matches."""
from types import SimpleNamespace

from bot.handlers.receipt import router


def _foreign_merchant_filters():
    for h in router.message.handlers:
        if h.callback.__name__ == "handle_foreign_merchant_name":
            return h.filters
    raise AssertionError("handle_foreign_merchant_name is not registered")


def _text_filter_passes(text: str | None) -> bool:
    filters = _foreign_merchant_filters()
    text_filter = filters[1]  # [0] is the state filter, [1] is our command guard
    return bool(text_filter.callback(SimpleNamespace(text=text)))


def test_handler_rejects_command_text():
    assert _text_filter_passes("/stats") is False
    assert _text_filter_passes("/budget") is False
    assert _text_filter_passes("/cancel") is False


def test_handler_still_accepts_merchant_names():
    assert _text_filter_passes("Spotify") is True
    assert _text_filter_passes("efihotel.cz Brno") is True


def test_handler_accepts_non_text_messages():
    assert _text_filter_passes(None) is True
