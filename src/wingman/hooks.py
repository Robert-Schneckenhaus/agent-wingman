"""Claude Code hook handlers (CONCEPT.md 6.3).

Hooks must never break the agent's session: every handler catches its errors and
reports them on stderr only.
"""

from __future__ import annotations

import json
from pathlib import Path

from wingman import embeddings
from wingman.indexer import index_repository
from wingman.query import workspace_stats
from wingman.workspace import Workspace

EDIT_PATH_KEYS = ("file_path", "notebook_path", "path")
SESSION_EMBED_SECONDS = 60  # the SessionStart hook timeout is 120 s


def edited_paths(payload: dict) -> list[Path]:
    """File paths touched by an Edit/Write/MultiEdit/NotebookEdit tool call."""
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or ".")
    paths = []
    for key in EDIT_PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            paths.append(path if path.is_absolute() else cwd / path)
    return paths


def post_edit(ws: Workspace, payload: dict) -> str:
    """Re-index the edited file(s); returns a short status line."""
    messages = []
    for path in edited_paths(payload):
        located = ws.locate(path)
        if located is None:
            continue
        repo, rel = located
        stats = index_repository(ws.engine, repo, paths=[rel])
        if stats.files_indexed or stats.files_removed:
            messages.append(f"wingman: re-indexed {repo.name}:{rel} ({stats.seconds}s)")
    return "\n".join(messages)


def session_start(ws: Workspace) -> str:
    """Bring the index up to date; returns context text for the new session."""
    changed = 0
    for repo in ws.repositories():
        try:
            stats = index_repository(ws.engine, repo)
        except Exception:  # a missing repository must not block the others
            continue
        changed += stats.files_indexed + stats.files_removed
    embedder = ws.embedder
    with ws.engine.connect() as conn:
        workspace_id = ws.workspace_id(conn)
    if embedder is not None:
        try:  # bounded, so the session never waits long; the rest follows later
            embeddings.ensure_embeddings(
                ws.engine, embedder, workspace_id, max_seconds=SESSION_EMBED_SECONDS
            )
        except embeddings.EmbeddingError as exc:
            ws.embedding_failed(exc)
    with ws.engine.connect() as conn:
        stats = workspace_stats(conn, workspace_id)
    return (
        f"agent-wingman: code index ready ({stats.repos} repos, {stats.files} files, "
        f"{stats.symbols} symbols; {changed} files updated just now). Use the wingman MCP "
        "tools (find_relevant_for_task, search_code, get_symbol, get_callers, "
        "find_tests_for) before grepping to locate relevant code."
    )


def read_payload(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
