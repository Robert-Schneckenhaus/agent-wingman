import subprocess
from pathlib import Path

import pytest

from wingman.cli import main


@pytest.fixture
def workspace(tmp_path, runner, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    result = runner.invoke(main, ["init", str(root), "--workspace", "demo"])
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(root)
    return root


def _repo_dir(parent: Path, name: str, git: bool = False) -> Path:
    path = parent / name
    path.mkdir()
    if git:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", f"https://example.com/{name}.git"],
            check=True,
        )
    return path


def test_init_creates_workspace(workspace):
    home = workspace / ".wingman"
    assert (home / "config.toml").is_file()
    assert (home / "index.db").is_file()
    assert (home / ".gitignore").read_text() == "index.db*\n"
    assert 'name = "demo"' in (home / "config.toml").read_text()


def test_init_twice_is_idempotent(workspace, runner):
    result = runner.invoke(main, ["init", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "already initialized" in result.output


def test_commands_without_workspace_fail(tmp_path, runner, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main, ["repo", "list"])
    assert result.exit_code != 0
    assert "wingman init" in result.output


def test_repo_add_list_remove(workspace, runner, tmp_path):
    backend = _repo_dir(tmp_path, "backend", git=True)
    frontend = _repo_dir(tmp_path, "frontend")

    result = runner.invoke(main, ["repo", "add", str(backend)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["repo", "add", str(frontend), "--name", "web"])
    assert result.exit_code == 0, result.output
    assert "not a git repository" in result.output

    result = runner.invoke(main, ["repo", "list"])
    assert result.exit_code == 0, result.output
    assert "backend" in result.output
    assert "https://example.com/backend.git" in result.output
    assert "web" in result.output

    result = runner.invoke(main, ["repo", "remove", "web", "--yes"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["repo", "list"])
    assert "web" not in result.output


def test_repo_add_rejects_duplicates(workspace, runner, tmp_path):
    backend = _repo_dir(tmp_path, "backend")
    assert runner.invoke(main, ["repo", "add", str(backend)]).exit_code == 0

    result = runner.invoke(main, ["repo", "add", str(backend), "--name", "other"])
    assert result.exit_code != 0
    assert "path already exists" in result.output

    other = _repo_dir(tmp_path, "other")
    result = runner.invoke(main, ["repo", "add", str(other), "--name", "backend"])
    assert result.exit_code != 0
    assert "name already exists" in result.output


def test_repo_add_rejects_invalid_name(workspace, runner, tmp_path):
    path = _repo_dir(tmp_path, "x")
    result = runner.invoke(main, ["repo", "add", str(path), "--name", "bad:name"])
    assert result.exit_code != 0
    assert "Invalid repository name" in result.output


def test_repo_remove_unknown(workspace, runner):
    result = runner.invoke(main, ["repo", "remove", "nope", "--yes"])
    assert result.exit_code != 0
    assert "No repository named" in result.output


def test_status(workspace, runner, tmp_path):
    runner.invoke(main, ["repo", "add", str(_repo_dir(tmp_path, "backend"))])
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Workspace: demo" in result.output
    assert "Repositories: 1" in result.output


def test_home_option(workspace, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main, ["--home", str(workspace / ".wingman"), "status"])
    assert result.exit_code == 0, result.output
    assert "Workspace: demo" in result.output


def test_database_errors_are_reported_without_traceback(workspace, runner, monkeypatch):
    monkeypatch.setenv("WINGMAN_DB", "sqlite:////nonexistent-dir/sub/x.db")
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **k: None)
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 1
    assert "Database error" in result.output
    assert isinstance(result.exception, SystemExit)


def test_db_upgrade(workspace, runner):
    result = runner.invoke(main, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
