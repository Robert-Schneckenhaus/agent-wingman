# agent-wingman

Code index and knowledge store that helps AI agents (Claude Code & co.) find the
relevant code for a bug report or coding task fast: symbols, definitions, call
graph, documentation and linked external docs, stored in SQLite (default) or
another SQL database.

The full design is in [CONCEPT.md](CONCEPT.md) (German).

> **Status:** Phase 1 – structural index for **TypeScript/TSX and C#**: symbols,
> docs, imports, call graph, inheritance and full-text search. MCP server, embeddings
> and LLM summaries follow in later phases (see the roadmap in CONCEPT.md).

## Installation

Requires Python 3.11+.

```bash
pip install -e ".[dev]"          # from a checkout
pip install -e ".[postgres]"     # optional PostgreSQL driver
```

## Usage

```bash
wingman init ~/work                       # creates ~/work/.wingman/ (config + SQLite DB)
cd ~/work
wingman repo add ./backend                # add any number of repositories
wingman repo add ./frontend --name web
wingman repo list
wingman index                             # incremental: only changed files are parsed
wingman status
wingman repo remove web
```

### Finding code

```bash
wingman search pdf tax lines              # full-text over names, signatures, docs, paths
wingman search invoice --kind class --repo backend --no-tests
wingman symbol TaxCalculator.Breakdown    # definition, doc, bases, overrides, members
wingman symbol '#42' --code               # by id, with source
wingman callers SaveEntitiesAsync --depth 2   # incl. calls through interfaces/base methods
wingman callees InvoicesController.GetTax
wingman outline src/export/pdfRenderer.ts # also repo:path or a unique path suffix
```

Every query command accepts `--json` for machine-readable output. Symbol names can
be simple names, qualified-name suffixes (`Class.member`) or full qualified names.

What gets indexed: files tracked or untracked-but-not-ignored by git (or all files
outside git), minus `node_modules/`, `bin/`, `obj/`, `dist/`, `build/`, … and
patterns from a `.wingmanignore` file in the repository root (gitignore syntax).
Test and generated files are indexed but ranked lower.

A workspace is found by searching upwards from the current directory for
`.wingman/config.toml`. Override with `--home <path>` or `WINGMAN_HOME`.

### Database

`.wingman/config.toml`:

```toml
[workspace]
name = "work"

[database]
url = "postgresql+psycopg://user:password@host/wingman"
```

`WINGMAN_DB` overrides the configured URL. SQLite databases are migrated
automatically; for other databases run `wingman db upgrade` after updating.

## Development

See [CLAUDE.md](CLAUDE.md) for commands and conventions.
