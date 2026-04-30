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
COCKROACHDB_URL=cockroachdb+asyncpg://postgres:password@host:26257/database-name
NVIDIA_API_KEY=your_nvidia_api_key_here
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=openai/gpt-oss-120b
COMMAND_PREFIX=!
TRANSLATOR_ROLE=Translator
AUTO_REDEEM_CHANNELS=123456789012345678,876543210987654321
RANDOM_REPLY_CHANCE=0.08
RANDOM_REPLY_COOLDOWN_SECONDS=180
CHAT_HISTORY_LIMIT=12
MAX_CHAT_RESPONSE_CHARS=500
LOG_LEVEL=INFO
```

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
- /removeplayer
- /listplayers
- /playerlist (alias)
- /giftcodes
- /toggleplayer

Additional commands are provided by translation, event, player info, KVK, and database handlers.

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
- The bot will always answer when directly mentioned or replied to, and it can also reply randomly based on recent channel history.

## License

MIT
