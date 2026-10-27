"""Notes: knowledge attached to symbols, files or directories by people or agents.

Targets are stored as stable keys (symbol ``stable_key`` or ``repo:path``) so notes
survive re-indexing and can later be shared (CONCEPT.md section 10).
"""

from __future__ import annotations

from sqlalchemy import Connection, select

from wingman import query
from wingman.storage import store
from wingman.storage.schema import repositories


class TargetError(Exception):
    pass


def resolve_target(conn: Connection, workspace_id: int, target: str) -> tuple[str, str, str]:
    """Return ``(target_type, target_key, label)`` for a symbol name/#id or ``repo:path``."""
    repo_name, sep, path = target.partition(":")
    if sep and repo_name:
        exists = conn.execute(
            select(repositories.c.id).where(
                repositories.c.workspace_id == workspace_id, repositories.c.name == repo_name
            )
        ).first()
        if exists is not None:
            path = path.strip("/")
            matches = query.find_files(conn, workspace_id, f"{repo_name}:{path}")
            kind = "file" if matches else ("repo" if not path else "directory")
            return kind, f"{repo_name}:{path}", f"{repo_name}:{path}"

    lookup = query.resolve_name(conn, workspace_id, target)
    if lookup.ambiguous:
        raise TargetError(
            f"'{target}' is ambiguous; use a qualified name or #id "
            f"(e.g. #{lookup.found[0].id} for {lookup.found[0].qualified_name})."
        )
    if lookup.found:
        hit = lookup.found[0]
        key = query.stable_keys(conn, [hit.id])[hit.id]
        return "symbol", key, hit.qualified_name

    files = query.find_files(conn, workspace_id, target)
    if len(files) == 1:
        _, repo, path = files[0]
        return "file", f"{repo}:{path}", f"{repo}:{path}"
    raise TargetError(f"Unknown target '{target}': not a symbol, file or repo:path.")


def add(
    conn: Connection, workspace_id: int, target: str, text: str, author: str | None = None
) -> tuple[int, str]:
    target_type, key, label = resolve_target(conn, workspace_id, target)
    try:
        note_id = store.add_note(
            conn, workspace_id, text, target_type=target_type, target_key=key, author=author
        )
    except store.StoreError as exc:
        raise TargetError(str(exc)) from exc
    return note_id, label


def list_for(conn: Connection, workspace_id: int, target: str | None = None) -> list[store.Note]:
    if target is None:
        return store.list_notes(conn, workspace_id)
    _, key, _ = resolve_target(conn, workspace_id, target)
    return store.list_notes(conn, workspace_id, [key])


def for_files(conn: Connection, workspace_id: int, repo_paths: list[str]) -> list[store.Note]:
    """Notes on the given ``repo:path`` files and on their parent directories."""
    keys: set[str] = set()
    for repo_path in repo_paths:
        repo, _, path = repo_path.partition(":")
        parts = path.split("/")
        keys.add(f"{repo}:")
        keys.update(f"{repo}:{'/'.join(parts[:i])}" for i in range(1, len(parts) + 1))
    return store.list_notes(conn, workspace_id, sorted(keys))
