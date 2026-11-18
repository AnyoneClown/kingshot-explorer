import pytest

from services.translation_languages import (
    ENGLISH,
    MAX_SERVER_LANGUAGES,
    SERVER_LANGUAGES,
    language_by_code,
    languages_from_setting,
    same_language,
    setting_from_codes,
)


def test_catalogue_fits_one_discord_menu_and_has_unique_codes_and_names():
    assert len(SERVER_LANGUAGES) <= 25 and ENGLISH.code == "en"
    assert len({language.code.casefold() for language in SERVER_LANGUAGES}) == len(SERVER_LANGUAGES)
    assert len({language.name for language in SERVER_LANGUAGES}) == len(SERVER_LANGUAGES)
    # Names are sent to the model as a target language and stored in String(50)/String(64) columns.
    assert all(len(language.name) <= 40 for language in SERVER_LANGUAGES)
    assert len(",".join(sorted((language.code for language in SERVER_LANGUAGES), key=len)[-MAX_SERVER_LANGUAGES:])) <= 64


def test_setting_round_trips_in_catalogue_order_and_ignores_junk():
    assert [language.name for language in languages_from_setting("uk, EN ,xx,uk")] == ["English", "Ukrainian"]
    assert languages_from_setting(None) == () and languages_from_setting("") == ()
    assert setting_from_codes(["uk", "en"]) == "en,uk"
    assert setting_from_codes([]) == ""
    assert setting_from_codes(["zh-TW", "ko"]) == "ko,zh-TW"
    # Russian is not offered: a server that had it stored keeps its other languages.
    assert language_by_code("ru") is None
    assert [language.code for language in languages_from_setting("en,ru,uk")] == ["en", "uk"]
    # Never more than the limit, whatever was stored.
    assert len(languages_from_setting("en,uk,de,es,pt")) == MAX_SERVER_LANGUAGES


@pytest.mark.parametrize(
    ("reported", "target"),
    [
        ("English", "English"), ("english", "English"), ("en-US", "English"), ("American English", "English"),
        ("Brazilian Portuguese", "Portuguese"), ("pt_BR", "Portuguese"), ("Tagalog", "Filipino"),
        ("Chinese", "Chinese (Simplified)"), ("Traditional Chinese", "Chinese (Simplified)"),
        ("zh", "Chinese (Traditional)"), ("Mandarin", "Chinese (Simplified)"), ("uk", "Ukrainian"),
    ],
)
def test_same_language_accepts_variants_and_codes(reported, target):
    assert same_language(reported, target) is True


@pytest.mark.parametrize(
    ("reported", "target"),
    [("Ukrainian", "Russian"), ("Russian", "Ukrainian"), ("Thai", "English"), (None, "English"), ("", "English"),
     ("Spanish", "Portuguese"), ("unknown", "English")],
)
def test_same_language_rejects_other_languages(reported, target):
    assert same_language(reported, target) is False
