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
GET /v1/leaderboards/kingdom/{type}?kid={kid}&limit={limit}
GET /v1/leaderboards/global/{type}?limit={limit}
GET /v1/leaderboards/search?type={type}&uid={uid}&kid={kid}
```

Common board types:

| Type | Board |
|---:|---|
| 1 | Alliance Power |
| 2 | Alliance Kills |
| 3 | Personal Power |
| 20 | Mystic Trial |

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
| 422 | Invalid request | Fix required params or types |
| 502 | Gateway error | Retry with backoff |
| 503 | Gateway session temporarily down | Retry shortly |
| 504 | Gateway timeout | Retry with backoff |

## Bot Integration Notes

Current project usage:

- `/stats` uses the default KingShot API for the profile image, then enriches fields from `GET /v1/players/by-fid/{fid}`.
- `/ks_arena` accepts `fid`, resolves `uid`, then calls `/v1/arena/{uid}`.
- `/ks_board_search` accepts `fid`, resolves `uid`, then calls `/v1/leaderboards/search`.
- `/ks_kingdom_board` and `/ks_global_board` use `resolve=true`.
- `/addalliance` uses `/v1/alliances/{aid}?kid={kid}` and only imports members that include `fid`.

## Client Etiquette

The API sits in front of a real game gateway session. Keep usage modest:

- Avoid tight polling loops.
- Cache repeated reads on your side when practical.
- Prefer alliance `/full` over many parallel player calls.
- Avoid heavy map sweeps from public Discord commands.
- Add cooldowns or admin gates for expensive bot commands.
