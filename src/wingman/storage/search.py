"""Full-text search over symbols, behind a small backend interface (CONCEPT.md 4.6).

SQLite uses an FTS5 table (``symbols_fts``, created by migration) with BM25 ranking.
Other databases fall back to a portable LIKE-based search until a dedicated
backend (e.g. PostgreSQL tsvector) exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Connection, bindparam, func, or_, select, text

from wingman.storage.schema import symbols

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORD = re.compile(r"[A-Za-z0-9_$]+")


def identifier_words(identifier: str) -> list[str]:
    """``renderTaxLines`` -> ``[render, tax, lines]``, ``HTTPServer`` -> ``[http, server]``."""
    words: list[str] = []
    for part in re.split(r"[_$\W]+", identifier):
        words.extend(w.lower() for w in _CAMEL.findall(part))
    return words


def searchable(text_value: str | None) -> str:
    """Original text plus split identifier words, so ``InvoiceService`` matches
    both ``invoiceservice`` and ``invoice service``."""
    if not text_value:
        return ""
    extra: list[str] = []
    for token in _WORD.findall(text_value):
        words = identifier_words(token)
        if len(words) > 1:
            extra.extend(words)
    return f"{text_value} {' '.join(extra)}" if extra else text_value


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in _WORD.findall(query):
        lowered = token.lower()
        if lowered not in terms:
            terms.append(lowered)
        for word in identifier_words(token):
            # fragments like "st" from "MwSt" would prefix-match half of the index
            if len(word) >= 3 and word not in terms:
                terms.append(word)
    return terms


@dataclass(frozen=True)
class FtsRow:
    symbol_id: int
    name: str
    qualified_name: str
    signature: str | None
    doc: str | None
    path: str


class SearchBackend(Protocol):
    def index(self, conn: Connection, rows: Sequence[FtsRow]) -> None: ...

    def delete_for_files(self, conn: Connection, file_ids: Iterable[int]) -> None: ...

    def replace(self, conn: Connection, rows: Sequence[FtsRow]) -> None:
        """Re-index existing symbols (e.g. after a summary was added)."""
        ...

    def purge_orphans(self, conn: Connection) -> None: ...

    def search(self, conn: Connection, query: str, limit: int) -> list[tuple[int, float]]:
        """Return ``(symbol_id, score)`` pairs, best first; higher score is better."""
        ...


class SqliteFtsBackend:
    WEIGHTS = (10.0, 6.0, 2.0, 1.0, 1.5)  # name, qualified_name, signature, doc, path

    def index(self, conn: Connection, rows: Sequence[FtsRow]) -> None:
        if not rows:
            return
        conn.execute(
            text(
                "INSERT INTO symbols_fts(rowid, name, qualified_name, signature, doc, path) "
                "VALUES (:id, :name, :qualified_name, :signature, :doc, :path)"
            ),
            [
                {
                    "id": r.symbol_id,
                    "name": searchable(r.name),
                    "qualified_name": searchable(r.qualified_name),
                    "signature": searchable(r.signature),
                    "doc": r.doc or "",
                    "path": searchable(r.path),
                }
                for r in rows
            ],
        )

    def replace(self, conn: Connection, rows: Sequence[FtsRow]) -> None:
        if not rows:
            return
        conn.execute(
            text("DELETE FROM symbols_fts WHERE rowid IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": [r.symbol_id for r in rows]},
        )
        self.index(conn, rows)

    def delete_for_files(self, conn: Connection, file_ids: Iterable[int]) -> None:
        ids = list(file_ids)
        if not ids:
            return
        conn.execute(
            text(
                "DELETE FROM symbols_fts WHERE rowid IN "
                "(SELECT id FROM symbols WHERE file_id IN :file_ids)"
            ).bindparams(bindparam("file_ids", expanding=True)),
            {"file_ids": ids},
        )

    def purge_orphans(self, conn: Connection) -> None:
        conn.execute(text("DELETE FROM symbols_fts WHERE rowid NOT IN (SELECT id FROM symbols)"))

    def search(self, conn: Connection, query: str, limit: int) -> list[tuple[int, float]]:
        terms = query_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"*' for t in terms)
        weights = ", ".join(str(w) for w in self.WEIGHTS)
        rows = conn.execute(
            text(
                f"SELECT rowid, bm25(symbols_fts, {weights}) AS rank FROM symbols_fts "
                "WHERE symbols_fts MATCH :match ORDER BY rank LIMIT :limit"
            ),
            {"match": match, "limit": limit},
        )
        return [(row.rowid, -row.rank) for row in rows]


class LikeBackend:
    """Portable fallback: counts matching terms in name, qualified name, doc and summary."""

    def index(self, conn: Connection, rows: Sequence[FtsRow]) -> None:
        pass

    def delete_for_files(self, conn: Connection, file_ids: Iterable[int]) -> None:
        pass

    def replace(self, conn: Connection, rows: Sequence[FtsRow]) -> None:
        pass

    def purge_orphans(self, conn: Connection) -> None:
        pass

    def search(self, conn: Connection, query: str, limit: int) -> list[tuple[int, float]]:
        terms = query_terms(query)
        if not terms:
            return []
        conditions = []
        for term in terms:
            pattern = f"%{term}%"
            conditions.append(
                or_(
                    func.lower(symbols.c.qualified_name).like(pattern),
                    func.lower(symbols.c.doc).like(pattern),
                    func.lower(symbols.c.summary).like(pattern),
                )
            )
        rows = conn.execute(
            select(
                symbols.c.id,
                symbols.c.name,
                symbols.c.qualified_name,
                symbols.c.doc,
                symbols.c.summary,
            )
            .where(or_(*conditions))
            .limit(limit * 5)
        )
        scored = []
        for row in rows:
            haystack = f"{row.qualified_name} {row.doc or row.summary or ''}".lower()
            score = sum(3.0 if t in row.name.lower() else 1.0 for t in terms if t in haystack)
            scored.append((row.id, score))
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]


def backend_for(conn: Connection) -> SearchBackend:
    return SqliteFtsBackend() if conn.dialect.name == "sqlite" else LikeBackend()
