"""Parse kingshot.net database, hero and tier-list pages into RAG-ready Kingshot knowledge JSON.

Reads pages saved from https://kingshot.net (``/database/...``, ``/heroes/...``,
``/hero-tier-list/...``; robots.txt allows them and disallows only ``/api/``, which this script
never touches) and writes a JSON array for ``scripts/upload_kingshot_knowledge.py``. Every entity
carries ready chunks: short, self-describing passages (entity name, type, section and row range in
each one), so a single retrieved chunk makes sense on its own.

    uv run python scripts/parse_kingshot_net.py --pages-dir pages/ --output kingshot_net.json
    uv run python scripts/parse_kingshot_net.py --fetch --output kingshot_net.json

Saved files are named after the URL path (and query) with ``/`` replaced by ``__``
(``heroes__ava.html``, ``database__truegold-requirements?tg=5.html``).

Sources used per page: the visible ``<main>`` content (decorative ``aria-hidden`` parts skipped,
exact numbers taken from ``title`` attributes instead of the rounded "6K" text), the page's
JSON-LD ``FAQPage`` for questions and answers, and the embedded page data for skills, whose
lower levels are not rendered in the HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

BASE_URL = "https://kingshot.net"
USER_AGENT = "Mozilla/5.0 (compatible; kingshot-knowledge-parser/1.0)"
MAX_CHUNK_CHARS = 850  # the chat tool clips each retrieved chunk at 900 characters
PAGE_PATTERN = re.compile(r"^/(database|heroes|hero-tier-list)(/|$)")
EXTRA_PAGES = [f"/database/truegold-requirements?tg={n}" for n in range(2, 9)]  # TG1 is the plain page

# URL section -> (entity_type, human label)
SECTIONS = {
    "heroes": ("hero", "hero"),
    "hero-tier-list": ("hero_tier_list", "hero tier list"),
    "buildings": ("building", "building"),
    "pets": ("pet", "pet"),
    "masters": ("master", "master"),
    "academy-research": ("academy_research", "Academy research"),
    "war-academy": ("war_academy_research", "War Academy research"),
    "advanced-truegold-research": ("advanced_truegold_research", "Advanced Truegold research"),
    "alliance-research": ("alliance_research", "Alliance research"),
    "governor-gear": ("governor_gear", "Governor gear"),
    "governor-charm": ("governor_charm", "Governor charms"),
    "truegold-requirements": ("truegold_requirements", "Truegold requirements"),
}

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "ul", "ol", "header", "footer", "dt", "dd", "dl", "br",
    "summary", "details", "figure", "figcaption", "blockquote", "tbody", "thead",
}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "button", "nav", "select", "option", "dialog"}
NOISE_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"^home$", r"^/", r"short numbers", r"^open .*calculator$", r"^calculate ", r"^show all",
        r"^compare (this hero|heroes)$", r"^[+\-]$", r"^stats skills exclusive weapon", r"^skill level$",
        r"^(\d+ ?level \d+ ?)+$", r"^tree list$", r"^new$", r"^copy (link|to my list)", r"^download picture$",
        r"^recommended my list$", r"^tree( →)? list$", r"^each row is a research rank", r"^(level \d+ ?)+$",
        r"^all buildings$", r"copy the list to adjust", r"^your lists are saved", r"^select a hero to see",
        r"^highlight troop type", r"^gen \d+( gen \d+)+$",
    )
]
STRIP_PHRASES = re.compile(r"\s*\b(From level From stars|Open calculator|Compare heroes)\b\s*")
SKIP_SECTIONS = {"story"}  # lore; a "recruit" line in it is kept in the overview
FAQ_SECTION = re.compile(r"\b(faq|questions)\b", re.I)
VALUE_RE = re.compile(r"[+\-]?[\d.,]+\s*[%KMB]?|([\d.,]+\s*[dhms]\s*)+|TG\d+|Level \d+|\d+–TG\d+")
VALUE_START = re.compile(r"(Level \d+|\d)")  # "Level 80, breakthrough 8" after "Required pet level"
NUMERIC_TITLE = re.compile(r"[\d][\d,.\s]*")
TIER_RE = re.compile(r"^[SABCDF][+]? (.+?): Tier ([SABCDF][+]?)$")
COUNT_SUFFIX = re.compile(r"^(.*\d) (\d+ (?:heroes|hero|levels|items|researches|pets|masters|buildings|pieces))$")
BOILERPLATE_MIN_PAGES = 10  # a sentence repeated on this many pages is site chrome, not knowledge
EMPTY_VALUES = {"", "—", "-", "–"}
NESTED_MARK = "\x00nested"  # a cell that only wrapped a nested table, emitted as its own table

# Short notes for tables whose page explanation is the same on every page of a section.
TABLE_NOTES = {
    "academy_research": "costs and time are per level; bonus and power are totals at that level",
    "war_academy_research": "costs and time are per level; bonus and power are totals at that level",
    "advanced_truegold_research": "costs and time are per level; bonus and power are totals at that level",
    "alliance_research": "alliance resources and time are per level; Contribution cost is one member's contribution; bonus is the total at that level",
    "pet": "cost to reach that level; power and bonuses are totals at that level",
}


def tidy(text: str) -> str:
    """Undo spacing artefacts of joined inline elements and strip UI phrases."""
    text = STRIP_PHRASES.sub(" ", text)
    text = re.sub(r"\s+([:;,.!?)%])", r"\1", text).replace("( ", "(")
    text = re.sub(r"(\d) ?: ?(\d\d)\b", r"\1:\2", text)  # "UTC 00: 00" -> "UTC 00:00"
    text = re.sub(r"\+ (\d)", r"+\1", text)
    text = re.sub(r"\s*→\s*$", "", text)
    text = " ".join(text.split())
    tier = TIER_RE.match(text)
    return f"{tier.group(1)}: Tier {tier.group(2)}" if tier else text


@dataclass
class Block:
    kind: str  # "h", "p" or "table"
    text: str = ""
    level: int = 0
    rows: list[list[tuple[str, int]]] = field(default_factory=list)  # (cell text, colspan)


class MainContentParser(HTMLParser):
    """Collect headings, text blocks and tables (nested ones too) from the page's <main>."""

    def __init__(self, tab_labels: Optional[list[str]] = None) -> None:
        super().__init__(convert_charrefs=True)
        self._tab_labels = list(tab_labels or [])  # tab button texts, in the order of their panels
        self.blocks: list[Block] = []
        self._main_depth = 0
        self._skip_depth = 0
        self._buffer: list[str] = []
        self._heading: Optional[int] = None
        self._tables: list[dict[str, Any]] = []
        self._title: Optional[list[Any]] = None  # [tag, exact value, used]

    def _flush_text(self) -> None:
        text = tidy("".join(self._buffer))
        self._buffer = []
        if not text:
            return
        if self._heading is not None:
            match = COUNT_SUFFIX.match(text)
            text = f"{match.group(1)} ({match.group(2)})" if match else text
            self.blocks.append(Block("h", text=text, level=self._heading))
        else:
            self.blocks.append(Block("p", text=text))

    def _cell(self) -> Optional[list[str]]:
        return self._tables[-1]["cell"] if self._tables else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "main":
            self._main_depth += 1
            return
        if not self._main_depth:
            return
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        attr = dict(attrs)
        label = attr.get("aria-label") or ""
        hero_tile = re.fullmatch(r"([^,]+), (Infantry|Cavalry|Archer)((?:, [^,]+)*)", label)
        if tag == "button" and "aria-pressed" in attr and hero_tile:
            name, troop, extra = hero_tile.groups()
            extra = extra.replace("new this generation", "new in this generation")
            self._buffer.append(f" {name} ({troop}{extra});")  # hero tiles on tier-list pages
            self._skip_depth = 1
            return
        if attr.get("role") == "tabpanel":
            # Inactive tabs are only hidden, not absent: "Growth", "Economy", "Battle" lists.
            self._flush_text()
            if self._tab_labels and not self._tables:
                self.blocks.append(Block("h", text=self._tab_labels.pop(0), level=3))
            return
        if tag in SKIP_TAGS or attr.get("aria-hidden") == "true":
            if tag == "svg":  # inline arrows: "Feed <svg> Ride", "level 0 <svg> 3"
                target = self._cell() if self._cell() is not None else self._buffer
                if target and "".join(target[-3:]).rstrip()[-1:].isalnum():
                    target.append(" → ")
            if tag not in VOID_TAGS:
                self._skip_depth = 1
            return
        title = attr.get("title")
        if title and NUMERIC_TITLE.fullmatch(title.strip()) and tag not in VOID_TAGS:
            self._title = [tag, title.strip(), False]
        if tag == "table":
            if not self._tables:
                self._flush_text()
            hint = "".join(self._cell() or []).strip() if self._tables else ""
            self._tables.append({"rows": [], "cell": None, "span": 1, "caption": None, "hint": tidy(hint)})
            return
        if self._tables:
            table = self._tables[-1]
            if tag == "caption":
                table["caption"] = []
            elif tag == "tr":
                table["rows"].append([])
            elif tag in ("td", "th"):
                table["cell"] = []
                try:
                    table["span"] = max(1, int(attr.get("colspan") or 1))
                except ValueError:
                    table["span"] = 1
            elif table["cell"] is not None:
                table["cell"].append(", " if tag in BLOCK_TAGS and "".join(table["cell"]).strip() else " ")
            elif table["caption"] is not None:
                table["caption"].append(" ")
            return
        if tag in HEADINGS:
            self._flush_text()
            self._heading = HEADINGS[tag]
        elif tag in BLOCK_TAGS:
            self._flush_text()
        else:
            self._buffer.append(" ")  # inline siblings such as badges must not run together

    def handle_endtag(self, tag: str) -> None:
        if not self._main_depth:
            return
        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth -= 1
            return
        if self._title and tag == self._title[0]:
            self._title = None
        if tag == "main":
            self._flush_text()
            self._main_depth -= 1
            return
        if self._tables:
            table = self._tables[-1]
            if tag in ("td", "th") and table["cell"] is not None:
                if table["rows"]:
                    text = NESTED_MARK if table.pop("nested", False) else tidy("".join(table["cell"]))
                    table["rows"][-1].append((text, table["span"]))
                table["cell"] = None
            elif tag == "caption" and table["caption"] is not None:
                table["caption_text"] = tidy("".join(table["caption"]))
                table["caption"] = None
            elif tag == "table":
                self._tables.pop()
                if self._tables:
                    self._tables[-1]["nested"] = True
                rows = [row for row in table["rows"] if any(text for text, _ in row)]
                if rows:
                    title = table.get("caption_text") or table["hint"]
                    self.blocks.append(Block("table", text=title, rows=rows))
            elif tag in BLOCK_TAGS and table["cell"] is not None:
                table["cell"].append(" ")
            return
        if tag in HEADINGS:
            self._flush_text()
            self._heading = None
        elif tag in BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if not self._main_depth or self._skip_depth:
            return
        if self._title:
            if self._title[2]:
                return
            if data.strip():
                data, self._title[2] = self._title[1], True
        if self._tables:
            table = self._tables[-1]
            if table["caption"] is not None:
                table["caption"].append(data)
            elif table["cell"] is not None:
                table["cell"].append(data)
            return
        self._buffer.append(data)


@dataclass
class Section:
    title: str
    items: list[Block] = field(default_factory=list)


@dataclass
class Page:
    path: str
    name: str
    badges: list[str]
    intro: list[str]
    sections: list[Section]
    faq: list[tuple[str, str]]
    skills: dict[str, list[dict[str, Any]]]


BOILERPLATE: set[str] = set()


def is_noise(text: str) -> bool:
    return any(p.search(text) for p in NOISE_PATTERNS)


def is_boilerplate(text: str) -> bool:
    return text in BOILERPLATE


def extract_faq(html: str) -> list[tuple[str, str]]:
    faq = []
    for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "FAQPage":
                continue
            for entry in item.get("mainEntity") or []:
                question = tidy(html_lib.unescape(str(entry.get("name") or "")))
                answer = (entry.get("acceptedAnswer") or {}).get("text") or ""
                answer = tidy(html_lib.unescape(re.sub(r"<[^>]+>", " ", str(answer))))
                if question and answer:
                    faq.append((question, answer))
    return faq


def extract_skills(html: str) -> dict[str, list[dict[str, Any]]]:
    """Skill name -> per-level data from the embedded page data (only the max level is rendered)."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    try:
        payload = "".join(json.loads(f'"{chunk}"') for chunk in chunks)
    except json.JSONDecodeError:
        return {}
    decoder = json.JSONDecoder()
    skills: dict[str, list[dict[str, Any]]] = {}
    for match in re.finditer(r'\{"name":"', payload):
        try:
            obj, _ = decoder.raw_decode(payload[match.start():])
        except json.JSONDecodeError:
            continue
        levels = obj.get("levels") if isinstance(obj, dict) else None
        if (
            isinstance(levels, list) and len(levels) > 1
            and all(isinstance(level, dict) and "description" in level for level in levels)
        ):
            kind = obj.get("kind")
            skills.setdefault(tidy(str(obj["name"])), {"levels": levels,
                                                      "kind": kind if kind and kind != "$undefined" else None})
    return skills


def describe_skill_levels(levels: list[dict[str, Any]]) -> str:
    """'... Attack*50%/55%/60%/65%/70% damage ... (skill levels 1–5; level 2 needs 1★, ...)'."""
    descriptions = [tidy(str(level.get("description") or "")) for level in levels]
    number = re.compile(r"\d+(?:[.,]\d+)?%?")
    values = [number.findall(text) for text in descriptions]
    text = descriptions[-1]
    if len({len(v) for v in values}) == 1 and values[0]:
        varying = [i for i in range(len(values[0])) if len({v[i] for v in values}) > 1]
        tokens = list(number.finditer(text))
        for index in reversed(varying):
            token = tokens[index]
            text = text[:token.start()] + "/".join(v[index] for v in values) + text[token.end():]
    requirements = []
    for level in levels:
        requirement = str(level.get("requirement") or "")
        if requirement and requirement != "$undefined":
            requirement = re.sub(r"Requires (\d+) stars?", r"\1★", requirement)
            requirements.append(f"level {level.get('level')} needs {requirement}")
    first, last = levels[0].get("level", 1), levels[-1].get("level", len(levels))
    details = f"values for skill levels {first}–{last}" + (f"; {', '.join(requirements)}" if requirements else "")
    return f"{text} ({details})"


def parse_page(path: str, html: str) -> Optional[Page]:
    tabs = [tidy(label) for label in re.findall(r'role="tab"[^>]*>([^<]+)<', html)]
    parser = MainContentParser(tab_labels=tabs)
    parser.feed(html)
    parser.close()
    blocks = [b for b in parser.blocks if b.kind == "table" or not is_noise(b.text)]
    name_index = next((i for i, b in enumerate(blocks) if b.kind == "h" and b.level == 1), None)
    if name_index is None:
        return None
    name = blocks[name_index].text
    badges = [b.text for b in blocks[:name_index] if b.kind == "p"]
    intro: list[str] = []
    sections: list[Section] = []
    seen_tables: set[str] = set()
    last_heading = ""
    for block in blocks[name_index + 1:]:
        if block.kind == "h":
            last_heading = block.text
        if block.kind == "table":
            # Same rows under different headings (Stable and Range copy Barracks) are separate facts.
            digest = hashlib.sha1(json.dumps([last_heading, block.text, block.rows]).encode()).hexdigest()
            if digest in seen_tables:  # pages render some tables twice (preview + full list)
                continue
            seen_tables.add(digest)
        if block.kind == "h" and (block.level == 2 or (not sections and block.level >= 3)):
            sections.append(Section(block.text))
        elif sections:
            sections[-1].items.append(block)
        elif block.kind == "p":
            intro.append(block.text)
        elif block.kind == "table":
            sections.append(Section("Overview table", [block]))
    return Page(path=path, name=name, badges=badges, intro=intro, sections=sections,
                faq=extract_faq(html), skills=extract_skills(html))


# ---------------------------------------------------------------------------- chunking


def pair_values(texts: list[str]) -> list[str]:
    """Join label blocks with the value block that follows them: 'Bread', '46.7K' -> 'Bread: 46.7K'."""
    out: list[str] = []
    for text in texts:
        prev = out[-1] if out else None
        short_value = len(text) <= 40 and VALUE_START.match(text) and len(prev or "") <= 30
        if (
            prev is not None
            and (VALUE_RE.fullmatch(text) or short_value)
            and len(prev) <= 70
            and not prev.endswith(":")
            and not VALUE_RE.fullmatch(prev)
            and ": " not in prev
        ):
            out[-1] = f"{prev}: {text}"
        else:
            out.append(text)
    return out


def pack(prefix: str, parts: list[str], *, joiner: str = "; ") -> list[str]:
    """Pack parts into chunks that start with ``prefix`` and stay under MAX_CHUNK_CHARS."""
    chunks: list[str] = []
    current: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(prefix) + len(part) + 2 > MAX_CHUNK_CHARS:  # split a single overlong part
            room = MAX_CHUNK_CHARS - len(prefix) - 2
            cut = part.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            if current:
                chunks.append(prefix + ": " + joiner.join(current))
                current = []
            chunks.append(prefix + ": " + part[:cut].strip())
            part = part[cut:].strip()
        candidate = prefix + ": " + join_parts(current + [part], joiner)
        if current and len(candidate) > MAX_CHUNK_CHARS:
            chunks.append(prefix + ": " + join_parts(current, joiner))
            current = [part]
        else:
            current.append(part)
    if current:
        chunks.append(prefix + ": " + join_parts(current, joiner))
    return chunks


def join_parts(parts: list[str], joiner: str) -> str:
    """Join with ``joiner``, but after a full sentence a space is enough (no ".;")."""
    out = ""
    for part in parts:
        if out:
            out += " " if out.endswith((".", "!", "?")) else joiner
        out += part
    return out


@dataclass
class TableContext:
    page_name: str
    bonus_name: Optional[str] = None  # alliance research tables call the stat just "Bonus"
    note: Optional[str] = None
    downsample: bool = False


def expand(row: list[tuple[str, int]]) -> list[str]:
    cells: list[str] = []
    for text, span in row:
        cells.append(text)
        cells.extend([""] * (span - 1))
    return cells


def clean_cell(value: str, page_name: str) -> str:
    # A level's own previous level is always required; listing it bloats every row.
    value = re.sub(rf"(,\s*)?{re.escape(page_name)} · Level \d+", "", value)
    return value.strip(" ,;")


def table_groups(block: Block, ctx: TableContext) -> tuple[str, list[tuple[str, list[tuple[str, str]]]]]:
    """Return (first column name, [(group label, [(row label, row text)])])."""
    header = expand(block.rows[0])
    if ctx.bonus_name:
        header = [ctx.bonus_name if h == "Bonus" else h for h in header]
    key = header[0] if header else ""
    groups: list[tuple[str, list[tuple[str, str]]]] = [("", [])]
    previous_signature = None
    data_rows = [row for row in block.rows[1:] if not any(text == NESTED_MARK for text, _ in row)]
    for index, row in enumerate(data_rows):
        filled = [(text, span) for text, span in row if text]
        if len(filled) == 1 and len(header) > 2 and (filled[0][1] > 1 or len(row) == 1):
            groups.append((filled[0][0], []))  # band row: "Uncommon · 2 levels", "Levels 1–10"
            continue
        cells = expand(row)
        label = cells[0] if cells else ""
        pairs = []
        for h, v in zip(header[1:], cells[1:]):
            v = clean_cell(v, ctx.page_name)
            if v in EMPTY_VALUES:
                continue
            pairs.append(f"({v})" if len(v.split()) > 6 or not h else f"{h}: {v}")
        if ctx.downsample and len(data_rows) > 40:  # master affinity: 100 near-linear rows
            signature = tuple(v for v in cells[1:] if v and not VALUE_RE.fullmatch(v))
            last = index == len(data_rows) - 1
            if signature == previous_signature and index % 10 and not last:
                continue
            previous_signature = signature
        lead = f"{key} {label}" if key and label[:1].isdigit() else label
        groups[-1][1].append((label, f"{lead}: {', '.join(pairs)}" if pairs else lead))
    return key, [g for g in groups if g[1]]


def table_chunks(prefix: str, title: str, block: Block, ctx: TableContext, meta: dict[str, Any],
                 section_note: Optional[str] = None) -> list[tuple[str, dict]]:
    chunks: list[tuple[str, dict]] = []
    key, groups = table_groups(block, ctx)
    note_text = (ctx.note if re.search(r"level|breakthrough", title, re.I) else None) or section_note
    note = f" [{note_text}]" if note_text else ""
    for group_label, rows in groups:
        full_title = f"{title} — {group_label}" if group_label else title
        head_len = len(prefix) + len(full_title) + len(note) + 40

        def emit(group: list[tuple[str, str]]) -> None:
            first, last = group[0][0], group[-1][0]
            span = f" ({first} to {last})" if first != last else (f" ({first})" if first else "")
            body = "; ".join(text for _, text in group)
            chunks.append((f"{prefix} — {full_title}{span}{note}: {body}",
                           meta | {"table": full_title, "rows": f"{first} to {last}"}))

        group: list[tuple[str, str]] = []
        for label, text in rows:
            if group and head_len + sum(len(t) + 2 for _, t in group + [(label, text)]) > MAX_CHUNK_CHARS:
                emit(group)
                group = []
            group.append((label, text))
        if group:
            emit(group)
    return chunks


def section_chunks(prefix: str, section: Section, ctx: TableContext, skills: dict[str, list]) -> list[tuple[str, dict]]:
    chunks: list[tuple[str, dict]] = []
    meta = {"section": section.title}
    # Text under each sub-heading stays together (one skill = one part); a level-4 heading
    # carries its level-3 group: "Expedition — Dissolution".
    groups: list[tuple[Optional[str], list[str], Optional[str]]] = [(None, [], None)]
    current_h3: Optional[str] = None
    current_sub: Optional[str] = None
    has_table = any(item.kind == "table" for item in section.items)
    tables: list[tuple[str, Block]] = []
    items = section.items
    for position, item in enumerate(items):
        following = items[position + 1] if position + 1 < len(items) else None
        if (
            item.kind == "p" and following is not None and following.kind == "h" and following.level >= 4
            and len(item.text) <= 25 and not re.search(r"[\d:;]", item.text)
        ):
            current_h3 = item.text  # "Conquest" / "Development" label right before a skill heading
            continue
        if item.kind == "h":
            current_sub = item.text
            if item.level <= 3:
                current_h3 = item.text
                label = item.text
            else:
                label = f"{current_h3} — {item.text}" if current_h3 else item.text
            last_label, last_texts, _ = groups[-1]
            if last_label is not None and not last_texts and item.level <= 3:
                label = f"{last_label} — {item.text}"
            if last_label is not None and not last_texts:
                groups[-1] = (label, [], item.text)
            else:
                groups.append((label, [], item.text))
        elif item.kind == "p":
            if has_table or not is_boilerplate(item.text):
                groups[-1][1].append(item.text)
        elif item.kind == "table":
            title = item.text or (f"{section.title} — {current_sub}" if current_sub else section.title)
            if item.text and not item.text.lower().startswith(section.title.lower()):
                title = f"{section.title} — {item.text}"
            if re.search(r"\bsteps after\b", title, re.I):
                title += " (already counted in the next level's cost)"
            tables.append((title, item))
    parts: list[str] = []
    for label, texts, skill_name in groups:
        if skill_name and skill_name in skills:
            skill = skills[skill_name]
            if skill["kind"]:
                label = f"{skill['kind']} — {skill_name}"
            extra = [t for t in pair_values(texts) if ": " in t and not t.lower().startswith("requires")]
            texts = [describe_skill_levels(skill["levels"])] + extra
        texts = pair_values(texts)
        if has_table and ctx.note:
            texts = [t for t in texts if not is_boilerplate(t)]
        if not texts:
            continue
        parts.append(f"{label}: " + "; ".join(texts) if label else "; ".join(texts))
    section_note = None
    if has_table and not ctx.note and parts and len(" ".join(parts)) <= 220:
        # A short explanation ("Each row shows ...") belongs next to the rows it explains.
        section_note, parts = " ".join(parts), []
    for title, table in tables:
        chunks.extend(table_chunks(prefix, title, table, ctx, meta, section_note))
    text_chunks = [(content, meta) for content in pack(f"{prefix} — {section.title}", parts)]
    return text_chunks + chunks


def hero_data(page: Page) -> dict[str, Any]:
    badge_text = " ".join(page.badges)
    data: dict[str, Any] = {}
    rarity = re.search(r"\b(Mythic|Epic|Rare|SSR|SR|R)\b", badge_text)
    troop = re.search(r"\b(Infantry|Cavalry|Archer)\b", badge_text, re.I)
    generation = re.search(r"Generation (\d+)", badge_text)
    if troop:
        data["class"] = troop.group(1).lower()
    if generation:
        data["generation"] = int(generation.group(1))
    if rarity:
        data["rarity"] = rarity.group(1)
    if page.intro:
        data["description"] = next((line for line in page.intro if len(line) > 30), page.intro[0])
    for section in page.sections:
        title = section.title.lower()
        if title == "exclusive weapon":
            gear = next((i.text for i in section.items if i.kind == "h" and i.level == 3), None)
            if gear:
                data["exclusive_gear"] = gear
        elif title == "tier list":
            tiers = {}
            for item in section.items:
                match = re.match(r"^(.+?): Tier ([SABCDF][+]?)$", item.text) if item.kind == "p" else None
                if match:
                    tiers[match.group(1).strip()] = match.group(2)
            if tiers:
                data["tier_list"] = tiers
        elif title == "how to obtain":
            sources, heading = [], None
            for item in section.items:
                if item.kind == "h":
                    heading = item.text
                elif item.kind == "p":
                    sources.append(f"{heading}: {item.text}" if heading else item.text)
            if sources:
                data["sources"] = sources[:10]
    return data


def faq_chunks(prefix: str, faq: list[tuple[str, str]]) -> list[tuple[str, dict]]:
    chunks = []
    for question, answer in faq:
        for content in pack(f"{prefix} — FAQ: {question}", [answer], joiner=" "):
            chunks.append((content, {"section": "FAQ", "question": question}))
    return chunks


def classify(path: str) -> Optional[tuple[str, str, str, bool, str]]:
    """Return (section key, entity_type, label, is_index, slug_tail) for a page path."""
    clean, _, query = path.partition("?")
    parts = clean.strip("/").split("/")
    if parts[0] in ("heroes", "hero-tier-list"):
        key = parts[0]
        is_index = len(parts) == 1
    elif parts[0] == "database" and len(parts) > 1:
        key = parts[1]
        is_index = len(parts) == 2
    else:
        return None
    if key not in SECTIONS:
        return None
    entity_type, label = SECTIONS[key]
    tail = parts[-1]
    tier = re.search(r"tg=(\d+)", query)
    if key == "truegold-requirements":
        is_index = False
        tail = f"tg{tier.group(1) if tier else 1}"
    return key, entity_type, label, is_index, tail


def build_entity(page: Page) -> Optional[dict[str, Any]]:
    info = classify(page.path)
    if not info:
        return None
    key, entity_type, label, is_index, tail = info
    url = BASE_URL + page.path
    name = page.name
    tags = " ".join(page.badges)
    if is_index:
        entity_type = f"{entity_type}_overview"
        slug = f"kingshot-net-{key}-overview"
        prefix = f"{name} overview (Kingshot {label}{'' if label.endswith('list') else ' list'})"
    elif entity_type == "hero":
        # Hero slugs drop hyphens to match the existing entities ("long-fei" -> "longfei"),
        # so an upload replaces the older kingshotguide.org rows instead of duplicating them.
        slug = tail.replace("-", "")
        prefix = f"{name} (Kingshot hero)"
    else:
        slug = f"{entity_type.replace('_', '-')}-{tail}"
        if key == "truegold-requirements":
            name = f"Truegold requirements {tail.upper()}"
        context = f", {tags}" if tags and entity_type != "hero_tier_list" else ""
        prefix = f"{name} (Kingshot {label}{context})"

    data: dict[str, Any] = {"category": label, "url": url}
    if tags:
        data["tags"] = tags
    if entity_type == "hero":
        data.update(hero_data(page))
    elif page.intro:
        data["summary"] = "; ".join(t for t in page.intro[:6] if not is_boilerplate(t))[:400]

    bonus_name = None
    for section in page.sections:
        if section.title.lower() == "maximum bonus":
            first = pair_values([i.text for i in section.items if i.kind == "p"])
            if first and ": " in first[0]:
                bonus_name = first[0].split(": ")[0]
    ctx = TableContext(page_name=page.name, bonus_name=bonus_name, note=TABLE_NOTES.get(entity_type),
                       downsample=entity_type == "master")

    chunks: list[tuple[str, dict[str, Any]]] = []
    overview_parts = [t for t in page.intro if not is_boilerplate(t)]
    for section in page.sections:
        title = section.title.lower()
        texts = [item.text for item in section.items if item.kind == "p"]
        if title in SKIP_SECTIONS:
            recruit, in_recruit = [], False
            for item in section.items:
                if item.kind == "h":
                    in_recruit = "recruit" in item.text.lower()
                elif item.kind == "p" and (in_recruit or "recruit" in item.text.lower()):
                    recruit.append(item.text)
            if recruit:
                overview_parts.append("How to recruit: " + " ".join(recruit))
            continue
        if FAQ_SECTION.search(title) and page.faq:
            continue  # answered from the page's FAQ data, one chunk per question
        if entity_type in ("hero_tier_list", "hero_tier_list_overview") and title.startswith("tier "):
            tier, names = section.title, {}
            for item in section.items:
                if item.kind == "h":
                    tier = item.text
                elif item.kind == "p" and ";" in item.text:  # hero tiles, not the tier letter
                    names.setdefault(tier, []).append(item.text.rstrip(";"))
            overview_parts.extend(f"{tier}: " + "; ".join(group) for tier, group in names.items())
            continue
        has_table = any(item.kind == "table" for item in section.items)
        text_len = sum(len(item.text) for item in section.items)
        if not has_table and text_len < 160 and entity_type not in ("hero", "hero_tier_list"):
            kept: list[str] = []
            for item in section.items:
                if item.kind == "h":
                    kept.append(f"{item.text}:")
                elif item.kind == "p" and not is_boilerplate(item.text):
                    if kept and kept[-1].endswith(":"):
                        kept[-1] = f"{kept[-1]} {item.text}"
                    elif kept and re.search(r":( \S+)+$", kept[-1]) and kept[-1].count(":") == 1 and len(item.text) < 30:
                        kept[-1] = f"{kept[-1]}, {item.text}"
                    else:
                        kept.append(item.text)
            kept = pair_values(kept)
            if kept:
                overview_parts.append(f"{section.title}: " + "; ".join(kept))
            continue
        chunks.extend(section_chunks(prefix, section, ctx, page.skills))
    chunks.extend(faq_chunks(prefix, page.faq))
    overview_parts = pair_values(overview_parts)
    if tags and not is_index and entity_type == "hero":  # other prefixes already carry the tags
        overview_parts.insert(0, tags)
    overview = [(c, {"section": "overview"}) for c in pack(prefix, overview_parts)]
    chunks = overview + chunks
    if not chunks:
        return None
    seen: set[str] = set()
    unique = []
    for content, metadata in chunks:
        if content in seen:
            continue
        seen.add(content)
        unique.append({"content": content, "metadata": metadata | {"url": url}})
    return {"entity_type": entity_type, "slug": slug, "name": name, "data": data, "source": url, "chunks": unique}


def chunk_body(content: str) -> str:
    return content.split(")", 1)[-1]  # text after "Name (Kingshot label, tags)"


def drop_shared_tier_chunks(entities: list[dict[str, Any]]) -> None:
    """TG2..TG8 pages repeat TG1's intro, overview and FAQ; keep each shared chunk once (on TG1)."""
    tiers = [e for e in entities if e["entity_type"] == "truegold_requirements"]
    base = next((e for e in tiers if e["slug"].endswith("-tg1")), None)
    if not base:
        return
    shared = {chunk_body(c["content"]) for c in base["chunks"]}
    for entity in tiers:
        if entity is base:
            continue
        entity["chunks"] = [c for c in entity["chunks"] if chunk_body(c["content"]) not in shared] or entity["chunks"][:1]


# ---------------------------------------------------------------------------- input


def fetch(path: str) -> str:
    request = urllib.request.Request(BASE_URL + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.read().decode("utf-8", errors="replace")


def sitemap_paths() -> list[str]:
    xml = fetch("/sitemap.xml")
    paths = [loc.replace(BASE_URL, "") for loc in re.findall(r"<loc>([^<]+)</loc>", xml)]
    return sorted({p.rstrip("/") or "/" for p in paths if PAGE_PATTERN.match(p)}) + EXTRA_PAGES


def path_from_filename(filename: str) -> str:
    return "/" + filename.removesuffix(".html").replace("__", "/")


def load_pages(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.pages_dir:
        files = sorted(Path(args.pages_dir).glob("*.html"))
        return [(path_from_filename(f.name), f.read_text(encoding="utf-8", errors="replace")) for f in files]
    pages = []
    for index, path in enumerate(sitemap_paths(), 1):
        if path.startswith("/api/"):
            continue
        pages.append((path, fetch(path)))
        print(f"[{index}] fetched {path}", file=sys.stderr)
        time.sleep(args.delay)
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pages-dir", help="Directory of saved pages (path with / replaced by __, .html).")
    source.add_argument("--fetch", action="store_true", help="Fetch every database, hero and tier-list page.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests with --fetch.")
    parser.add_argument("--output", "-o", required=True, type=Path)
    args = parser.parse_args()

    pages = [(path, html) for path, html in load_pages(args) if classify(path)]
    parsed = [(path, parse_page(path, html)) for path, html in pages]
    counts: dict[str, int] = {}
    for _, page in parsed:
        if page:
            texts = set(page.intro) | {i.text for s in page.sections for i in s.items if i.kind == "p"}
            for text in texts:
                counts[text] = counts.get(text, 0) + 1
    # Repeated explanations are chrome; repeated short facts ("Pet system: Town Center level 18")
    # are knowledge and stay.
    BOILERPLATE.update(
        text for text, n in counts.items()
        if n >= BOILERPLATE_MIN_PAGES and len(text) > 40 and not re.search(r"\d|: ", text)
    )
    entities, skipped = [], []
    for path, page in parsed:
        entity = build_entity(page) if page else None
        (entities if entity else skipped).append(entity or path)
    drop_shared_tier_chunks(entities)
    args.output.write_text(json.dumps(entities, ensure_ascii=False, indent=2), encoding="utf-8")
    by_type: dict[str, list[int]] = {}
    for entity in entities:
        stats = by_type.setdefault(entity["entity_type"], [0, 0])
        stats[0] += 1
        stats[1] += len(entity["chunks"])
    total = sum(len(e["chunks"]) for e in entities)
    print(f"entities: {len(entities)}  chunks: {total}  boilerplate dropped: {len(BOILERPLATE)}  skipped: {skipped[:10]}")
    for entity_type, (count, chunk_count) in sorted(by_type.items()):
        print(f"  {entity_type}: {count} entities, {chunk_count} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
