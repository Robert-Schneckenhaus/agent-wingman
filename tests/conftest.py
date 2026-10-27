import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import Engine

from wingman.config import ENV_DB, ENV_HOME
from wingman.indexer import index_repository
from wingman.storage import db, store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.delenv(ENV_DB, raising=False)


@pytest.fixture
def runner():
    return CliRunner()


@dataclass
class IndexedWorkspace:
    engine: Engine
    workspace_id: int
    root: Path

    def repo(self, name: str) -> store.RepoInfo:
        with self.engine.connect() as conn:
            return next(
                r for r in store.list_repositories(conn, self.workspace_id) if r.name == name
            )

    def reindex(self, name: str, **kwargs):
        return index_repository(self.engine, self.repo(name), **kwargs)


@pytest.fixture
def indexed(tmp_path) -> IndexedWorkspace:
    """Both fixture repositories copied to a temp dir and indexed into a fresh DB."""
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'index.db'}")
    db.upgrade(engine)
    with engine.begin() as conn:
        workspace_id = store.ensure_workspace(conn, "test")
        for name in ("ts_app", "cs_app"):
            shutil.copytree(FIXTURES / name, tmp_path / name)
            store.add_repository(conn, workspace_id, name, (tmp_path / name).as_posix())
    workspace = IndexedWorkspace(engine, workspace_id, tmp_path)
    for name in ("ts_app", "cs_app"):
        workspace.reindex(name)
    return workspace
