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
- `src/wingman/storage/store.py` – data access for workspaces/repositories
- `src/wingman/storage/search.py` – search backends (SQLite FTS5, LIKE fallback)
- `src/wingman/scanner.py` – which files get indexed
- `src/wingman/extract/` – tree-sitter visitors per language (`typescript.py`, `csharp.py`)
- `src/wingman/indexer.py` – incremental indexing (hash compare, store, FTS)
- `src/wingman/resolver.py` – resolves imports (incl. package.json registry), refs and
  inheritance across the workspace for a scope of repositories; builds `symbol_edges`
- `src/wingman/query.py` – read side used by CLI and MCP server
- `src/wingman/context.py` – bug report/task text -> signals -> ranked context pack
- `src/wingman/embeddings.py` – embedding texts, providers (fastembed), content-addressed
  vector cache, semantic search; tests use a deterministic fake embedder and
  `WINGMAN_EMBEDDINGS=off` (set in `tests/conftest.py`), so no model is downloaded
- `src/wingman/summaries.py` – opt-in LLM descriptions for undocumented symbols (Anthropic
  SDK, bottom-up over the call graph, budget, `summary_cache`); tests use a fake summarizer
- `src/wingman/render.py` – text output shared by CLI and MCP server
- `src/wingman/notes.py` – notes on symbols/files/directories (keyed by stable keys)
- `src/wingman/workspace.py` – config + engine access shared by CLI, MCP server, hooks
- `src/wingman/mcp_server.py` – MCP server (stdio, `wingman mcp`)
- `src/wingman/endpoints.py` – HTTP route normalization and client -> server matching
  (`http_call` refs, rebuilt workspace-wide after every change)
- `src/wingman/hooks.py`, `claude_setup.py` – Claude Code hooks and `wingman claude install`
- `tests/` – pytest; `tests/test_migrations.py` fails if schema and migrations diverge;
  `tests/fixtures/{ts_app,cs_app,shared_lib}` are small repos with known expected
  resolutions (incl. cross-repo packages and HTTP calls); the `indexed` (API) and `ws`
  (CLI) fixtures in `tests/conftest.py` index all three

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
   If only the extractor output changes (no schema change), bump
   `EXTRACTOR_VERSION` in `src/wingman/extract/__init__.py` instead: it is part of
   each file's content hash, so the next `wingman index` re-parses everything.
4. SQLite does not detect column comment changes; check PostgreSQL if possible.

## Conventions

- SQLAlchemy Core, no ORM. Keep SQL portable between SQLite and PostgreSQL;
  backend-specific search features go behind a backend interface (CONCEPT.md 4.6).
- Cross-references that should survive re-indexing or be shared (doc links, notes)
  use `stable_key` / `repo:path`, not numeric IDs.

## Commits

- Author and committer: `Robert Schneckenhaus <robert@studio-robert.com>`
  (`git config user.name "Robert Schneckenhaus"` / `git config user.email "robert@studio-robert.com"`).
- No `Co-Authored-By` or other AI attribution trailers in commit messages or PR descriptions.
