# KingShot Data Service — API Spec

Read-only HTTP API over the KingShot game gateway. A sibling service calls these
endpoints to pull live player / alliance / arena / world-map data; the sibling owns
digestion and storage. This document is the contract. The server also publishes a
live machine-readable schema at **`/openapi.json`** and Swagger UI at **`/docs`**.

- **Implementation:** FastAPI over [`KingshotService`](ks/service.py) (the same
  extractors the CLI uses). One supervised gateway session, reconnect-on-drop.
- **Field-level response shapes:** [`SCHEMAS.md`](SCHEMAS.md) — the data dictionary for
  every response below. Protocol background (optional, repo root): [`../SPEC.md`](../SPEC.md).
- **Base URL:** `http://<host>:<port>` (default bind `127.0.0.1:8080`). Put TLS /
  a public hostname behind a reverse proxy if exposing beyond localhost.

## Authentication

Every `/v1/*` route requires an API key (unless the server runs in no-auth mode).
Supply it as either header:

```
X-API-Key: <key>
Authorization: Bearer <key>
```

Keys are multi-tenant: each key maps to a label for per-caller attribution, so
additional keys can be issued to other consumers later without code changes
(see **Configuration**). Missing/invalid key → **401**. `/healthz` needs no key.

## Endpoints

All responses are JSON. Data handlers return the extractor's normalized dict
verbatim (the same shape as `python -m ks.cli …`). `{uid, fid, aid, kid, x, y}` are
integers.

Every data route declares a named response schema in **`/openapi.json`**
(`components/schemas`: `PlayerProfile`, `Alliance`, `ArenaTeam`, `MapTile`,
`Leaderboard`, `RankSearch`, …) — defined in [`ks/api/models.py`](ks/api/models.py),
browsable in `/docs`, and codegen-ready (`openapi-python-client`,
`openapi-typescript`). The schemas are deliberately permissive: every field is
nullable (unknown ids return null-filled profiles, not 404s) and unknown keys
pass through, so a schema update is never required for you to receive new fields.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/healthz` | liveness + gateway session state (no auth) |
| `GET`  | `/v1/players/{uid}` | player profile by internal `uid` |
| `GET`  | `/v1/players/by-fid/{fid}` | player profile by shared Governor ID (`fid`) |
| `GET`  | `/v1/alliances/{aid}?kid=` | alliance info + member roster |
| `GET`  | `/v1/alliances/{aid}/full?kid=` | alliance + a full profile per member |
| `GET`  | `/v1/arena/{uid}` | arena defense hero team |
| `GET`  | `/v1/map/tile?kid=&x=&y=` | normalized world-map object at a coordinate |
| `GET`  | `/v1/map/find?type=&…` | locate a map object, returns its `{x, y}` |
| `POST` | `/v1/map/scan` | scan rectangular regions, returns all tiles |
| `POST` | `/v1/map/sweep` | sweep a whole kingdom map, returns every entity with coords |
| `GET`  | `/v1/leaderboards/kingdom/{type}?kid=` | kingdom ranking board |
| `GET`  | `/v1/leaderboards/global/{type}` | cross-kingdom ranking board |
| `GET`  | `/v1/leaderboards/search?type=&uid=&kid=` | one player's rank on a board |
| `GET`  | `/v1/kingdoms/{kid}/castle?history=&kvk=&resolve=&aid=` | castle snapshot: current king + owner (+history, +KvK wins) |
| `GET`  | `/v1/kingdoms/{kid}/kings?resolve=` | throne reign history + per-player king tally |
| `GET`  | `/v1/players/{uid}/reigns?kid=&resolve=` | one player's reigns (king / high-king counts) |
| `POST` | `/v1/call` | raw `req_*` passthrough (disabled by default) |

### `GET /v1/players/{uid}` · `GET /v1/players/by-fid/{fid}`

`uid` is the gateway's internal player id; `fid` is the externally-shared "Governor
ID" (a different number space). Use `by-fid` when you only have the in-game Governor
ID — it resolves fid→uid first (cross-kingdom). Response (abridged; full field-level
spec — gear/charms/stats/privacy — in [`SCHEMAS.md`](SCHEMAS.md)):

```jsonc
{
  "uid": 85826384, "name": "Moonberry", "power": 940162, "vip": 3,
  "lv": 30, "stove_lv": 30, "life_tree_level": 12, "kid": 7, "fid": 277720981,
  "alliance": { "aid": 1200322, "abbr": "AQM", "name": "Aquarium" },
  "gear":  [ { "slot": 0, "equipid": 0, "charms": [ ] } ],   // empty when privacy on
  "stats": { "12": 34567 },                                  // {statId: value}, raw ids
  "raw":   { "detail": { … }, "record": { … } }              // lossless passthrough
}
```

Unknown id → a profile with `null` fields (the gateway has no not-found signal — the
server does **not** synthesize a 404). `by-fid` for an unknown fid →
`{ "fid": …, "error": "fid not found" }`.

### `GET /v1/alliances/{aid}?kid=` · `…/full`

`kid` (kingdom id) is **required**. `/full` enriches each member with a complete
`get_player` profile, fetched sequentially with a small inter-call delay (polite
own-session pacing); a failed member becomes `{"uid": …, "error": "…"}` and does not
sink the roster.

```jsonc
{
  "aid": 1200322, "name": "Aquarium", "abbr": "AQM", "power": 12345678,
  "power_rank": 4, "exp": …, "honor_level": …, "lv": …, "count": 48, "member_max": 50,
  "leader": { "uid": …, "name": "…" },
  "members": [ { "uid": …, "rank": 5 } ],          // rank = R1..R5 alliance role
  "resource": { "<item_id>": <count> },
  "condition": { "level": …, "power": … }
}
```

### `GET /v1/arena/{uid}`

Arena **defense** hero team for a player → `ArenaTeam`
`{uid, heroes:[{slot, id, lv, star, pos, skills:[{id, level}], exclusive_equip,
exclusive_equip_lv, equipment}]}`. Hero/skill/equipment ids are raw; `skills[].level` is a
**float** (fixed-point). Full shape in [`SCHEMAS.md` → Arena](SCHEMAS.md). This is the only
hero data the service exposes (defense formation only — no full hero roster).

### World map

- `GET /v1/map/tile?kid=&x=&y=` → one normalized `world_mapobj`
  `{point, kind, type, id, data, raw}`, or `null` if the tile is empty.
- `GET /v1/map/find?type=&level_min=&level_max=&res_id=&full=` → the `{x, y}` of a
  matching object (`type` is the `world_mapobj` union tag, e.g. `3`=resource), or `null`.
- `POST /v1/map/scan` body:

  ```json
  { "kid": 7, "lod": 0,
    "regions": [ { "startpoint": {"x": 500, "y": 470}, "endpoint": {"x": 520, "y": 490} } ] }
  ```

  → a list of normalized tiles across the regions. Regions larger than ~30×30 are
  auto-split into sub-tiles and deduped (the gateway silently returns nothing for an
  oversized single region).
- `POST /v1/map/sweep` body:

  ```json
  { "kid": 271, "bound": 1200, "tile": 30, "reconnect_every": 250,
    "timeout": 4.0, "kinds": ["city"] }
  ```

  → sweeps the **whole** kingdom map (tiles `0..bound` on both axes) and returns every
  object found, deduped by coordinate:

  ```jsonc
  {
    "kid": 271, "scans": 1600, "reconnects": 6, "count": 1083,
    "counts": { "city": 1083, "resource": 27683, "monster": 23145, … },
    "entities": [ { "x": 471, "y": 650, "kind": "city", "type": 2, "id": …,
                    "data": { "uid": 13481087, "nickName": "Tokii", "aid": 28000169 } } ]
  }
  ```

  Long-running (minutes — it tiles the full map). `kinds` filters output (omit for all
  entity kinds). A `city` entity's `data` carries the player (`uid`/`nickName`/`aid`);
  `power` is not on the tile — follow up with `/v1/players/{uid}`. Runs on its own
  recycled connections (the gateway throttles long scan runs), leaving the shared
  session untouched, and under a **separate** concurrency pool (`KS_API_MAX_SWEEPS`,
  default 1) so it doesn't starve other endpoints. `skipped` counts tiles that failed
  every retry. **CLI:** `python -m ks.cli sweep --kid 271 [--kinds city]`.

### Leaderboards

Ranking boards are keyed by an integer **`type`** from the game's `leaderboard_config`
(e.g. **20 = Mystic Trial**, 1 = Alliance Power, 2 = Alliance Kills, 3 = Personal Power).
Pass `type` directly; the **full board-`type` catalog** (~29 boards + score units) is in
[`SCHEMAS.md` → Board `type` catalog](SCHEMAS.md). Entries come back ordered by score
descending, carrying `uid` only unless `resolve=true`.

- `GET /v1/leaderboards/kingdom/{type}?kid=&limit=&key=&aid=&rank_id=&resolve=` — kingdom board
  (`req_rank_range`). `kid` required. `aid`+`rank_id` scope alliance/season boards.
- `GET /v1/leaderboards/global/{type}?limit=&key=&resolve=` — cross-kingdom board
  (`req_global_rank_range`).
- `GET /v1/leaderboards/search?type=&uid=&kid=` — one player's position
  (`req_search_rank`).

`resolve=true` folds each entry's Governor id (`fid`), `name`, and `alliance` in via one
batched lookup and sets `resolved:true` on the board (boards natively return only `uid`).
Omit it for cheap rank-only pulls. Full entry shape in [`SCHEMAS.md` → Leaderboards](SCHEMAS.md).

```jsonc
// GET /v1/leaderboards/kingdom/20?kid=777&limit=100   (Mystic Trial)
{
  "type": 20, "kid": 777, "scope": "kingdom", "count": 100,
  "entries": [
    { "rank": 1, "uid": 28928146, "score": 3013, "score_decimal": 301.3, "update_ts": 1780994960 }
  ],
  "self": { "rank": …, "uid": …, "score": … }   // present only if the gateway returns it
}
```

**Cross-kingdom works for any `kid`** — verified live (2026-06-11): the service account
(kingdom 2181, no transfer-group relationship to 777) pulled 777's Mystic Trial board and
got `#1 score 3013, #2 2955`, **matching the in-game display exactly**. There is no
transfer-group/event gating on the gateway — the migrant UI flow is just how the *client*
surfaces it. A board returns **0 entries when it's off-season for that kingdom**, not when
access is denied. `self` is absent for a kingdom you're not ranked in.

> **`score` vs `score_decimal`:** the game treats sproto `decimal(N)` fields as plain
> integers, so `score` is the **raw integer the game displays** (e.g. `3013`) and
> `update_ts` is a real unix time. `score_decimal` (= `score/10`) is the fixed-point
> reading, included only for boards that are genuinely decimal. For integer-score boards
> like Mystic Trial, use `score`. (The codec was previously mis-scaling all `decimal(N)`
> fields by ÷10 — that's fixed globally, so player/alliance **power** and **timestamps**
> are now clean integers too.)

### Castle / Kings (Capital War throne)

Every kingdom has a **Castle**; whoever holds it is the **King** (the throne
"president"). A king is crowned one of two ways, and the data distinguishes them:

- **Internal castle fight** — a within-kingdom win. `is_cross_mode=false`.
- **Cross-kingdom KvK** (Cross-Capital War) — the monthly cross-server war. The winner
  held the throne *through the cross-kingdom war*: `is_cross_mode=true`. If that winner
  is based in **another** kingdom, they conquered a foreign castle — `is_invader=true`
  (their `home_kid` differs from the kingdom queried). This is the "**High King**" case.

All three routes are **cross-kingdom — any `kid`/`uid` works** (verified live 2026-06-15;
the throne history and ownership are globally readable year-round, unlike live KvK prep
which is match-scoped). Sources: `req_capital_war_president_info` (current king),
`req_capital_war_kingdom_showinfo` (ownership), `req_capital_war_hall_of_president`
(reign log), `req_alliance_cross_capital_war_win_history` (KvK victories).

- `GET /v1/kingdoms/{kid}/castle?history=&kvk=&resolve=&aid=` — the composite snapshot.
  `history=true` (default) adds the reign log + per-player tally; `history=false` gives
  just current state. `kvk=true` adds the ruling alliance's cross-kingdom win record
  (`aid` overrides which alliance). `resolve=true` folds each tallied king's present-day
  `fid`/`power`/`current_alliance` in.
- `GET /v1/kingdoms/{kid}/kings?resolve=` — just the reign history + per-player tally.
- `GET /v1/players/{uid}/reigns?kid=&resolve=` — one player's reign tally. Reads the
  player's **home** kingdom hall by default (`kid=0`), or a specific kingdom's hall when
  `kid` is given.

**Per-king tally fields** (`kings[]`, and the top-level of `/reigns`):

| field | meaning |
| ------- | --------- |
| `king_terms` | total throne terms held |
| `high_king_terms` | terms won during a cross-kingdom KvK (`is_cross_mode`) |
| `invader_terms` | terms held while based in a *foreign* kingdom (a conquest) |
| `defender_terms` | the remainder (home / internal-fight terms) |
| `first_ts` / `last_ts` | unix seconds of earliest / latest term; name+tag track the latest |

> **Scope caveat for `/players/{uid}/reigns`:** a player appears only in the halls of
> kingdoms they actually ruled — their home kingdom (home reigns + home KvK defenses) and
> any foreign kingdom they conquered. There is **no global reverse index**, so the
> home-scoped tally does **not** include conquests of *other* kingdoms; to count those,
> query each conquered kingdom's `/kings` and sum where `uid` matches. `scope` flags which
> hall you read (`home_kingdom` vs `queried_kingdom`).

```jsonc
// GET /v1/kingdoms/555/castle?kvk=true&resolve=true   (trimmed)
{
  "kid": 555, "owner_kid": 555, "foreign_owner": false,   // foreign_owner=true ⇒ castle occupied
  "castle_name": "MAFIA", "flag": 12, "super_count": 0,
  "king": { "uid": 21473081, "name": "ニャアンᵀᴹᴺ", "abbr": "TMN", "aname": "555MAFIA",
            "home_kid": 555, "is_cross_mode": false, "manifesto": "" },
  "count": 22,                                              // total terms in the hall
  "history": [ { "uid": 21242190, "name": "Zero", "abbr": "J4F", "home_kid": 556,
                 "ts": 1762626159, "is_cross_mode": true, "is_invader": true } ],  // oldest→newest
  "kings": [
    { "uid": 21473081, "name": "ニャアンᵀᴹᴺ", "home_kid": 555,
      "king_terms": 18, "high_king_terms": 3, "invader_terms": 0, "defender_terms": 18,
      "first_ts": 1755961621, "last_ts": 1780759657,
      "fid": 87187103, "power": 1230074433,                // present only with resolve=true
      "current_alliance": { "aid": 56400075, "abbr": "TMN", "name": "555MAFIA" } },
    { "uid": 18635742, "name": "Bunny", "home_kid": 481,    // a foreign High King who invaded 555
      "king_terms": 1, "high_king_terms": 1, "invader_terms": 1, "defender_terms": 0 }
  ],
  "kvk": { "aid": 56400075, "victories": 5,
           "opponents": [ { "opponent_kid": 552, "ts": 1760205601 },
                          { "opponent_kid": 565, "ts": 1779549621 } ] }   // KvKs this alliance won
}
```

> **`kvk` needs an alliance id.** `president_info` doesn't carry one, so `kvk=true`
> resolves the current king → their alliance, then pulls that alliance's win history
> (one extra player lookup). The all-kingdoms-at-once ownership map
> (`req_capital_war_kingdom_showinfo_list`) is **not** exposed: it's gated to an active
> KvK and times out headless off-season — query per-kingdom instead.

### `POST /v1/call` — raw passthrough (off by default)

Encodes any of the ~1500 `req_*` protocols and returns the decoded response body.
Powerful and unvalidated; **disabled** (returns 404) unless `KS_API_ALLOW_RAW=1`.
Intended for first-party debugging, not third-party consumers.

```json
{ "name": "req_heartbeat", "args": {} }
```

## Status codes

| Code | Meaning |
|------|---------|
| 200 | success |
| 401 | missing / invalid API key |
| 404 | raw passthrough disabled |
| 422 | request validation failed (e.g. missing `kid`) |
| 502 | gateway returned an error |
| 503 | gateway unavailable (connect/login failed — e.g. expired `session_key`) |
| 504 | gateway timed out |

Errors use FastAPI's shape: `{ "detail": "<message>" }`.

## Configuration (env)

Server-specific vars (gateway credentials are separate — see
[`README.md`](README.md) for `KS_SESSION_KEY` / `KS_FPID` / …):

| Env var | Default | Purpose |
|---------|---------|---------|
| `KS_API_HOST` | `127.0.0.1` | HTTP bind address |
| `KS_API_PORT` | `8080` | HTTP bind port |
| `KS_API_KEYS` | — | comma-separated keys; each `key` or `key:label` |
| `KS_API_KEYS_FILE` | — | JSON `{key: label}` or `[key, …]` (loaded before `KS_API_KEYS`) |
| `KS_API_NO_AUTH` | `0` | `1` runs open (no key) — only behind a trusted network |
| `KS_API_ALLOW_RAW` | `0` | `1` enables `POST /v1/call` |
| `KS_API_MAX_CONCURRENCY` | `4` | per-account cap on simultaneous in-flight gateway calls |
| `KS_ACCOUNTS_FILE` | `accounts.json` | JSON array of pooled gateway accounts (`[{label, session_key, fpid}, …]`); load-balanced. Falls back to single `KS_SESSION_KEY`/`KS_FPID` if absent |
| `KS_PROXY_MAP_FILE` | — | JSON `{label: proxy_url}` pinning each account to its own egress proxy (unique IP). See `PROXY_ROUTING.md` |
| `KS_PROXY` | — | single global egress proxy (one-account/dev; not for unique-IP pinning) |

> `KS_HOST`/`KS_PORT` configure the **game gateway**; `KS_API_HOST`/`KS_API_PORT`
> configure **this server**. Don't confuse them.

With `require_auth` on (the default) and no keys configured, the server refuses to
start — fail closed rather than silently open.
