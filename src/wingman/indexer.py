"""Incremental indexing of a repository: scan, extract, store, resolve."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine, delete, insert, select, update

from wingman import git
from wingman.extract import extract
from wingman.extract.base import short_hash
from wingman.extract.model import Extraction
from wingman.resolver import resolve_repository
from wingman.scanner import SourceFile, is_generated, is_test_path, scan
from wingman.storage.schema import files, imports, index_runs, refs, repositories, symbols
from wingman.storage.search import FtsRow, SearchBackend, backend_for
from wingman.storage.store import RepoInfo


class IndexingError(Exception):
    pass


@dataclass
class IndexStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    symbols: int = 0
    refs: int = 0
    refs_resolved: int = 0
    edges: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "errors"} | {
            "errors": len(self.errors)
        }


ProgressCallback = Callable[[str], None]


def stable_key(repo: str, path: str, qualified_name: str, kind: str) -> str:
    return f"{repo}:{path}#{qualified_name}:{kind}"


def index_repository(
    engine: Engine,
    repo: RepoInfo,
    *,
    full: bool = False,
    progress: ProgressCallback | None = None,
) -> IndexStats:
    root = Path(repo.root_path)
    if not root.is_dir():
        raise IndexingError(f"Repository path does not exist: {root}")

    started = time.monotonic()
    stats = IndexStats()
    sources = scan(root)
    stats.files_scanned = len(sources)
    commit = git.head_commit(root)

    with engine.begin() as conn:
        backend = backend_for(conn)
        run_id = conn.execute(
            insert(index_runs).values(repo_id=repo.id, status="running", commit_sha=commit)
        ).inserted_primary_key[0]

        existing = {
            row.path: (row.id, row.content_hash)
            for row in conn.execute(
                select(files.c.id, files.c.path, files.c.content_hash).where(
                    files.c.repo_id == repo.id
                )
            )
        }

        for number, source in enumerate(sources, 1):
            try:
                data = source.abs_path.read_bytes()
            except OSError as exc:
                stats.errors.append(f"{source.path}: {exc}")
                continue
            content_hash = hashlib.sha256(data).hexdigest()
            previous = existing.get(source.path)
            if previous is not None and previous[1] == content_hash and not full:
                stats.files_unchanged += 1
                continue
            if previous is not None:
                _delete_files(conn, backend, [previous[0]])
            try:
                extraction = extract(source.path, data)
            except Exception as exc:  # a broken file must not stop the run
                stats.errors.append(f"{source.path}: extraction failed: {exc}")
                extraction = Extraction()
            counts = _store_file(conn, backend, repo, source, data, content_hash, extraction)
            stats.files_indexed += 1
            stats.symbols += counts[0]
            stats.refs += counts[1]
            if progress and number % 200 == 0:
                progress(f"{repo.name}: {number}/{len(sources)} files")

        scanned = {s.path for s in sources}
        removed = [file_id for path, (file_id, _) in existing.items() if path not in scanned]
        _delete_files(conn, backend, removed)
        stats.files_removed = len(removed)

        if progress and len(sources) > 200:
            progress(f"{repo.name}: resolving references")
        resolved = resolve_repository(conn, repo.id, root)
        stats.refs_resolved = resolved.resolved
        stats.refs = resolved.refs
        stats.edges = resolved.edges
        stats.seconds = round(time.monotonic() - started, 2)

        now = datetime.now(UTC)
        conn.execute(
            update(index_runs)
            .where(index_runs.c.id == run_id)
            .values(
                finished_at=now,
                status="succeeded",
                files_changed=stats.files_indexed + stats.files_removed,
                stats=stats.as_dict(),
            )
        )
        conn.execute(
            update(repositories)
            .where(repositories.c.id == repo.id)
            .values(last_commit=commit, updated_at=now)
        )
    return stats


def _delete_files(conn: Connection, backend: SearchBackend, file_ids: list[int]) -> None:
    if not file_ids:
        return
    backend.delete_for_files(conn, file_ids)
    conn.execute(delete(files).where(files.c.id.in_(file_ids)))


def _store_file(
    conn: Connection,
    backend: SearchBackend,
    repo: RepoInfo,
    source: SourceFile,
    data: bytes,
    content_hash: str,
    extraction: Extraction,
) -> tuple[int, int]:
    stat = source.abs_path.stat()
    file_id = conn.execute(
        insert(files).values(
            repo_id=repo.id,
            path=source.path,
            language=source.language,
            content_hash=content_hash,
            size_bytes=len(data),
            line_count=data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1),
            is_test=is_test_path(source.path),
            is_generated=is_generated(source.path, data),
            last_modified=datetime.fromtimestamp(stat.st_mtime, UTC),
            namespaces=extraction.namespaces or None,
        )
    ).inserted_primary_key[0]

    ids: list[int] = []
    keys: set[str] = set()
    fts_rows: list[FtsRow] = []
    for sym in extraction.symbols:
        key = stable_key(repo.name, source.path, sym.qualified_name, sym.kind)
        if key in keys:  # overloads / duplicate declarations
            key = f"{key}~{short_hash((sym.signature or '').encode())[:8]}"
            suffix = 2
            while key in keys:
                key = f"{key.rsplit('#', 1)[0]}#{sym.qualified_name}:{sym.kind}~{suffix}"
                suffix += 1
        keys.add(key)
        symbol_id = conn.execute(
            insert(symbols).values(
                file_id=file_id,
                parent_id=ids[sym.parent] if sym.parent is not None else None,
                kind=sym.kind,
                name=sym.name,
                qualified_name=sym.qualified_name,
                signature=sym.signature,
                type_name=sym.type_name,
                visibility=sym.visibility,
                modifiers=sym.modifiers or None,
                decorators=sym.decorators or None,
                start_line=sym.start_line,
                end_line=sym.end_line,
                start_byte=sym.start_byte,
                end_byte=sym.end_byte,
                body_hash=sym.body_hash,
                doc=sym.doc,
                summary=_first_sentence(sym.doc),
                summary_source="docstring" if sym.doc else None,
                stable_key=key,
            )
        ).inserted_primary_key[0]
        ids.append(symbol_id)
        fts_rows.append(
            FtsRow(symbol_id, sym.name, sym.qualified_name, sym.signature, sym.doc, source.path)
        )
    backend.index(conn, fts_rows)

    if extraction.refs:
        conn.execute(
            insert(refs),
            [
                {
                    "from_symbol_id": ids[r.from_symbol] if r.from_symbol is not None else None,
                    "from_file_id": file_id,
                    "to_symbol_id": None,
                    "target_name": r.target_name,
                    "qualifier": r.qualifier,
                    "qualifier_type": r.qualifier_type,
                    "kind": r.kind,
                    "line": r.line,
                    "col": r.col,
                    "confidence": 0.0,
                    "resolver": None,
                }
                for r in extraction.refs
            ],
        )
    if extraction.imports:
        conn.execute(
            insert(imports),
            [
                {
                    "file_id": file_id,
                    "module": i.module,
                    "alias": i.alias,
                    "imported_name": i.imported_name,
                    "line": i.line,
                    "kind": i.kind,
                    "resolved_file_id": None,
                }
                for i in extraction.imports
            ],
        )
    return len(ids), len(extraction.refs)


def _first_sentence(doc: str | None, limit: int = 200) -> str | None:
    if not doc:
        return None
    first = doc.strip().split("\n\n", 1)[0].replace("\n", " ")
    end = first.find(". ")
    if end != -1:
        first = first[: end + 1]
    return first if len(first) <= limit else first[: limit - 1] + "…"
