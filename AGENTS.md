# DS Translator Bot — Agent Guide

Python Discord bot for translation, scheduling, player lookup, KvK tracking and gift-code automation. Python requirements and dependencies are in [pyproject.toml](pyproject.toml).

## Project instructions

Read [local project instructions](.codex/AGENTS.md) for documentation rules and the API reference index. For setup and operation, see [README.md](README.md).

## Commands

| Task | Command |
|---|---|
| Apply database migrations | `uv run alembic upgrade head` |
| Run the bot | `uv run python main.py` |

## Knowledge graph

`graphify-out/` holds the local code graph. Git ignores it. See [.graphifyignore](.graphifyignore) for the scan scope.
For code relationships, run `graphify query "<question>"` before you search the source.
After code changes, run `graphify update .` to refresh the AST graph.
