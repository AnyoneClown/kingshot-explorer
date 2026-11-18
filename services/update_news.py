"""Update news: the notes the bot posts in every server after it changed.

The notes are written by hand in ``assets/update_news.toml``, one ``[[update]]`` per
post, oldest first. Each server remembers the id of the newest note it was given
(``guild_configurations.update_news_last_id``); what comes after it in the file is
what the server has not seen. This module only reads the file and decides what is
pending: posting is done by ``handlers.update_news_handler``.
"""

from __future__ import annotations

import functools
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

UPDATE_NEWS_PATH = Path(__file__).resolve().parents[1] / "assets" / "update_news.toml"

MAX_ID_CHARS = 64
MAX_TITLE_CHARS = 80
# Three languages of one note, each grown by translation, must fit one Discord message.
MAX_TEXT_CHARS = 900
# A server that fell behind gets the newest notes only, not a wall of posts.
MAX_PENDING = 3

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class UpdateNote:
    id: str
    title: str
    text: str


def parse_update_news(raw: str) -> tuple[UpdateNote, ...]:
    """The notes of a news file, oldest first. Raises ValueError on a malformed file."""
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"update news is not valid TOML: {error}") from None
    notes: list[UpdateNote] = []
    seen: set[str] = set()
    for position, item in enumerate(data.get("update") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"update {position} is not a table")
        note_id, title, text = (str(item.get(key) or "").strip() for key in ("id", "title", "text"))
        where = f"update {position} ({note_id or 'no id'})"
        if not _ID_PATTERN.match(note_id) or len(note_id) > MAX_ID_CHARS:
            raise ValueError(f"{where}: id must be lower-case letters, digits and dashes, up to {MAX_ID_CHARS}")
        if note_id in seen:
            raise ValueError(f"{where}: this id is used twice")
        if not title or len(title) > MAX_TITLE_CHARS:
            raise ValueError(f"{where}: title must have 1 to {MAX_TITLE_CHARS} characters")
        if not text or len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"{where}: text must have 1 to {MAX_TEXT_CHARS} characters, it has {len(text)}")
        seen.add(note_id)
        notes.append(UpdateNote(note_id, title, text))
    return tuple(notes)


@functools.lru_cache(maxsize=1)
def load_update_news() -> tuple[UpdateNote, ...]:
    """The bundled notes. A missing or broken file means no news, never a bot that cannot start."""
    try:
        return parse_update_news(UPDATE_NEWS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read update news from %s", UPDATE_NEWS_PATH)
        return ()


def latest_id(notes: Sequence[UpdateNote]) -> Optional[str]:
    return notes[-1].id if notes else None


def pending_notes(notes: Sequence[UpdateNote], last_id: Optional[str], limit: int = MAX_PENDING) -> list[UpdateNote]:
    """The notes a server has not been given yet, oldest first.

    A server without a marker is new to update news and gets the newest note only. A
    marker that is no longer in the file (the note was withdrawn) means nothing is
    pending: guessing would post old news again.
    """
    if not notes:
        return []
    if last_id is None:
        return [notes[-1]]
    ids = [note.id for note in notes]
    if last_id not in ids:
        return []
    return list(notes[ids.index(last_id) + 1 :])[-limit:]
