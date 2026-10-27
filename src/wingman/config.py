"""Locating and loading the workspace configuration.

A workspace lives in a ``.wingman/`` directory ("home") that holds ``config.toml``
and, by default, the SQLite database ``index.db``. The home is found by walking up
from the current directory, or set explicitly via ``--home`` / ``WINGMAN_HOME``.
The database URL can be overridden with ``WINGMAN_DB``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

WINGMAN_DIR = ".wingman"
CONFIG_FILE = "config.toml"
DEFAULT_DB_FILE = "index.db"

ENV_HOME = "WINGMAN_HOME"
ENV_DB = "WINGMAN_DB"


class ConfigError(Exception):
    """Raised when no usable workspace configuration can be found."""


@dataclass(frozen=True)
class Config:
    home: Path
    """The ``.wingman`` directory."""
    workspace: str
    db_url: str

    @property
    def root(self) -> Path:
        """The directory that contains ``.wingman``."""
        return self.home.parent


def find_home(start: Path | None = None) -> Path | None:
    """Return the nearest ``.wingman`` directory at or above ``start``."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / WINGMAN_DIR
        if (candidate / CONFIG_FILE).is_file():
            return candidate
    return None


def default_db_url(home: Path) -> str:
    return f"sqlite:///{(home / DEFAULT_DB_FILE).as_posix()}"


def resolve_db_url(url: str, base: Path) -> str:
    """Make a relative SQLite path absolute (relative to ``base``)."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database:
        return url
    if parsed.database == ":memory:" or Path(parsed.database).is_absolute():
        return url
    absolute = (base / parsed.database).resolve()
    return parsed.set(database=absolute.as_posix()).render_as_string(hide_password=False)


def load_config(home: Path | None = None) -> Config:
    if home is None:
        env_home = os.environ.get(ENV_HOME)
        home = Path(env_home) if env_home else find_home()
    if home is None:
        raise ConfigError(
            f"No {WINGMAN_DIR}/{CONFIG_FILE} found here or in any parent directory. "
            "Run 'wingman init' first."
        )
    home = home.resolve()
    config_path = home / CONFIG_FILE
    if not config_path.is_file():
        raise ConfigError(f"Missing configuration file: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    workspace = data.get("workspace", {}).get("name") or home.parent.name
    db_url = os.environ.get(ENV_DB) or data.get("database", {}).get("url")
    db_url = resolve_db_url(db_url, home.parent) if db_url else default_db_url(home)
    return Config(home=home, workspace=workspace, db_url=db_url)


def render_config(workspace: str, db_url: str | None = None) -> str:
    db_line = (
        f'url = "{db_url}"' if db_url else f'# url = "sqlite:///{WINGMAN_DIR}/{DEFAULT_DB_FILE}"'
    )
    return f"""\
# agent-wingman workspace configuration

[workspace]
name = "{workspace}"

[database]
# Default: SQLite file inside .wingman/. Relative SQLite paths are resolved
# against the directory containing .wingman/. WINGMAN_DB overrides this value.
{db_line}
# url = "postgresql+psycopg://user:password@host/wingman"
"""
