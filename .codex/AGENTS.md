# Local Project Instructions

## Project knowledge base

Project documentation lives in the repo-local `docs/` folder:

- `docs/kingshot-data-api.md` — bot-facing KingShot Data API usage, examples, and command integration notes.
- `docs/API.md` — full KingShot Data Service endpoint contract.
- `docs/SCHEMAS.md` — field-level response dictionary and normalization notes.
- `docs/INTEGRATION.md` — sibling-service integration guidance and API client examples.

When changing behavior, API integration, data shapes, or operational assumptions, update the relevant file in `docs/` with:

- Behavioral and architectural changes
- Decisions and their rationale
- New dependencies
- Important debugging discoveries
- Known limitations
- Verification performed

Never copy secrets, credentials, customer data, or large source-code excerpts into documentation.
