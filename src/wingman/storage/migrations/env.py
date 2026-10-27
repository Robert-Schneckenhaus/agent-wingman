"""Alembic environment.

Used programmatically by ``wingman.storage.db.upgrade`` (connection passed via
``config.attributes["connection"]``) and by the ``alembic`` CLI during development
(URL from ``WINGMAN_DB`` or ``sqlalchemy.url`` in alembic.ini).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from wingman.storage.db import VERSION_TABLE, include_object
from wingman.storage.schema import metadata

config = context.config


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=os.environ.get("WINGMAN_DB") or config.get_main_option("sqlalchemy.url"),
        target_metadata=metadata,
        version_table=VERSION_TABLE,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return

    url = os.environ.get("WINGMAN_DB") or config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as conn:
        _configure(conn)
        conn.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
