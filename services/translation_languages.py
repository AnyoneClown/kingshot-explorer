"""The languages a server can pick for automatic chat translation.

A server stores its choice as language codes (``"en,uk"``) in
``guild_configurations.translation_languages``: every chat message is translated
into each chosen language it is not written in. ``NULL`` means the server never
chose, an empty string means translation is switched off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Language:
    code: str
    # English name: what the model is asked for and what it reports back.
    name: str
    # Name in the language itself, for the people who read that translation.
    native: str
    flag: str


# Discord allows 25 options in one select menu.
SERVER_LANGUAGES: tuple[Language, ...] = (
    Language("en", "English", "English", "🇬🇧"),
    Language("uk", "Ukrainian", "Українська", "🇺🇦"),
    Language("es", "Spanish", "Español", "🇪🇸"),
    Language("pt", "Portuguese", "Português", "🇵🇹"),
    Language("de", "German", "Deutsch", "🇩🇪"),
    Language("fr", "French", "Français", "🇫🇷"),
    Language("it", "Italian", "Italiano", "🇮🇹"),
    Language("pl", "Polish", "Polski", "🇵🇱"),
    Language("tr", "Turkish", "Türkçe", "🇹🇷"),
    Language("ar", "Arabic", "العربية", "🇸🇦"),
    Language("ko", "Korean", "한국어", "🇰🇷"),
    Language("ja", "Japanese", "日本語", "🇯🇵"),
    Language("zh-CN", "Chinese (Simplified)", "简体中文", "🇨🇳"),
    Language("zh-TW", "Chinese (Traditional)", "繁體中文", "🇹🇼"),
    Language("vi", "Vietnamese", "Tiếng Việt", "🇻🇳"),
    Language("th", "Thai", "ไทย", "🇹🇭"),
    Language("id", "Indonesian", "Bahasa Indonesia", "🇮🇩"),
    Language("fil", "Filipino", "Filipino", "🇵🇭"),
    Language("hi", "Hindi", "हिन्दी", "🇮🇳"),
    Language("nl", "Dutch", "Nederlands", "🇳🇱"),
    Language("ro", "Romanian", "Română", "🇷🇴"),
    Language("cs", "Czech", "Čeština", "🇨🇿"),
    Language("hu", "Hungarian", "Magyar", "🇭🇺"),
    Language("el", "Greek", "Ελληνικά", "🇬🇷"),
)
# More languages mean a bot reply of that many translations under one message.
MAX_SERVER_LANGUAGES = 3
ENGLISH = SERVER_LANGUAGES[0]

_BY_CODE = {language.code.casefold(): language for language in SERVER_LANGUAGES}
# Other names a model may report for a language of the list.
_ALIASES = {
    "tagalog": "filipino",
    "pilipino": "filipino",
    "mandarin": "chinese",
    "cantonese": "chinese",
    "castilian": "spanish",
}
# "zh", "pt-BR", "en_US": the part of a locale code before its region.
_BY_CODE_PREFIX: dict[str, Language] = {}
for _language in SERVER_LANGUAGES:
    _BY_CODE_PREFIX.setdefault(_language.code.split("-")[0].casefold(), _language)
_NAME_WORD = re.compile(r"[^\W\d_]+")


def languages_from_setting(raw: Optional[str]) -> tuple[Language, ...]:
    """The chosen languages in catalogue order; unknown codes and repeats are dropped."""
    codes = {part.strip().casefold() for part in str(raw or "").split(",")}
    return tuple(language for language in SERVER_LANGUAGES if language.code.casefold() in codes)[
        :MAX_SERVER_LANGUAGES
    ]


def setting_from_codes(codes: Iterable[str]) -> str:
    """The value to store for the picked codes (``""`` switches translation off)."""
    return ",".join(language.code for language in languages_from_setting(",".join(str(code) for code in codes)))


def language_by_code(code: Optional[str]) -> Optional[Language]:
    return _BY_CODE.get(str(code or "").strip().casefold())


def _name_words(name: Optional[str]) -> set[str]:
    words = {word.casefold() for word in _NAME_WORD.findall(str(name or ""))}
    return {_ALIASES.get(word, word) for word in words}


def same_language(reported: Optional[str], target: str) -> bool:
    """True when a model-reported language name means the target language.

    Variants count as the language itself: "Brazilian Portuguese" is Portuguese,
    "en-US" is English, and plain "Chinese" matches both Chinese scripts.
    """
    reported_text = str(reported or "").strip()
    if not reported_text:
        return False
    by_code = language_by_code(reported_text)
    if by_code is None and re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?", reported_text):
        by_code = _BY_CODE_PREFIX.get(re.split(r"[-_]", reported_text)[0].casefold())
    reported_words = _name_words(by_code.name if by_code else reported_text)
    # The script of Chinese is a detail: a message in either needs no other.
    base = _name_words(re.sub(r"\(.*?\)", " ", target))
    return bool(base) and base <= reported_words
