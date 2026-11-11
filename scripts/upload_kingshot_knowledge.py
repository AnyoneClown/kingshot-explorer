#!/usr/bin/env python3
"""Upload ready-chunked Kingshot knowledge JSON (e.g. from parse_kingshot_net.py) with embeddings.

Input: a JSON array of entities ``{entity_type, slug, name, data, source, chunks: [{content,
metadata}]}``. Each entity is upserted by slug and its chunks are replaced. Chunk embeddings are
computed in batches (see ``KingshotRAGService.EMBEDDING_BATCH_SIZE``) before the transaction opens.

    uv run python scripts/upload_kingshot_knowledge.py -i kingshot_net.json --dry-run
    uv run python scripts/upload_kingshot_knowledge.py -i kingshot_net.json --backup backup.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.session import DatabaseManager  # noqa: E402
from services.kingshot_rag_service import KingshotRAGService  # noqa: E402

MAX_CHUNK_CHARS = 900  # the chat tool clips each retrieved chunk at 900 characters


def load_entities(path: Path) -> list[dict[str, Any]]:
    entities = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entities, list):
        raise ValueError("Input must be a JSON array of entities")
    seen: set[str] = set()
    for entity in entities:
        for key in ("entity_type", "slug", "name", "data", "chunks"):
            if key not in entity:
                raise ValueError(f"Entity {entity.get('slug')!r} is missing {key!r}")
        if entity["slug"] in seen:
            raise ValueError(f"Duplicate slug {entity['slug']!r}")
        seen.add(entity["slug"])
        if not entity["chunks"]:
            raise ValueError(f"Entity {entity['slug']!r} has no chunks")
        for chunk in entity["chunks"]:
            if not str(chunk.get("content", "")).strip():
                raise ValueError(f"Entity {entity['slug']!r} has an empty chunk")
    return entities


async def backup_existing(db_manager: DatabaseManager, slugs: list[str], path: Path) -> int:
    """Save entities (and chunk texts, without vectors) that the upload will replace."""
    async with db_manager.session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT e.entity_type, e.slug, e.name, e.data, e.source,
                           c.content, c.metadata, c.source AS chunk_source
                    FROM kingshot_entities e
                    LEFT JOIN kingshot_chunks c ON c.entity_id = e.id
                    WHERE e.slug = ANY(:slugs)
                    ORDER BY e.slug, c.created_at, c.id
                    """
                ),
                {"slugs": slugs},
            )
        ).mappings().all()
    entities: dict[str, dict[str, Any]] = {}
    for row in rows:
        entity = entities.setdefault(
            row["slug"],
            {k: row[k] for k in ("entity_type", "slug", "name", "data", "source")} | {"chunks": []},
        )
        if row["content"] is not None:
            entity["chunks"].append(
                {"content": row["content"], "metadata": row["metadata"] or {}, "source": row["chunk_source"]}
            )
    path.write_text(json.dumps(list(entities.values()), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return len(entities)


async def upload(args: argparse.Namespace) -> int:
    load_dotenv()
    entities = load_entities(args.input)
    if args.only_type:
        entities = [e for e in entities if e["entity_type"] in set(args.only_type)]
    if args.limit is not None:
        entities = entities[: args.limit]

    chunk_total = sum(len(e["chunks"]) for e in entities)
    longest = max((len(c["content"]) for e in entities for c in e["chunks"]), default=0)
    by_type: dict[str, list[int]] = {}
    for entity in entities:
        stats = by_type.setdefault(entity["entity_type"], [0, 0])
        stats[0] += 1
        stats[1] += len(entity["chunks"])
    print(f"{len(entities)} entities, {chunk_total} chunks, longest chunk {longest} chars")
    for entity_type, (count, chunks) in sorted(by_type.items()):
        print(f"  {entity_type}: {count} entities, {chunks} chunks")
    if longest > MAX_CHUNK_CHARS:
        print(f"warning: chunks longer than {MAX_CHUNK_CHARS} chars are clipped in chat tool results")
    if args.dry_run:
        return 0

    database_url = args.database_url or os.getenv("COCKROACHDB_URL")
    api_key = args.api_key or os.getenv("NVIDIA_API_KEY")
    if not database_url or not api_key:
        raise ValueError("COCKROACHDB_URL and NVIDIA_API_KEY are required (env, .env or flags)")
    db_manager = DatabaseManager(database_url)
    client = AsyncOpenAI(
        base_url=args.base_url or os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=api_key,
        max_retries=3,
    )
    service = KingshotRAGService(
        db_manager,
        client,
        embedding_model=args.embedding_model or os.getenv("NVIDIA_EMBEDDING_MODEL", "nvidia/nemotron-3.5-embed-1b"),
        chat_model=os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b"),
    )
    try:
        if args.backup:
            saved = await backup_existing(db_manager, [e["slug"] for e in entities], args.backup)
            print(f"backed up {saved} existing entities that will be replaced -> {args.backup}")
        started = time.monotonic()
        for index, entity in enumerate(entities, 1):
            await service.upsert_knowledge(
                entity_type=entity["entity_type"],
                slug=entity["slug"],
                name=entity["name"],
                data=entity["data"],
                chunks=entity["chunks"],
                source=entity.get("source"),
                replace_chunks=True,
            )
            if index % 10 == 0 or index == len(entities):
                print(f"[{index}/{len(entities)}] {entity['slug']} ({time.monotonic() - started:.0f}s)", flush=True)
            await asyncio.sleep(args.pause)
    finally:
        await client.close()
        await db_manager.close()
    print(f"uploaded {len(entities)} entities with {chunk_total} chunks")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", "-i", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarise without DB or API calls.")
    parser.add_argument("--backup", type=Path, help="Write entities that will be replaced to this JSON file first.")
    parser.add_argument("--only-type", action="append", help="Upload only this entity_type (repeatable).")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--pause", type=float, default=1.5,
                        help="Seconds between entities, leaving room in the shared NVIDIA rate limit.")
    parser.add_argument("--database-url")
    parser.add_argument("--api-key")
    parser.add_argument("--base-url")
    parser.add_argument("--embedding-model")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "WARNING"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s:%(name)s:%(message)s")
    return asyncio.run(upload(args))


if __name__ == "__main__":
    raise SystemExit(main())
