# Kingshot RAG Notes

This project has a reusable Kingshot RAG feature backed by CockroachDB and NVIDIA NIM.

## Current Models

- Embedding provider base URL: `https://integrate.api.nvidia.com/v1`
- Embedding model: `nvidia/nemotron-3.5-embed-1b`
- Chat model: `nvidia/nemotron-3.5-super-vl-120b-a12b`
- Embedding dimension: `2048`

Important: `nvidia/nemotron-3.5-embed-1b` is an asymmetric embedding model. NVIDIA requires `input_type`:

- stored knowledge chunks: `extra_body={"input_type": "passage"}`
- user search queries: `extra_body={"input_type": "query"}`

Vectors from different embedding models are not comparable. After changing the model, re-embed every
`kingshot_chunks` row with the new one (content, metadata and entities stay as they are).

History: the previous model `nvidia/llama-nemotron-embed-1b-v2` reached end of life on NVIDIA on
2026-08-25 and returned `410 Gone`, so every knowledge search and upsert
failed until 2026-11-11. That day the model was switched to `nvidia/nemotron-3.5-embed-1b` (same 2048
dimensions, same `input_type`) and all 320 chunks were re-embedded in one transaction. Verified with
in-memory search (each of five hero queries, one in Ukrainian, ranked the right hero first at distance
0.37–0.52) and with the bot's real direct-mention tool loop, which now finds and cites hero data.

The service sends `dimensions=2048`, matching the model's native output and the current DB schema.

## Database Shape

The active RAG migration is:

- `alembic/versions/20260513120000_add_kingshot_rag_tables.py`

It creates:

- `kingshot_entities`
  - structured entity data
  - `data JSONB`
  - unique `slug`
- `kingshot_chunks`
  - embedded searchable text chunks
  - `embedding VECTOR(2048)`
  - `metadata JSONB`

Search reads from `kingshot_chunks`, not only `kingshot_entities`. If an entity exists but no chunk exists, a search cannot retrieve it.

## Main Code Paths

- Service: `services/kingshot_rag_service.py`
- Chat tool: `search_kingshot_knowledge` in `services/chat_tools.py`
- App wiring: `main.py`
- Config: `config/bot_config.py`

Reusable service methods:

- `upsert_entity(...)`
- `upsert_knowledge(...)`
- `search(...)`
- `answer_question(...)`

Discord access (changed 2026-11-11): the `/kingshot_ask` and `/kingshot_upsert` slash commands
and their handler were removed. Members reach the knowledge base by mentioning the bot (the
`search_kingshot_knowledge` chat tool); knowledge is added with the upload scripts below, which
call `upsert_knowledge(...)`. `answer_question(...)` stays in the service but nothing calls it.

- Earlier NVIDIA error:
  - `input_type parameter is required for asymmetric models`
  - fixed by passing `input_type=query` or `input_type=passage`

## Operational Notes

- Rebuild/restart the Docker container after code changes.
- If `.env` lacks `NVIDIA_API_KEY`, the bot exits with a configuration error.
- The container may warn about `/app/logs` permissions. That affects file logging only; console logs still work.
- If the embedding dimension changes later, update all of these together:
  - migration `VECTOR(...)`
  - `db.models.KingshotChunk.embedding`
  - `KingshotRAGService.EMBEDDING_DIMENSIONS`
  - SQL casts in `KingshotRAGService`
  - tests

## Retrieval Cutoff and Chat Tools

- `KingshotRAGService.search()` ignores chunks whose cosine distance exceeds
  `max_distance` (env `KINGSHOT_RAG_MAX_DISTANCE`, default `0.7`, i.e. similarity ≥ 0.3).
  The default is a starting guess; tune it from the distances the service logs for each search.
- When nothing passes the cutoff, `answer_question` returns a fixed "no matching
  knowledge" answer without calling the chat model.
- Retrieved text and the question are wrapped in `<knowledge>` / `<question>` tags and
  marked as untrusted data in the prompt.
- Mention chat can search the knowledge base through the `search_kingshot_knowledge` chat tool
  (`services/chat_tools.py`), alongside the reminder, gift-code, event calendar and
  Governor ID tools.

## kingshot.net knowledge import (2026-11-11)

The knowledge base holds the kingshot.net database, hero and hero tier-list pages: 471 entities and
3,548 chunks (heroes 37, hero tier lists 8 roles, buildings 10, pets 14, masters 10, Academy / War
Academy / Advanced Truegold / Alliance research 373, governor gear and charms, Truegold TG1–TG8, plus an
overview entity per section). It replaced the 31 heroes scraped from kingshotguide.org in May 2026 (same
slugs, so no duplicates); a copy of those rows, without vectors, is on the Pi in
`~/backups/ds-translator/kingshot-entities-before-kingshot-net-20261111.json`.

Refresh:

```bash
uv run python scripts/parse_kingshot_net.py --fetch -o kingshot_net.json   # ~470 pages, 1 request/s
uv run python scripts/upload_kingshot_knowledge.py -i kingshot_net.json --dry-run
uv run python scripts/upload_kingshot_knowledge.py -i kingshot_net.json --backup backup.json
```

- robots.txt allows these pages and disallows `/api/`; the parser never calls `/api/`.
- The parser reads `<main>` (skipping `aria-hidden` decoration, taking exact numbers from `title`
  attributes instead of the rounded "6K" text, keeping hidden tab panels and closed accordions), the
  page's JSON-LD `FAQPage` (one chunk per question) and the embedded page data for skill levels (the
  HTML shows only the max level). Every chunk names its entity, type and section, and table chunks
  their row range, so one retrieved chunk stands on its own; chunks stay under 850 characters because
  the chat tool clips results at 900.
- The uploader embeds 16 chunks per request and before the transaction opens; a full upload takes
  about 22 minutes with the default 1.5 s pause between entities (shared 40 requests/minute limit).
- Entities that disappear from the site are not deleted by an upload: it upserts by slug only.
- Verification: four independent reviews compared chunks with the pages (all table rows of buildings,
  pets, research, gear and charms, 1,290 hero skill values, every tier placement) until no mismatches
  were left; an end-to-end run of the bot's tool loop retrieved the right entities for hero, building,
  pet, Truegold and governor gear questions in English and Ukrainian.
- Known limits: master research covers only the stage rendered on the page (Masterful); overview lists
  of research cards have no separators between name, stat and value; some cell badges run together
  ("Requires: Embassy 24 Barracks 24").
