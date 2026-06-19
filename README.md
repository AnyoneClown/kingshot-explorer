# DS Translator Bot

A Discord bot focused on translation, scheduling, player lookup, KVK tracking, and gift code automation.

## Features

- Slash command based bot architecture.
- Auto-translation and manual translation features.
- NVIDIA NIM powered translation plus contextual chat replies.
- Event scheduling with background task execution.
- Player info lookups and KVK command support.
- Gift code polling and auto-redemption for registered players.
- Paginated player list output for large registrations.
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
- Posting summary embeds to optional announcement channels.

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
BOT_PROFILE=local
NVIDIA_API_KEY=your_nvidia_api_key_here
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=openai/gpt-oss-120b
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

`BOT_PROFILE` controls which bot surface starts from the same codebase:

- `local` starts every current feature: translation, chat replies, events, gift code automation, admin/config/database commands, player stats, scout, and KVK. This profile requires `NVIDIA_API_KEY`.
- `global` starts only the public command set: `/stats`, `/scout`, `/kvk`, and `/kvk_compare`. It does not start event scheduling, gift-code polling, RAG, translation, voice replies, admin/config, or database command handlers, and does not require `NVIDIA_API_KEY`.

When running without Docker, start the local and global bots as two separate processes with different `DISCORD_TOKEN` and `BOT_PROFILE` environment values.

3. Run migrations.

```bash
uv run alembic upgrade head
```

4. Start the bot.

```bash
uv run python main.py
```

## Docker

Docker is split into two Compose files:

- `compose.local.yaml` runs `translator-bot-local` with `.env.local` and `BOT_PROFILE=local`.
- `compose.prod.yaml` runs `translator-bot-global` with `.env.prod` and `BOT_PROFILE=global`.
- `compose.grafana.yaml` runs Grafana Alloy to forward bot container logs to Grafana Cloud Logs.

Create the real env files from the committed examples:

```bash
cp .env.local.example .env.local
cp .env.prod.example .env.prod
```

Start local/full bot:

```bash
docker compose -f compose.local.yaml up -d --build
```

Start prod/global bot:

```bash
docker compose -f compose.prod.yaml up -d --build
```

Start both bots together:

```bash
docker compose -f compose.local.yaml -f compose.prod.yaml up -d --build
```

Common commands:

```bash
docker compose -f compose.local.yaml logs -f
docker compose -f compose.prod.yaml logs -f
docker compose -f compose.local.yaml -f compose.prod.yaml logs -f
docker compose -f compose.prod.yaml restart
docker compose -f compose.local.yaml stop
docker compose -f compose.local.yaml -f compose.prod.yaml stop
```

Log output includes the bot instance name, for example `local` or `global`. File logs are separated by instance in `logs/`:

```bash
tail -f logs/local_$(date +%Y%m%d).log
tail -f logs/global_$(date +%Y%m%d).log
```

Use two different Discord applications for the two tokens.

## Grafana Cloud Logs

Grafana Cloud Free includes Loki logs with limited usage and 14-day retention. This project uses Grafana Alloy to read Docker logs for the local/global bot containers and send them to Grafana Cloud Logs.

Create the Grafana env file:

```bash
cp .env.grafana.example .env.grafana
```

Fill these values in `.env.grafana` from Grafana Cloud:

- `GRAFANA_CLOUD_LOKI_URL`: Loki push URL, usually ending with `/loki/api/v1/push`.
- `GRAFANA_CLOUD_LOKI_USERNAME`: Loki username or instance ID.
- `GRAFANA_CLOUD_API_KEY`: access policy token with `logs:write`.

Start both bots with Grafana log forwarding:

```bash
docker compose -f compose.local.yaml -f compose.prod.yaml -f compose.grafana.yaml up -d --build
```

Start only the log collector after bots are already running:

```bash
docker compose -f compose.grafana.yaml up -d
```

Check Alloy locally:

```bash
docker compose -f compose.grafana.yaml logs -f
open http://localhost:12345
```

Useful Grafana Explore LogQL queries:

```logql
{app="ds-translator"}
{app="ds-translator", bot_instance="global"}
{app="ds-translator", bot_instance="local", level="E"}
{app="ds-translator"} |= "Failed"
```

Import the ready-made dashboards:

1. Open Grafana Cloud.
2. Go to Dashboards.
3. Click New -> Import.
4. Upload one of these JSON files:
   - `grafana/dashboards/ds-translator-local-logs.json` for the local/full bot.
   - `grafana/dashboards/ds-translator-prod-logs.json` for the prod/global bot.
   - `grafana/dashboards/ds-translator-logs.json` for one combined dashboard with a bot selector.
5. Select your Loki / Grafana Cloud Logs data source when Grafana asks for `DS_LOKI`.

The dashboard includes:

- Errors and warnings for the selected time range.
- Log volume by bot instance and level.
- Top error/warning logger names.
- Filterable live logs with `local` / `global` and level variables.

## Commands Overview

The bot registers slash commands through handler modules in handlers/.

Gift code related commands include:

- /redeem
- /addplayer
- /addalliance
- /removeplayer
- /listplayers
- /playerlist (alias)
- /giftcodes

Additional commands are provided by translation, event, player info, KVK, and database handlers.

- `/configure` is an ephemeral guild configuration panel available only to bot admins listed in `ADMIN_USER_ID` or `ADMIN_USER_IDS`. It can toggle voice replies and random AI chat replies.
- `/scout` fetches players from KingShot leaderboard type 8 for a kingdom and returns stats-style embeds enriched by Governor ID. It defaults to 5 players and caps the limit at 15.
- In `BOT_PROFILE=global`, only `/stats`, `/scout`, `/kvk`, and `/kvk_compare` are registered.

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

## License

MIT
