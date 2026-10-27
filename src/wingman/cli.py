"""Command line interface: ``wingman``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from wingman import __version__, git, query
from wingman.config import (
    CONFIG_FILE,
    DEFAULT_DB_FILE,
    ENV_HOME,
    WINGMAN_DIR,
    Config,
    ConfigError,
    load_config,
    render_config,
)
from wingman.indexer import IndexingError, index_repository
from wingman.storage import db, store
from wingman.storage.search import backend_for

REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AppContext:
    """Lazily loads configuration and database for subcommands."""

    def __init__(self, home: Path | None) -> None:
        self._home = home
        self._config: Config | None = None
        self._engine: Engine | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = load_config(self._home)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from exc
        return self._config

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            engine = db.create_db_engine(self.config.db_url)
            if not db.is_up_to_date(engine):
                if db.is_sqlite(self.config.db_url):
                    db.upgrade(engine)
                else:
                    raise click.ClickException(
                        "Database schema is out of date. Run 'wingman db upgrade'."
                    )
            self._engine = engine
        return self._engine

    def workspace_id(self, conn) -> int:
        try:
            return store.get_workspace_id(conn, self.config.workspace)
        except store.StoreError as exc:
            raise click.ClickException(str(exc)) from exc


pass_app = click.make_pass_decorator(AppContext)


class WingmanGroup(click.Group):
    """Turns database errors into a short message instead of a traceback."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except DBAPIError as exc:
            raise click.ClickException(f"Database error: {exc.orig}") from exc
        except NoSuchModuleError as exc:
            raise click.ClickException(
                f"{exc}. Is the database driver installed (e.g. pip install "
                "'agent-wingman[postgres]')?"
            ) from exc


@click.group(cls=WingmanGroup)
@click.version_option(__version__, prog_name="wingman")
@click.option(
    "--home",
    type=click.Path(file_okay=False, path_type=Path),
    envvar=ENV_HOME,
    help=f"Path to the {WINGMAN_DIR} directory (default: search upwards from cwd).",
)
@click.pass_context
def main(ctx: click.Context, home: Path | None) -> None:
    """agent-wingman: code index and knowledge store for AI agents."""
    ctx.obj = AppContext(home)


@main.command()
@click.argument(
    "directory",
    required=False,
    default=".",
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option("--workspace", "-w", help="Workspace name (default: directory name).")
@click.option("--db", "db_url", help="Database URL (default: SQLite in .wingman/).")
def init(directory: Path, workspace: str | None, db_url: str | None) -> None:
    """Create a workspace in DIRECTORY (default: current directory)."""
    root = directory.resolve()
    home = root / WINGMAN_DIR
    config_path = home / CONFIG_FILE

    if config_path.exists():
        click.echo(f"Workspace already initialized at {home}")
        if workspace or db_url:
            click.echo(f"Options --workspace/--db are ignored; edit {config_path} instead.")
    else:
        home.mkdir(parents=True, exist_ok=True)
        config_path.write_text(render_config(workspace or root.name, db_url), encoding="utf-8")
        (home / ".gitignore").write_text(f"{DEFAULT_DB_FILE}*\n", encoding="utf-8")
        click.echo(f"Created {config_path}")

    try:
        config = load_config(home)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    engine = db.create_db_engine(config.db_url)
    db.upgrade(engine)
    with engine.begin() as conn:
        store.ensure_workspace(conn, config.workspace)

    click.echo(f"Workspace '{config.workspace}' ready ({_display_url(engine)}).")
    click.echo("Next: wingman repo add <path>")


@main.command()
@pass_app
def status(app: AppContext) -> None:
    """Show workspace, database and repositories."""
    engine = app.engine
    with engine.connect() as conn:
        workspace_id = app.workspace_id(conn)
        repos = store.list_repositories(conn, workspace_id)
        stats = query.workspace_stats(conn, workspace_id)

    click.echo(f"Workspace: {app.config.workspace}")
    click.echo(f"Home:      {app.config.home}")
    click.echo(f"Database:  {_display_url(engine)}")
    click.echo(f"Schema:    {db.current_revision(engine)}")
    click.echo(
        f"Index:     {stats.files} files, {stats.symbols} symbols, "
        f"{stats.resolved_refs}/{stats.refs} references resolved"
    )
    click.echo(f"Repositories: {len(repos)}")
    for repo in repos:
        indexed = repo.last_commit[:10] if repo.last_commit else "-"
        click.echo(f"  {repo.name:<20} {repo.file_count:>6} files  {indexed:<10}  {repo.root_path}")


# --- repo ----------------------------------------------------------------------


@main.group()
def repo() -> None:
    """Manage the repositories of the workspace."""


@repo.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--name", "-n", help="Repository name (default: directory name).")
@pass_app
def repo_add(app: AppContext, path: Path, name: str | None) -> None:
    """Add the repository at PATH to the workspace."""
    root = path.resolve()
    name = name or root.name
    if not REPO_NAME_PATTERN.match(name):
        raise click.ClickException(
            f"Invalid repository name '{name}'. Use letters, digits, '.', '_' and '-'."
        )

    git_root = git.toplevel(root)
    remote = git.remote_url(root) if git_root else None

    with app.engine.begin() as conn:
        try:
            store.add_repository(
                conn, app.workspace_id(conn), name, root.as_posix(), remote_url=remote
            )
        except store.StoreError as exc:
            raise click.ClickException(str(exc)) from exc

    click.echo(f"Added repository '{name}' ({root})")
    if git_root is None:
        click.echo("Note: not a git repository; incremental indexing will use file hashes only.")
    elif git_root != root:
        click.echo(f"Note: this is a subdirectory of the git repository {git_root}.")


@repo.command("list")
@pass_app
def repo_list(app: AppContext) -> None:
    """List repositories."""
    with app.engine.connect() as conn:
        repos = store.list_repositories(conn, app.workspace_id(conn))
    if not repos:
        click.echo("No repositories. Add one with: wingman repo add <path>")
        return
    for r in repos:
        indexed = r.last_commit[:10] if r.last_commit else "not indexed"
        click.echo(f"{r.name:<20} {r.file_count:>6} files  {indexed:<12} {r.root_path}")
        if r.remote_url:
            click.echo(f"{'':<20} remote: {r.remote_url}")


@repo.command("remove")
@click.argument("name")
@click.confirmation_option(prompt="Remove the repository and all its index data?")
@pass_app
def repo_remove(app: AppContext, name: str) -> None:
    """Remove repository NAME and its index data (files on disk are untouched)."""
    with app.engine.begin() as conn:
        try:
            store.remove_repository(conn, app.workspace_id(conn), name)
            backend_for(conn).purge_orphans(conn)
        except store.StoreError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Removed repository '{name}'.")


# --- indexing ------------------------------------------------------------------


@main.command()
@click.argument("names", nargs=-1)
@click.option("--full", is_flag=True, help="Re-index all files, not only changed ones.")
@pass_app
def index(app: AppContext, names: tuple[str, ...], full: bool) -> None:
    """Index repositories (default: all). Only changed files are re-parsed."""
    engine = app.engine
    with engine.connect() as conn:
        repos = store.list_repositories(conn, app.workspace_id(conn))
    if names:
        unknown = set(names) - {r.name for r in repos}
        if unknown:
            raise click.ClickException(f"Unknown repositories: {', '.join(sorted(unknown))}")
        repos = [r for r in repos if r.name in names]
    if not repos:
        raise click.ClickException("No repositories. Add one with: wingman repo add <path>")

    failed = False
    for repo_info in repos:
        try:
            stats = index_repository(
                engine, repo_info, full=full, progress=lambda m: click.echo(m, err=True)
            )
        except IndexingError as exc:
            click.echo(f"{repo_info.name}: {exc}", err=True)
            failed = True
            continue
        share = f" ({100 * stats.refs_resolved // stats.refs}%)" if stats.refs else ""
        click.echo(
            f"{repo_info.name}: {stats.files_indexed} indexed, {stats.files_unchanged} unchanged, "
            f"{stats.files_removed} removed; {stats.refs_resolved}/{stats.refs} references "
            f"resolved{share}; {stats.seconds}s"
        )
        for error in stats.errors[:5]:
            click.echo(f"  warning: {error}", err=True)
        if len(stats.errors) > 5:
            click.echo(f"  ... {len(stats.errors) - 5} more warnings", err=True)
    if failed:
        raise SystemExit(1)


# --- queries -------------------------------------------------------------------

json_option = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
repo_option = click.option("--repo", "-r", help="Restrict to one repository.")
kind_option = click.option(
    "--kind", "-k", "kinds", multiple=True, help="Restrict to symbol kinds (repeatable)."
)


def _echo_json(data) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _hit_lines(hit: query.SymbolHit, indent: str = "") -> list[str]:
    lines = [f"{indent}{hit.location}  {hit.kind} {hit.qualified_name}"]
    if hit.signature and hit.kind not in ("namespace",):
        lines.append(f"{indent}    {hit.signature}")
    if hit.summary:
        lines.append(f"{indent}    — {hit.summary}")
    return lines


def _resolve_symbols(
    conn, app: AppContext, name: str, repo: str | None, kinds: tuple[str, ...]
) -> list[query.SymbolHit]:
    """Symbols for NAME; all overloads/partial parts if they share one qualified name."""
    if name.startswith("#") and name[1:].isdigit():
        found = list(query.get_symbols(conn, [int(name[1:])]).values())
    else:
        found = query.find_symbols(
            conn, app.workspace_id(conn), name, repo=repo, kinds=set(kinds) or None
        )
    if not found:
        raise click.ClickException(f"No symbol '{name}'. Try: wingman search {name}")
    qualified = {(h.repo, h.qualified_name) for h in found}
    if len(qualified) > 1:
        lines = [f"'{name}' is ambiguous; use a qualified name or #id:"]
        lines += [f"  #{h.id:<6} {h.kind:<10} {h.qualified_name}  [{h.location}]" for h in found]
        raise click.ClickException("\n".join(lines))
    return found


@main.command("search")
@click.argument("text", nargs=-1, required=True)
@click.option("--limit", "-n", default=10, show_default=True)
@click.option("--no-tests", is_flag=True, help="Exclude symbols from test files.")
@repo_option
@kind_option
@json_option
@pass_app
def search_cmd(app, text, limit, no_tests, repo, kinds, as_json) -> None:
    """Full-text search over symbol names, signatures, docs and paths."""
    with app.engine.connect() as conn:
        hits = query.search(
            conn,
            app.workspace_id(conn),
            " ".join(text),
            repo=repo,
            kinds=set(kinds) or None,
            include_tests=not no_tests,
            limit=limit,
        )
    if as_json:
        _echo_json([h.to_dict() for h in hits])
        return
    if not hits:
        click.echo("No matches.")
    for hit in hits:
        click.echo("\n".join(_hit_lines(hit)))


@main.command("symbol")
@click.argument("name")
@click.option("--code", is_flag=True, help="Print the source code.")
@repo_option
@kind_option
@json_option
@pass_app
def symbol_cmd(app, name, code, repo, kinds, as_json) -> None:
    """Definition, documentation and relations of a symbol (name, Class.member or #id)."""
    with app.engine.connect() as conn:
        found = _resolve_symbols(conn, app, name, repo, kinds)
        details = [query.symbol_details(conn, h.id) for h in found]
        sources = {h.id: query.read_source(conn, h.id) for h in found} if code else {}
    if as_json:
        data = [d.to_dict() | ({"code": sources.get(d.symbol.id)} if code else {}) for d in details]
        _echo_json(data)
        return
    for number, d in enumerate(details):
        if number:
            click.echo("")
        s = d.symbol
        click.echo(f"{s.kind} {s.qualified_name}  [{s.location}]  #{s.id}")
        if s.signature:
            click.echo(f"  {s.signature}")
        attributes = list(d.modifiers)
        if d.visibility and d.visibility.split()[0] not in attributes:
            attributes.insert(0, d.visibility)
        if attributes:
            click.echo(f"  modifiers: {' '.join(attributes)}")
        for decorator in d.decorators:
            click.echo(f"  {decorator}")
        if d.doc:
            click.echo("  doc: " + d.doc.replace("\n", "\n       "))
        for label, items in (
            ("bases", d.bases),
            ("derived", d.derived),
            ("overrides", d.overrides),
            ("overridden by", d.overridden_by),
        ):
            for item in items:
                click.echo(f"  {label}: {item.qualified_name}  [{item.location}]")
        if d.members:
            click.echo(f"  members ({len(d.members)}):")
            for m in d.members:
                click.echo(f"    {m.start_line:>5}  {m.kind:<11} {m.name}")
        click.echo(f"  callers: {d.caller_count}")
        if code and sources.get(s.id):
            click.echo("")
            click.echo(sources[s.id])


def _print_ref_tree(hits: list[query.RefHit], indent: str = "  ") -> None:
    for hit in hits:
        if hit.symbol is not None:
            who = f"{hit.symbol.kind} {hit.symbol.qualified_name}"
        else:
            who = "<top-level code>"
        via = f", via {hit.via}" if hit.via else ""
        click.echo(f"{indent}{hit.path}:{hit.line}  {who}  ({hit.kind}, {hit.confidence:.2f}{via})")
        _print_ref_tree(hit.children, indent + "  ")


@main.command("callers")
@click.argument("name")
@click.option("--depth", "-d", default=1, show_default=True, help="Levels of callers.")
@click.option("--types", "include_types", is_flag=True, help="Include type references.")
@repo_option
@kind_option
@json_option
@pass_app
def callers_cmd(app, name, depth, include_types, repo, kinds, as_json) -> None:
    """Where a symbol is called or instantiated (including via interfaces/base methods)."""
    with app.engine.connect() as conn:
        found = _resolve_symbols(conn, app, name, repo, kinds)
        results = {
            h.id: query.callers(conn, h.id, depth=depth, include_type_refs=include_types)
            for h in found
        }
    if as_json:
        _echo_json(
            [
                {"symbol": h.to_dict(), "callers": [c.to_dict() for c in results[h.id]]}
                for h in found
            ]
        )
        return
    for hit in found:
        click.echo(f"Callers of {hit.kind} {hit.qualified_name}  [{hit.location}]")
        if not results[hit.id]:
            click.echo("  (none found)")
        _print_ref_tree(results[hit.id])


@main.command("callees")
@click.argument("name")
@click.option("--types", "include_types", is_flag=True, help="Include type references.")
@repo_option
@kind_option
@json_option
@pass_app
def callees_cmd(app, name, include_types, repo, kinds, as_json) -> None:
    """What a symbol calls or instantiates."""
    with app.engine.connect() as conn:
        found = _resolve_symbols(conn, app, name, repo, kinds)
        results = {h.id: query.callees(conn, h.id, include_type_refs=include_types) for h in found}
    if as_json:
        _echo_json(
            [
                {"symbol": h.to_dict(), "callees": [c.to_dict() for c in results[h.id]]}
                for h in found
            ]
        )
        return
    for hit in found:
        click.echo(f"Callees of {hit.kind} {hit.qualified_name}  [{hit.location}]")
        for ref in results[hit.id]:
            if ref.symbol is not None:
                target = f"{ref.symbol.qualified_name}  [{ref.symbol.location}]"
                click.echo(
                    f"  L{ref.line:<5} {ref.kind:<11} {ref.target_name} -> {target} "
                    f"({ref.confidence:.2f})"
                )
            else:
                click.echo(f"  L{ref.line:<5} {ref.kind:<11} {ref.target_name}  (unresolved)")


@main.command("outline")
@click.argument("path")
@json_option
@pass_app
def outline_cmd(app, path, as_json) -> None:
    """Symbols of a file (path relative to cwd, repo-relative path, or repo:path)."""
    with app.engine.connect() as conn:
        matches = query.find_files(conn, app.workspace_id(conn), path)
        if not matches:
            raise click.ClickException(f"File not in index: {path}")
        if len(matches) > 1:
            lines = [f"'{path}' matches several files:"]
            lines += [f"  {repo}:{rel}" for _, repo, rel in matches]
            raise click.ClickException("\n".join(lines))
        file_id, repo_name, rel = matches[0]
        entries = query.outline(conn, file_id)
    if as_json:
        _echo_json(
            {
                "repo": repo_name,
                "path": rel,
                "symbols": [e.symbol.to_dict() | {"depth": e.depth} for e in entries],
            }
        )
        return
    click.echo(f"{repo_name}:{rel}")
    for entry in entries:
        s = entry.symbol
        lines = f"{s.start_line}-{s.end_line}"
        click.echo(f"  {lines:>9}  {'  ' * entry.depth}{s.kind} {s.name}")


# --- db ------------------------------------------------------------------------


@main.group("db")
def db_group() -> None:
    """Database maintenance."""


@db_group.command("upgrade")
@pass_app
def db_upgrade(app: AppContext) -> None:
    """Migrate the database schema to the latest version."""
    engine = db.create_db_engine(app.config.db_url)
    before = db.current_revision(engine)
    db.upgrade(engine)
    after = db.current_revision(engine)
    if before == after:
        click.echo(f"Schema is up to date ({after}).")
    else:
        click.echo(f"Schema upgraded: {before or 'empty'} -> {after}")


def _display_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=True)
