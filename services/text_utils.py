"""Small text helpers shared by translation and chat replies."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?…。！？](?:[\"')\]]*)(?=\s|$)")
_CJK_SENTENCE_END = re.compile(r"[。！？]")


def _boundary(window: str, *, minimum: int, sentence_minimum: int | None = None) -> int:
    """Return the best cut position inside ``window`` (exclusive), or -1."""
    sentence_minimum = minimum if sentence_minimum is None else sentence_minimum
    for separator in ("\n\n", "\n"):
        index = window.rfind(separator)
        if index >= minimum:
            return index

    sentence_cut = -1
    for match in _SENTENCE_END.finditer(window):
        if match.end() >= sentence_minimum:
            sentence_cut = match.end()
    for match in _CJK_SENTENCE_END.finditer(window):
        if match.end() >= sentence_minimum:
            sentence_cut = max(sentence_cut, match.end())
    if sentence_cut != -1:
        return sentence_cut

    index = window.rfind(" ")
    if index >= minimum:
        return index
    return -1


def truncate_cleanly(
    text: str,
    limit: int,
    *,
    ellipsis: str = "…",
    sentence_min_ratio: float = 0.5,
) -> str:
    """Cut ``text`` to at most ``limit`` characters at a line, sentence or word boundary.

    Boundaries earlier than half the limit are ignored (so little text is lost);
    ``sentence_min_ratio`` lowers that threshold for sentence ends, e.g. for speech.
    """
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]

    budget = limit - len(ellipsis)
    window = text[: budget + 1]
    cut = _boundary(
        window,
        minimum=max(1, budget // 2),
        sentence_minimum=max(1, int(budget * sentence_min_ratio)),
    )
    if cut == -1 or cut > budget:
        cut = budget
    head = text[:cut].rstrip()
    # A sentence boundary already ends cleanly; only mark mid-sentence cuts.
    if head and head[-1] in ".!?…。！？":
        return head
    return head + ellipsis


def split_for_discord(text: str, limit: int = 1900, *, max_chunks: int | None = None) -> list[str]:
    """Split text into chunks of at most ``limit`` characters at natural boundaries."""
    remaining = (text or "").strip()
    chunks: list[str] = []
    while remaining:
        if max_chunks is not None and len(chunks) == max_chunks - 1:
            chunks.append(truncate_cleanly(remaining, limit))
            break
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[: limit + 1]
        cut = _boundary(window, minimum=max(1, limit // 2))
        if cut == -1 or cut > limit:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    return chunks
