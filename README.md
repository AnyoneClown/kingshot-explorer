# DS Translator Bot

A Discord bot focused on translation, scheduling, player lookup, KVK tracking, and gift code automation.

## Features

- A small command surface: one command per job, picture cards instead of button
  browsers, and a private `/help` whose commands are clickable.
- A gift-code panel with buttons: members add their Governor ID, manage their accounts,
  copy active codes and send in codes the bot has not found yet, without knowing any
  command. `/gift` opens it privately; `/giftadmin panel` posts it permanently in a channel.
- Multi-server ready: per-server settings in `/settings` (the server's kingdom, the
  gift-code channel, daily event calendar, timezone, bot manager role) and one gift-code list
  shared by all servers, shown to each server as the roster of its kingdom.
- Picture cards instead of walls of text: the event calendar, gift-code results, KvK
  history, KvK comparison, the prep dossier's rankings, the scout report, the
  `/leaderboard` rankings and the
  reminders dashboard are drawn
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
- Searchable kingdom rosters for managers, with enabled/disabled filters.
- Scout reports and KvK history as single picture cards, without controls.
- Accounts added later get the codes that are still active.
- Reminders in the server's timezone, with save previews and edit/cancel controls.
- Unified player profile storage in a single players table.
- Clear redemption result categories:
    - Success
    - Already redeemed
    - API rejected
    - Invalid ID

## Gift Code Flow

Gift code support includes:

- One gift-code list for all servers (`registered_players`). An account is listed once,
  with one enabled flag and the Discord user who added it, and a code is redeemed for
  every enabled account whichever server it was added from. Members can enroll up to
  `MAX_SELF_REGISTERED_PLAYERS` (3) accounts of their own; server managers can add any
  number and import alliances.
- Members manage their own accounts in one place: **My accounts** on the panel lists
  them with the result of the three latest codes and has **Add account**,
  **Pause** / **Resume** and **Remove** (with confirmation).
- Kingdom rosters for managers. `/giftadmin roster` shows the accounts of the kingdom
  chosen in the server's `/settings`, whoever added them and from whichever server; two
  servers of the same kingdom see the same roster. A server without a kingdom is asked to
  set one. Bot operators can open any kingdom with `/giftadmin roster kingdom:<n>`.
- Who may remove or pause an account: the user who added it, the managers of a
  server set to the account's kingdom, and bot operators. Managers also get a **Roster
  health** report for their kingdom: how many of its players recent codes reached, and
  players whose redemptions keep failing permanently, with a one-click **Disable these**.
- Polling the upstream gift code source every minute (with back-off on network errors).
  Discovery runs independently of redemption, so a new code is recorded even while
  another code is being redeemed. Members can also send in a code with **Submit a code**
  on the panel: it is checked with up to three accounts (the member's own first) and, if
  it works, queued for everyone; one submission per member per minute. Managers do the
  same with `/giftadmin redeem`, without the rate limit.
- One kind of run and one kind of report. Every redemption goes through the automatic
  pipeline and ends as the summary card in the gift-code channel; there is no separate
  manual job.
- Active codes for accounts added later. When an account is added, imported or switched
  back on, the codes the source still lists are queued again. Such a re-run skips every
  account with a stored result, so it costs one request per new account and code. It is
  quiet: it updates the summaries that exist and posts nothing new, and when the code
  has stopped working meanwhile it leaves them alone. An account added while a code is
  running gets one more pass when that run ends.
- Running a code again. When a manager enters a code the bot already finished
  (`/giftadmin redeem`, or **Submit a code**), it is redeemed for the enabled accounts
  that do not have it, including those whose earlier failure looked final; the
  manager's server gets a fresh summary in its gift-code channel. Members who enter a
  known code are told the bot already has it.
- The summary posted after an automatic run carries a result card (picture): share of
  accounts that have the reward, counts of received / already had it / rejected / invalid
  ID, and the same split per kingdom (the five largest; the rest folded into one row).
  The code itself stays as copyable text. Where the bot lacks **Attach Files**, or the
  card cannot be drawn, the counts are posted as text instead. Cards are drawn by
  `services/gift_card_renderer.py` with the same drawing code as the calendar cards
  (`services/card_drawing.py`).
- Members' own accounts go first: a run redeems self-registered accounts (owners with
  at most `MAX_SELF_REGISTERED_PLAYERS` accounts on the list), then the accounts added
  in bulk. The gift-code server allows about 30 requests a minute, so 1,000 accounts
  take roughly 40 minutes.
- Auto-redeeming new codes for every enabled account on the list. Removing an account
  takes it off the list for every server.
- Resumable runs. New codes are stored as `pending`; each player's result is saved as
  soon as it is known, and every poll resumes pending, unexpired codes, skipping players
  already handled. A restart or deploy mid-run therefore continues where it stopped.
  Auto runs skip players with final failures for the code (invalid ID, kingdom mismatch,
  level or spend requirement); a manager's re-run skips only players who already have it.
  Temporary failures (outage, rate limit, HTTP 5xx, an unreachable player lookup) are
  not final: an auto pass whose probe fails temporarily stops at once, a pass that meets
  six temporary failures in a row ends early, and a pass that ends with temporary
  failures keeps the code pending and retries just those players after 15 minutes (up
  to 6 passes, or until the code expires). A pass that could not reach everyone does not
  count toward those 6: the code stays pending until it expires, or for 72 hours when
  it has no expiry date. Entering a code is refused while the bot is shutting down.
- Accounts that keep failing are paused. After a completed run, an account whose
  Governor ID or kingdom was rejected on at least 3 of the last 5 codes (including the
  current one) and that none of them reached is disabled. The gift-code channels of
  the servers set to its kingdom and the bot operators (by DM) get the list; a manager
  of that kingdom re-enables the account in `/giftadmin roster`, or the owner presses
  **Resume** under **My accounts**, after fixing the ID.
- One job per code: runs and the check of an entered code share a guard, so a code is
  never redeemed twice concurrently.
- Posting summary embeds when at least one auto-redemption succeeds: each server's
  summary (the players of its kingdom; every player when the server has no kingdom)
  goes to the gift-code channel set in `/settings`, and
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
ignores), `MAX_SELF_REGISTERED_PLAYERS` (3),
`LLM_TIMEOUT_SECONDS` (30), `CHAT_USER_COOLDOWN_SECONDS` (10),
`KINGSHOT_RAG_MAX_DISTANCE` (0.7) and `TRANSLATION_LOG_RETENTION_DAYS` (30; `0` keeps
stored translation text forever). See `.env.local.example`.

## Permissions

The bot uses three access levels, enforced by `handlers/permissions.py`:

- **Bot operators** (`ADMIN_USER_ID` / `ADMIN_USER_IDS`) can do everything, including
  managing the gift-code roster of any kingdom.
- **Server managers** manage the bot inside one server: members with **Manage Server**
  or **Administrator**, members with the bot manager role chosen in `/settings`, and
  bot operators. They configure the server, create/edit/cancel reminders, start/stop
  alliance tracking, import alliances, run redemptions (for everyone on the shared
  list) and manage the gift-code accounts of the server's kingdom.
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

Gift code related commands:

- `/gift` (everyone) opens the gift-code panel privately: **Add my ID**, **My accounts**,
  **Active codes** and **Submit a code**. There are no subcommands.
- `/giftadmin panel` (managers) posts the same panel permanently in a channel; its buttons
  keep working after restarts. If the server has no gift-code channel yet, the panel's
  channel becomes it.
- `/giftadmin redeem gift_code:<code>` (managers) hands the bot a code: a new one is
  checked and queued for every enabled account, a finished one is run again for the
  accounts without it (see Gift Code Flow).
- `/giftadmin import kid:<id> alliance:<choice>` (managers; top 15 by power with names from
  MightPulse's public kingdom view; AIDs remain the choice values and appear as fallback
  labels when names are unavailable). Governor IDs come from the roster, the local UID
  cache, the optional key-protected MightPulse roster (`MIGHTPULSE_API_KEY`) or player profiles.
- `/giftadmin roster` (managers) browses the accounts of this server's kingdom (operators:
  `kingdom:<n>` for another kingdom): name/ID search, enabled/disabled filter,
  **Enable/Disable**, **Remove** and **Roster health**.

Gift-code redemption normally uses each Governor ID's kingdom cached in the database.
If Century Games rejects that player/kingdom pair after a transfer, the bot refreshes the
player through the configured KingShot Data API and retries once with the new kingdom.
It validates a code with one player before starting bounded bulk batches, so expired,
unknown, or globally exhausted codes do not produce hundreds of redundant requests.
Announcements are posted only when at least one redemption succeeded (a manager who runs
a code again always gets the summary).
Century Games advertises a 30-request-per-minute limit, so requests are globally paced
with at least two seconds between starts. Each request is attempted once by the API
client; the only retry layer is `GiftRedemptionService.redeem_with_retries`. An
already-claimed code is reported as already claimed, not as a failed redemption.

Additional commands are provided by translation, event, player info, KVK, and database handlers.

- `/settings` is the server managers' settings panel: the server's kingdom, gift-code
  announcement channel, event calendar channel, reminder timezone, bot manager role (only
  members with Manage Server can change it) and AI replies. While setup is incomplete it
  opens as a three-step checklist (kingdom, gift-code channel and sign-up panel, daily
  calendar channel) that marks what is done and says what to press next; **Post sign-up
  panel** posts the gift-code panel in the chosen channel. The menus show the saved
  values, and the panel warns when the bot cannot post in a chosen channel.
  When the bot joins a server it posts a short hint to start with `/settings`.
- `/player` shows a player's profile with a hero card (portraits, stars, levels) and a
  governor gear strip (equipment with its charms; "Equipment hidden" when the player
  hides it). Lookups use the KingShot Data API; when it is unavailable the bot says so
  instead of reporting the player as not found.
- `/scout [kingdom]` posts one card with the top 5 Mystic Trial players of a kingdom:
  rank, alliance tag and name, Town Center level, power and trial stages. It has no
  controls; `/player` opens a profile and `/leaderboard` the full ranking. Town Center
  and power come from one profile request per player (run together, 8 seconds at most);
  a row whose profile is missing shows `–` there.
- `/leaderboard` shows the game's rankings as a picture card: Personal Power, Kill Count,
  Alliance Power and Kills, Town Center Level, hero, pet and master power, Island
  Prosperity, Rebel Conquest, Mystic Trial and the six trial towers. `board` picks the
  ranking (Personal Power by default), `kingdom` the kingdom (this server's kingdom
  when left empty), and `scope: All kingdoms` the cross-kingdom board. The panel holds
  the top 100 in pages of 15; a menu switches the board and a button switches between
  the kingdom and all kingdoms, in the same public message. Boards are kept for two
  minutes. Tower scores are shown without the game's tower offset and Town Center
  levels above 30 as Truegold levels (`TG8`). An alliance that cannot be named shows
  its ID.
- `/kvk [kingdom] [opponent]` is one command. With one kingdom it posts the history card
  (castle and prep records, the streak, the last ten seasons) and nothing else. With an
  opponent it posts the comparison card, the latest direct matchups and one button,
  **Prep dossier**, which posts a dossier for both kingdoms: castle holder and king
  history, top alliances, top players by power and kills, and KvK records. Sections that
  fail show as unavailable; results are cached for 10 minutes.
- `kingdom` is optional in `/scout`, `/kvk`, `/leaderboard` and `/calendar`: left empty,
  the server's kingdom from `/settings` is used.
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

- The whole surface: `/gift` for everyone, `/giftadmin redeem|import|roster|panel` for
  server managers (hidden from members, because Discord hides whole commands, not
  subcommands), `/kvk`, `/player`, `/scout`, `/leaderboard`, `/calendar`, `/reminders`,
  `/settings`, `/status`, `/help`.
- `/kvk` draws a kingdom's history as a card and a comparison as a card with one row per
  metric and the better side highlighted; the prep dossier draws top alliances, top
  power and top kills of both kingdoms in two columns on one scale; `/scout` draws its
  top-5 report and `/leaderboard` any ranking board, with bars on the scale of the
  board's first place.
  Player and alliance names are drawn in Latin, Cyrillic, Greek, Chinese, Japanese,
  Korean and Thai; characters outside those (emoji, Arabic and other scripts that need
  shaping) are left out, and a name with none left is labeled as unsupported.
- `/help` is one private screen without buttons: what the bot does, with each command as
  a clickable mention that opens the real command (plain text when Discord's command
  IDs are not known yet). Manager commands are listed for server managers only.
- `/calendar [kingdom]` posts the kingdom's event calendar (data: kingshot.net) as
  picture cards, with **Today**, **This week** and **Next week** tabs and a category
  filter. The bot needs **Attach Files** in the calendar channel for the daily pictures
  (without it the post is text). Without a
  kingdom it uses the server's kingdom. Anyone who presses a control on someone
  else's panel gets a private copy. In `/settings`, pick an **event calendar channel**
  and set the server's **kingdom** to have the Today view posted every day at 08:00 UTC; the
  first one is posted right away, and the buttons under each post keep working after
  restarts. See [event calendar](docs/event-calendar.md).
- `/reminders` is the one reminders command. It shows a dashboard card of the channel's
  reminders (the selected one with its countdown, a strip of the next seven days with a
  mark per reminder, and the list; redrawn when another reminder is selected) above the
  selected reminder's details: its pings, last delivery and last problem. Server managers
  get **Edit reminder**, **Cancel reminder** (with confirmation) and **Create reminder**;
  a channel without reminders still offers **Create reminder**.
- The reminder form asks for the date and time the reminder is posted, the message and
  an optional repeat interval. Times are entered in the server's timezone from
  `/settings` (UTC until one is set); the form and the preview name it. Nothing is saved
  until **Save reminder**. The form refuses a channel where the bot cannot post, and the
  preview warns when the bot lacks **Mention @everyone** there (the reminder would be
  posted without a ping). Recurring reminders keep the same UTC time; local time may
  change with daylight saving. Ambiguous or nonexistent local times must be corrected
  before saving.

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
