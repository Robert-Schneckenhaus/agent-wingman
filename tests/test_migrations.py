from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import URL, create_engine, inspect

from wingman.storage import db
from wingman.storage.schema import metadata


def _engine(tmp_path):
    return db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_upgrade_creates_all_tables(tmp_path):
    engine = _engine(tmp_path)
    assert db.current_revision(engine) is None
    db.upgrade(engine)

    tables = set(inspect(engine).get_table_names())
    assert set(metadata.tables) <= tables
    assert db.VERSION_TABLE in tables
    assert db.is_up_to_date(engine)


def test_upgrade_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    db.upgrade(engine)
    db.upgrade(engine)
    assert db.current_revision(engine) == db.head_revision(engine)


def test_migrations_match_schema(tmp_path):
    """Fails if schema.py was changed without generating a migration."""
    engine = _engine(tmp_path)
    db.upgrade(engine)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "version_table": db.VERSION_TABLE,
                "include_object": db.include_object,
            },
        )
        diff = compare_metadata(context, metadata)
    assert diff == []


def test_foreign_keys_enabled(tmp_path):
    engine = _engine(tmp_path)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_urls_with_percent_signs():
    # postgresql+psycopg://u@/db?host=%2Fvar%2Frun: configparser treats "%" specially
    url = URL.create("sqlite", database="index.db", query={"uri": "true", "vfs": "/run"})
    assert "%2F" in url.render_as_string()
    assert db.head_revision(create_engine(url)) is not None
