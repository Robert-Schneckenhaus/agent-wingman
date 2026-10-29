"""Incremental indexing of a repository: scan, extract, store, resolve."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine, case, delete, func, insert, select, update

from wingman import embeddings, git
from wingman import endpoints as http
from wingman.extract import content_hash, extract
from wingman.extract.base import short_hash
from wingman.extract.model import Extraction
from wingman.resolver import Changes, resolve_workspace
from wingman.scanner import (
    SourceFile,
    git_changed_since,
    git_dirty,
    is_generated,
    is_test_path,
    rules_hash,
    scan,
    scan_paths,
)
from wingman.storage.schema import (
    endpoints,
    files,
    imports,
    index_runs,
    refs,
    repositories,
    symbols,
)
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
    http_calls: int = 0
    """Client calls matched to server routes in the whole workspace (when re-matched)."""
    mode: str = "scan"
    """How changed files were found: scan (all files hashed), git, or paths."""
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
    paths: list[str] | None = None,
    progress: ProgressCallback | None = None,
) -> IndexStats:
    """Index ``repo``; with ``paths`` (repository-relative) only those files are
    checked: re-parsed if changed, removed from the index if gone or ignored.

    Without ``paths`` in a git repository, git tells which files can have changed since
    the last run (see ``_git_candidates``); other repositories are scanned and every
    file's content hash is compared."""
    root = Path(repo.root_path)
    if not root.is_dir():
        raise IndexingError(f"Repository path does not exist: {root}")

    started = time.monotonic()
    stats = IndexStats()
    with engine.connect() as conn:
        state = (
            conn.execute(
                select(repositories.c.index_state).where(repositories.c.id == repo.id)
            ).scalar_one()
            or {}
        )
        first_index = (
            conn.execute(select(files.c.id).where(files.c.repo_id == repo.id).limit(1)).first()
            is None
        )
    rules = rules_hash(root)
    if paths is not None:  # single files (e.g. the edit hook): last_commit stays
        stats.mode = "paths"
        commit = repo.last_commit
        new_state = {**state, "dirty": sorted(set(state.get("dirty", [])) | set(paths))}
    else:
        commit = git.head_commit(root)
        dirty = git_dirty(root) if commit else None
        new_state = {"rules": rules, "dirty": sorted(dirty or [])}
        if not full and not first_index and dirty is not None and state.get("rules") == rules:
            paths = _git_candidates(root, repo.last_commit, dirty, state)
            stats.mode = "git" if paths is not None else "scan"
    sources = scan(root) if paths is None else scan_paths(root, paths)
    stats.files_scanned = len(sources)

    with engine.begin() as conn:
        backend = backend_for(conn)
        run_id = conn.execute(
            insert(index_runs).values(repo_id=repo.id, status="running", commit_sha=commit)
        ).inserted_primary_key[0]

        existing_query = select(files.c.id, files.c.path, files.c.content_hash).where(
            files.c.repo_id == repo.id
        )
        if paths is not None:
            requested = [Path(p).as_posix().removeprefix("./") for p in paths]
            existing_query = existing_query.where(files.c.path.in_(requested))
        existing = {row.path: (row.id, row.content_hash) for row in conn.execute(existing_query)}
        changed_files: set[int] = set()
        changed_names: set[str] = set()
        orphaned_imports: set[int] = set()
        new_paths = False

        for number, source in enumerate(sources, 1):
            try:
                data = source.abs_path.read_bytes()
            except OSError as exc:
                stats.errors.append(f"{source.path}: {exc}")
                continue
            file_hash = content_hash(data)
            previous = existing.get(source.path)
            if previous is not None and previous[1] == file_hash and not full:
                stats.files_unchanged += 1
                continue
            if previous is not None:
                changed_names |= _symbol_names(conn, [previous[0]])
                orphaned_imports |= _imports_into(conn, [previous[0]])
                _delete_files(conn, backend, [previous[0]])
            else:
                new_paths = True
            try:
                extraction = extract(source.path, data)
            except Exception as exc:  # a broken file must not stop the run
                stats.errors.append(f"{source.path}: extraction failed: {exc}")
                extraction = Extraction()
            counts = _store_file(conn, backend, repo, source, data, file_hash, extraction)
            changed_files.add(counts[2])
            changed_names.update(sym.name for sym in extraction.symbols)
            stats.files_indexed += 1
            stats.symbols += counts[0]
            stats.refs += counts[1]
            if progress and number % 200 == 0:
                progress(f"{repo.name}: {number}/{len(sources)} files")

        scanned = {s.path for s in sources}
        removed = [file_id for path, (file_id, _) in existing.items() if path not in scanned]
        changed_names |= _symbol_names(conn, removed)
        orphaned_imports |= _imports_into(conn, removed)
        _delete_files(conn, backend, removed)
        stats.files_removed = len(removed)

        if stats.files_indexed or stats.files_removed:
            if progress and len(sources) > 200:
                progress(f"{repo.name}: resolving references")
            workspace_id = conn.execute(
                select(repositories.c.workspace_id).where(repositories.c.id == repo.id)
            ).scalar_one()
            http.clear_http_refs(conn, workspace_id)  # rebuilt below, never "stale"
            if full or first_index:
                # first index: other repositories may now resolve into it; --full:
                # everything; references elsewhere into deleted symbols lost their target
                scope = {repo.id} | _repos_with_stale_refs(conn, workspace_id)
                if first_index:
                    scope |= _workspace_repos(conn, workspace_id)
                changes = None
            else:  # only what the changed files can affect
                scope = {repo.id}
                changes = Changes(changed_files, changed_names, orphaned_imports, new_paths)
            stats.edges = resolve_workspace(conn, workspace_id, scope, changes).edges
            stats.http_calls = http.match_endpoints(conn, workspace_id)
        stats.refs, stats.refs_resolved = _ref_counts(conn, repo.id)
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
            .values(last_commit=commit, updated_at=now, index_state=new_state)
        )
    return stats


def _git_candidates(
    root: Path, last_commit: str | None, dirty: list[str], state: dict
) -> list[str] | None:
    """Files that can differ from the index: changed in commits since the last run,
    different from HEAD now, or different from the then-HEAD at the last run (their
    indexed content is not the committed one). Everything else is unchanged, so its
    content does not need to be read. None if git cannot tell."""
    if not last_commit:
        return None
    committed = git_changed_since(root, last_commit)
    if committed is None:  # unknown commit, e.g. after a rebase or in a shallow clone
        return None
    return sorted(set(committed) | set(dirty) | set(state.get("dirty", [])))


def _workspace_repos(conn: Connection, workspace_id: int) -> set[int]:
    return set(
        conn.execute(
            select(repositories.c.id).where(repositories.c.workspace_id == workspace_id)
        ).scalars()
    )


def _repos_with_stale_refs(conn: Connection, workspace_id: int) -> set[int]:
    """Repositories with references that were resolved, but whose target was deleted."""
    return set(
        conn.execute(
            select(files.c.repo_id)
            .distinct()
            .join(refs, refs.c.from_file_id == files.c.id)
            .join(repositories, repositories.c.id == files.c.repo_id)
            .where(
                repositories.c.workspace_id == workspace_id,
                refs.c.to_symbol_id.is_(None),
                refs.c.resolver.is_not(None),
            )
        ).scalars()
    )


def _ref_counts(conn: Connection, repo_id: int) -> tuple[int, int]:
    row = conn.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((refs.c.to_symbol_id.is_not(None), 1), else_=0)), 0),
        )
        .select_from(refs)
        .join(files, files.c.id == refs.c.from_file_id)
        .where(files.c.repo_id == repo_id)
    ).one()
    return row[0], row[1]


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
    members: dict[int, list] = defaultdict(list)
    for sym in extraction.symbols:
        if sym.parent is not None:
            members[sym.parent].append(sym)
    for index, sym in enumerate(extraction.symbols):
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
                embed_hash=_embed_hash(sym, source.path, data, members.get(index, [])),
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
    if extraction.endpoints:
        conn.execute(
            insert(endpoints),
            [
                {
                    "symbol_id": ids[e.symbol],
                    "role": e.role,
                    "http_method": e.method,
                    "route_pattern": e.route,
                    "line": e.line,
                }
                for e in extraction.endpoints
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
    return len(ids), len(extraction.refs), file_id


def _symbol_names(conn: Connection, file_ids: list[int]) -> set[str]:
    names: set[str] = set()
    for start in range(0, len(file_ids), 500):
        names.update(
            conn.execute(
                select(symbols.c.name).where(symbols.c.file_id.in_(file_ids[start : start + 500]))
            ).scalars()
        )
    return names


def _imports_into(conn: Connection, file_ids: list[int]) -> set[int]:
    """Imports (anywhere) that resolve to the given files."""
    found: set[int] = set()
    for start in range(0, len(file_ids), 500):
        found.update(
            conn.execute(
                select(imports.c.id).where(
                    imports.c.resolved_file_id.in_(file_ids[start : start + 500])
                )
            ).scalars()
        )
    return found


def _embed_hash(sym, path: str, data: bytes, members: list) -> str | None:
    if sym.kind not in embeddings.EMBED_KINDS:
        return None
    text = embeddings.embedding_text(
        kind=sym.kind,
        qualified_name=sym.qualified_name,
        signature=sym.signature,
        doc=sym.doc,
        path=path,
        body=embeddings.body_text(data, sym.start_byte, sym.end_byte),
        members=[m.name for m in sorted(members, key=lambda m: m.start_byte)],
    )
    return embeddings.text_hash(text)


def _first_sentence(doc: str | None, limit: int = 200) -> str | None:
    if not doc:
        return None
    first = doc.strip().split("\n\n", 1)[0].replace("\n", " ")
    end = first.find(". ")
    if end != -1:
        first = first[: end + 1]
    return first if len(first) <= limit else first[: limit - 1] + "…"
