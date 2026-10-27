"""Semantic search with embeddings (CONCEPT.md 3.6, 4.5).

Each symbol gets an *embedding text* (kind, name split into words, signature, doc,
path and the beginning of its body). The indexer stores only a hash of that text in
``symbols.embed_hash``; vectors live in ``embedding_cache`` keyed by
``(model, text_hash)``. Unchanged code is therefore never embedded twice, also not
after re-indexing, on another branch or (later) on another machine.

Embedding runs separately from parsing (``wingman embed``, at the end of
``wingman index``, or lazily with a time budget before a semantic query), because it
is much slower than parsing and needs an optional model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
from sqlalchemy import Connection, Engine, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from wingman.config import EmbeddingConfig
from wingman.extract import content_hash
from wingman.storage.schema import embedding_cache, files, repositories, symbols
from wingman.storage.search import identifier_words

# Symbols that get an embedding; members such as fields and properties are found via
# their class, which lists member names in its embedding text.
EMBED_KINDS = frozenset(
    {
        "class",
        "interface",
        "struct",
        "record",
        "enum",
        "type_alias",
        "delegate",
        "function",
        "method",
        "constructor",
        "component",
        "test",
        "test_suite",
    }
)
TYPE_KINDS = frozenset({"class", "interface", "struct", "record", "enum"})
MAX_BODY_CHARS = 1200
MAX_MEMBERS = 40
MAX_TEXT_CHARS = 2000  # ~500 tokens, the input limit of small embedding models
DEFAULT_CACHE_DIR = Path("~/.cache/wingman/models").expanduser()


class EmbeddingError(Exception):
    pass


# --- embedding texts ------------------------------------------------------------------


def embedding_text(
    *,
    kind: str,
    qualified_name: str,
    signature: str | None,
    doc: str | None,
    path: str,
    body: str,
    members: Sequence[str] = (),
) -> str:
    """The text that represents a symbol for the embedding model."""
    name = qualified_name.rsplit(".", 1)[-1]
    parts = [f"{kind} {qualified_name}", " ".join(identifier_words(name))]
    if signature:
        parts.append(signature)
    if doc:
        parts.append(doc)
    parts.append(f"file {path}")
    if kind in TYPE_KINDS:
        if members:
            parts.append("members: " + ", ".join(members[:MAX_MEMBERS]))
    elif body:
        parts.append(" ".join(body.split())[:MAX_BODY_CHARS])
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def body_text(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("utf-8", errors="replace")


# --- providers ----------------------------------------------------------------------


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """``(len(texts), dim)`` float32, L2-normalized."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """``(dim,)`` float32, L2-normalized."""
        ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


class FastEmbedEmbedder:
    """Local ONNX models via ``fastembed`` (no PyTorch). Downloads the model on first use."""

    def __init__(self, model: str, cache_dir: Path | None = None) -> None:
        self.model = model
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._impl = None

    def _model(self):
        if self._impl is None:
            from fastembed import TextEmbedding

            try:
                self._impl = TextEmbedding(model_name=self.model, cache_dir=str(self.cache_dir))
            except Exception as exc:
                raise EmbeddingError(
                    f"Could not load embedding model '{self.model}': {exc}. The model is "
                    "downloaded from Hugging Face on first use; check network access or "
                    "point embeddings.cache_dir to a pre-downloaded copy."
                ) from exc
        return self._impl

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return _normalize(np.array(list(self._model().embed(list(texts), batch_size=32))))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalize(np.array(list(self._model().query_embed([text]))))[0]


def provider_available() -> bool:
    return importlib.util.find_spec("fastembed") is not None


def create_embedder(config: EmbeddingConfig) -> Embedder | None:
    """The configured embedder, or None if embeddings are off (or "auto" and the
    provider is not installed)."""
    if config.enabled is False:
        return None
    if not provider_available():
        if config.enabled:
            raise EmbeddingError(
                "Embeddings are enabled but no provider is installed: "
                "pip install 'agent-wingman[embeddings]'"
            )
        return None
    return FastEmbedEmbedder(config.model, config.cache_dir)


# --- computing missing embeddings -----------------------------------------------------


@dataclass
class EmbedStats:
    pending: int = 0
    """Texts without a vector before this run."""
    embedded: int = 0
    skipped: int = 0
    """Symbols whose file changed since indexing (re-index first)."""
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def _missing(conn: Connection, workspace_id: int, model: str):
    cached = select(embedding_cache.c.text_hash).where(embedding_cache.c.model == model)
    return conn.execute(
        select(
            symbols.c.id,
            symbols.c.kind,
            symbols.c.qualified_name,
            symbols.c.signature,
            symbols.c.doc,
            symbols.c.start_byte,
            symbols.c.end_byte,
            symbols.c.embed_hash,
            files.c.id.label("file_id"),
            files.c.path,
            files.c.content_hash,
            repositories.c.root_path,
        )
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(
            repositories.c.workspace_id == workspace_id,
            symbols.c.embed_hash.is_not(None),
            symbols.c.embed_hash.not_in(cached),
        )
        .order_by(files.c.id, symbols.c.start_byte)
    ).all()


def _member_names(conn: Connection, parent_ids: list[int]) -> dict[int, list[str]]:
    members: dict[int, list[str]] = defaultdict(list)
    for start in range(0, len(parent_ids), 500):
        rows = conn.execute(
            select(symbols.c.parent_id, symbols.c.name)
            .where(symbols.c.parent_id.in_(parent_ids[start : start + 500]))
            .order_by(symbols.c.parent_id, symbols.c.start_byte)
        )
        for row in rows:
            members[row.parent_id].append(row.name)
    return members


def _store_vectors(conn: Connection, model: str, items: list[tuple[str, np.ndarray]]) -> None:
    rows = [
        {"model": model, "text_hash": h, "dim": int(v.shape[0]), "vector": v.tobytes()}
        for h, v in items
    ]
    if conn.dialect.name == "postgresql":
        statement = pg_insert(embedding_cache).on_conflict_do_nothing()
    else:  # SQLite: another process may have stored the same text meanwhile
        statement = insert(embedding_cache).prefix_with("OR IGNORE")
    conn.execute(statement, rows)


def ensure_embeddings(
    engine: Engine,
    embedder: Embedder,
    workspace_id: int,
    *,
    max_texts: int | None = None,
    max_seconds: float | None = None,
    batch_size: int = 64,
    progress: Callable[[str], None] | None = None,
) -> EmbedStats:
    """Embed symbols that have no vector for ``embedder.model`` yet."""
    started = time.monotonic()
    stats = EmbedStats()
    with engine.connect() as conn:
        rows = _missing(conn, workspace_id, embedder.model)
        type_ids = [r.id for r in rows if r.kind in TYPE_KINDS]
        members = _member_names(conn, type_ids) if type_ids else {}

    texts: dict[str, str] = {}
    by_file: dict[int, list] = defaultdict(list)
    for row in rows:
        by_file[row.file_id].append(row)
    for file_rows in by_file.values():
        first = file_rows[0]
        try:
            data = (Path(first.root_path) / first.path).read_bytes()
        except OSError:
            stats.skipped += len(file_rows)
            continue
        if content_hash(data) != first.content_hash:
            stats.skipped += len(file_rows)
            continue
        for row in file_rows:
            if row.embed_hash in texts:
                continue
            text = embedding_text(
                kind=row.kind,
                qualified_name=row.qualified_name,
                signature=row.signature,
                doc=row.doc,
                path=row.path,
                body=body_text(data, row.start_byte, row.end_byte),
                members=members.get(row.id, ()),
            )
            if text_hash(text) == row.embed_hash:
                texts[row.embed_hash] = text
            else:
                stats.skipped += 1

    stats.pending = len(texts)
    todo = list(texts.items())
    if max_texts is not None:
        todo = todo[:max_texts]
    for start in range(0, len(todo), batch_size):
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            break
        batch = todo[start : start + batch_size]
        vectors = embedder.embed_documents([text for _, text in batch])
        with engine.begin() as conn:
            _store_vectors(
                conn, embedder.model, [(h, vectors[i]) for i, (h, _) in enumerate(batch)]
            )
        stats.embedded += len(batch)
        if progress and len(todo) > batch_size:
            progress(f"embedded {stats.embedded}/{len(todo)} symbols")
    stats.seconds = round(time.monotonic() - started, 2)
    return stats


# --- searching ----------------------------------------------------------------------

_VECTORS: dict[tuple, tuple[tuple, np.ndarray, np.ndarray]] = {}


def _vector_query(workspace_id: int, model: str):
    return (
        select(symbols.c.id, embedding_cache.c.vector)
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .join(
            embedding_cache,
            (embedding_cache.c.text_hash == symbols.c.embed_hash)
            & (embedding_cache.c.model == model),
        )
        .where(repositories.c.workspace_id == workspace_id)
    )


def load_vectors(conn: Connection, workspace_id: int, model: str) -> tuple[np.ndarray, np.ndarray]:
    """``(symbol_ids, matrix)`` of all embedded symbols; cached while unchanged."""
    subquery = _vector_query(workspace_id, model).subquery()
    signature = tuple(
        conn.execute(select(func.count(), func.max(subquery.c.id), func.sum(subquery.c.id))).one()
    )
    key = (str(conn.engine.url), workspace_id, model)
    cached = _VECTORS.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    rows = conn.execute(_vector_query(workspace_id, model)).all()
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
    ids = np.array([r.id for r in rows], dtype=np.int64)
    matrix = np.vstack([np.frombuffer(r.vector, dtype=np.float32) for r in rows])
    _VECTORS[key] = (signature, ids, matrix)
    return ids, matrix


def semantic_search(
    engine: Engine,
    embedder: Embedder,
    workspace_id: int,
    text: str,
    *,
    limit: int = 100,
    prepare_seconds: float | None = 10.0,
) -> list[tuple[int, float]]:
    """Symbols most similar to ``text`` as ``(symbol_id, cosine similarity)``.

    With ``prepare_seconds`` missing embeddings are computed first, within that time
    budget (the model is loaded anyway for the query).
    """
    if prepare_seconds:
        ensure_embeddings(engine, embedder, workspace_id, max_seconds=prepare_seconds)
    with engine.connect() as conn:
        ids, matrix = load_vectors(conn, workspace_id, embedder.model)
    if ids.size == 0:
        return []
    query = embedder.embed_query(text)
    similarities = matrix @ query
    count = min(limit, ids.size)
    top = np.argpartition(-similarities, count - 1)[:count]
    top = top[np.argsort(-similarities[top])]
    return [(int(ids[i]), float(similarities[i])) for i in top]


def coverage(conn: Connection, workspace_id: int, model: str) -> tuple[int, int]:
    """``(embedded, embeddable)`` symbol counts."""
    embeddable = conn.execute(
        select(func.count())
        .select_from(symbols)
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(repositories.c.workspace_id == workspace_id, symbols.c.embed_hash.is_not(None))
    ).scalar_one()
    embedded = conn.execute(
        select(func.count()).select_from(_vector_query(workspace_id, model).subquery())
    ).scalar_one()
    return embedded, embeddable
