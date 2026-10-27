# agent-wingman – notes for coding agents

Design and roadmap: `CONCEPT.md` (German). Code, comments and commit messages are in English.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"   # or: python -m venv .venv && pip install -e ".[dev]"
.venv/bin/pytest                        # tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Layout

- `src/wingman/cli.py` – click CLI (`wingman`)
- `src/wingman/config.py` – finds `.wingman/config.toml`, resolves DB URL
- `src/wingman/storage/schema.py` – SQLAlchemy Core schema (single source of truth)
- `src/wingman/storage/migrations/` – Alembic migrations
- `src/wingman/storage/store.py` – data access functions
- `tests/` – pytest; `tests/test_migrations.py` fails if schema and migrations diverge

## Schema changes

1. Edit `schema.py`.
2. Generate a migration against a fresh dev DB:
   ```bash
   rm -rf .wingman-dev && mkdir .wingman-dev
   .venv/bin/alembic upgrade head
   .venv/bin/alembic revision --autogenerate -m "describe change"
   ```
3. Review the generated file (replace SQLite-specific defaults such as
   `sa.text('(CURRENT_TIMESTAMP)')` with `sa.func.now()`), then run the tests.

## Conventions

- SQLAlchemy Core, no ORM. Keep SQL portable between SQLite and PostgreSQL;
  backend-specific search features go behind a backend interface (CONCEPT.md 4.6).
- Cross-references that should survive re-indexing or be shared (doc links, notes)
  use `stable_key` / `repo:path`, not numeric IDs.
