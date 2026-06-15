# KingShot Data Service — Response Schemas

Field-level data dictionary for the JSON the service emits. [`API.md`](API.md) is the
endpoint/transport contract; this is the **shape of what comes back**. Everything here
is the *normalized, API-emitted* form — the same dict the live `/openapi.json` schemas
(`PlayerProfile`, `ArenaTeam`, `Leaderboard`, …) describe and the same shape
[`ks/extractors/`](ks/extractors/) produce. Self-contained: no references outside
`service/`.

> This supersedes the raw-capture shapes in the repo's RE notes (`../PLAYER_DATA.md`,
> `../LEADERBOARDS.md`). Those document the **game-side / Lua-hook** form (sproto `map`
> fields as slot-keyed objects, e.g. `lords_equipments["1"]`, `hero_map["1"]`); the
> service decodes maps as **lists** and normalizes them. Use the shapes **here** to write
> a client — they're what the wire actually carries.

## Conventions (apply to every response)

- **Ids are raw integers.** `equipid`, charm/gem ids, hero `id`, skill `id`, `statId`,
  `image`, alliance `resource` item ids, leaderboard board `type` — all numeric, no
  names. Resolve them against your own static tables (see [id → name](#id--name-resolution)).
- **No 404 for "not found."** The gateway has no not-found signal. An unknown `uid`
  returns a profile with **all fields `null`**; an unknown `fid` returns
  `{"fid": …, "error": "fid not found"}`. Check `name`/`error`, not the HTTP status.
- **Permissive by design.** Every field is nullable, unknown keys pass through, and keys
  the gateway omitted are simply **absent** (`response_model_exclude_unset`). Don't assume
  a key is present — `.get()` it. New gateway fields appear without a schema bump.
- **Privacy is honored.** The service emits only what the gateway sends a normal viewer;
  hidden data comes back empty (see `gear` below), never synthesized.

---

## Player profile

`GET /v1/players/{uid}` · `GET /v1/players/by-fid/{fid}` → `PlayerProfile`
(extractor: [`ks/extractors/player.py`](ks/extractors/player.py)).

```jsonc
{
  "uid": 69500617,            // internal gateway player id
  "fid": 279237115,           // shared "Governor ID" (different number space; the in-game id)
  "kid": 3,                   // kingdom id
  "name": "Cassidy",          // nickName
  "power": 52922243,          // total power
  "lv": 30,                   // governor level
  "vip": 5,
  "stove_lv": 27,             // furnace / town-center level
  "life_tree_level": 10,
  "alliance": { "aid": 1200322, "abbr": "AQM", "name": "Aquarium" },  // null fields if none
  "gear":  [ /* GearSlot, see below */ ],   // [] when privacy hides equipment
  "stats": { "12": 34567 },                 // { statId(int): value(int) }, raw ids
  "raw":   { "detail": { … }, "record": { … } }  // lossless decoded gateway responses
}
```

### `gear[]` — governor equipment + charms (`GearSlot`)

Governor loadout, up to **6 slots**, ordered by slot. Sourced from the gateway's
`lords_equipments`.

```jsonc
{ "slot": 1, "equipid": 1005, "charms": [201, 201, 201] }
```

- `slot` — equipment slot, 1–6.
- `equipid` — the equipped item id (raw).
- `charms` — the slot's charm (gem) ids, **ordered by gem key** (1→3); the game calls
  these "gems". `[]` when the slot has no charms.
- **Privacy:** when the player sets equipment to hidden (`lords_equip_show=false` on the
  gateway), `gear` comes back **`[]`** for the whole profile. An empty `gear` means
  "hidden or none," not an error.

### `stats` — numbered stat records

`{ statId: value }` with **raw integer stat ids** (from the gateway's
`player_records.records`). The ids are an in-game enum (kills, gathering, etc.); the
service does not label them — map them yourself. Key fields like `power`, `stove_lv`,
`life_tree_level`, `lv`, `vip` are surfaced at the top level (above); `stats` is the
extended record set.

### `raw`

`{ "detail": …, "record": … }` — the two lossless decoded gateway responses
(`req_player_detail_info`, `req_player_other_record`). Use it only if you need a field
the normalizer didn't surface; prefer the top-level fields otherwise.

> **Heroes are not in the player profile.** A governor profile is the *governor* loadout
> only. For a player's heroes, use the Arena endpoint below (defense team) — there is no
> full hero-roster endpoint.

---

## Arena defense team

`GET /v1/arena/{uid}` → `ArenaTeam` (extractor:
[`ks/extractors/arena.py`](ks/extractors/arena.py)). The player's Arena **defense**
formation.

```jsonc
{
  "uid": 69500617,
  "heroes": [                 // ordered by formation slot; [] if none / not set
    {
      "slot": 1,              // formation slot (= pos)
      "id": 50009,            // hero id (raw; e.g. 50009=Helga, 50004=Howard, 50003=Olive)
      "lv": 10,               // hero level
      "star": 6,              // star / ascension (0,3,6,10,…)
      "pos": 1,               // formation position (same as slot)
      "skills": [             // arr_skill_lv, normalized
        { "id": 500091, "level": 1.0 }   // id = skillId; level is FLOAT (fixed-point, 1 dp)
      ],
      "exclusive_equip": 1050009,   // exclusive-weapon id, or null when none equipped
      "exclusive_equip_lv": 3,      // its level (elv), or null
      "equipment": [ /* hero gear entries; [] when none equipped */ ]
    }
  ]
}
```

- `skills[].level` is a **float**, not an int — the wire type is `decimal(1)`, so real
  levels can be fractional (e.g. `0.5`). Don't cast to int.
- `exclusive_equip` / `exclusive_equip_lv` are `null` when the hero has no exclusive
  weapon. (The gateway's literal field name is the misspelled `exclusive_equipemnt`; the
  service renames it.)
- `equipment` is the hero's gear list (from `hero_equipment.equip_detail`), `[]` when the
  hero has nothing equipped. Entry ids are raw.

---

## Alliance

`GET /v1/alliances/{aid}?kid=` → `Alliance`. `kid` is **required**.
`GET /v1/alliances/{aid}/full?kid=` returns the same shape but each member carries a full
`player` profile (see below). Extractor:
[`ks/extractors/alliance.py`](ks/extractors/alliance.py).

```jsonc
{
  "aid": 1200322, "name": "Aquarium", "abbr": "AQM",
  "power": 12345678, "power_rank": 4,
  "exp": 0, "honor_level": 0, "lv": 0,
  "count": 48, "member_max": 50,
  "leader": { "uid": 85826384, "name": "Moonberry" },
  "members": [ /* AllianceMember */ ],
  "resource": { "<item_id>": <count> },           // { itemId(int): count(int) }
  "condition": { "level": 0, "power": 0 }          // join requirements; {} when none
}
```

### `members[]` (`AllianceMember`)

```jsonc
// lean roster (GET /alliances/{aid}):
{ "uid": 85826384, "rank": 5 }              // rank = alliance role R1..R5 (5 = leader)

// enriched (GET /alliances/{aid}/full): adds a full profile, or an error if that pull failed
{ "uid": 85826384, "rank": 5, "player": { /* PlayerProfile */ } }
{ "uid": 12345678, "rank": 3, "error": "gateway timeout" }   // one bad member never sinks the roster
```

`/full` fetches each member's profile sequentially with a small inter-call delay (polite
own-session pacing) — it's slower; prefer it over hand-rolling N `/players` calls.

---

## Leaderboards

`GET /v1/leaderboards/kingdom/{type}?kid=` ·
`GET /v1/leaderboards/global/{type}` → `Leaderboard`.
`GET /v1/leaderboards/search?type=&uid=&kid=` → `RankSearch`.
Extractor: [`ks/extractors/leaderboard.py`](ks/extractors/leaderboard.py).

```jsonc
// Leaderboard (kingdom or global)
{
  "type": 20, "kid": 777,        // kid present on kingdom scope only
  "scope": "kingdom",            // "kingdom" | "global"
  "count": 100,
  "entries": [ /* LeaderboardEntry, ordered by score descending */ ],
  "self": { /* LeaderboardEntry */ },  // session account's own row — present ONLY if ranked
  "resolved": true               // present only when fetched with resolve=true (see below)
}
```

```jsonc
// LeaderboardEntry
{
  "rank": 1,
  "uid": 28928146,
  "score": 3013,                 // RAW INTEGER the game displays — use this
  "score_decimal": 301.3,        // score / 10; fixed-point reading, only for decimal boards
  "update_ts": 1780994960,       // unix seconds
  // the three below appear ONLY with resolve=true:
  "fid": 279237115,              // shared Governor ID
  "name": "Cassidy",
  "alliance": { "aid": 1200322, "abbr": "AQM", "name": "Aquarium" }
}
```

```jsonc
// RankSearch — one player's position on a board
{ "type": 20, "kid": 777, "rank": 42, "entry": { /* LeaderboardEntry */ } }  // entry null if unranked
```

- **`score` vs `score_decimal`:** `score` is the raw integer the game shows (e.g. `3013`)
  — use it for integer boards like Mystic Trial. `score_decimal` (`score/10`) is the
  fixed-point reading, meaningful only on boards that are genuinely decimal.
- **`resolve=true`** (`GET …?resolve=true`): boards natively carry only `uid`. With
  `resolve=true` the service folds each entry's `fid`, `name`, and `alliance` in via one
  batched lookup and sets `resolved: true`. Without it, you get `uid` only and must
  resolve ids yourself. (This is a per-call enrichment — leave it off for cheap rank-only
  pulls.)
- **Cross-kingdom works for any `kid`** — verified live: there is no transfer-group/event
  gating on the gateway (that's only the client UI's framing). A board returns **0
  entries when it is off-season for that kingdom**, not when access is denied. `self` is
  absent for a kingdom you're not ranked in.

### Board `type` catalog

`type` is the integer board enum from the game's `leaderboard_config`. Pass it directly;
the API returns `type` raw and does **not** embed the label — map it with this table.

| Type | Display Name | Score Unit | Notes |
|------|-------------|------------|-------|
| 1 | Alliance Power | Power | alliance board |
| 2 | Alliance Kills | Kills | alliance board |
| 3 | Personal Power | Power | |
| 4 | Kill Count | Kills | |
| 5 | Town Center Level | Level | |
| 6 | Rebel Conquest Stage | Stage | |
| 7 | Hero Power | Hero Power | single hero |
| 8 | Hero's Total Power | Hero's Total Power | all heroes combined |
| 16 | Total Pet Power | Total Pet Power | |
| 18 | Island Prosperity | Prosperity | requires unlock |
| 20 | Mystic Trial | Total Stages | requires unlock |
| 21 | Coliseum | — | Climb Tower group |
| 22 | Forest of Life | — | Climb Tower group |
| 23 | Crystal Cave | — | Climb Tower group |
| 24 | Knowledge Nexus | — | Climb Tower group |
| 25 | Molten Fort | — | Climb Tower group |
| 26 | Radiant Spire | — | Climb Tower group |
| 27 | Stage Leaderboard | Stage | TD activity |
| 28 | Star Leaderboard | Star Rating | TD activity |
| 29 | Master Total Power | Master Total Power | requires unlock |

Types **101 / 102 / 105** exist in config with no display name and no data — internal/unused.

---

## Castle / Kings (Capital War throne)

`GET /v1/kingdoms/{kid}/castle?history=&kvk=&resolve=&aid=` → `KingdomCastle`.
`GET /v1/kingdoms/{kid}/kings?resolve=` → `KingdomKings` (the `count`/`history`/`kings`
subset). `GET /v1/players/{uid}/reigns?kid=&resolve=` → `PlayerReigns`.
Extractor: [`ks/extractors/castle.py`](ks/extractors/castle.py).

Every kingdom has a **Castle**; its holder is the **King** (throne "president"). A crown
is won two ways, and the data distinguishes them on every reign record:

- **`is_cross_mode`** — `true` ⇒ won during a **cross-kingdom KvK** (Cross-Capital War);
  `false` ⇒ an internal within-kingdom castle fight. (Wire omits the field when false;
  the extractor normalizes it to a real bool.) This is the "**High King**" signal.
- **`is_invader`** — `true` ⇒ the king's `home_kid` differs from the queried kingdom, i.e.
  a foreigner who **conquered** this castle. Derived, not on the wire.

```jsonc
// KingdomCastle  (history[] + kings[] present unless history=false; kvk only if kvk=true)
{
  "kid": 555,
  "owner_kid": 555,              // kingdom currently HOLDING the castle (showinfo.owner_kid)
  "foreign_owner": false,        // owner_kid != kid ⇒ castle is occupied by another kingdom
  "castle_name": "MAFIA",
  "flag": 12,                    // castle flag/emblem id
  "super_count": 0,
  "king": { /* CurrentKing — null if the throne is vacant */ },
  "count": 22,                   // total terms in the hall
  "history": [ /* ReignRecord, oldest → newest */ ],
  "kings": [ /* KingTally, ordered by king_terms desc */ ],
  "kvk": { /* KvkVictories — the ruling alliance's cross-kingdom wins */ }
}
```

```jsonc
// CurrentKing  (req_capital_war_president_info)
{
  "uid": 21473081,
  "name": "ニャアンᵀᴹᴺ",
  "abbr": "TMN",                 // alliance tag
  "aname": "555MAFIA",           // alliance full name
  "aid": null,                   // president_info does NOT carry the alliance id (resolved on demand for kvk)
  "image": 1028,                 // throne avatar-frame id
  "home_kid": 555,               // king's own kingdom (≠ kid ⇒ a foreign ruler)
  "is_cross_mode": false,        // true only while the live KvK fight phase is active
  "manifesto": ""                // king's public declaration (often empty)
}
```

```jsonc
// ReignRecord  (one past throne term; from req_capital_war_hall_of_president)
{
  "uid": 21242190,
  "name": "Zero",
  "abbr": "J4F",                 // alliance tag AT THE TIME of the term
  "home_kid": 556,               // king's kingdom — differs from the queried kid for an invader
  "image": 1018,
  "ts": 1762626159,              // unix seconds the term began
  "is_cross_mode": true,         // won during a cross-kingdom KvK
  "is_invader": true             // home_kid != queried kid (a conquest)
}
```

```jsonc
// KingTally  (per-player aggregate over a kingdom's hall)
{
  "uid": 21473081,
  "name": "ニャアンᵀᴹᴺ",          // name/abbr/image track the player's MOST RECENT term
  "abbr": "TMN",
  "home_kid": 555,
  "image": 1028,
  "king_terms": 18,              // total terms held
  "high_king_terms": 3,          // terms won during a cross-kingdom KvK (is_cross_mode)
  "invader_terms": 0,            // terms held while based in a FOREIGN kingdom (conquests)
  "defender_terms": 18,          // the remainder (home / internal-fight terms)
  "first_ts": 1755961621,
  "last_ts": 1780759657,
  // the three below appear ONLY with resolve=true (present-day identity):
  "fid": 87187103,
  "power": 1230074433,
  "current_alliance": { "aid": 56400075, "abbr": "TMN", "name": "555MAFIA" }
}
```

```jsonc
// KvkVictories  (ruling alliance's cross-kingdom KvK wins; req_alliance_cross_capital_war_win_history)
{
  "aid": 56400075,
  "victories": 5,
  "opponents": [                 // sorted by ts ascending
    { "opponent_kid": 552, "ts": 1760205601 },
    { "opponent_kid": 565, "ts": 1779549621 }
  ]
}
```

```jsonc
// PlayerReigns  (a KingTally for one player + which hall it came from + each reign)
{
  /* …all KingTally fields… */
  "kid": 555,                    // the hall that was read
  "scope": "home_kingdom",       // "home_kingdom" (kid=0 → resolved) | "queried_kingdom" (kid given)
  "reigns": [ /* ReignRecord, this player only */ ],
  "error": "could not resolve player's kingdom"  // only on failure to resolve a home kid
}
```

- **King vs High King are two different counts** — `high_king_terms` (won via cross-kingdom
  KvK, incl. a home king defending) and `invader_terms` (conquered a *foreign* throne) are
  independent; pick whichever fits your UX. A home defender scores `high_king_terms` but not
  `invader_terms`; a foreign conqueror scores both.
- **`kvk=true` costs one extra lookup** — `president_info` has no `aid`, so the service
  resolves the current king → their alliance, then pulls its win history. Pass `?aid=` to
  query a former ruling alliance instead.
- **`/players/{uid}/reigns` is hall-scoped, not global** — a player appears only in halls of
  kingdoms they ruled (home + any they conquered) and there is **no reverse index**. The
  default home-kingdom tally therefore omits conquests of *other* kingdoms; to count those,
  query each conquered kingdom's `/kings` and sum where `uid` matches.
- **Cross-kingdom works for any `kid`/`uid`, year-round** — verified live 2026-06-15. Throne
  history and ownership are globally readable even off-season (unlike live KvK *prep*, which
  is match-scoped). The all-kingdoms-at-once ownership map (`req_capital_war_kingdom_showinfo_list`)
  is **not exposed**: it's gated to an active KvK and times out headless off-season.

---

## World map

Map shapes (`MapTile`, `MapEntity`, `KingdomSweep`) are documented with worked examples
in [`API.md` → World map](API.md). Quick reference:

- `GET /v1/map/tile` → one `MapTile` `{point:{x,y}, kind, type, id, data, raw}`, or `null`.
- `GET /v1/map/find` → a `MapPoint` `{x, y}`, or `null`.
- `POST /v1/map/scan` → `list[MapTile]`.
- `POST /v1/map/sweep` → `KingdomSweep` `{kid, scans, reconnects, count, counts, entities[]}`,
  where each `MapEntity` is `{x, y, kind, type, id, data}`. A `city` entity's `data` carries
  the player (`uid`/`nickName`/`aid`) but **not** `power` — follow up with `/v1/players/{uid}`.

`kind` is the populated map-object sub-type (`city`/`resource`/`monster`/`fort`/…); `data`
is that sub-type's struct; `raw` (tile/scan only) is the lossless decoded union.

---

## id → name resolution

Everything id-shaped is returned **raw** and is the consumer's job to resolve:

- **Gear / charm / hero / skill / stat ids** — map against your own static config tables.
- **Leaderboard board `type`** — use the [catalog above](#board-type-catalog).
- **uid ↔ fid** — the API gives both on a profile; on leaderboards use `resolve=true`
  (or call `/v1/players/by-fid/{fid}` when you only hold the Governor ID). `uid` is the
  internal key; `fid` is the in-game shared id.
- **Castle ids** — `kid` / `home_kid` / `owner_kid` / `opponent_kid` are kingdom (server)
  ids, self-describing. Throne `image` is an avatar-frame id and castle `flag` an emblem id
  — both map against your static config tables (no name endpoint). For a king's present-day
  name/power/alliance, fetch the castle/king routes with `resolve=true`.
