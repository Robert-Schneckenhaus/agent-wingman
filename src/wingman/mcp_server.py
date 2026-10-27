"""MCP server (stdio) exposing the index to Claude Code and other agents (CONCEPT.md 6.2)."""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from wingman import __version__, context, notes, render
from wingman import query as queries
from wingman.indexer import IndexingError, index_repository
from wingman.workspace import Workspace

INSTRUCTIONS = """\
agent-wingman has indexed the code of this workspace (symbols, call graph, inheritance,
docs, tests, notes). Use it before grepping or reading files at random:

- Bug report, stack trace or coding task: call find_relevant_for_task with the full text.
- Looking for a concept or name: search_code.
- Navigating: get_symbol, get_callers (includes calls through interfaces/base methods),
  get_callees, find_tests_for, get_file_outline.
- Locations are repo:path:start-end; read exactly those lines.
- Learned something non-obvious about the code? Store it with add_note.
- After editing files outside the normal edit tools (e.g. via shell), call reindex.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def create_server(home: Path | None = None, workspace: Workspace | None = None) -> MCPServer:
    ws = workspace or Workspace(home)
    server = MCPServer(
        "wingman", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING"
    )

    def lookup(conn, name: str):
        """Resolve a symbol name; returns (hits, None) or (None, message)."""
        result = queries.resolve_name(conn, ws.workspace_id(conn), name)
        if not result.found:
            return None, f"No symbol '{name}'. Try search_code with related words."
        if result.ambiguous:
            return None, render.ambiguous(name, result.found)
        return result.found, None

    @server.tool(annotations=READ_ONLY)
    def find_relevant_for_task(
        task: str, limit: int = 8, include_code: bool = False, budget_tokens: int = 4000
    ) -> str:
        """Find the code relevant to a bug report, error message/stack trace or coding task.

        Pass the complete text. Returns ranked symbols with location (repo:path:lines),
        signature, why they match, callers, callees, tests and notes.
        """
        with ws.engine.connect() as conn:
            pack = context.find_relevant(
                conn, ws.workspace_id(conn), task, limit=limit, include_code=include_code
            )
            return context.render_pack(pack, budget=budget_tokens)

    @server.tool(annotations=READ_ONLY)
    def search_code(
        query: str,
        limit: int = 10,
        repo: str | None = None,
        kind: str | None = None,
        include_tests: bool = True,
    ) -> str:
        """Full-text search over symbol names (camelCase/snake_case aware), signatures,
        documentation and file paths. ``kind`` filters e.g. class, method, function,
        interface, property, component."""
        with ws.engine.connect() as conn:
            hits = queries.search(
                conn,
                ws.workspace_id(conn),
                query,
                repo=repo,
                kinds={kind} if kind else None,
                include_tests=include_tests,
                limit=limit,
            )
            return render.hits(hits)

    @server.tool(annotations=READ_ONLY)
    def get_symbol(name: str, include_code: bool = False) -> str:
        """Definition of a symbol: location, signature, doc, modifiers, bases/derived types,
        overrides, members, caller count and notes. ``name`` may be a simple name,
        ``Class.member``, a fully qualified name or ``#id``."""
        with ws.engine.connect() as conn:
            hits, message = lookup(conn, name)
            if message:
                return message
            details = [queries.symbol_details(conn, h.id) for h in hits]
            sources = {h.id: queries.read_source(conn, h.id) for h in hits} if include_code else {}
            notes_by_id = queries.notes_for_symbols(
                conn, ws.workspace_id(conn), [h.id for h in hits]
            )
            return render.details(details, sources, notes_by_id)

    @server.tool(annotations=READ_ONLY)
    def get_callers(name: str, depth: int = 1) -> str:
        """Who calls or instantiates a symbol, as a tree up to ``depth`` levels. Includes
        calls through interfaces and base methods (marked "via")."""
        with ws.engine.connect() as conn:
            hits, message = lookup(conn, name)
            if message:
                return message
            return "\n".join(
                render.callers(h, queries.callers(conn, h.id, depth=max(1, min(depth, 5))))
                for h in hits
            )

    @server.tool(annotations=READ_ONLY)
    def get_callees(name: str) -> str:
        """What a symbol calls or instantiates (unresolved = external/library code)."""
        with ws.engine.connect() as conn:
            hits, message = lookup(conn, name)
            if message:
                return message
            return "\n".join(render.callees(h, queries.callees(conn, h.id)) for h in hits)

    @server.tool(annotations=READ_ONLY)
    def find_tests_for(name: str, depth: int = 3) -> str:
        """Tests that call a symbol directly or through up to ``depth`` calls."""
        with ws.engine.connect() as conn:
            hits, message = lookup(conn, name)
            if message:
                return message
            blocks = []
            for hit in hits:
                tests = queries.tests_for(conn, hit.id, depth=max(1, min(depth, 5)))
                header = f"Tests reaching {hit.kind} {hit.qualified_name}  [{hit.location}]"
                blocks.append(header + "\n" + (render.hits(tests) if tests else "  (none found)"))
            return "\n\n".join(blocks)

    @server.tool(annotations=READ_ONLY)
    def get_file_outline(path: str) -> str:
        """Symbols of a file without bodies. ``path``: repo:path, a repository-relative
        path or a unique path suffix."""
        with ws.engine.connect() as conn:
            matches = queries.find_files(conn, ws.workspace_id(conn), path)
            if not matches:
                return f"File not in index: {path}"
            if len(matches) > 1:
                return "Several files match:\n" + "\n".join(f"  {r}:{p}" for _, r, p in matches)
            file_id, repo, rel = matches[0]
            return render.outline(repo, rel, queries.outline(conn, file_id))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def add_note(target: str, text: str) -> str:
        """Store knowledge about the code for later sessions (e.g. a non-obvious rule, a
        pitfall, the reason behind a design). ``target``: symbol name/#id or repo:path
        (file or directory)."""
        with ws.engine.begin() as conn:
            try:
                note_id, label = notes.add(conn, ws.workspace_id(conn), target, text, "agent")
            except notes.TargetError as exc:
                return str(exc)
        return f"Note #{note_id} added to {label}."

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
    def reindex(paths: list[str] | None = None) -> str:
        """Update the index. Without ``paths`` all repositories are checked for changes
        (only changed files are parsed); with ``paths`` (absolute or relative to the
        workspace) only those files."""
        lines = []
        if paths:
            by_repo: dict[str, tuple] = {}
            for raw in paths:
                path = Path(raw)
                if not path.is_absolute():
                    path = ws.config.root / path
                located = ws.locate(path)
                if located is None:
                    lines.append(f"{raw}: not inside an indexed repository")
                    continue
                repo, rel = located
                by_repo.setdefault(repo.name, (repo, []))[1].append(rel)
            jobs = [(repo, rels) for repo, rels in by_repo.values()]
        else:
            jobs = [(repo, None) for repo in ws.repositories()]
        for repo, rels in jobs:
            try:
                stats = index_repository(ws.engine, repo, paths=rels)
            except IndexingError as exc:
                lines.append(f"{repo.name}: {exc}")
                continue
            lines.append(
                f"{repo.name}: {stats.files_indexed} indexed, {stats.files_removed} removed, "
                f"{stats.files_unchanged} unchanged ({stats.seconds}s)"
            )
        return "\n".join(lines) or "Nothing to index."

    return server


def run(home: Path | None = None) -> None:
    create_server(home).run("stdio")
