# agent-wingman

Code index and knowledge store that helps AI agents (Claude Code & co.) find the
relevant code for a bug report or coding task fast: symbols, definitions, call
graph, documentation and linked external docs, stored in SQLite (default) or
another SQL database.

The full design is in [CONCEPT.md](CONCEPT.md) (German).

> **Status:** structural index for **TypeScript/TSX and C#** (symbols, docs, imports,
> call graph, inheritance), full-text + semantic search, context packs for bug reports,
> multi-repo linking (packages, HTTP endpoints), LLM descriptions for undocumented code,
> an MCP server and hooks for Claude Code. External docs follow (see the roadmap in
> CONCEPT.md).

## Installation

Requires Python 3.11+.

```bash
pip install -e ".[dev]"          # from a checkout
pip install -e ".[postgres]"     # optional PostgreSQL driver
pip install -e ".[embeddings]"   # optional semantic search (local model via fastembed)
pip install -e ".[summaries]"    # optional LLM descriptions (Anthropic SDK)
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
`get_file_outline`, `list_endpoints`, `add_note`, `reindex`.

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

### Several repositories

All repositories of a workspace are resolved together:

- **Packages**: `import { x } from "@acme/shared"` resolves into the repository (or
  monorepo package) whose `package.json` is named `@acme/shared`; C# `using`s find
  namespaces in other repositories.
- **HTTP**: server routes (ASP.NET controllers and minimal APIs, NestJS, Express) are
  matched with client calls (`fetch`, `axios`, Angular `HttpClient`, .NET
  `HttpClient`), so `wingman callers InvoicesController.GetTax` also lists the
  frontend function that calls `GET /api/invoices/{id}/tax`.

```bash
wingman endpoints                         # routes, handlers and number of client calls
wingman endpoints --clients --unmatched   # HTTP calls that match no known route
```

### Semantic search

With the `embeddings` extra installed, search and context packs also find code by
meaning, not only by matching words: synonyms or paraphrased bug reports. The
default model `BAAI/bge-small-en-v1.5` runs locally (ONNX, no GPU needed) and is downloaded from Hugging Face on first use; code never leaves the
machine. Embeddings are computed at the end of `wingman index` (or with
`wingman embed`) and cached by content, so unchanged code is never embedded twice.

```toml
[embeddings]
enabled = "auto"          # auto | true | false   (env: WINGMAN_EMBEDDINGS=on|off|auto)
model = "BAAI/bge-small-en-v1.5"
```

The default model is English-only. For bug reports in other languages, configure a
multilingual model such as `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(finds German queries, but ranks English ones noticeably worse).

`--no-semantic` on `search`/`context` uses full-text search only. Without the model
(offline, blocked download) everything keeps working with full-text search and a warning.

### Descriptions for undocumented code (LLM, opt-in)

`wingman summarize` asks Claude (`claude-haiku-5-5` by default) for a one- or
two-sentence description and a few search keywords for every class, function and
method without a doc comment. These descriptions make undocumented code findable by
what it does: they go into full-text search, semantic search and context packs.

```toml
[summaries]
enabled = true                # opt-in: this sends code to the Anthropic API and costs tokens
model = "claude-haiku-5-5"
max_tokens_per_run = 1000000  # budget per run (input + output)
min_lines = 3                 # skip shorter functions and methods
```

```bash
pip install -e ".[summaries]"             # Anthropic SDK; uses ANTHROPIC_API_KEY
wingman summarize --dry-run               # count candidates, estimate tokens, send nothing
wingman summarize backend --limit 200     # one repository, at most 200 symbols
```

Called code is described before its callers, so prompts can include what a function
calls. Results are cached by code hash: unchanged code is never sent twice, and
re-indexed symbols get their description back for free.

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
