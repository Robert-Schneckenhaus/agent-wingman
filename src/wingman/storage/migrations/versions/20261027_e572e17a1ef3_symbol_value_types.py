"""symbol value types and foreign key indexes

Revision ID: e572e17a1ef3
Revises: d129c13387b8
Create Date: 2026-10-27 07:59:41.199089
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'e572e17a1ef3'
down_revision: str | None = 'd129c13387b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('symbols', schema=None) as batch_op:
        batch_op.add_column(sa.Column('type_name', sa.Text(), nullable=True, comment='Declared value type (field/property type, unwrapped return type)'))

    # indexes on foreign keys: deleting re-indexed files cascades into these tables
    op.create_index(op.f('ix_symbols_parent_id'), 'symbols', ['parent_id'])
    op.create_index(op.f('ix_symbol_locations_file_id'), 'symbol_locations', ['file_id'])
    op.create_index(op.f('ix_symbol_edges_dst_id'), 'symbol_edges', ['dst_id'])
    op.create_index(op.f('ix_imports_resolved_file_id'), 'imports', ['resolved_file_id'])
    op.create_index(op.f('ix_endpoints_symbol_id'), 'endpoints', ['symbol_id'])

    # force the next `wingman index` to re-parse all files so type_name gets filled
    op.execute("UPDATE files SET content_hash = ''")


def downgrade() -> None:
    op.drop_index(op.f('ix_endpoints_symbol_id'), table_name='endpoints')
    op.drop_index(op.f('ix_imports_resolved_file_id'), table_name='imports')
    op.drop_index(op.f('ix_symbol_edges_dst_id'), table_name='symbol_edges')
    op.drop_index(op.f('ix_symbol_locations_file_id'), table_name='symbol_locations')
    op.drop_index(op.f('ix_symbols_parent_id'), table_name='symbols')
    with op.batch_alter_table('symbols', schema=None) as batch_op:
        batch_op.drop_column('type_name')
