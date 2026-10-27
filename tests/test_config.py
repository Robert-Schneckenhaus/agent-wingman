from pathlib import Path

import pytest

from wingman.config import (
    ConfigError,
    find_home,
    load_config,
    render_config,
    resolve_db_url,
)


def _make_home(root: Path, content: str | None = None) -> Path:
    home = root / ".wingman"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(content or render_config("ws"), encoding="utf-8")
    return home


def test_find_home_walks_up(tmp_path):
    home = _make_home(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_home(nested) == home.resolve()


def test_find_home_none(tmp_path):
    assert find_home(tmp_path) is None


def test_load_config_defaults(tmp_path):
    home = _make_home(tmp_path)
    config = load_config(home)
    assert config.workspace == "ws"
    assert config.db_url == f"sqlite:///{(home / 'index.db').resolve().as_posix()}"
    assert config.root == tmp_path.resolve()


def test_load_config_env_db_overrides(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setenv("WINGMAN_DB", "postgresql://u:p@h/db")
    assert load_config(home).db_url == "postgresql://u:p@h/db"


def test_load_config_env_home(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.setenv("WINGMAN_HOME", str(home))
    monkeypatch.chdir("/")
    assert load_config().home == home.resolve()


def test_load_config_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="wingman init"):
        load_config()


def test_load_config_invalid_toml(tmp_path):
    home = _make_home(tmp_path, content="this is = = not toml")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(home)


def test_relative_sqlite_url_is_resolved_against_root(tmp_path):
    home = _make_home(tmp_path, render_config("ws", "sqlite:///data/my.db"))
    assert load_config(home).db_url == f"sqlite:///{(tmp_path / 'data/my.db').resolve().as_posix()}"


@pytest.mark.parametrize(
    "url",
    ["sqlite:///:memory:", "sqlite:////abs/path.db", "postgresql://u:p@h/db"],
)
def test_resolve_db_url_leaves_others_untouched(tmp_path, url):
    assert resolve_db_url(url, tmp_path) == url
