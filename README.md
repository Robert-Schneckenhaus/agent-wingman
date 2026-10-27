# agent-wingman

Code index and knowledge store that helps AI agents (Claude Code & co.) find the
relevant code for a bug report or coding task fast: symbols, definitions, call
graph, documentation and linked external docs, stored in SQLite (default) or
another SQL database.

The full design is in [CONCEPT.md](CONCEPT.md) (German).

> **Status:** Phase 0 – project skeleton. Workspaces, repositories and the database
> schema exist; indexing comes in Phase 1.

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
wingman status
wingman repo remove web
```

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
