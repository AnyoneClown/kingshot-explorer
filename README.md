# DS Translator Bot

A Discord bot focused on translation, scheduling, player lookup, KVK tracking, and gift code automation.

## Features

- Slash commands with interactive Discord panels and a private `/help` guide.
- Multi-server ready: per-server settings in `/configure` (gift-code and alliance report
  channels, bot manager role) and per-server gift-code rosters.
- Auto-translation and manual translation features that keep mentions, emoji, links and
  line breaks intact, skip text that needs no translation, and cache repeated messages.
- NVIDIA NIM powered translation plus contextual chat replies. Mentioning the bot can
  answer questions from this server's data (alliance power, reminders, active gift codes,
  a Governor ID lookup, the Kingshot knowledge base) through read-only tools.
- Event scheduling with a self-healing background scheduler, role pings and delivery tracking.
- Player info lookups with a hero and governor gear/charm card, plus KVK history,
  comparisons and a KvK prep dossier.
- Gift code polling and auto-redemption for registered players.
- Searchable player lists with enabled/disabled filters and direct profile lookup.
- Single-message scout reports and paginated KVK history with comparison actions.
- Daily alliance and member power snapshots with daily and weekly trend reports, roster
  changes (joined / left / renamed), a "possibly inactive" view and an optional daily report post.
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
- Enabling/disabling roster entries from `/listplayers` (owners for their own entries,
  managers for all), plus a manager **Roster health** report: how many of the server's
  players recent codes reached, and players whose redemptions keep failing permanently,
  with a one-click **Disable these**.
- Polling the upstream gift code source every minute (with back-off on network errors).
- Auto-redeeming new codes for every enabled profile that is enabled in at least one
  server roster. Profiles created before per-server rosters ("unassigned legacy") keep
  being redeemed until a bot operator assigns them to a server (**Assign unassigned
  here** in `/listplayers`). Removing a server's last roster entry deletes the profile.
- Resumable runs. New codes are stored as `pending`; each player's result is saved as
  soon as it is known, and every poll resumes pending, unexpired codes, skipping players
  already handled. A restart or deploy mid-run therefore continues where it stopped.
  Auto runs skip players with final failures for the code (invalid ID, kingdom mismatch,
  level or spend requirement); manual runs skip only players who already have it.
  Temporary failures (outage, rate limit, 5xx) are not final: an auto pass whose probe
  fails temporarily stops at once, and a pass that ends with temporary failures keeps
  the code pending and retries just those players after 15 minutes (up to 6 passes, or
  until the code expires). `/redeem` is refused while the bot is shutting down.
- One job per code: automatic and manual runs share a guard, so a code is never
  redeemed twice concurrently; each server can run one manual job at a time.
- Posting summary embeds when at least one auto-redemption succeeds: each server's
  summary (its own players only) goes to the gift-code channel set in `/configure`, and
  the legacy `AUTO_REDEEM_CHANNELS` receive the global summary.

The redemption summary separates outcomes by category (Success, Already redeemed, API
rejected, Invalid ID) using one label map everywhere.

## Requirements

- Python 3.11 through 3.13
- A Discord bot token
- CockroachDB (or compatible PostgreSQL setup used by current models/migrations)

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
NVIDIA_MODEL=openai/gpt-oss-120b
NVIDIA_CHAT_MODEL=nvidia/nemotron-3-ultra-550b-a55b
ENABLE_VOICE_REPLIES=false
NVIDIA_TTS_SERVER=grpc.nvcf.nvidia.com:443
NVIDIA_TTS_FUNCTION_ID=877104f7-e885-42b9-8de8-f6e4c6303969
NVIDIA_TTS_DEFAULT_VOICE=Magpie-Multilingual.EN-US.Aria
NVIDIA_TTS_AUDIO_ENCODING=LINEAR_PCM
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
  actions that affect every server: the shared Kingshot knowledge base
  (`/kingshot_upsert`), unassigned legacy players and the all-servers redemption job.
- **Server managers** manage the bot inside one server: members with **Manage Server**
  or **Administrator**, members with the bot manager role chosen in `/configure`, and
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

Docker uses one Compose file with three services:

- `migrate` is a one-off service that runs `alembic upgrade head` with `.env.local`; the
  bot starts only after it succeeds.
- `translator-bot-local` uses `.env.local` and runs the complete bot. It shuts down
  gracefully on `docker stop` (SIGTERM, 30 s grace period): background jobs stop,
  gift-code progress is saved and connections close.
- `grafana-alloy` uses `.env.grafana` to forward the bot container logs to Grafana Cloud Logs.

Create the real env files from the committed examples:

```bash
cp .env.local.example .env.local
cp .env.grafana.example .env.grafana
```

Start the bot and Grafana Alloy:

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

## Grafana Cloud Logs

Grafana Cloud Free includes Loki logs with limited usage and 14-day retention. This project uses Grafana Alloy to read the bot's Docker logs and send them to Grafana Cloud Logs.

Create the Grafana env file:

```bash
cp .env.grafana.example .env.grafana
```

Fill these values in `.env.grafana` from Grafana Cloud:

- `GRAFANA_CLOUD_LOKI_URL`: Loki push URL, usually ending with `/loki/api/v1/push`.
- `GRAFANA_CLOUD_LOKI_USERNAME`: Loki username or instance ID.
- `GRAFANA_CLOUD_API_KEY`: access policy token with `logs:write`.

Start the bot with Grafana log forwarding:

```bash
docker compose -f compose.yaml up -d --build
```

Start only the log collector after bots are already running:

```bash
docker compose -f compose.yaml up -d grafana-alloy
```

Check Alloy locally:

```bash
docker compose -f compose.yaml logs -f grafana-alloy
open http://localhost:12345
```

Useful Grafana Explore LogQL queries:

```logql
{app="ds-translator"}
{app="ds-translator", bot_instance="local", level="E"}
{app="ds-translator"} |= "Failed"
```

The dashboard includes:

- Errors and warnings for the selected time range.
- Log volume by bot instance and level.
- Top error/warning logger names.
- Filterable live logs with bot-instance and level variables.

## Commands Overview

The bot registers slash commands through handler modules in handlers/.

Gift code related commands include:

- `/redeem gift_code:<code>` (managers) redeems for this server's enabled roster; bot
  operators can add `all_servers:true` to run the global job.
- `/addplayer player_ids:<ids>` adds Governor IDs to this server's roster (private reply).
- `/addalliance kid:<id> alliance:<choice>` (managers; top 15 by power with names from
  MightPulse's public kingdom view; AIDs remain the choice values and appear as fallback
  labels when names are unavailable). Governor IDs come from the roster, the local UID
  cache, the optional key-protected MightPulse roster (`MIGHTPULSE_API_KEY`) or player profiles.
- `/removeplayer player_id:<id>` removes an entry from this server's roster after confirmation.
- `/listplayers` (alias `/playerlist`) browses this server's roster.
- `/giftcodes` lists active codes as copyable lines.

Gift-code redemption normally uses each Governor ID's kingdom cached in the database.
If Century Games rejects that player/kingdom pair after a transfer, the bot refreshes the
player through the configured KingShot Data API and retries once with the new kingdom.
It validates a code with one player before starting bounded bulk batches, so expired,
unknown, or globally exhausted codes do not produce hundreds of redundant requests.
Manual runs always report their outcome, including all-failure and early-abort results;
automatic announcements are posted only when at least one redemption succeeded.
Century Games advertises a 30-request-per-minute limit, so requests are globally paced
with at least two seconds between starts. Each request is attempted once by the API
client; the only retry layer is `GiftRedemptionService.redeem_with_retries`. `/redeem`
immediately starts a single background job and acknowledges it privately. A normal
channel message shows a progress bar, processed players, outcome counts, an ETA and a
**Cancel** button (requester or managers), then becomes the final summary, independent
of Discord's interaction webhook lifetime; a separate short message pings the requester
when the job finishes. Failed and
skipped players can be inspected with the Details button. An already-claimed code
is reported as already claimed, not as a failed redemption.

Additional commands are provided by translation, event, player info, KVK, and database handlers.

- `/configure` is the server managers' settings panel: gift-code announcement channel,
  alliance report channel, bot manager role (only members with Manage Server can change
  it), voice replies and AI replies. It warns when the bot cannot post in a chosen channel.
  When the bot joins a server it posts a short hint to run `/configure`.
- `/stats` shows a player's profile with a hero card (portraits, stars, levels) and a
  governor gear strip (equipment with its charms; "Equipment hidden" when the player
  hides it). Lookups use the KingShot Data API; when it is unavailable the bot says so
  instead of reporting the player as not found.
- `/scout` shows a ranked Mystic Trial leaderboard summary for a kingdom. Selecting a
  player loads their profile in the same public message; **Previous / Next** page
  through players. It defaults to 5 players and caps the limit at 15.
- `/kvk_prep kingdom_a kingdom_b` (also the **Prep dossier** button on `/kvk_compare`)
  posts a dossier for both kingdoms: castle holder and king history, top alliances,
  top players by power and kills, and KvK records. Sections that fail show as
  unavailable; results are cached for 10 minutes.
- `/status` returns a private health summary for Discord, the database, KingShot Data
  API, and background workers, including reminder delivery stats. Deployment details
  (model names, server count) are shown to bot operators only.
- `/alliance` shows this server's tracked alliance power trends. A server manager
  starts tracking with `/alliance kid:830 alliance:<selection>` using native autocomplete
  or an exact 3-character tag. Each server tracks one alliance; selecting another replaces
  that server's selection. **Members** opens paginated individual power changes,
  **Changes** lists who joined, left or was renamed between the two latest complete
  snapshots, and **Inactive** (managers) lists members whose power has not changed for
  7 or 14 days — only a hint, since maxed players also stop growing. **Stop tracking**
  asks for confirmation, stops collection for this server and preserves saved history.
  After each new daily snapshot, a compact report is posted to the alliance report
  channel set in `/configure`.

Power tracking saves the first complete snapshot each UTC day and checks hourly for
missing snapshots, including after restarts. Captures use the Data API's `/full`
alliance roster (falling back to one profile call per member) and also store each
member's Town Center level, VIP, life tree level and alliance rank. It sums the current roster's member power;
roster changes therefore affect the total. Daily and weekly changes compare exact UTC
dates (one and seven days earlier), not rolling 24-hour periods. Missing baselines show
as new history, and failed or incomplete captures preserve the last complete snapshot.
There is no historical backfill: trends build from the first capture. Snapshots are
shared when multiple servers track the same alliance, and only alliances tracked by
servers the bot has joined are collected. Viewing a report does not register players
for gift redemption.

Before running this feature, apply the database migration. For Docker:

```bash
docker compose build translator-bot-local
docker compose run --rm --no-deps translator-bot-local alembic upgrade head
docker compose up -d translator-bot-local
```

### Interactive workflows

- `/help` privately groups the features available on the current bot. Buttons open
  the existing workflows; actions requiring input open a form. Manager actions appear
  only for server managers. Player and KVK reports are posted in the channel.
  **Import alliance** explains the native `/addalliance` workflow: enter `kid`, then
  select `alliance` from autocomplete so kingdom and alliance suggestions remain available.
- `/kvk` paginates every match returned by the service. **Compare kingdom** opens a
  form with the current kingdom already selected. Comparison results also paginate
  their direct match history.
- `/listplayers` supports name/ID search, enabled/disabled filters, **View player**,
  **Enable/Disable**, and (managers) **Roster health**.
  `/giftcodes` lists active codes as copyable lines with a note on redeeming them on the
  website; server managers also get **Redeem selected code**.
- `/schedule` without arguments opens a form. Enter the event date/time, an IANA
  timezone such as `Europe/Kyiv`, the message, reminder lead time, and optional repeat
  interval. Slash-command arguments remain available, with `time_zone` defaulting to
  `UTC`. Both routes show a private preview; nothing is saved until **Save reminder**.
- `/events` provides **Edit reminder**, **Cancel event** (with confirmation), and
  **Create event** controls for server managers, and shows each reminder's pings, last
  delivery and last problem. `/cancel event_id:123` uses the stable ID shown on the event
  card (with autocomplete), rather than its position in the list. Editing changes the stored reminder time
  directly. Recurring reminders keep the same UTC time; local time may change with
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
main.py      Application entrypoint
```

## Notes

- OCR functionality has been removed from this project.
- Keep AUTO_REDEEM_CHANNELS empty if you do not want the global announcement; servers
  choose their own gift-code channel in `/configure`.
- Stored translation text (`translation_logs`) is purged daily after
  `TRANSLATION_LOG_RETENTION_DAYS` (default 30).
- `/configure` can turn contextual AI replies on or off for a server, including direct mentions/replies and random chat replies.
- Banned Discord users (`BANNED_PLAYERS`) are ignored by slash commands, translation and AI replies.
- Voice replies are uploaded as audio attachments. Discord bots cannot send native mobile-only voice messages.

Contextual replies use the OpenAI SDK against the NIM endpoint, streaming
`NVIDIA_CHAT_MODEL` (default `nvidia/nemotron-3-ultra-550b-a55b`) with thinking enabled
via `chat_template_kwargs`, temperature `1`, top-p `0.95` and a 4,096-token ceiling.
Internal reasoning is discarded; only final answer content is posted to Discord.
Direct mentions may use read-only, server-scoped tools through `NVIDIA_MODEL`; if the
API rejects tool calls the bot falls back to a plain reply. Direct mentions and replies
get one retry with thinking disabled if generation fails or returns no usable answer;
if both attempts fail, the bot reports a generation failure instead of asking for more
context. Stream summaries log stop reasons and token usage for diagnosis. See
[contextual chat recovery](docs/contextual-chat.md) for details. Random replies first
pass a cheap yes/no check on `NVIDIA_MODEL` and get a single attempt. AI replies have a
per-user cooldown (`CHAT_USER_COOLDOWN_SECONDS`, default 10 s) and at most two
concurrent model requests. `NVIDIA_MODEL` remains the model for translation and
KingShot RAG requests. Voice audio is attached a moment after the text reply.
The chat persona favors concise, context-specific dry humor and playful sarcasm while
avoiding hostile teasing, sensitive topics, and generic bot-like filler.
Discord user, role, and channel mentions are resolved to readable names before the current
message, reply target, and recent conversation history are sent to the chat model.

## License

MIT
