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

- Python 3.11+
- A Discord bot token
- CockroachDB (or compatible PostgreSQL setup used by current models/migrations)

## Setup

1. Install dependencies.

```bash
pip install -r requirements.txt
```

2. Create a .env file.

```env
DISCORD_TOKEN=your_discord_bot_token_here
ADMIN_USER_ID=your_discord_user_id
COCKROACHDB_URL=cockroachdb+asyncpg://postgres:password@host:26257/database-name
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

3. Run migrations.

```bash
python -m alembic upgrade head
```

4. Start the bot.

```bash
python main.py
```

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
