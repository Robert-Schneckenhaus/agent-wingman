# agent-wingman

Code index and knowledge store that helps AI agents (Claude Code & co.) find the
relevant code for a bug report or coding task fast: symbols, definitions, call
graph, documentation and linked external docs, stored in SQLite (default) or
another SQL database.

The full design is in [CONCEPT.md](CONCEPT.md) (German).

> **Status:** Phase 2 – structural index for **TypeScript/TSX and C#** (symbols, docs,
> imports, call graph, inheritance, full-text search), context packs for bug reports,
> an MCP server and hooks for Claude Code. Multi-repo linking, embeddings, LLM
> summaries and external docs follow (see the roadmap in CONCEPT.md).

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

### Using it from Claude Code

```bash
cd ~/work/backend
wingman claude install --claude-md      # MCP server + hooks for this project
```

This registers the MCP server `wingman` (local scope), adds two hooks to
`.claude/settings.local.json` and a short usage section to `CLAUDE.md`:

- **SessionStart** brings the index up to date and tells the agent to use the tools.
- **PostToolUse** (Edit/Write) re-indexes the edited file immediately.

MCP tools: `find_relevant_for_task` (paste a bug report, stack trace or task),
`search_code`, `get_symbol`, `get_callers`, `get_callees`, `find_tests_for`,
`get_file_outline`, `add_note`, `reindex`.

Use `--shared` to write portable, committable config instead (`.mcp.json` and
`.claude/settings.json`; requires `wingman` on `PATH` and the workspace in a parent
directory of the project). `wingman claude uninstall` removes everything again.

### Finding code

```bash
wingman context --file bug-report.txt     # ranked symbols for a bug report / stack trace
wingman context "VAT line missing in PDF export" --code --budget 3000
wingman search pdf tax lines              # full-text over names, signatures, docs, paths
wingman search invoice --kind class --repo backend --no-tests
wingman symbol TaxCalculator.Breakdown    # definition, doc, bases, overrides, members
wingman symbol '#42' --code               # by id, with source
wingman callers SaveEntitiesAsync --depth 2   # incl. calls through interfaces/base methods
wingman callees InvoicesController.GetTax
wingman outline src/export/pdfRenderer.ts # also repo:path or a unique path suffix
wingman tests TaxCalculator.Breakdown     # tests that reach a symbol (up to 3 calls deep)
wingman note add TaxCalculator.Breakdown "Discount is applied before tax (#123)"
wingman note add backend:src/Api "Controllers must call Audit()"
wingman reindex src/billing/tax.ts        # single files, e.g. after a git checkout
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
