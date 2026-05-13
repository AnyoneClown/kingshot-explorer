#!/usr/bin/env python3
"""Upload parsed Kingshot hero JSON into the RAG tables with embeddings."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.session import DatabaseManager  # noqa: E402
from services.kingshot_rag_service import KingshotChunkInput, KingshotRAGService  # noqa: E402


DEFAULT_SOURCE_BASE_URL = "https://www.kingshotguide.org/heroes"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def load_entities(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    entities = payload if isinstance(payload, list) else [payload]
    if not all(isinstance(entity, dict) for entity in entities):
        raise ValueError("Input JSON must be one entity object or a list of entity objects")
    return entities


def validate_entity(entity: dict[str, Any]) -> None:
    for field in ("entity_type", "slug", "name", "data"):
        if field not in entity:
            raise ValueError(f"Entity is missing required field: {field}")
    if entity["entity_type"] != "hero":
        raise ValueError(f"Only hero entities are supported, got: {entity['entity_type']!r}")
    if not isinstance(entity["data"], dict):
        raise ValueError(f"Entity {entity.get('slug', '<unknown>')} data must be a JSON object")


def source_url_for(entity: dict[str, Any], source_base_url: str) -> str:
    source = clean_text(entity.get("source"))
    if source:
        return source
    return f"{source_base_url.rstrip('/')}/{entity['slug']}"


def format_stats(stats: dict[str, Any]) -> str:
    return ", ".join(f"{name}: {value}" for name, value in stats.items())


def format_skill(skill: dict[str, Any]) -> str:
    description = clean_text(skill.get("description"))
    progression = skill.get("progression") if isinstance(skill.get("progression"), dict) else {}
    progression_name = clean_text(progression.get("name"))
    values = progression.get("values") if isinstance(progression.get("values"), list) else []
    values_text = " / ".join(clean_text(value) for value in values if clean_text(value))
    parts = [clean_text(skill.get("name"))]
    if description:
        parts.append(description)
    if progression_name and values_text:
        parts.append(f"{progression_name}: {values_text}")
    elif progression_name:
        parts.append(progression_name)
    return " - ".join(part for part in parts if part)


def add_chunk(chunks: list[KingshotChunkInput], content: str, metadata: dict[str, Any], source: str) -> None:
    cleaned = clean_text(content)
    if cleaned:
        chunks.append(KingshotChunkInput(content=cleaned, metadata=metadata, source=source))


def build_chunks(entity: dict[str, Any], *, source: str) -> list[KingshotChunkInput]:
    data = entity["data"]
    name = clean_text(entity["name"])
    chunks: list[KingshotChunkInput] = []

    overview_parts = [
        f"{name} is a Kingshot hero.",
        f"Class: {data.get('class')}." if data.get("class") else "",
        f"Generation: {data.get('generation')}." if data.get("generation") is not None else "",
        f"Rarity: {data.get('rarity')}." if data.get("rarity") else "",
        clean_text(data.get("description")),
    ]
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    if sources:
        overview_parts.append("Sources: " + ", ".join(clean_text(item) for item in sources if clean_text(item)) + ".")
    add_chunk(
        chunks,
        " ".join(part for part in overview_parts if part),
        {"section": "overview", "slug": entity["slug"], "name": name},
        source,
    )

    for section_name in ("conquest", "expedition"):
        section = data.get(section_name) if isinstance(data.get(section_name), dict) else {}
        stats = section.get("stats") if isinstance(section.get("stats"), dict) else {}
        if stats:
            add_chunk(
                chunks,
                f"{name} {section_name} stats: {format_stats(stats)}.",
                {"section": section_name, "kind": "stats", "slug": entity["slug"], "name": name},
                source,
            )

        skills = section.get("skills") if isinstance(section.get("skills"), list) else []
        for index, skill in enumerate(skills, 1):
            if not isinstance(skill, dict):
                continue
            add_chunk(
                chunks,
                f"{name} {section_name} skill {index}: {format_skill(skill)}.",
                {
                    "section": section_name,
                    "kind": "skill",
                    "skill_index": index,
                    "slug": entity["slug"],
                    "name": name,
                },
                source,
            )

    gear = data.get("exclusive_gear") if isinstance(data.get("exclusive_gear"), dict) else None
    if gear:
        gear_name = clean_text(gear.get("name")) or "Exclusive Gear"
        gear_stats = gear.get("stats") if isinstance(gear.get("stats"), dict) else {}
        if gear_stats:
            add_chunk(
                chunks,
                f"{name} exclusive gear {gear_name} stats: {format_stats(gear_stats)}.",
                {"section": "exclusive_gear", "kind": "stats", "slug": entity["slug"], "name": name},
                source,
            )
        gear_skills = gear.get("skills") if isinstance(gear.get("skills"), list) else []
        for index, skill in enumerate(gear_skills, 1):
            if not isinstance(skill, dict):
                continue
            add_chunk(
                chunks,
                f"{name} exclusive gear {gear_name} skill {index}: {format_skill(skill)}.",
                {
                    "section": "exclusive_gear",
                    "kind": "skill",
                    "skill_index": index,
                    "slug": entity["slug"],
                    "name": name,
                },
                source,
            )

    return chunks


async def upload_entities(args: argparse.Namespace) -> int:
    load_dotenv()
    database_url = args.database_url or os.getenv("COCKROACHDB_URL")
    api_key = args.api_key or os.getenv("NVIDIA_API_KEY")
    base_url = args.base_url or os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    embedding_model = args.embedding_model or os.getenv("NVIDIA_EMBEDDING_MODEL", "nvidia/llama-nemotron-embed-1b-v2")
    chat_model = args.chat_model or os.getenv("NVIDIA_MODEL", "openai/gpt-oss-120b")

    entities = load_entities(args.input)
    if args.limit is not None:
        entities = entities[: args.limit]

    for entity in entities:
        validate_entity(entity)

    prepared = [
        (entity, source_url_for(entity, args.source_base_url), build_chunks(entity, source=source_url_for(entity, args.source_base_url)))
        for entity in entities
    ]

    if args.dry_run:
        for entity, source, chunks in prepared:
            print(f"{entity['slug']}: {len(chunks)} chunks source={source}")
        return 0

    if not database_url:
        raise ValueError("COCKROACHDB_URL is required. Set it in .env or pass --database-url.")
    if not api_key:
        raise ValueError("NVIDIA_API_KEY is required for embeddings. Set it in .env or pass --api-key.")

    db_manager = DatabaseManager(database_url)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    service = KingshotRAGService(
        db_manager,
        client,
        embedding_model=embedding_model,
        chat_model=chat_model,
    )

    try:
        for index, (entity, source, chunks) in enumerate(prepared, 1):
            print(f"[{index}/{len(prepared)}] Uploading {entity['slug']} with {len(chunks)} chunks")
            await service.upsert_knowledge(
                entity_type=entity["entity_type"],
                slug=entity["slug"],
                name=entity["name"],
                data=entity["data"],
                chunks=chunks,
                source=source,
                replace_chunks=not args.keep_existing_chunks,
            )
    finally:
        await client.close()
        await db_manager.close()

    print(f"Uploaded {len(prepared)} hero entities")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload parsed Kingshot hero JSON to RAG DB tables with embeddings.")
    parser.add_argument("--input", "-i", required=True, type=Path, help="JSON file from parse_kingshot_heroes.py")
    parser.add_argument("--database-url", help="Database URL. Defaults to COCKROACHDB_URL.")
    parser.add_argument("--api-key", help="Embedding API key. Defaults to NVIDIA_API_KEY.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL. Defaults to NVIDIA_BASE_URL.")
    parser.add_argument("--embedding-model", help="Embedding model. Defaults to NVIDIA_EMBEDDING_MODEL.")
    parser.add_argument("--chat-model", help="Chat model stored on the service. Defaults to NVIDIA_MODEL.")
    parser.add_argument("--source-base-url", default=DEFAULT_SOURCE_BASE_URL, help="Base URL used when entity source is absent.")
    parser.add_argument("--keep-existing-chunks", action="store_true", help="Append chunks instead of replacing existing chunks.")
    parser.add_argument("--limit", type=int, help="Only upload the first N entities from the file.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print planned uploads without DB/API calls.")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "WARNING"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s:%(name)s:%(message)s")
    return asyncio.run(upload_entities(args))


if __name__ == "__main__":
    raise SystemExit(main())
