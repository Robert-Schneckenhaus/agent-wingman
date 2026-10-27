"""Engine creation and schema migrations (Alembic)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url

MIGRATIONS_LOCATION = "wingman.storage:migrations"
VERSION_TABLE = "wingman_alembic_version"


def is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def create_db_engine(url: str) -> Engine:
    if not is_sqlite(url):
        return create_engine(url)

    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def _alembic_config(engine: Engine) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", MIGRATIONS_LOCATION)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


def head_revision(engine: Engine) -> str | None:
    return ScriptDirectory.from_config(_alembic_config(engine)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"version_table": VERSION_TABLE})
        return context.get_current_revision()


def is_up_to_date(engine: Engine) -> bool:
    return current_revision(engine) == head_revision(engine)


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
