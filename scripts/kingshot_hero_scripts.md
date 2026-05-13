# Kingshot Hero Data Scripts

This guide covers the two helper scripts used to scrape Kingshot Guide hero data and upload it into the RAG database.

## Scripts

- `scripts/parse_kingshot_heroes.py`: scrapes hero pages and creates JSON.
- `scripts/upload_kingshot_heroes.py`: uploads parsed JSON into `KingshotEntity` and creates embedded `KingshotChunk` records.

## 1. Parse Hero Data

Parse one hero:

```bash
python3 scripts/parse_kingshot_heroes.py \
  --url https://www.kingshotguide.org/heroes/amadeus \
  --output kingshot_heroes.json \
  --pretty
```

Parse all heroes linked from the Kingshot Guide heroes index:

```bash
python3 scripts/parse_kingshot_heroes.py \
  --all-heroes \
  --output kingshot_heroes.json \
  --pretty
```

Parse a custom list of URLs:

```bash
python3 scripts/parse_kingshot_heroes.py \
  --urls-file hero_urls.txt \
  --output kingshot_heroes.json \
  --pretty
```

The URL file should contain one hero URL per line. Blank lines and lines starting with `#` are ignored.

## Output Shape

For one URL, the parser writes one JSON object. For multiple URLs, it writes a JSON array.

```json
{
  "entity_type": "hero",
  "slug": "amadeus",
  "name": "Amadeus",
  "data": {
    "class": "infantry",
    "generation": 1,
    "rarity": "Mythic",
    "description": "Kingshot Gen 1 Infantry hero...",
    "sources": ["Strongest Governor (Gen 1)", "VIP Package"],
    "conquest": {
      "stats": {},
      "skills": []
    },
    "expedition": {
      "stats": {},
      "skills": []
    },
    "exclusive_gear": {
      "name": "Aegis of Fate",
      "stats": {},
      "skills": []
    }
  }
}
```

## 2. Validate Before Upload

Use dry-run mode first. This validates the file and shows how many chunks will be embedded per hero.

```bash
.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json \
  --dry-run
```

Limit validation to the first few heroes:

```bash
.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json \
  --dry-run \
  --limit 3
```

## 3. Upload To Database

The upload script reads configuration from `.env` by default.

Required:

```bash
COCKROACHDB_URL=...
NVIDIA_API_KEY=...
```

Optional:

```bash
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_EMBEDDING_MODEL=nvidia/llama-nemotron-embed-1b-v2
NVIDIA_MODEL=openai/gpt-oss-120b
```

Run upload:

```bash
.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json
```

By default, upload replaces existing chunks for each matching hero slug. The structured `KingshotEntity` row is updated, and new embedded chunks are inserted.

Append chunks instead of replacing them:

```bash
.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json \
  --keep-existing-chunks
```

Upload only the first hero:

```bash
.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json \
  --limit 1
```

## Full Flow

```bash
python3 scripts/parse_kingshot_heroes.py \
  --all-heroes \
  --output kingshot_heroes.json \
  --pretty

.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json \
  --dry-run

.venv/bin/python scripts/upload_kingshot_heroes.py \
  --input kingshot_heroes.json
```

## Useful Flags

Parser:

- `--url`: parse one hero URL. Can be repeated.
- `--urls-file`: parse URLs from a text file.
- `--all-heroes`: discover and parse all heroes linked from `/heroes`.
- `--output` / `-o`: write JSON to a file. Without this, JSON prints to stdout.
- `--pretty`: format JSON with indentation.

Uploader:

- `--input` / `-i`: parsed JSON file to upload.
- `--dry-run`: validate and print planned uploads without DB or API calls.
- `--limit`: upload only the first N entities.
- `--keep-existing-chunks`: append chunks instead of replacing existing chunks.
- `--database-url`: override `COCKROACHDB_URL`.
- `--api-key`: override `NVIDIA_API_KEY`.
- `--base-url`: override `NVIDIA_BASE_URL`.
- `--embedding-model`: override `NVIDIA_EMBEDDING_MODEL`.
- `--chat-model`: override `NVIDIA_MODEL`.
