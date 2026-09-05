# DS Translator Bot

A Discord bot focused on translation, scheduling, player lookup, KVK tracking, and gift code automation.

## Features

- Slash commands with interactive Discord panels and a private `/help` guide.
- Auto-translation and manual translation features.
- NVIDIA NIM powered translation plus contextual chat replies.
- Event scheduling with background task execution.
- Player info lookups and KVK command support.
- Gift code polling and auto-redemption for registered players.
- Searchable player lists with enabled/disabled filters and direct profile lookup.
- Single-message scout reports and paginated KVK history with comparison actions.
- Daily alliance and member power snapshots with daily and weekly trend reports.
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

- Registering players for redemption.
- Toggling player enabled/disabled status.
- Polling upstream gift code source every 10 minutes.
- Auto-redeeming newly discovered codes for enabled players.
- Logging redemption attempts to the database.
- Posting summary embeds to optional announcement channels when at least one auto-redemption succeeds.

The redemption summary now separates outcomes by category instead of a single generic failure bucket.

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

Use `ADMIN_USER_IDS=123,456` instead of `ADMIN_USER_ID` if multiple Discord users should be allowed to run protected admin commands.

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

- `translator-bot-local` uses `.env.local` and runs the complete bot.
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

- /redeem
- /addplayer
- `/addalliance kid:<id> alliance:<tag>` (top 15 by power, shown as `[TAG] Name - N members`)
- /removeplayer
- /listplayers
- /playerlist (alias)
- /giftcodes

Gift-code redemption normally uses each Governor ID's kingdom cached in the database.
If Century Games rejects that player/kingdom pair after a transfer, the bot refreshes the
player through the configured KingShot Data API and retries once with the new kingdom.
It validates a code with one player before starting bounded bulk batches, so expired,
unknown, or globally exhausted codes do not produce hundreds of redundant requests.
Completion announcements include all-failure and early-abort outcomes as well as
successful runs. Century Games advertises a 30-request-per-minute limit, so requests are
globally paced with at least two seconds between starts. `/redeem` immediately starts a
single background job and acknowledges it privately. A normal channel message shows
processed players, outcome counts, and rate-limit waiting status, then becomes the
final summary, independent of Discord's interaction webhook lifetime. Failed and
skipped players can be inspected with the Details button. An already-claimed code
is reported as already claimed, not as a failed redemption.

Additional commands are provided by translation, event, player info, KVK, and database handlers.

- `/configure` is an ephemeral guild configuration panel available only to bot admins listed in `ADMIN_USER_ID` or `ADMIN_USER_IDS`. It can toggle voice replies and random AI chat replies.
- `/scout` shows a ranked Mystic Trial leaderboard summary for a kingdom. Select a player and choose **View player**, or use **Previous / Next**, to load their profile in the same message. It defaults to 5 players and caps the limit at 15.
- `/status` returns a private health summary for Discord, the database, KingShot Data API, and background workers.
- `/alliance` shows this server's tracked alliance power trends. A configured bot admin
  starts tracking with `/alliance kid:830 alliance:<selection>` using native autocomplete.
  Each server tracks one alliance; selecting another replaces that server's selection.
  **Members** opens paginated individual power changes. **Stop tracking** stops collection
  for this server and preserves saved history.

Power tracking saves the first complete snapshot each UTC day and checks hourly for
missing snapshots, including after restarts. It sums the current roster's member power;
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
  the existing workflows; actions requiring input open a form. Admin actions appear
  only for configured bot admins. Player and KVK reports are posted in the channel.
  **Import alliance** explains the native `/addalliance` workflow: enter `kid`, then
  select `alliance` from autocomplete so kingdom and alliance suggestions remain available.
- `/kvk` paginates every match returned by the service. **Compare kingdom** opens a
  form with the current kingdom already selected. Comparison results also paginate
  their direct match history.
- `/listplayers` supports name/ID search, enabled/disabled filters, and **View player**.
  `/giftcodes` paginates active codes and provides **Redeem selected code** to bot admins.
- `/schedule` without arguments opens a form. Enter the event date/time, an IANA
  timezone such as `Europe/Kyiv`, the message, reminder lead time, and optional repeat
  interval. Slash-command arguments remain available, with `time_zone` defaulting to
  `UTC`. Both routes show a private preview; nothing is saved until **Save reminder**.
- `/events` provides **Edit reminder**, **Cancel event**, and **Create event** controls
  for bot admins. `/cancel event_id:123` uses the stable ID shown on the event card,
  rather than its position in the list. Editing changes the stored reminder time
  directly. Recurring reminders keep the same UTC time; local time may change with
  daylight saving. Ambiguous or nonexistent local times must be corrected before saving.

Interactive panels belong to the person who opened them. Expired controls are
disabled; rerun the command to open a fresh panel. Missing data is labeled unavailable,
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
- Keep AUTO_REDEEM_CHANNELS empty if you do not want announcement messages.
- `/configure` can turn contextual AI replies on or off for a server, including direct mentions/replies and random chat replies.
- Voice replies are uploaded as audio attachments. Discord bots cannot send native mobile-only voice messages.

Contextual replies use `langchain-nvidia-ai-endpoints` with `ChatNVIDIA.astream()` and
the `NVIDIA_CHAT_MODEL`. The default is `nvidia/nemotron-3-ultra-550b-a55b` with thinking
enabled, temperature `1`, top-p `0.95`, and a 16,384-token completion ceiling. Internal
reasoning chunks are discarded; only final answer content is parsed and posted to Discord.
`NVIDIA_MODEL` remains the model setting for translation and KingShot RAG requests.
The chat persona favors concise, context-specific dry humor and playful sarcasm while
avoiding hostile teasing, sensitive topics, and generic bot-like filler.
Discord user, role, and channel mentions are resolved to readable names before the current
message, reply target, and recent conversation history are sent to the chat model.

## License

MIT
