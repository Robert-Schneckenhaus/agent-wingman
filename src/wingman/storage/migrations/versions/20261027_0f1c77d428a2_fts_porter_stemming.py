"""fts porter stemming

Revision ID: 0f1c77d428a2
Revises: e572e17a1ef3
Create Date: 2026-10-27 08:31:30.994097
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0f1c77d428a2'
down_revision: str | None = 'e572e17a1ef3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FTS_COLUMNS = "name, qualified_name, signature, doc, path"


def _rebuild(tokenize: str) -> None:
    """Recreate symbols_fts with another tokenizer and refill it from the symbols table."""
    from wingman.storage.search import FtsRow, SqliteFtsBackend

    bind = op.get_bind()
    op.execute("DROP TABLE IF EXISTS symbols_fts")
    op.execute(
        f"CREATE VIRTUAL TABLE symbols_fts USING fts5({FTS_COLUMNS}, tokenize = '{tokenize}')"
    )
    rows = bind.execute(
        sa.text(
            "SELECT s.id, s.name, s.qualified_name, s.signature, s.doc, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        )
    ).all()
    SqliteFtsBackend().index(bind, [FtsRow(*row) for row in rows])


def upgrade() -> None:
    # stemming lets "cancels" match "Cancelled" in names and docs
    if op.get_bind().dialect.name == "sqlite":
        _rebuild("porter unicode61")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _rebuild("unicode61")
