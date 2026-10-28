"""Database schema (SQLAlchemy Core), see CONCEPT.md section 4.

The schema is portable between SQLite and PostgreSQL. Backend-specific search
structures (FTS5 / tsvector, sqlite-vec / pgvector) are added by the search
backends in later phases and are not part of this metadata.

Any change here needs a new Alembic migration (see CLAUDE.md).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _created_at() -> Column:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


# --- Workspaces & repositories -------------------------------------------------

workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False, unique=True),
    _created_at(),
)

repositories = Table(
    "repositories",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "workspace_id",
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(200), nullable=False),
    Column("root_path", Text, nullable=False),
    Column("remote_url", Text),
    Column("last_commit", String(64), comment="Last indexed git commit"),
    _created_at(),
    Column("updated_at", DateTime(timezone=True)),
    UniqueConstraint("workspace_id", "name"),
    UniqueConstraint("workspace_id", "root_path"),
)

index_runs = Table(
    "index_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("repo_id", Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("commit_sha", String(64)),
    Column("files_changed", Integer),
    Column("status", String(20), nullable=False, comment="running|succeeded|failed"),
    Column("stats", JSON),
)

# --- Code structure ------------------------------------------------------------

files = Table(
    "files",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("repo_id", Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
    Column("path", Text, nullable=False, comment="Relative to the repository root"),
    Column("language", String(40)),
    Column("content_hash", String(64), nullable=False),
    Column("size_bytes", Integer),
    Column("line_count", Integer),
    Column("is_test", Boolean, nullable=False, server_default="0"),
    Column("is_generated", Boolean, nullable=False, server_default="0"),
    Column("last_modified", DateTime(timezone=True)),
    Column("namespaces", JSON, comment="Namespaces declared in the file (C#)"),
    UniqueConstraint("repo_id", "path"),
)

symbols = Table(
    "symbols",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("parent_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE")),
    Column(
        "kind",
        String(40),
        nullable=False,
        comment="class|struct|function|method|interface|enum|const|module|property|macro|...",
    ),
    Column("name", Text, nullable=False),
    Column("qualified_name", Text, nullable=False),
    Column("signature", Text),
    Column(
        "type_name",
        Text,
        comment="Declared value type (field/property type, unwrapped return type)",
    ),
    Column("visibility", String(20)),
    Column("modifiers", JSON),
    Column("decorators", JSON),
    Column("start_line", Integer),
    Column("end_line", Integer),
    Column("start_byte", Integer),
    Column("end_byte", Integer),
    Column("body_hash", String(64)),
    Column("doc", Text),
    Column("summary", Text),
    Column("summary_source", String(20), comment="docstring|generated|manual"),
    Column("complexity", Integer),
    Column(
        "stable_key",
        Text,
        nullable=False,
        comment="repo + path + qualified_name + kind (+ signature for overloads)",
    ),
    Column(
        "embed_hash",
        String(64),
        comment="Hash of the text embedded for this symbol; key into embedding_cache",
    ),
    Index(None, "name"),
    Index(None, "qualified_name"),
    Index(None, "file_id"),
    Index(None, "parent_id"),
    Index(None, "stable_key", unique=True),
    Index(None, "embed_hash"),
)

symbol_locations = Table(
    "symbol_locations",
    metadata,
    Column("symbol_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False),
    Column("file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(20), nullable=False, comment="declaration|definition|partial"),
    Column("start_line", Integer),
    Column("end_line", Integer),
    Index(None, "symbol_id"),
    Index(None, "file_id"),
)

refs = Table(
    "refs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("from_symbol_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE")),
    Column("from_file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("to_symbol_id", Integer, ForeignKey("symbols.id", ondelete="SET NULL")),
    Column("target_name", Text, nullable=False, comment="Name as written in the code"),
    Column("qualifier", Text, comment="Receiver as written: this, base, _calc, ns, Type"),
    Column("qualifier_type", Text, comment="Declared type of the receiver, if known"),
    Column(
        "kind",
        String(20),
        nullable=False,
        comment="call|instantiate|type_ref|inherits|implements|http_call",
    ),
    Column("line", Integer),
    Column("col", Integer),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Column("resolver", String(20), comment="local|import|heuristic|name|scip|lsp"),
    Index(None, "to_symbol_id"),
    Index(None, "from_symbol_id"),
    Index(None, "from_file_id"),
    Index(None, "target_name"),
)

symbol_edges = Table(
    "symbol_edges",
    metadata,
    Column("src_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True),
    Column("dst_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True),
    Column("kind", String(20), primary_key=True, comment="inherits|implements|overrides|tests"),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Index(None, "dst_id"),
)

imports = Table(
    "imports",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("module", Text, nullable=False),
    Column("alias", Text),
    Column("imported_name", Text),
    Column("resolved_file_id", Integer, ForeignKey("files.id", ondelete="SET NULL")),
    Column("line", Integer),
    Column(
        "kind",
        String(20),
        nullable=False,
        server_default="import",
        comment="import|reexport|using|using_static|global_using",
    ),
    Index(None, "file_id"),
    Index(None, "resolved_file_id"),
)

endpoints = Table(
    "endpoints",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("symbol_id", Integer, ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(10), nullable=False, comment="server|client"),
    Column("http_method", String(10)),
    Column("route_pattern", Text, nullable=False, comment="Normalized, e.g. api/invoices/{}"),
    Column("line", Integer),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Index(None, "route_pattern"),
    Index(None, "symbol_id"),
)

# --- External documentation ----------------------------------------------------

documents = Table(
    "documents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "workspace_id",
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("repo_id", Integer, ForeignKey("repositories.id", ondelete="CASCADE")),
    Column("source_type", String(20), nullable=False, comment="pdf|url|markdown|html|openapi"),
    Column("uri", Text, nullable=False),
    Column("title", Text),
    Column("content_hash", String(64)),
    Column("fetched_at", DateTime(timezone=True)),
    Column("metadata", JSON),
)

doc_chunks = Table(
    "doc_chunks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("document_id", Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("heading_path", Text),
    Column("locator", Text, comment="Page number or anchor"),
    Column("text", Text, nullable=False),
    Column("token_count", Integer),
    Index(None, "document_id"),
)

doc_links = Table(
    "doc_links",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("chunk_id", Integer, ForeignKey("doc_chunks.id", ondelete="CASCADE"), nullable=False),
    Column("target_type", String(20), nullable=False, comment="symbol|file|directory"),
    Column("target_key", Text, nullable=False, comment="symbol stable_key or repo:path"),
    Column("link_source", String(20), comment="manual|name_match|semantic"),
    Column("confidence", Float, nullable=False, server_default="1.0"),
    Index(None, "chunk_id"),
    Index(None, "target_key"),
)

# --- Derived data: embeddings and LLM summaries -------------------------------

embeddings = Table(
    "embeddings",
    metadata,
    Column("owner_type", String(20), primary_key=True, comment="symbol|file|doc_chunk"),
    Column("owner_id", Integer, primary_key=True),
    Column("model", String(200), primary_key=True),
    Column("dim", Integer, nullable=False),
    Column("vector", LargeBinary, nullable=False, comment="float32 array"),
    Column("text_hash", String(64)),
)

# Content-addressed: the same text is embedded once per model, no matter how often
# symbols are re-indexed or on which branch/machine (CONCEPT.md section 10).
embedding_cache = Table(
    "embedding_cache",
    metadata,
    Column("model", String(200), primary_key=True),
    Column("text_hash", String(64), primary_key=True),
    Column("dim", Integer, nullable=False),
    Column("vector", LargeBinary, nullable=False, comment="float32, L2-normalized"),
    _created_at(),
)

summary_cache = Table(
    "summary_cache",
    metadata,
    Column("key", String(64), primary_key=True, comment="hash(body_hash + model + prompt_version)"),
    Column("summary", Text, nullable=False),
    Column("keywords", JSON),
    Column("model", String(200)),
    _created_at(),
)

# --- Knowledge added by people or agents --------------------------------------

notes = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "workspace_id",
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("target_type", String(20), comment="symbol|file|directory|repo"),
    Column("target_key", Text, comment="symbol stable_key or repo:path"),
    Column("author", Text),
    Column("text", Text, nullable=False),
    _created_at(),
    Index(None, "target_key"),
)
