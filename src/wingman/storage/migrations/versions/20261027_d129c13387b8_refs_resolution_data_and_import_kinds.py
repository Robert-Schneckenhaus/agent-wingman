"""refs resolution data and import kinds

Revision ID: d129c13387b8
Revises: f3e4b201af48
Create Date: 2026-10-27 07:45:54.139772
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'd129c13387b8'
down_revision: str | None = 'f3e4b201af48'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # refs are derived data; clear them so the new NOT NULL column can be added
    op.execute("DELETE FROM refs")

    with op.batch_alter_table('files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('namespaces', sa.JSON(), nullable=True, comment='Namespaces declared in the file (C#)'))

    with op.batch_alter_table('imports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), server_default='import', nullable=False, comment='import|reexport|using|using_static|global_using'))

    with op.batch_alter_table('refs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target_name', sa.Text(), nullable=False, comment='Name as written in the code'))
        batch_op.add_column(sa.Column('qualifier', sa.Text(), nullable=True, comment='Receiver as written: this, base, _calc, ns, Type'))
        batch_op.add_column(sa.Column('qualifier_type', sa.Text(), nullable=True, comment='Declared type of the receiver, if known'))
        batch_op.create_index(batch_op.f('ix_refs_target_name'), ['target_name'], unique=False)
        batch_op.drop_column('unresolved_name')

    if op.get_bind().dialect.name != "sqlite":  # SQLite has no column comments
        op.alter_column(
            "refs",
            "kind",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            existing_comment="call|instantiate|type_ref|read|write|import|http_call",
            comment="call|instantiate|type_ref|inherits|implements|http_call",
        )

    if op.get_bind().dialect.name == "sqlite":
        # Full-text index over symbols, maintained by wingman.storage.search.
        op.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5("
            "name, qualified_name, signature, doc, path, tokenize = 'unicode61')"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS symbols_fts")
    op.execute("DELETE FROM refs")

    with op.batch_alter_table('refs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unresolved_name', sa.TEXT(), nullable=True))
        batch_op.drop_index(batch_op.f('ix_refs_target_name'))
        batch_op.drop_column('qualifier_type')
        batch_op.drop_column('qualifier')
        batch_op.drop_column('target_name')

    with op.batch_alter_table('imports', schema=None) as batch_op:
        batch_op.drop_column('kind')

    with op.batch_alter_table('files', schema=None) as batch_op:
        batch_op.drop_column('namespaces')

