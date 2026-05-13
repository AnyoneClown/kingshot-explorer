# Repository Guidelines

## Project Structure & Module Organization

This is a Python Discord bot. `main.py` wires configuration, services, handlers, and startup. Runtime settings live in `config/`, SQLAlchemy models and sessions in `db/`, Discord command/event handlers in `handlers/`, and reusable business logic/API integrations in `services/`. Database migrations are in `alembic/versions/`. Tests live in `tests/` and follow the service or handler they cover, for example `tests/test_translation_service.py`.

Keep architecture changes scoped: handlers should delegate work to services, and reusable logic should live in `services/` rather than directly in Discord command callbacks.

## Build, Test, and Development Commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Run database migrations:

```bash
python -m alembic upgrade head
```

Run the bot locally:

```bash
python main.py
```

Run tests from the repo root:

```bash
PYTHONPATH=. .venv/bin/pytest
```

Run with Docker Compose:

```bash
docker compose up --build
```

Use `compose.dev.yaml` for bind-mounted local development.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code with 4-space indentation and type hints for public service methods. Use `snake_case` for functions, variables, and module names; `PascalCase` for classes. Keep async database and Discord flows async end-to-end. Prefer dependency injection, as in `TranslatorBot`, over importing global service instances.

No formatter is configured in this repo. Keep imports tidy, avoid unrelated rewrites, and keep comments short and useful.

## Testing Guidelines

Tests use `pytest` and `pytest-asyncio`. Name test files `test_*.py` and test functions `test_*`. Prefer fake clients/sessions over network or live Discord calls. Add focused tests for new service behavior and handler validation. For database changes, add migration/model coverage where practical and verify SQL text for Cockroach-specific behavior.

## Commit & Pull Request Guidelines

Recent history uses short feature-style commits such as `feature: update reminder logic, add migration` and `Feature: fix ai`. Prefer concise imperative messages with a scope or type, for example `feature: add kingshot rag logs` or `fix: pass embedding input type`.

Pull requests should include a short summary, migration notes if schemas changed, test commands run, and any required environment variables. Include screenshots or Discord output examples for command UX changes.

## Security & Configuration Tips

Never commit `.env` or secrets. Required production settings include `DISCORD_TOKEN`, `COCKROACHDB_URL`, and `NVIDIA_API_KEY`. NVIDIA defaults are configured in `.env.example`. File logging may fail if `./logs` is owned by another user in Docker; console logging still works.
