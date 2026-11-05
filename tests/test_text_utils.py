from services.text_utils import split_for_discord, truncate_cleanly


def test_truncate_cleanly_keeps_short_text():
    assert truncate_cleanly("hello", 10) == "hello"


def test_truncate_cleanly_prefers_line_then_word_boundaries():
    assert truncate_cleanly("first line here\nsecond line is longer", 20) == "first line here…"
    assert truncate_cleanly("alpha bravo charlie delta", 15) == "alpha bravo…"
    assert truncate_cleanly("abcdefghijklmnop", 8) == "abcdefg…"


def test_truncate_cleanly_ends_on_sentence_without_ellipsis():
    assert truncate_cleanly("One two three. Four five six seven", 20) == "One two three."


def test_split_for_discord_respects_limit_and_boundaries():
    text = "\n".join(f"line {index} with some words" for index in range(50))

    chunks = split_for_discord(text, 200)

    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "\n".join(chunks) == text
    assert all(chunk.startswith("line") for chunk in chunks)


def test_split_for_discord_caps_chunk_count():
    chunks = split_for_discord("word " * 500, 100, max_chunks=3)

    assert len(chunks) == 3
    assert chunks[-1].endswith("…")
    assert all(len(chunk) <= 100 for chunk in chunks)
