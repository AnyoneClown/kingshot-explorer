# KingShot Data API

This project uses the KingShot Data API as a read-only gateway for live player, alliance, arena, leaderboard, and map data.

Do not hardcode the API key in source code. Configure it with:

```env
KS_DATA_API_KEY=your_kingshot_data_api_key
KS_DATA_API_BASE_URL=https://ks.jeab.dev
KS_DATA_TIMEOUT_SECONDS=30
```

The service client sends the key as:

```http
X-API-Key: <KS_DATA_API_KEY>
```

`Authorization: Bearer <KS_DATA_API_KEY>` is also supported by the API, but this bot uses `X-API-Key`.

## Base URL

```text
https://ks.jeab.dev
```

## Health Check

No auth is required:

```bash
curl https://ks.jeab.dev/healthz
```

## Player Endpoints

Use `fid` for the Governor ID shown in game. Use `uid` only when the API has already returned it.

```text
GET /v1/players/by-fid/{fid}
GET /v1/players/{uid}
```

Example:

```bash
curl -H "X-API-Key: $KS_DATA_API_KEY" \
  "https://ks.jeab.dev/v1/players/by-fid/125685742"
```

Important response behavior:

- Unknown `fid` returns HTTP 200 with `{"error":"fid not found"}`.
- Unknown `uid` can return a profile object with null fields.
- Privacy settings can hide fields such as VIP, gear, or profile details.
- Power may appear as top-level `power`, or in `stats["8"]` for sparse responses.

## Alliance Endpoints

```text
GET /v1/alliances/{aid}?kid={kid}
GET /v1/alliances/{aid}/full?kid={kid}
```

Use `/v1/alliances/{aid}` for alliance summary plus lean roster.

Use `/v1/alliances/{aid}/full` when full member profiles are needed. This is preferred over firing many individual player requests because the API paces the member fetches.

Example:

```bash
curl -H "X-API-Key: $KS_DATA_API_KEY" \
  "https://ks.jeab.dev/v1/alliances/83900009?kid=830"
```

Current lean roster entries may contain only:

```json
{"uid":30669791,"rank":4}
```

Future responses may include `fid`; `/addalliance` should use `fid` when present because gift code redemption needs Governor ID, not internal `uid`.

## Arena Endpoint

Arena uses internal `uid`.

```text
GET /v1/arena/{uid}
```

If the user only provides `fid`, resolve it first:

```text
GET /v1/players/by-fid/{fid}
GET /v1/arena/{uid}
```

## Leaderboard Endpoints

The kingdom leaderboard path uses board type in the URL and kingdom ID as a query param:

```text
GET /v1/leaderboards/kingdom/{type}?kid={kid}&limit={limit}&key={key}&aid={aid}&rank_id={rank_id}
GET /v1/leaderboards/global/{type}?limit={limit}&key={key}
GET /v1/leaderboards/search?type={type}&uid={uid}&kid={kid}
```

Kingdom boards require `kid`. `aid` and `rank_id` scope alliance or season boards when the game board supports that. Global boards are cross-kingdom. Search returns one player's position for a given `type`, `uid`, and `kid`.

Leaderboard board types:

| Type | Name | Entries | Score unit |
|---:|---|---|---|
| 1 | Alliance Power | alliance | Power |
| 2 | Alliance Kills | alliance | Kills |
| 3 | Personal Power | player | Power |
| 4 | Kill Count | player | Kills |
| 5 | Town Center Level | player | Level |
| 6 | Rebel Conquest Stage | player | Stage |
| 7 | Hero Power | player | Hero Power |
| 8 | Hero's Total Power | player | Hero's Total Power |
| 16 | Total Pet Power | player | Pet Power |
| 18 | Island Prosperity | player | Prosperity |
| 20 | Mystic Trial | player | Total Stages |
| 21 | Coliseum | player | tier-banded |
| 22 | Forest of Life | player | tier-banded |
| 23 | Crystal Cave | player | tier-banded |
| 24 | Knowledge Nexus | player | tier-banded |
| 25 | Molten Fort | player | tier-banded |
| 26 | Radiant Spire | player | tier-banded |
| 27 | Stage Leaderboard | player | Stage |
| 28 | Star Leaderboard | player | Star Rating |
| 29 | Master Total Power | player | Master Total Power |

Types `9` through `15`, `17`, and `19` have no in-game board.

The Climb Tower trial family is type `20` plus `21` through `26`. Type `20` is Mystic Trial with band `0`. For types `21` through `26`, scores are tier-encoded as:

```text
score = (type - 20) * 10000 + points
```

Strip the band offset to read actual progress. For example, Radiant Spire score `60753` means `753`.

Example: personal power in kingdom 830:

```bash
curl -H "X-API-Key: $KS_DATA_API_KEY" \
  "https://ks.jeab.dev/v1/leaderboards/kingdom/3?kid=830&limit=20"
```

This returns entries ordered from best to lowest, usually by `rank` ascending and `score` descending.

To enrich entries with Governor ID, name, and alliance info:

```bash
curl -H "X-API-Key: $KS_DATA_API_KEY" \
  "https://ks.jeab.dev/v1/leaderboards/kingdom/3?kid=830&limit=20&resolve=true"
```

Do not call `/v1/leaderboards/kingdom/830` expecting kingdom 830. That treats `830` as the board type and fails unless `kid` is also provided.

Example response for Mystic Trial:

```json
{
  "type": 20,
  "kid": 777,
  "scope": "kingdom",
  "count": 100,
  "entries": [
    {
      "rank": 1,
      "uid": 28928146,
      "score": 3013,
      "score_decimal": 301.3,
      "update_ts": 1780994960
    }
  ],
  "self": {
    "rank": 12,
    "uid": 12345678,
    "score": 2500
  }
}
```

`self` is present only if the gateway returns the current account's ranked entry. It is absent when the account is not ranked in that kingdom.

Cross-kingdom lookups work for any `kid`; there is no transfer-group or event gating on the gateway. A board returning `0` entries means the board is off-season for that kingdom, not that access was denied.

Use `score` as the game-displayed raw integer. `score_decimal` is included only for boards that are genuinely decimal. Integer-score boards such as Mystic Trial should use `score`. `update_ts` is a Unix timestamp.

## Raw Passthrough Endpoint

`POST /v1/call` is a raw passthrough endpoint for first-party debugging. It encodes any of the game gateway `req_*` protocols and returns the decoded response body.

This endpoint is disabled by default and returns `404` unless `KS_API_ALLOW_RAW=1`.

Example payload:

```json
{"name":"req_heartbeat","args":{}}
```

## Map Endpoints

```text
GET /v1/map/tile?kid={kid}&x={x}&y={y}
GET /v1/map/find?type={type}&...
POST /v1/map/scan
```

Targeted map calls are intended for normal use. Avoid large sweeps from bot commands.

Example tile lookup:

```bash
curl -H "X-API-Key: $KS_DATA_API_KEY" \
  "https://ks.jeab.dev/v1/map/tile?kid=830&x=500&y=500"
```

## Error Handling

| Status | Meaning | Bot behavior |
|---:|---|---|
| 200 | Success, including some not-found bodies | Check body fields like `error` or `name` |
| 401 | Bad or missing key | Do not retry; fix config |
| 404 | Raw passthrough disabled | Do not use `/v1/call` unless explicitly enabled |
| 422 | Invalid request | Fix required params or types |
| 502 | Gateway error | Retry with backoff |
| 503 | Gateway unavailable, connect or login failed | Retry shortly; server session may need repair |
| 504 | Gateway timeout | Retry with backoff |

Errors use FastAPI's shape:

```json
{"detail":"<message>"}
```

## Server Configuration

These are server-side variables for the API service itself. The bot normally only needs `KS_DATA_API_KEY`, `KS_DATA_API_BASE_URL`, and `KS_DATA_TIMEOUT_SECONDS`.

| Env var | Default | Purpose |
|---|---|---|
| `KS_API_HOST` | `127.0.0.1` | HTTP bind address |
| `KS_API_PORT` | `8080` | HTTP bind port |
| `KS_API_KEYS` | none | Comma-separated keys; each entry can be `key` or `key:label` |
| `KS_API_KEYS_FILE` | none | JSON `{key: label}` or `[key, ...]`; loaded before `KS_API_KEYS` |
| `KS_API_NO_AUTH` | `0` | `1` runs open with no key; use only behind a trusted network |
| `KS_API_ALLOW_RAW` | `0` | `1` enables `POST /v1/call` |
| `KS_API_MAX_CONCURRENCY` | `4` | Maximum simultaneous in-flight gateway calls |

`KS_HOST` and `KS_PORT` configure the game gateway. `KS_API_HOST` and `KS_API_PORT` configure this HTTP API server. With auth required by default and no keys configured, the server refuses to start so it fails closed.

## Bot Integration Notes

Current project usage:

- `/stats` uses the default KingShot API for the profile image, then enriches fields from `GET /v1/players/by-fid/{fid}`.
- `/scout` uses `GET /v1/leaderboards/kingdom/8?kid={kid}&limit={limit}&resolve=true`, then enriches returned entries with `GET /v1/players/by-fid/{fid}` and sends stats-style embeds. The command defaults to 5 players and caps the limit at 15.
- `/addalliance` uses `/v1/alliances/{aid}?kid={kid}` and only imports members that include `fid`.

## Client Etiquette

The API sits in front of a real game gateway session. Keep usage modest:

- Avoid tight polling loops.
- Cache repeated reads on your side when practical.
- Prefer alliance `/full` over many parallel player calls.
- Avoid heavy map sweeps from public Discord commands.
- Add cooldowns or admin gates for expensive bot commands.
