"""Access to a workspace (config + database), shared by CLI, MCP server and hooks."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, Engine

from wingman.config import Config, ConfigError, load_config
from wingman.storage import db, store


class WorkspaceError(Exception):
    pass


class Workspace:
    """Lazily loads configuration and database."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._config: Config | None = None
        self._engine: Engine | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = load_config(self._home)
            except ConfigError as exc:
                raise WorkspaceError(str(exc)) from exc
        return self._config

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            engine = db.create_db_engine(self.config.db_url)
            if not db.is_up_to_date(engine):
                if not db.is_sqlite(self.config.db_url):
                    raise WorkspaceError(
                        "Database schema is out of date. Run 'wingman db upgrade'."
                    )
                db.upgrade(engine)
            self._engine = engine
        return self._engine

    def workspace_id(self, conn: Connection) -> int:
        try:
            return store.get_workspace_id(conn, self.config.workspace)
        except store.StoreError as exc:
            raise WorkspaceError(str(exc)) from exc

    def repositories(self) -> list[store.RepoInfo]:
        with self.engine.connect() as conn:
            return store.list_repositories(conn, self.workspace_id(conn))

    def locate(self, path: Path) -> tuple[store.RepoInfo, str] | None:
        """Repository containing ``path`` and the path relative to its root."""
        absolute = path.resolve()
        best: tuple[store.RepoInfo, str] | None = None
        for repo in self.repositories():
            root = Path(repo.root_path)
            if absolute.is_relative_to(root):
                rel = absolute.relative_to(root).as_posix()
                if best is None or len(repo.root_path) > len(best[0].root_path):
                    best = (repo, rel)
        return best
