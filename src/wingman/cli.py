"""Command line interface: ``wingman``."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from wingman import (
    __version__,
    claude_setup,
    context,
    embeddings,
    git,
    hooks,
    notes,
    query,
    render,
)
from wingman.config import (
    CONFIG_FILE,
    DEFAULT_DB_FILE,
    ENV_HOME,
    WINGMAN_DIR,
    ConfigError,
    load_config,
    render_config,
)
from wingman.indexer import IndexingError, index_repository
from wingman.storage import db, store
from wingman.storage.search import backend_for
from wingman.workspace import Workspace, WorkspaceError

REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AppContext(Workspace):
    """Workspace for CLI subcommands (errors become click errors via WingmanGroup)."""


pass_app = click.make_pass_decorator(AppContext)


class WingmanGroup(click.Group):
    """Turns database errors into a short message instead of a traceback."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except WorkspaceError as exc:
            raise click.ClickException(str(exc)) from exc
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
    embedder = app.embedder
    if embedder is not None:
        with engine.connect() as conn:
            done, total = embeddings.coverage(conn, app.workspace_id(conn), embedder.model)
        click.echo(f"Semantic:  {done}/{total} symbols embedded ({embedder.model})")
    else:
        click.echo("Semantic:  off" + (f" ({app.warnings[0]})" if app.warnings else ""))
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
@click.option("--no-embed", is_flag=True, help="Skip computing embeddings afterwards.")
@pass_app
def index(app: AppContext, names: tuple[str, ...], full: bool, no_embed: bool) -> None:
    """Index repositories (default: all). Only changed files are re-parsed; embeddings
    are computed afterwards when semantic search is enabled."""
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
    if not no_embed:
        _embed(app, max_seconds=None, quiet_if_disabled=True)
    _echo_warnings(app)
    if failed:
        raise SystemExit(1)


def _embed(app: AppContext, *, max_seconds: float | None, quiet_if_disabled: bool) -> None:
    embedder = app.embedder
    if embedder is None:
        if not quiet_if_disabled:
            raise click.ClickException(
                "Semantic search is disabled or unavailable"
                + (f": {app.warnings[0]}" if app.warnings else ".")
            )
        return
    with app.engine.connect() as conn:
        workspace_id = app.workspace_id(conn)
    try:
        stats = embeddings.ensure_embeddings(
            app.engine,
            embedder,
            workspace_id,
            max_seconds=max_seconds,
            progress=lambda m: click.echo(m, err=True),
        )
    except embeddings.EmbeddingError as exc:
        if not quiet_if_disabled:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"warning: embeddings skipped: {exc}", err=True)
        return
    if stats.pending or not quiet_if_disabled:
        rest = stats.pending - stats.embedded
        click.echo(
            f"embeddings ({embedder.model}): {stats.embedded} computed"
            + (f", {rest} still missing" if rest else "")
            + (f", {stats.skipped} skipped (files changed, re-index)" if stats.skipped else "")
            + f"; {stats.seconds}s"
        )


def _echo_warnings(app: AppContext) -> None:
    for warning in app.warnings:
        click.echo(f"warning: {warning}", err=True)


@main.command("embed")
@click.option("--max-seconds", type=float, help="Stop after this many seconds.")
@pass_app
def embed_cmd(app: AppContext, max_seconds: float | None) -> None:
    """Compute missing embeddings for semantic search."""
    _embed(app, max_seconds=max_seconds, quiet_if_disabled=False)


# --- queries -------------------------------------------------------------------

json_option = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
repo_option = click.option("--repo", "-r", help="Restrict to one repository.")
kind_option = click.option(
    "--kind", "-k", "kinds", multiple=True, help="Restrict to symbol kinds (repeatable)."
)


def _echo_json(data) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _resolve_symbols(
    conn, app: AppContext, name: str, repo: str | None, kinds: tuple[str, ...]
) -> list[query.SymbolHit]:
    lookup = query.resolve_name(
        conn, app.workspace_id(conn), name, repo=repo, kinds=set(kinds) or None
    )
    if not lookup.found:
        raise click.ClickException(f"No symbol '{name}'. Try: wingman search {name}")
    if lookup.ambiguous:
        raise click.ClickException(render.ambiguous(name, lookup.found))
    return lookup.found


semantic_option = click.option(
    "--semantic/--no-semantic",
    default=True,
    help="Combine full-text with semantic search (when embeddings are available).",
)


@main.command("search")
@click.argument("text", nargs=-1, required=True)
@click.option("--limit", "-n", default=10, show_default=True)
@click.option("--no-tests", is_flag=True, help="Exclude symbols from test files.")
@repo_option
@kind_option
@semantic_option
@json_option
@pass_app
def search_cmd(app, text, limit, no_tests, repo, kinds, semantic, as_json) -> None:
    """Search symbol names, signatures, docs and paths (full-text + semantic)."""
    text = " ".join(text)
    similar = app.semantic_search(text) if semantic else None
    with app.engine.connect() as conn:
        hits = query.search(
            conn,
            app.workspace_id(conn),
            text,
            repo=repo,
            kinds=set(kinds) or None,
            include_tests=not no_tests,
            limit=limit,
            semantic=similar,
        )
    _echo_warnings(app)
    if as_json:
        _echo_json([h.to_dict() for h in hits])
    else:
        click.echo(render.hits(hits))


@main.command("context")
@click.argument("task", nargs=-1)
@click.option(
    "--file", "task_file", type=click.File("r"), help="Read the task from a file ('-' = stdin)."
)
@click.option("--limit", "-n", default=8, show_default=True, help="Number of symbols.")
@click.option("--budget", default=4000, show_default=True, help="Approximate token budget.")
@click.option("--code", is_flag=True, help="Include source code of the top symbols.")
@semantic_option
@json_option
@pass_app
def context_cmd(app, task, task_file, limit, budget, code, semantic, as_json) -> None:
    """Relevant code for a bug report or task (identifiers, stack traces, full text)."""
    text = task_file.read() if task_file else " ".join(task)
    if not text.strip():
        raise click.UsageError("Give the task as arguments or with --file.")
    with app.engine.connect() as conn:
        pack = context.find_relevant(
            conn,
            app.workspace_id(conn),
            text,
            limit=limit,
            include_code=code,
            semantic=app.semantic_search if semantic else None,
        )
        output = pack.to_dict() if as_json else context.render_pack(pack, budget=budget)
    _echo_warnings(app)
    if as_json:
        _echo_json(output)
    else:
        click.echo(output)


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
        notes = query.notes_for_symbols(conn, app.workspace_id(conn), [h.id for h in found])
    if as_json:
        _echo_json(
            [
                d.to_dict()
                | {"notes": [n.text for n in notes.get(d.symbol.id, [])]}
                | ({"code": sources.get(d.symbol.id)} if code else {})
                for d in details
            ]
        )
    else:
        click.echo(render.details(details, sources, notes))


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
    else:
        click.echo("\n".join(render.callers(h, results[h.id]) for h in found))


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
    else:
        click.echo("\n".join(render.callees(h, results[h.id]) for h in found))


@main.command("tests")
@click.argument("name")
@click.option("--depth", "-d", default=3, show_default=True, help="Max. call depth.")
@repo_option
@kind_option
@json_option
@pass_app
def tests_cmd(app, name, depth, repo, kinds, as_json) -> None:
    """Tests that (directly or indirectly) call a symbol."""
    with app.engine.connect() as conn:
        found = _resolve_symbols(conn, app, name, repo, kinds)
        tests = {h.id: query.tests_for(conn, h.id, depth=depth) for h in found}
    if as_json:
        _echo_json(
            [{"symbol": h.to_dict(), "tests": [t.to_dict() for t in tests[h.id]]} for h in found]
        )
        return
    for hit in found:
        click.echo(f"Tests reaching {hit.kind} {hit.qualified_name}  [{hit.location}]")
        click.echo(render.hits(tests[hit.id]) if tests[hit.id] else "  (none found)")


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
    else:
        click.echo(render.outline(repo_name, rel, entries))


# --- notes ---------------------------------------------------------------------


@main.group()
def note() -> None:
    """Knowledge attached to symbols or files (survives re-indexing)."""


@note.command("add")
@click.argument("target")
@click.argument("text", nargs=-1, required=True)
@click.option("--author", "-a", help="Who wrote the note.")
@pass_app
def note_add(app, target, text, author) -> None:
    """Attach a note to TARGET (a symbol name, #id, or repo:path)."""
    with app.engine.begin() as conn:
        try:
            note_id, label = notes.add(conn, app.workspace_id(conn), target, " ".join(text), author)
        except notes.TargetError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Note #{note_id} added to {label}.")


@note.command("list")
@click.argument("target", required=False)
@json_option
@pass_app
def note_list(app, target, as_json) -> None:
    """List notes (optionally only those of TARGET)."""
    with app.engine.connect() as conn:
        try:
            items = notes.list_for(conn, app.workspace_id(conn), target)
        except notes.TargetError as exc:
            raise click.ClickException(str(exc)) from exc
    if as_json:
        _echo_json([n.__dict__ for n in items])
        return
    if not items:
        click.echo("No notes.")
    for item in items:
        author = f" ({item.author})" if item.author else ""
        click.echo(f"#{item.id:<4} {item.target_key or '-'}{author}\n      {item.text}")


@note.command("remove")
@click.argument("note_id", type=int)
@pass_app
def note_remove(app, note_id) -> None:
    """Delete note NOTE_ID."""
    with app.engine.begin() as conn:
        try:
            store.delete_note(conn, app.workspace_id(conn), note_id)
        except store.StoreError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Note #{note_id} removed.")


# --- agent integration -----------------------------------------------------------


@main.command("mcp")
@pass_app
def mcp_cmd(app: AppContext) -> None:
    """Run the MCP server on stdio (used by Claude Code)."""
    from wingman.mcp_server import create_server

    with app.engine.connect() as conn:  # fail early with a readable message
        app.workspace_id(conn)
    create_server(workspace=app).run("stdio")


@main.command("reindex")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path, dir_okay=False))
@pass_app
def reindex_cmd(app: AppContext, paths: tuple[Path, ...]) -> None:
    """Re-index single files (absolute or relative to the current directory)."""
    by_repo: dict[str, tuple[store.RepoInfo, list[str]]] = {}
    for path in paths:
        located = app.locate(path)
        if located is None:
            click.echo(f"{path}: not inside an indexed repository", err=True)
            continue
        repo_info, rel = located
        by_repo.setdefault(repo_info.name, (repo_info, []))[1].append(rel)
    for repo_info, rels in by_repo.values():
        stats = index_repository(app.engine, repo_info, paths=rels)
        click.echo(
            f"{repo_info.name}: {stats.files_indexed} indexed, {stats.files_removed} removed, "
            f"{stats.files_unchanged} unchanged; {stats.seconds}s"
        )


@main.group()
def hook() -> None:
    """Claude Code hook handlers (read the hook payload from stdin; never fail)."""


@hook.command("post-edit")
@pass_app
def hook_post_edit(app: AppContext) -> None:
    """PostToolUse: re-index the file an Edit/Write tool just changed."""
    try:
        message = hooks.post_edit(app, hooks.read_payload(sys.stdin.read()))
    except Exception as exc:  # never break the agent's session
        click.echo(f"wingman hook post-edit: {exc}", err=True)
        return
    if message:
        click.echo(message, err=True)


@hook.command("session-start")
@pass_app
def hook_session_start(app: AppContext) -> None:
    """SessionStart: update the index and tell the agent about the MCP tools."""
    try:
        click.echo(hooks.session_start(app))
    except Exception as exc:
        click.echo(f"wingman hook session-start: {exc}", err=True)


@main.group("claude")
def claude_group() -> None:
    """Set up Claude Code for a project (MCP server, hooks, CLAUDE.md)."""


project_argument = click.argument(
    "project", required=False, default=".", type=click.Path(file_okay=False, path_type=Path)
)
shared_option = click.option(
    "--shared",
    is_flag=True,
    help="Write portable config to commit (.mcp.json, .claude/settings.json) instead of "
    "machine-local config.",
)


@claude_group.command("install")
@project_argument
@shared_option
@click.option("--claude-md", is_flag=True, help="Add usage instructions to CLAUDE.md.")
@click.option("--no-mcp", is_flag=True, help="Only install hooks.")
@pass_app
def claude_install(app: AppContext, project: Path, shared, claude_md, no_mcp) -> None:
    """Register the MCP server and hooks for PROJECT (default: current directory)."""
    project = project.resolve()
    home = app.config.home
    if shared and not project.is_relative_to(app.config.root):
        click.echo(
            f"Warning: {project} is not inside the workspace {app.config.root}; shared "
            "config finds the workspace by walking up from the project directory.",
            err=True,
        )
    try:
        result = claude_setup.install(
            project, home, shared=shared, claude_md=claude_md, register_mcp=not no_mcp
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for changed in result.changed:
        click.echo(f"updated: {changed}")
    for instruction in result.instructions:
        click.echo(instruction)
    click.echo("Restart Claude Code in the project to load the MCP server and hooks.")


@claude_group.command("uninstall")
@project_argument
@shared_option
def claude_uninstall(project: Path, shared) -> None:
    """Remove the MCP server, hooks and CLAUDE.md section from PROJECT."""
    try:
        result = claude_setup.uninstall(project.resolve(), shared=shared)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for changed in result.changed:
        click.echo(f"updated: {changed}")


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
