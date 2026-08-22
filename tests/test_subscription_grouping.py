from bot.db.crud import _merchant_group_key, _strip_reference_suffix


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
