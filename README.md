# DS Translator Bot

A Discord bot focused on translation, scheduling, player lookup, KVK tracking, and gift code automation.

## Features

- Slash commands with interactive Discord panels and a private `/help` guide.
- A permanent gift-code panel (`/giftadmin panel`) with buttons: members add their Governor
  ID, check their accounts, copy active codes and send in codes the bot has not found
  yet, without knowing any command.
- Multi-server ready: per-server settings in `/settings` (the server's kingdom, the
  gift-code channel, daily event calendar, bot manager role) and per-server gift-code
  rosters.
- Picture cards instead of walls of text: the event calendar, gift-code results, KvK
  history, KvK comparison, the prep dossier's rankings and the scout leaderboard are drawn
  as images in one shared style (`services/card_drawing.py`) and attached to their embeds.
  When a card cannot be drawn or attached, the same information is posted as text.
- Kingdom event calendar from kingshot.net as picture cards with the event artwork:
  `/calendar` shows what is running today, what is next, and a timeline of this week and
  the next; a server can have the daily card posted every day at 08:00 UTC.
- Auto-translation and manual translation features that keep mentions, emoji, links and
  line breaks intact, skip text that needs no translation, and cache repeated messages.
  Right-click a message → **Apps → Translate** shows a private translation in the
  reader's own Discord language. Forwarded messages are translated from the forwarded
  text.
- NVIDIA NIM powered translation plus contextual chat replies. Mentioning the bot (or
  its own role, which Discord's autocomplete also offers) can
  answer questions from this server's data (reminders, active gift codes,
  a Governor ID lookup, the kingdom's event calendar, the Kingshot knowledge base) through
  read-only tools.
- Event scheduling with a self-healing background scheduler, role pings and delivery tracking.
- Player info lookups with a hero and governor gear/charm card, plus KVK history,
  comparisons and a KvK prep dossier.
- Gift code polling and auto-redemption for registered players.
- Searchable player lists with enabled/disabled filters and direct profile lookup.
- Single-message scout reports and paginated KVK history with comparison actions.
- Live manual-redemption progress with compact results and browsable failure details.
- Timezone-aware event forms, save previews, and edit/cancel controls.
- Unified player profile storage in a single players table.
- Clear redemption result categories:
    - Success
    - Already redeemed
    - API rejected
    - Invalid ID

## Gift Code Flow

Gift code support includes:

- Per-server rosters. Player profiles are shared (one in-game account is redeemed once
  per code), and each server keeps its own roster entry with its own enabled flag and
  owner (`guild_player_registrations`). Members can enroll up to
  `MAX_SELF_REGISTERED_PLAYERS` (3) accounts of their own; server managers can fill the
  roster up to `MAX_PLAYERS_PER_GUILD` (300) and import alliances.
- Enabling/disabling roster entries from `/gift roster` (owners for their own entries,
  managers for all), plus a manager **Roster health** report: how many of the server's
  players recent codes reached, and players whose redemptions keep failing permanently,
  with a one-click **Disable these**.
- Polling the upstream gift code source every minute (with back-off on network errors).
  Discovery runs independently of redemption, so a new code is recorded even while
  another code is being redeemed. Members can also send in a code with `/gift submit`
  (or **Submit a code** on the panel): it is checked with one account (the member's own
  when they have one) and, if it works, queued for everyone; one submission per member
  per minute.
- The summary posted after an automatic run carries a result card (picture): share of
  accounts that have the reward, counts of received / already had it / rejected / invalid
  ID, and the same split per kingdom (the five largest; the rest folded into one row).
  The code itself stays as copyable text. Where the bot lacks **Attach Files**, or the
  card cannot be drawn, the counts are posted as text instead. Cards are drawn by
  `services/gift_card_renderer.py` with the same drawing code as the calendar cards
  (`services/card_drawing.py`).
- Members' own accounts go first: a run redeems self-registered accounts (owners with
  at most `MAX_SELF_REGISTERED_PLAYERS` entries in a server), then the rest of the
  server rosters, then unassigned legacy profiles. The gift-code server allows about 30
  requests a minute, so 1,000 accounts take roughly 40 minutes.
- Auto-redeeming new codes for every enabled profile that is enabled in at least one
  server roster. Profiles created before per-server rosters ("unassigned legacy") keep
  being redeemed until a bot operator assigns them to a server (**Assign unassigned
  here** in `/gift roster`). Removing a server's last roster entry deletes the profile.
- Resumable runs. New codes are stored as `pending`; each player's result is saved as
  soon as it is known, and every poll resumes pending, unexpired codes, skipping players
  already handled. A restart or deploy mid-run therefore continues where it stopped.
  Auto runs skip players with final failures for the code (invalid ID, kingdom mismatch,
  level or spend requirement); manual runs skip only players who already have it.
  Temporary failures (outage, rate limit, HTTP 5xx, an unreachable player lookup) are
  not final: an auto pass whose probe fails temporarily stops at once, a pass that meets
  six temporary failures in a row ends early, and a pass that ends with temporary
  failures keeps the code pending and retries just those players after 15 minutes (up
  to 6 passes, or until the code expires). A pass that could not reach everyone does not
  count toward those 6: the code stays pending until it expires, or for 72 hours when
  it has no expiry date. `/giftadmin redeem` is refused while the bot is shutting down.
- Accounts that keep failing are paused. After a completed run, an account whose
  Governor ID or kingdom was rejected on at least 3 of the last 5 codes (including the
  current one) and that none of them reached is disabled: its roster entry in every
  server, or the profile itself when it is unassigned. The server's gift-code channel
  and the bot operators (by DM) get the list; a manager or the owner re-enables an
  entry in `/gift roster` after fixing the ID.
- One job per code: automatic and manual runs share a guard, so a code is never
  redeemed twice concurrently; each server can run one manual job at a time.
- Posting summary embeds when at least one auto-redemption succeeds: each server's
  summary (its own players only) goes to the gift-code channel set in `/settings`, and
  the legacy `AUTO_REDEEM_CHANNELS` receive the global summary (a legacy channel that a
  server also chose in `/settings` gets only that server's summary). While at least a
  minute of a run is left, the same message is posted early as "New Gift Code —
  Redeeming…" with the code, its expiry, progress and how to claim it by hand; it is
  refreshed about once a minute and becomes the summary. Message IDs are stored, so a
  restart edits the message instead of posting a second one.

The redemption summary separates outcomes by category (Success, Already redeemed, API
rejected, Invalid ID) using one label map everywhere.

## Requirements

- Python 3.11 through 3.13
- A Discord bot token
- CockroachDB (or compatible PostgreSQL setup used by current models/migrations).
  asyncpg connections time out after 15 s when connecting and 60 s per statement
  (`db/session.py`), so a connection left half-open by a network blip fails one
  operation instead of stalling a background loop for minutes.

## Setup

1. Install dependencies.

```bash
uv sync --group dev
```

2. Create a .env file.

```env
DISCORD_TOKEN=your_local_discord_bot_token_here
ADMIN_USER_ID=your_discord_user_id
COCKROACHDB_URL=cockroachdb+asyncpg://postgres:password@host:26257/database-name
NVIDIA_API_KEY=your_nvidia_api_key_here
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b
NVIDIA_CHAT_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b
KS_DATA_API_KEY=your_kingshot_data_api_key
KS_DATA_API_BASE_URL=https://ks.jeab.dev
KS_DATA_TIMEOUT_SECONDS=30
COMMAND_PREFIX=!
TRANSLATOR_ROLE=Translator
AUTO_REDEEM_CHANNELS=123456789012345678,876543210987654321
RANDOM_REPLY_CHANCE=0
RANDOM_REPLY_COOLDOWN_SECONDS=180
CHAT_HISTORY_LIMIT=25
MAX_CHAT_RESPONSE_CHARS=500
LOG_LEVEL=INFO
```

Use `ADMIN_USER_IDS=123,456` instead of `ADMIN_USER_ID` if multiple Discord users should be bot operators.

Optional settings (defaults in parentheses): `BANNED_PLAYERS` (Discord user IDs the bot
ignores), `MAX_PLAYERS_PER_GUILD` (300), `MAX_SELF_REGISTERED_PLAYERS` (3),
`LLM_TIMEOUT_SECONDS` (30), `CHAT_USER_COOLDOWN_SECONDS` (10),
`KINGSHOT_RAG_MAX_DISTANCE` (0.7) and `TRANSLATION_LOG_RETENTION_DAYS` (30; `0` keeps
stored translation text forever). See `.env.local.example`.

## Permissions

The bot uses three access levels, enforced by `handlers/permissions.py`:

- **Bot operators** (`ADMIN_USER_ID` / `ADMIN_USER_IDS`) can do everything, including
  actions that affect every server: unassigned legacy players and the all-servers
  redemption job.
- **Server managers** manage the bot inside one server: members with **Manage Server**
  or **Administrator**, members with the bot manager role chosen in `/settings`, and
  bot operators. They configure the server, create/edit/cancel reminders, start/stop
  alliance tracking, import alliances and run redemptions for the server's roster.
- **Members** use everything else: lookups, reports, translation, AI replies, and
  enrolling a few of their own accounts for gift codes.

Manager-only slash commands are hidden from members by default
(`default_permissions(manage_guild=True)`); a server can expose them to the manager
role in **Server Settings → Integrations**. The bot re-checks access on every action,
so hiding is a convenience, not the guard. Messages generated from user or model text
can never ping `@everyone`, `@here` or roles; only reminders opt in to pings.

3. Run migrations.

```bash
uv run alembic upgrade head
```

4. Start the bot.

```bash
uv run python main.py
```

## Docker

Docker uses one Compose file with two services:

- `migrate` is a one-off service that runs `alembic upgrade head` with `.env.local`; the
  bot starts only after it succeeds.
- `translator-bot-local` uses `.env.local` and runs the complete bot. It shuts down
  gracefully on `docker stop` (SIGTERM, 30 s grace period): background jobs stop,
  gift-code progress is saved and connections close.

Container logs are rotated by Docker (`json-file`, 10 MB × 3 files per container) and
start empty whenever the container is recreated. The bot also writes a rotating file
log (`/app/logs/<instance>.log`, 10 MB × 6 files) to the `bot-logs` volume, which
survives rebuilds: `docker compose exec translator-bot-local tail -n 200 /app/logs/local.log`.

Create the real env file from the committed example:

```bash
cp .env.local.example .env.local
```

Start the bot:

```bash
docker compose -f compose.yaml up --build
```

Start in the background:

```bash
docker compose -f compose.yaml up -d --build
```

Start only the bot:

```bash
docker compose -f compose.yaml up -d --build translator-bot-local
```

Common commands:

```bash
docker compose -f compose.yaml logs -f translator-bot-local
docker compose -f compose.yaml restart translator-bot-local
docker compose -f compose.yaml stop translator-bot-local
```

## Commands Overview

The bot registers slash commands through handler modules in handlers/.

Gift code related commands include:

- `/giftadmin redeem gift_code:<code>` (managers) redeems for this server's enabled roster; bot
  operators can add `all_servers:true` to run the global job.
- `/gift add player_ids:<ids>` adds Governor IDs to this server's roster (private reply).
- `/giftadmin import kid:<id> alliance:<choice>` (managers; top 15 by power with names from
  MightPulse's public kingdom view; AIDs remain the choice values and appear as fallback
  labels when names are unavailable). Governor IDs come from the roster, the local UID
  cache, the optional key-protected MightPulse roster (`MIGHTPULSE_API_KEY`) or player profiles.
- `/gift remove player_id:<id>` removes an entry from this server's roster after confirmation.
- `/gift roster` browses this server's roster.
- `/gift codes` lists active codes as copyable lines.
- `/gift accounts` privately shows the caller's own accounts in this server, whether each
  is receiving codes, and the result of the three latest codes.
- `/gift submit code:<code>` sends in a code the bot has not found yet (see Gift Code Flow).
- `/giftadmin panel` (managers) posts the permanent panel with **Add my ID**, **My accounts**,
  **Active codes** and **Submit a code** buttons; the buttons keep working after
  restarts. If the server has no gift-code channel yet, the panel's channel becomes it.

Gift-code redemption normally uses each Governor ID's kingdom cached in the database.
If Century Games rejects that player/kingdom pair after a transfer, the bot refreshes the
player through the configured KingShot Data API and retries once with the new kingdom.
It validates a code with one player before starting bounded bulk batches, so expired,
unknown, or globally exhausted codes do not produce hundreds of redundant requests.
Manual runs always report their outcome, including all-failure and early-abort results;
automatic announcements are posted only when at least one redemption succeeded.
Century Games advertises a 30-request-per-minute limit, so requests are globally paced
with at least two seconds between starts. Each request is attempted once by the API
client; the only retry layer is `GiftRedemptionService.redeem_with_retries`. `/giftadmin redeem`
immediately starts a single background job and acknowledges it privately. A normal
channel message shows a progress bar, processed players, outcome counts, an ETA and a
**Cancel** button (requester or managers), then becomes the final summary, independent
of Discord's interaction webhook lifetime; a separate short message pings the requester
when the job finishes. Failed and
skipped players can be inspected with the Details button for six hours (by the requester
and server managers). `/giftadmin redeem` is refused in a channel where the bot cannot post the
progress card. An already-claimed code
is reported as already claimed, not as a failed redemption.

Additional commands are provided by translation, event, player info, KVK, and database handlers.

- `/settings` is the server managers' settings panel: the server's kingdom, gift-code
  announcement channel, event calendar channel, bot manager role (only members with
  Manage Server can change it) and AI replies. The menus show the saved values, and the
  panel says what is still missing. It warns when the bot cannot post in a chosen
  channel. Managers are also offered a one-click **use this channel** prompt where it
  matters: after `/gift add` and `/giftadmin import` (gift-code channel) and when opening
  `/calendar` (calendar channel; today's calendar is posted at once).
  When the bot joins a server it posts a short hint to run `/giftadmin panel` and `/settings`.
- `/player` shows a player's profile with a hero card (portraits, stars, levels) and a
  governor gear strip (equipment with its charms; "Equipment hidden" when the player
  hides it). Lookups use the KingShot Data API; when it is unavailable the bot says so
  instead of reporting the player as not found.
- `/scout` shows a ranked Mystic Trial leaderboard summary for a kingdom. Selecting a
  player loads their profile in the same public message; **Previous / Next** page
  through players. It defaults to 5 players and caps the limit at 15.
- `/kvk prep kingdom_a kingdom_b` (also the **Prep dossier** button on `/kvk compare`)
  posts a dossier for both kingdoms: castle holder and king history, top alliances,
  top players by power and kills, and KvK records. Sections that fail show as
  unavailable; results are cached for 10 minutes.
- `/status` returns a private health summary for Discord, the database, KingShot Data
  API, and background workers, including reminder delivery stats and the most used
  commands of the last 30 days (this server; all servers for bot operators). Deployment
  details (model names, server count) and scheduler error text are shown to bot
  operators only. Usage is stored as one counter per command, server and UTC day
  (`command_usage_daily`, kept 180 days), without user data.

After an update, apply the database migrations before starting the bot. For Docker:

```bash
docker compose build translator-bot-local
docker compose run --rm --no-deps translator-bot-local alembic upgrade head
docker compose up -d translator-bot-local
```

### Interactive workflows

- Commands are grouped: `/gift codes|add|remove|accounts|submit|roster` for everyone,
  `/giftadmin redeem|import|panel` for server managers (hidden from members, because
  Discord hides whole commands, not subcommands), `/kvk history|compare|prep`, `/player`,
  `/scout`, `/calendar`, `/reminders`, `/remind`, `/settings`, `/status`, `/help`.
- `/kvk history` shows the castle and prep records, the streak and the last ten seasons as
  a card; `/kvk compare` puts two kingdoms side by side, one row per metric with the better
  side highlighted; `/kvk prep` draws top alliances, top power and top kills of both
  kingdoms in two columns on one scale; `/scout` draws the Mystic Trial leaderboard.
  Player and alliance names are drawn in Latin, Cyrillic, Greek, Chinese, Japanese,
  Korean and Thai; characters outside those (emoji, Arabic and other scripts that need
  shaping) are left out, and a name with none left is labeled as unsupported.
- `/help` privately groups the features available on the current bot. Buttons open
  the existing workflows; actions requiring input open a form. Manager actions appear
  only for server managers. Player and KVK reports are posted in the channel.
  **Import alliance** explains the native `/giftadmin import` workflow: enter `kid`, then
  select `alliance` from autocomplete so kingdom and alliance suggestions remain available.
- `/calendar [kingdom]` posts the kingdom's event calendar (data: kingshot.net) as
  picture cards, with **Today**, **This week** and **Next week** tabs and a category
  filter. The bot needs **Attach Files** in the calendar channel for the daily pictures
  (without it the post is text). Without a
  kingdom it uses the server's kingdom. Anyone who presses a control on someone
  else's panel gets a private copy. In `/settings`, pick an **event calendar channel**
  and set the server's **kingdom** to have the Today view posted every day at 08:00 UTC; the
  first one is posted right away, and the buttons under each post keep working after
  restarts. See [event calendar](docs/event-calendar.md).
- `/kvk history` paginates every match returned by the service. **Compare kingdom** opens a
  form with the current kingdom already selected. Comparison results also paginate
  their direct match history.
- `/gift roster` supports name/ID search, enabled/disabled filters, **View player**,
  **Enable/Disable**, and (managers) **Roster health**.
  `/gift codes` lists active codes as copyable lines with a note on redeeming them on the
  website; server managers also get **Redeem selected code**.
- `/remind` refuses a channel where the bot cannot post, and the preview warns when
  the bot lacks **Mention @everyone** there (the reminder would be posted without a ping).
- `/remind` without arguments opens a form. Enter the event date/time, an IANA
  timezone such as `Europe/Kyiv`, the message, reminder lead time, and optional repeat
  interval. Slash-command arguments remain available, with `time_zone` defaulting to
  `UTC`. Both routes show a private preview; nothing is saved until **Save reminder**.
- `/reminders` provides **Edit reminder**, **Cancel event** (with confirmation), and
  **Create event** controls for server managers, and shows each reminder's pings, last
  delivery and last problem. Each card shows the reminder's stable ID. Editing changes the
  stored reminder time directly. Recurring reminders keep the same UTC time; local time may change with
  daylight saving. Ambiguous or nonexistent local times must be corrected before saving.

Reminder delivery: the scheduler runs every minute and survives errors (a failing
channel or a database hiccup no longer stops all reminders). A reminder is consumed only
after its send is attempted; temporary Discord errors are retried for up to 30 minutes,
missing channels or permissions are recorded and skipped. After downtime, one-time
reminders more than 30 minutes overdue are marked missed instead of firing late, and
recurring ones skip to their next occurrence. Reminders with stored roles ping those
roles (the roles must be mentionable or the bot needs "Mention all roles"); others
ping `@everyone`.

Interactive panels belong to the person who opened them. Public browse panels stay
active for 15 minutes; expired controls are disabled; rerun the command to open a fresh panel. Missing data is labeled unavailable,
and status messages use both text and color.

## Project Layout

```text
config/      Runtime configuration and logging setup
db/          SQLAlchemy models and session management
handlers/    Discord command/event handlers
services/    Service layer and API integrations
alembic/     Database migrations
images/      Game artwork for rendered cards (heroes, gear, event icons)
assets/      Fonts for rendered cards
main.py      Application entrypoint
```

## Notes

- OCR functionality has been removed from this project.
- Keep AUTO_REDEEM_CHANNELS empty if you do not want the global announcement; servers
  choose their own gift-code channel in `/settings`.
- Stored translation text (`translation_logs`) is purged daily after
  `TRANSLATION_LOG_RETENTION_DAYS` (default 30).
- `/settings` can turn contextual AI replies on or off for a server, including direct mentions/replies and random chat replies.
- Banned Discord users (`BANNED_PLAYERS`) are ignored by slash commands, translation and AI replies.

Contextual replies use the OpenAI SDK against the NIM endpoint, streaming
`NVIDIA_CHAT_MODEL` (default `nvidia/nemotron-3.5-super-vl-120b-a12b`) with thinking
enabled via `chat_template_kwargs`, temperature `1`, top-p `0.95`, seed `42` and a
4,096-token ceiling. Internal reasoning is discarded; only final answer content is
posted to Discord. Direct mentions may use read-only, server-scoped tools through
`NVIDIA_MODEL` (parallel tool calls enabled); if the API rejects tool calls the bot falls
back to a plain reply. Direct mentions and replies retry with thinking disabled if
generation fails or returns no usable answer. Temporary provider errors (timeouts,
connection errors, 429, 5xx) allow up to four attempts with 2-, 4- and 8-second backoff;
empty or malformed answers allow two attempts; permanent HTTP errors are not retried.
Each stream is limited to 30 seconds and the complete reply to 60 seconds. If all
attempts fail, the bot reports a generation failure instead of asking for more context.
Stream summaries log stop reasons and token usage for diagnosis. See
[contextual chat recovery](docs/contextual-chat.md) for details. Random replies first
pass a cheap yes/no check on `NVIDIA_MODEL` and get a single attempt. AI replies have a
per-user cooldown (`CHAT_USER_COOLDOWN_SECONDS`, default 10 s) and at most two
concurrent model requests. `NVIDIA_MODEL` controls translation and KingShot RAG requests
and defaults to the same Nemotron 3.5 Super VL model; these short requests (and the
random-reply check) disable thinking to reserve their completion budget for the answer.
The chat persona favors concise, context-specific dry humor and playful sarcasm while
avoiding hostile teasing, sensitive topics, and generic bot-like filler.
Discord user, role, and channel mentions are resolved to readable names before the current
message, reply target, and recent conversation history are sent to the chat model.

## License

MIT
