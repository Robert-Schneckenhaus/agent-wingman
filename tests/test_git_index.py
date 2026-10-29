"""Finding changed files via git must index exactly what a full scan indexes."""

import shutil
import subprocess

import pytest
from conftest import FIXTURES
from sqlalchemy import select, update

from wingman.indexer import index_repository
from wingman.storage import db, store
from wingman.storage.schema import files, repositories

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "PATH": "/usr/bin:/bin",
}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=ENV)


class Repo:
    """ts_app in a git repository (optionally in a subdirectory of it)."""

    def __init__(self, tmp_path, subdir: str = ""):
        self.top = tmp_path / "top"
        self.root = self.top / subdir if subdir else self.top
        shutil.copytree(FIXTURES / "ts_app", self.root)
        (self.top / ".gitignore").write_text("node_modules/\n")
        git(self.top, "init", "-q")
        git(self.top, "add", "-A")
        git(self.top, "commit", "-qm", "initial")
        self.engine = db.create_db_engine(f"sqlite:///{tmp_path / 'index.db'}")
        db.upgrade(engine=self.engine)
        with self.engine.begin() as conn:
            workspace_id = store.ensure_workspace(conn, "test")
            store.add_repository(conn, workspace_id, "ts_app", self.root.as_posix())

    def info(self):
        with self.engine.connect() as conn:
            return store.list_repositories(conn, 1)[0]

    def index(self, **kwargs):
        return index_repository(self.engine, self.info(), **kwargs)

    def files(self) -> dict[str, str]:
        with self.engine.connect() as conn:
            return dict(conn.execute(select(files.c.path, files.c.content_hash)).all())

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def assert_matches_full_scan(self):
        incremental = self.files()
        stats = self.index(full=True)
        assert stats.mode == "scan"
        assert incremental == self.files()


@pytest.fixture(params=["", "frontend"], ids=["root", "subdirectory"])
def repo(tmp_path, request):
    return Repo(tmp_path, request.param)


def test_first_run_scans_then_git_finds_changes(repo):
    assert repo.index().mode == "scan"
    assert repo.index().mode == "git"  # nothing changed: nothing read

    repo.write("src/billing/tax.ts", (repo.root / "src/billing/tax.ts").read_text() + "\n// c\n")
    git(repo.top, "commit", "-qam", "committed change")
    repo.write("src/shared/money.ts", "export const VAT_RATE = 0.2;\n")  # uncommitted
    repo.write("src/new/feature.ts", "export function feature() {}\n")  # untracked
    (repo.root / "src/export/summary.ts").unlink()  # deleted, uncommitted
    stats = repo.index()
    assert stats.mode == "git"
    assert stats.files_scanned == 3  # tax.ts, money.ts, feature.ts (summary.ts is gone)
    assert (stats.files_indexed, stats.files_removed) == (3, 1)
    repo.assert_matches_full_scan()


def test_reverted_uncommitted_change_is_picked_up(repo):
    repo.index()
    original = (repo.root / "src/shared/money.ts").read_text()
    repo.write("src/shared/money.ts", original + "\nexport const EXTRA = 1;\n")
    repo.index()  # indexes the uncommitted version and remembers the file as dirty
    git(repo.top, "checkout", "--", ".")  # back to the committed version: git sees no change
    stats = repo.index()
    assert stats.mode == "git" and stats.files_indexed == 1
    repo.assert_matches_full_scan()


def test_single_file_runs_are_remembered(repo):
    repo.index()
    path = "src/shared/money.ts"
    original = (repo.root / path).read_text()
    repo.write(path, original + "\n// hook\n")
    assert repo.index(paths=[path]).mode == "paths"
    repo.write(path, original)  # reverted outside of git's view of "changes"
    stats = repo.index()
    assert stats.mode == "git" and stats.files_indexed == 1
    repo.assert_matches_full_scan()


def test_changed_rules_or_unknown_commit_fall_back_to_a_scan(repo):
    repo.index()
    (repo.root / ".wingmanignore").write_text("src/ui/\n")
    stats = repo.index()
    assert stats.mode == "scan" and stats.files_removed == 1  # InvoiceView.tsx
    assert repo.index().mode == "git"

    with repo.engine.begin() as conn:
        conn.execute(update(repositories).values(last_commit="0" * 40))
    assert repo.index().mode == "scan"
    repo.assert_matches_full_scan()


def test_outside_git_every_run_scans(tmp_path):
    root = tmp_path / "plain"
    shutil.copytree(FIXTURES / "ts_app", root)
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'index.db'}")
    db.upgrade(engine)
    with engine.begin() as conn:
        store.add_repository(conn, store.ensure_workspace(conn, "t"), "plain", root.as_posix())
        info = store.list_repositories(conn, 1)[0]
    index_repository(engine, info)
    assert index_repository(engine, info).mode == "scan"
