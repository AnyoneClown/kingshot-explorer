# Kingshot RAG Notes

This project has a reusable Kingshot RAG feature backed by CockroachDB and NVIDIA NIM.

## Current Models

- Embedding provider base URL: `https://integrate.api.nvidia.com/v1`
- Embedding model: `nvidia/llama-nemotron-embed-1b-v2`
- Chat model: `openai/gpt-oss-120b`
- Embedding dimension: `2048`

Important: `nvidia/llama-nemotron-embed-1b-v2` is an asymmetric embedding model. NVIDIA requires `input_type`:

- stored knowledge chunks: `extra_body={"input_type": "passage"}`
- user search queries: `extra_body={"input_type": "query"}`

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

Search reads from `kingshot_chunks`, not only `kingshot_entities`. If an entity exists but no chunk exists, `/kingshot_ask` cannot retrieve it.

## Main Code Paths

- Service: `services/kingshot_rag_service.py`
- Discord handler: `handlers/kingshot_rag_handler.py`
- App wiring: `main.py`
- Config: `config/bot_config.py`

Reusable service methods:

- `upsert_entity(...)`
- `upsert_knowledge(...)`
- `search(...)`
- `answer_question(...)`

Discord commands:

- `/kingshot_upsert`
  - admin-only
  - creates or updates one entity by `slug`
  - replaces prior chunks for that slug
  - embeds chunk content as `passage`
- `/kingshot_ask`
  - embeds question as `query`
  - retrieves nearest chunks by cosine distance
  - sends retrieved chunk content and entity JSON data to the chat model

## Slug Convention

`slug` is the stable unique key for a knowledge item. Reusing a slug updates the existing item.

Examples:

- `bear-trap`
- `frostfire-mine`
- `amane`
- `city-hall-level-30`

Prefer lowercase hyphenated slugs. The current code does not force lowercase, so `Amane` and `amane` would be different slugs.

## Debugging Retrieval

The service logs these useful diagnostics:

- ask/upsert command user, guild, channel, entity type, slug
- embedding input type, model, dimension, text length
- chunk deletion and insertion counts during upsert
- search query, entity type filter, result count
- retrieved chunk slug, entity type, distance, content length, content preview
- answer generation context size and top retrieved slug

Important log interpretation:

- `Kingshot search returned no chunks`
  - likely no rows in `kingshot_chunks`, or entity type filter is too restrictive
- `Kingshot search returned 1 chunks` followed by answer `I don't know`
  - retrieval worked; inspect chunk content and entity JSON data because context may be too thin
- Earlier NVIDIA error:
  - `input_type parameter is required for asymmetric models`
  - fixed by passing `input_type=query` or `input_type=passage`

## Example Upsert

Discord `/kingshot_upsert`:

```text
entity_type: hero
slug: amane
name: Amane
content: Amane can blind people.
data_json: {"skill":"blinding people"}
metadata_json: {"topic":"hero_skill"}
source: manual
```

Then ask:

```text
/kingshot_ask question:"what amane can do?"
```

Expected answer:

```text
Amane can blind people.
```

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
