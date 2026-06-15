# Integration Guide — KingShot Data Service

How a sibling service consumes this API. The data service is a thin **live
pull-through**: it authenticates to the game gateway and serves normalized JSON.
**Your service owns digestion, storage, scheduling, and history** — the data service
keeps no database and caches nothing.

```
┌─────────────┐   HTTP + API key    ┌──────────────────┐   sproto/TCP   ┌──────────┐
│ your sibling │ ─────────────────▶ │  ks data service │ ═════════════▶ │ KingShot │
│  (digest +   │ ◀───── JSON ────── │ (FastAPI +       │ ═════════════▶ │  gateway │
│   storage)   │                    │  SessionPool)    │   N sessions   └──────────┘
└─────────────┘                     └──────────────────┘
```

The service holds a **pool of gateway accounts** (one durable `session_key` each, often
in different kingdoms) and load-balances requests across them — least-in-flight, with
failover if one account's session drops. More accounts = more concurrent capacity and a
smaller per-account request rate (politer to anti-cheat). A single account still works;
the pool just has one member.

## 1. Run the data service

Copy just the `service/` directory to the host/VPS — it is self-contained (the sproto
schemas are bundled in the package; no sibling `proto/` dir is needed at runtime, and
the base install pulls no third-party packages — only `.[api]` adds fastapi+uvicorn).
One-time gateway credentials are harvested per [`README.md`](README.md) (they last ~10
years):

```bash
# gateway credentials. EITHER a pool of accounts (load-balanced) …
#   accounts.json  ->  [{"label":"k2181","session_key":"…","fpid":"…"}, {…}]
#   (defaults to <service-root>/accounts.json; or point KS_ACCOUNTS_FILE at it)
export KS_DISTINCT_ID=…   KS_DEVICE_ID=…      # device-scoped, shared by same-device accounts
# … OR a single account via env (used when no accounts.json is present):
export KS_SESSION_KEY=…   KS_FPID=…   KS_DISTINCT_ID=…   KS_DEVICE_ID=…

# API server config
export KS_API_HOST=0.0.0.0           # or keep 127.0.0.1 + reverse proxy
export KS_API_PORT=8080
export KS_API_KEYS="$(openssl rand -hex 24):my-sibling"   # key:label

pip install -e '.[api]'              # fastapi + uvicorn
python -m ks.api
```

Verify it's up and logged into the gateway:

```bash
curl -s localhost:8080/healthz
# {"status":"ok","connected":true,
#  "login":{"serverid":2181,"uid":85826384},          # first connected account (back-compat)
#  "accounts":[{"label":"k2181-primary","connected":true,"serverid":2181,"uid":85826384,"in_flight":0},
#              {"label":"k2201-secondary","connected":true,"serverid":2201,"uid":86726615,"in_flight":0}]}
```

`status` is `ok` if **any** account is connected (`degraded` if none); inspect
`accounts[].connected` to spot a single dead session in an otherwise-healthy pool.

For production, run it under a process supervisor (systemd / a container) so it
restarts on crash. A minimal systemd unit:

```ini
[Service]
EnvironmentFile=/etc/ks-service.env     # the KS_* exports above (mode 600)
ExecStart=/opt/ks/.venv/bin/python -m ks.api
Restart=always
```

## 2. Issue keys

Each consumer gets its own key+label so you can attribute and revoke independently.
Either inline (`KS_API_KEYS="keyA:teamA,keyB:teamB"`) or via a file:

```bash
export KS_API_KEYS_FILE=/etc/ks-api-keys.json   # {"keyA":"teamA","keyB":"teamB"}
```

Rotate by editing the set and restarting. Treat keys as secrets (don't log them).

## 3. Call it

The full endpoint contract is in [`API.md`](API.md) / `/docs`; the field-level response
data dictionary (player gear/charms/stats, arena heroes, leaderboard entries + board-type
catalog) is in [`SCHEMAS.md`](SCHEMAS.md). Response shapes are also published as named
schemas in `/openapi.json`, so you can generate a typed client instead of hand-rolling one:

```bash
# Python:     pip install openapi-python-client
openapi-python-client generate --url http://localhost:8080/openapi.json
# TypeScript: npx openapi-typescript http://localhost:8080/openapi.json -o ks-api.d.ts
```

Or a minimal hand-rolled Python client:

```python
import httpx

class KSData:
    def __init__(self, base, key):
        self._c = httpx.Client(base_url=base, headers={"X-API-Key": key}, timeout=20)

    def player(self, uid):            return self._get(f"/v1/players/{uid}")
    def player_by_fid(self, fid):     return self._get(f"/v1/players/by-fid/{fid}")
    def alliance(self, aid, kid):     return self._get(f"/v1/alliances/{aid}", kid=kid)
    def alliance_full(self, aid, kid):return self._get(f"/v1/alliances/{aid}/full", kid=kid)

    def _get(self, path, **params):
        r = self._c.get(path, params=params)
        r.raise_for_status()          # 4xx/5xx -> exception (see error handling)
        return r.json()

ks = KSData("http://localhost:8080", "my-key")
gov = ks.player_by_fid(277720981)     # Governor ID -> full profile
```

Which player id do you have? In-game you see the **Governor ID** (= `fid`) — use
`/v1/players/by-fid/{fid}`. The internal `uid` only appears in data you've already
pulled (e.g. alliance rosters expose `uid`), so chain
`alliance(aid, kid)` → member `uid` → `player(uid)` — or just use
`/v1/alliances/{aid}/full` to get the whole roster enriched in one call.

## 4. Error handling

Map HTTP status to your retry policy (full table in [`API.md`](API.md)):

| Status | What it means for you |
|--------|-----------------------|
| 401 | bad key — fix config, don't retry |
| 422 | you sent a bad request (e.g. missing `kid`) — fix the call |
| 502 / 504 | transient gateway error/timeout — retry with backoff |
| 503 | gateway session down (the service auto-reconnects) — retry after a short delay |

"Not found" is **not** a 404: an unknown `uid` returns a profile with `null` fields;
an unknown `fid` returns `{"error": "fid not found"}`. Check `name`/`error`, not the
status code.

## 5. Be a polite client

Automated access is against the game's ToS and is anti-cheat-watched; this runs on
real account sessions. To keep them durable:

- **Pull on demand / on a modest schedule**, not in tight loops. Each account caps its
  own concurrent gateway calls (`KS_API_MAX_CONCURRENCY`, default 4) and the pool spreads
  load across accounts to keep any one account's request rate low — but pacing is still
  your responsibility.
- **`/v1/map/sweep` is a heavyweight, minutes-long call.** It tiles a whole kingdom on
  its own recycled connections and runs under a **separate** permit pool
  (`KS_API_MAX_SWEEPS`, default 1) so it can't starve the normal call slots — but it still
  ties up an HTTP request for minutes. Prefer the CLI (`python -m ks.cli sweep`) for bulk
  harvests, and don't fire sweeps in parallel expecting them to overlap.
- **Cache on your side.** This service stores nothing — if you need the same player
  twice in a minute, read it from your store, not the API.
- Prefer `/alliances/{aid}/full` over hand-rolling N player calls in parallel; it
  paces members for you.

## 6. What's deferred

- **Board `type` labels** — boards are keyed by an integer `type` (e.g. 20 = Mystic
  Trial — live-verified; 1 = Alliance Power; full catalog in
  [`SCHEMAS.md`](SCHEMAS.md)); the API returns `type` raw and does not embed labels.
  Cross-kingdom pulls work for **any** `kid` (no transfer-group needed — verified); a board
  is empty when off-season for that kingdom.
- **id → name resolution** — gear/hero/skill/stat ids are returned raw; resolve them
  against your own static tables. Exception: leaderboard entries can be enriched with
  `fid`/`name`/`alliance` at request time via `?resolve=true` (see [`API.md`](API.md));
  the rest stay raw.
- **Caching / rate policy / multi-host failover** — out of scope for this service by
  design (SPEC §8); the sibling owns persistence.
