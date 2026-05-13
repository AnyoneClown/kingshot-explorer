#!/usr/bin/env python3
"""Parse Kingshot Guide hero pages into RAG entity JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests


DEFAULT_HEROES_INDEX = "https://www.kingshotguide.org/heroes"
USER_AGENT = "ds-translator-kingshot-parser/1.0"


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: "Node | None" = None
    children: list["Node | str"] = field(default_factory=list)

    def text(self) -> str:
        parts: list[str] = []

        def visit(value: Node | str) -> None:
            if isinstance(value, str):
                parts.append(value)
                return
            for child in value.children:
                visit(child)

        visit(self)
        return clean_text(" ".join(parts))

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def descendants(self, tag: str | None = None) -> Iterable["Node"]:
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if tag is None or child.tag == tag:
                yield child
            yield from child.descendants(tag)

    def direct_children(self, tag: str | None = None) -> list["Node"]:
        return [
            child
            for child in self.children
            if isinstance(child, Node) and (tag is None or child.tag == tag)
        ]

    def first_descendant(self, tag: str, text: str | None = None) -> "Node | None":
        for node in self.descendants(tag):
            if text is None or node.text() == text:
                return node
        return None


class TreeBuilder(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {name: value or "" for name, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if node.tag not in self.VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].children.append(data)


def fetch_url(url: str, timeout: int = 30) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.text


def parse_html(html: str) -> Node:
    parser = TreeBuilder()
    parser.feed(html)
    return parser.root


def clean_text(value: str) -> str:
    return " ".join(unescape(value or "").replace("\xa0", " ").split())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return slug.strip("-")


def canonical_slug(url: str, name: str) -> str:
    parsed = urlparse(url)
    path_slug = parsed.path.rstrip("/").split("/")[-1]
    return path_slug or slugify(name)


def parse_generation_and_rarity(value: str) -> tuple[int | None, str | None]:
    generation_match = re.search(r"Generation\s+(\d+)", value, re.IGNORECASE)
    generation = int(generation_match.group(1)) if generation_match else None
    parts = [part.strip() for part in value.split("\u00b7")]
    rarity = parts[1] if len(parts) > 1 and parts[1] else None
    return generation, rarity


def infer_rarity(description: str) -> str | None:
    match = re.search(r"\b(SSR|SR|R)(?:-grade)?\b", description, re.IGNORECASE)
    return match.group(1).upper() if match else None


def image_url(node: Node, base_url: str) -> str | None:
    image = node.first_descendant("img")
    if not image:
        return None
    raw_url = image.attrs.get("src")
    if not raw_url:
        srcset = image.attrs.get("srcset", "")
        raw_url = srcset.split(",")[0].strip().split(" ")[0] if srcset else ""
    return urljoin(base_url, raw_url) if raw_url else None


def find_section(root: Node, title: str) -> Node | None:
    heading = root.first_descendant("h2", title)
    return heading.parent if heading else None


def parse_stats(section: Node | None) -> dict[str, str]:
    if not section:
        return {}
    stats: dict[str, str] = {}
    for card in section.descendants("div"):
        if "bg-gray-50" not in card.classes():
            continue
        paragraphs = card.direct_children("p")
        if len(paragraphs) != 2:
            continue
        label = paragraphs[0].text()
        value = paragraphs[1].text()
        if label and value:
            stats[label] = value
    return stats


def parse_skills(section: Node | None, *, base_url: str, image_alt_prefix: str = "Skill") -> list[dict[str, object]]:
    if not section:
        return []
    skills: list[dict[str, object]] = []
    for image in section.descendants("img"):
        alt = clean_text(image.attrs.get("alt", ""))
        if not alt.startswith(image_alt_prefix):
            continue
        card = image.parent
        while card and card.tag != "div":
            card = card.parent
        while card and len(card.direct_children("p")) < 2:
            card = card.parent
        if not card:
            continue
        paragraphs = card.direct_children("p")
        description = paragraphs[0].text() if paragraphs else ""
        progression_text = paragraphs[1].text() if len(paragraphs) > 1 else ""
        progression_name, progression_values = parse_progression(progression_text)
        skills.append(
            {
                "name": alt,
                "description": description,
                "progression": {
                    "name": progression_name,
                    "values": progression_values,
                },
                "image_url": image_url(card, base_url),
            }
        )
    return skills


def parse_progression(value: str) -> tuple[str | None, list[str]]:
    value = clean_text(value).removeprefix("\U0001f4c8").strip()
    if ":" not in value:
        return (value or None), []
    name, raw_values = value.split(":", 1)
    return clean_text(name), [clean_text(item) for item in raw_values.split("/") if clean_text(item)]


def parse_combat_section(root: Node, title: str, base_url: str) -> dict[str, object]:
    section = find_section(root, title)
    return {
        "stats": parse_stats(section),
        "skills": parse_skills(section, base_url=base_url),
    }


def parse_exclusive_gear(root: Node, base_url: str) -> dict[str, object] | None:
    section = find_section(root, "Exclusive Gear")
    if not section:
        return None
    heading = section.first_descendant("h3")
    return {
        "name": heading.text() if heading else None,
        "stats": parse_stats(section),
        "skills": parse_skills(section, base_url=base_url, image_alt_prefix="Gear Skill"),
    }


def parse_sources(root: Node) -> list[str]:
    sources_heading = root.first_descendant("h3", "Sources")
    if not sources_heading or not sources_heading.parent:
        return []
    return [item.text() for item in sources_heading.parent.descendants("li") if item.text()]


def parse_hero_page(url: str, html: str) -> dict[str, object]:
    root = parse_html(html)
    name_heading = root.first_descendant("h1")
    if not name_heading:
        raise ValueError(f"Could not find hero name in {url}")

    name = name_heading.text()
    profile = name_heading.parent
    profile_paragraphs = profile.direct_children("p") if profile else []
    first_paragraph = profile_paragraphs[0].text() if profile_paragraphs else ""
    generation, rarity = parse_generation_and_rarity(first_paragraph)
    if generation is not None:
        hero_class = profile_paragraphs[1].text().lower() if len(profile_paragraphs) > 1 else None
        description = profile_paragraphs[2].text() if len(profile_paragraphs) > 2 else ""
    else:
        hero_class = first_paragraph.lower() if first_paragraph else None
        description = profile_paragraphs[1].text() if len(profile_paragraphs) > 1 else ""
    rarity = rarity or infer_rarity(description)

    data: dict[str, object] = {
        "class": hero_class,
        "generation": generation,
        "rarity": rarity,
        "description": description,
        "sources": parse_sources(root),
        "image_url": image_url(name_heading.parent.parent if name_heading.parent and name_heading.parent.parent else root, url),
        "conquest": parse_combat_section(root, "Conquest", url),
        "expedition": parse_combat_section(root, "Expedition", url),
        "exclusive_gear": parse_exclusive_gear(root, url),
    }

    return {
        "entity_type": "hero",
        "slug": canonical_slug(url, name),
        "name": name,
        "data": data,
    }


def discover_hero_urls(index_url: str = DEFAULT_HEROES_INDEX) -> list[str]:
    root = parse_html(fetch_url(index_url))
    urls: list[str] = []
    seen: set[str] = set()
    for link in root.descendants("a"):
        href = link.attrs.get("href", "")
        absolute = urljoin(index_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != urlparse(index_url).netloc:
            continue
        if not parsed.path.startswith("/heroes/") or parsed.path.rstrip("/") == "/heroes":
            continue
        clean_url = absolute.split("#", 1)[0].split("?", 1)[0]
        if clean_url not in seen:
            seen.add(clean_url)
            urls.append(clean_url)
    return urls


def read_urls_file(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse Kingshot Guide hero pages into JSON for RAG ingestion.")
    parser.add_argument("--url", action="append", default=[], help="Hero page URL. Can be passed more than once.")
    parser.add_argument("--urls-file", type=Path, help="Text file containing one hero URL per line.")
    parser.add_argument("--all-heroes", action="store_true", help="Discover and parse every hero linked from /heroes.")
    parser.add_argument("--output", "-o", type=Path, help="Output JSON path. Defaults to stdout.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON with indentation.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    urls = list(args.url)
    if args.urls_file:
        urls.extend(read_urls_file(args.urls_file))
    if args.all_heroes:
        urls.extend(discover_hero_urls())

    deduped_urls = list(dict.fromkeys(urls))
    if not deduped_urls:
        print("Provide --url, --urls-file, or --all-heroes.", file=sys.stderr)
        return 2

    entities = [parse_hero_page(url, fetch_url(url)) for url in deduped_urls]
    payload: object = entities[0] if len(entities) == 1 else entities
    output = json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
