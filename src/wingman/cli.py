"""Command line interface: ``wingman``."""

from __future__ import annotations

import re
from pathlib import Path

import click
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from wingman import __version__, git
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
from wingman.storage import db, store

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
        repos = store.list_repositories(conn, app.workspace_id(conn))

    click.echo(f"Workspace: {app.config.workspace}")
    click.echo(f"Home:      {app.config.home}")
    click.echo(f"Database:  {_display_url(engine)}")
    click.echo(f"Schema:    {db.current_revision(engine)}")
    click.echo(f"Repositories: {len(repos)}")
    for repo in repos:
        click.echo(f"  {repo.name:<20} {repo.root_path}")


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
        except store.StoreError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"Removed repository '{name}'.")


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
