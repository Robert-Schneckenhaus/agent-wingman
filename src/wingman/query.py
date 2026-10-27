"""Read-side queries used by the CLI (and later the MCP server)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import Connection, and_, case, func, or_, select

from wingman.storage.schema import files, refs, repositories, symbol_edges, symbols
from wingman.storage.search import backend_for, query_terms, searchable
from wingman.storage.store import Note, list_notes

CALL_KINDS = ("call", "instantiate")
MEMBER_KINDS = frozenset({"property", "field", "const"})


@dataclass
class SymbolHit:
    id: int
    repo: str
    path: str
    kind: str
    name: str
    qualified_name: str
    signature: str | None
    start_line: int
    end_line: int
    summary: str | None
    is_test: bool
    is_generated: bool
    parent_id: int | None = None
    score: float | None = None
    matched_by: str | None = None
    """For search results: "text", "meaning" or "text+meaning"."""

    @property
    def location(self) -> str:
        return f"{self.repo}:{self.path}:{self.start_line}-{self.end_line}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["location"] = self.location
        return data


@dataclass
class RefHit:
    """One edge of the call graph, seen from either end."""

    symbol: SymbolHit | None
    """The other end (caller or callee); None if unresolved or file-level code."""
    kind: str
    line: int
    confidence: float
    target_name: str
    path: str | None = None
    via: str | None = None
    """Set when the edge goes through an interface/base method."""
    children: list[RefHit] = field(default_factory=list)
    repo: str | None = None

    @property
    def repo_path(self) -> str:
        return f"{self.repo}:{self.path}" if self.repo else str(self.path)

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "symbol": self.symbol.to_dict() if self.symbol else None,
            "kind": self.kind,
            "line": self.line,
            "confidence": self.confidence,
            "target_name": self.target_name,
            "path": self.path,
            "via": self.via,
            "children": [c.to_dict() for c in self.children],
        }


def _hit_query():
    return (
        select(
            symbols.c.id,
            repositories.c.name.label("repo"),
            files.c.path,
            symbols.c.kind,
            symbols.c.name,
            symbols.c.qualified_name,
            symbols.c.signature,
            symbols.c.start_line,
            symbols.c.end_line,
            symbols.c.summary,
            files.c.is_test,
            files.c.is_generated,
            symbols.c.parent_id,
        )
        .select_from(symbols)
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
    )


def _hit(row, score: float | None = None) -> SymbolHit:
    return SymbolHit(**row._mapping, score=score)


def _scope(query, workspace_id: int, repo: str | None, kinds: set[str] | None):
    query = query.where(repositories.c.workspace_id == workspace_id)
    if repo:
        query = query.where(repositories.c.name == repo)
    if kinds:
        query = query.where(symbols.c.kind.in_(kinds))
    return query


def get_symbols(conn: Connection, ids: list[int]) -> dict[int, SymbolHit]:
    if not ids:
        return {}
    rows = conn.execute(_hit_query().where(symbols.c.id.in_(ids)))
    return {row.id: _hit(row) for row in rows}


def find_symbols(
    conn: Connection,
    workspace_id: int,
    name: str,
    *,
    repo: str | None = None,
    kinds: set[str] | None = None,
    limit: int = 20,
) -> list[SymbolHit]:
    """Exact qualified name, then qualified-name suffix (``Class.method``), then name."""
    base = _scope(_hit_query(), workspace_id, repo, kinds)
    order = (files.c.is_test, func.length(symbols.c.qualified_name), symbols.c.id)
    for condition in (
        symbols.c.qualified_name == name,
        symbols.c.qualified_name.like(f"%.{name}"),
        symbols.c.name == name,
        func.lower(symbols.c.name) == name.lower(),
    ):
        rows = conn.execute(base.where(condition).order_by(*order).limit(limit)).all()
        if rows:
            return [_hit(r) for r in rows]
    return []


@dataclass
class NameLookup:
    found: list[SymbolHit]
    ambiguous: bool


def resolve_name(
    conn: Connection,
    workspace_id: int,
    name: str,
    *,
    repo: str | None = None,
    kinds: set[str] | None = None,
) -> NameLookup:
    """Symbols for ``name`` (simple name, ``Class.member``, qualified name or ``#id``).

    Overloads and partial-class parts share one qualified name and are returned
    together; different qualified names make the lookup ambiguous.
    """
    if name.startswith("#") and name[1:].isdigit():
        found = list(get_symbols(conn, [int(name[1:])]).values())
    else:
        found = find_symbols(conn, workspace_id, name, repo=repo, kinds=kinds)
    qualified = {(h.repo, h.qualified_name) for h in found}
    return NameLookup(found, len(qualified) > 1)


def symbol_at(conn: Connection, file_id: int, line: int) -> SymbolHit | None:
    """Innermost symbol whose range contains ``line``."""
    row = conn.execute(
        _hit_query()
        .where(
            symbols.c.file_id == file_id,
            symbols.c.start_line <= line,
            symbols.c.end_line >= line,
        )
        .order_by((symbols.c.end_line - symbols.c.start_line), symbols.c.start_byte.desc())
        .limit(1)
    ).first()
    return _hit(row) if row is not None else None


def tests_for(conn: Connection, symbol_id: int, *, depth: int = 3) -> list[SymbolHit]:
    """Symbols in test files that reach ``symbol_id`` through at most ``depth`` calls."""
    found: dict[int, SymbolHit] = {}

    def walk(refs_: list[RefHit]) -> None:
        for ref in refs_:
            if ref.symbol is not None and ref.symbol.is_test:
                found.setdefault(ref.symbol.id, ref.symbol)
            walk(ref.children)

    walk(callers(conn, symbol_id, depth=depth))
    return sorted(found.values(), key=lambda h: (h.repo, h.path, h.start_line))


def search(
    conn: Connection,
    workspace_id: int,
    query: str,
    *,
    repo: str | None = None,
    kinds: set[str] | None = None,
    include_tests: bool = True,
    limit: int = 10,
    semantic: list[tuple[int, float]] | None = None,
    semantic_weight: float = 0.5,
) -> list[SymbolHit]:
    """Full-text search, optionally fused with semantic candidates
    (``(symbol_id, similarity)`` from ``embeddings.semantic_search``)."""
    candidates = backend_for(conn).search(conn, query, limit=max(limit * 20, 200))
    scores = dict(candidates)
    meaning = _normalize_similarities(semantic or [])
    if not scores and not meaning:
        return []
    best_text = max(scores.values(), default=0.0) or 1.0
    rows = conn.execute(
        _scope(_hit_query(), workspace_id, repo, kinds).where(
            symbols.c.id.in_(list(set(scores) | set(meaning)))
        )
    )
    words = {w.lower() for w in query.split()}
    terms = query_terms(query)
    hits = []
    own_match: set[int] = set()  # symbols whose own name/signature/doc matches
    for row in rows:
        if row.is_test and not include_tests:
            continue
        own = searchable(f"{row.name} {row.signature or ''} {row.summary or ''}").lower()
        if any(t in own for t in terms):
            own_match.add(row.id)
        # prefer symbols that cover more of the query terms
        haystack = searchable(
            f"{row.qualified_name} {row.signature or ''} {row.summary or ''} {row.path}"
        ).lower()
        coverage = sum(1 for t in terms if t in haystack) / len(terms) if terms else 1.0
        lexical = scores.get(row.id, 0.0) * (0.5 + coverage)
        if meaning:  # both parts scaled to 0..10 before weighting
            text_part = 10 * lexical / (1.5 * best_text)
            meaning_part = 10 * meaning.get(row.id, 0.0)
            score = (1 - semantic_weight) * text_part + semantic_weight * meaning_part
            matched_by = "+".join(
                label
                for label, part in (("text", text_part), ("meaning", meaning_part))
                if part > 0
            )
        else:
            score, matched_by = lexical, "text"
        if row.name.lower() in words or row.qualified_name.lower() in words:
            score *= 1.5
        if row.is_test:
            score *= 0.6
        if row.is_generated:
            score *= 0.3
        if row.kind in MEMBER_KINDS:
            score *= 0.7
        if row.kind == "constructor":  # same name as its class, which should win
            score *= 0.5
        hit = _hit(row, round(score, 3))
        hit.matched_by = matched_by
        hits.append(hit)
    return _diversify(hits, limit, own_match)


def _normalize_similarities(semantic: list[tuple[int, float]]) -> dict[int, float]:
    """Min-max scale similarities of the candidate list to 0..1 (models differ a lot in
    their absolute cosine ranges)."""
    if not semantic:
        return {}
    values = [s for _, s in semantic]
    low, high = min(values), max(values)
    span = high - low
    return {i: (s - low) / span if span > 1e-9 else 1.0 for i, s in semantic}


MAX_MEMBERS_PER_PARENT = 2


def _diversify(hits: list[SymbolHit], limit: int, own_match: set[int]) -> list[SymbolHit]:
    """Members often match only through their class name in the qualified name: rank
    those below the matching parent and keep at most a few per parent. Symbols with the
    same simple name (copies in several services) are damped after the first two."""
    present = {h.id for h in hits}
    for hit in hits:
        if hit.parent_id in present and hit.id not in own_match:
            hit.score = round(hit.score * 0.5, 3)
    hits.sort(key=lambda h: -h.score)
    same_name: dict[str, int] = {}
    for hit in hits:
        same_name[hit.name] = same_name.get(hit.name, 0) + 1
        if same_name[hit.name] > 2:
            hit.score = round(hit.score * 0.5, 3)
    hits.sort(key=lambda h: -h.score)
    per_parent: dict[int, int] = {}
    result = []
    for hit in hits:
        if hit.parent_id is not None:
            per_parent[hit.parent_id] = per_parent.get(hit.parent_id, 0) + 1
            if per_parent[hit.parent_id] > MAX_MEMBERS_PER_PARENT:
                continue
        result.append(hit)
        if len(result) == limit:
            break
    return result


def _overridden(conn: Connection, symbol_id: int) -> list[int]:
    """Base/interface members this symbol overrides or implements (transitively)."""
    result: list[int] = []
    frontier = [symbol_id]
    while frontier and len(result) < 20:
        rows = conn.execute(
            select(symbol_edges.c.dst_id).where(
                symbol_edges.c.src_id.in_(frontier), symbol_edges.c.kind == "overrides"
            )
        ).scalars()
        frontier = [r for r in rows if r not in result and r != symbol_id]
        result.extend(frontier)
    return result


def _overriding(conn: Connection, symbol_id: int) -> list[int]:
    return list(
        conn.execute(
            select(symbol_edges.c.src_id).where(
                symbol_edges.c.dst_id == symbol_id, symbol_edges.c.kind == "overrides"
            )
        ).scalars()
    )


def callers(
    conn: Connection,
    symbol_id: int,
    *,
    depth: int = 1,
    include_type_refs: bool = False,
    _seen: set[int] | None = None,
) -> list[RefHit]:
    seen = _seen if _seen is not None else {symbol_id}
    kinds = (*CALL_KINDS, "type_ref") if include_type_refs else CALL_KINDS
    targets = {symbol_id: None}
    for base_id in _overridden(conn, symbol_id):
        targets[base_id] = base_id
    base_names = get_symbols(conn, [t for t in targets if t != symbol_id])

    rows = conn.execute(
        select(
            refs.c.from_symbol_id,
            refs.c.to_symbol_id,
            refs.c.kind,
            refs.c.line,
            refs.c.confidence,
            refs.c.target_name,
            files.c.path,
            repositories.c.name.label("repo"),
        )
        .join(files, files.c.id == refs.c.from_file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(refs.c.to_symbol_id.in_(list(targets)), refs.c.kind.in_(kinds))
        .order_by(files.c.path, refs.c.line)
    ).all()
    sources = get_symbols(conn, [r.from_symbol_id for r in rows if r.from_symbol_id])
    result = []
    for row in rows:
        via = base_names[row.to_symbol_id].qualified_name if targets[row.to_symbol_id] else None
        hit = RefHit(
            symbol=sources.get(row.from_symbol_id),
            kind=row.kind,
            line=row.line,
            confidence=row.confidence * (0.8 if via else 1.0),
            target_name=row.target_name,
            path=row.path,
            via=via,
            repo=row.repo,
        )
        if depth > 1 and row.from_symbol_id and row.from_symbol_id not in seen:
            seen.add(row.from_symbol_id)
            hit.children = callers(
                conn,
                row.from_symbol_id,
                depth=depth - 1,
                include_type_refs=include_type_refs,
                _seen=seen,
            )
        result.append(hit)
    return result


def callees(conn: Connection, symbol_id: int, *, include_type_refs: bool = False) -> list[RefHit]:
    kinds = (*CALL_KINDS, "type_ref") if include_type_refs else CALL_KINDS
    rows = conn.execute(
        select(
            refs.c.to_symbol_id,
            refs.c.kind,
            refs.c.line,
            refs.c.confidence,
            refs.c.target_name,
            refs.c.qualifier,
        )
        .where(refs.c.from_symbol_id == symbol_id, refs.c.kind.in_(kinds))
        .order_by(refs.c.line, refs.c.col)
    ).all()
    targets = get_symbols(conn, [r.to_symbol_id for r in rows if r.to_symbol_id])
    return [
        RefHit(
            symbol=targets.get(row.to_symbol_id),
            kind=row.kind,
            line=row.line,
            confidence=row.confidence,
            target_name=f"{row.qualifier}.{row.target_name}" if row.qualifier else row.target_name,
        )
        for row in rows
    ]


@dataclass
class SymbolDetails:
    symbol: SymbolHit
    doc: str | None
    visibility: str | None
    modifiers: list[str]
    decorators: list[str]
    members: list[SymbolHit]
    bases: list[SymbolHit]
    """Types this type inherits from / implements."""
    derived: list[SymbolHit]
    """Types deriving from / implementing this type."""
    overrides: list[SymbolHit]
    """Base members this member overrides or implements."""
    overridden_by: list[SymbolHit]
    caller_count: int

    def to_dict(self) -> dict:
        data = {k: getattr(self, k) for k in ("doc", "visibility", "modifiers", "decorators")}
        data["symbol"] = self.symbol.to_dict()
        for key in ("members", "bases", "derived", "overrides", "overridden_by"):
            data[key] = [s.to_dict() for s in getattr(self, key)]
        data["caller_count"] = self.caller_count
        return data


def _json_list(value) -> list[str]:
    if not value:
        return []
    return json.loads(value) if isinstance(value, str) else list(value)


def symbol_details(conn: Connection, symbol_id: int) -> SymbolDetails:
    hit = get_symbols(conn, [symbol_id])[symbol_id]
    row = conn.execute(
        select(
            symbols.c.doc, symbols.c.visibility, symbols.c.modifiers, symbols.c.decorators
        ).where(symbols.c.id == symbol_id)
    ).one()
    members = [
        _hit(r)
        for r in conn.execute(
            _hit_query().where(symbols.c.parent_id == symbol_id).order_by(symbols.c.start_line)
        )
    ]

    def edge_targets(column_from, column_to, kinds) -> list[SymbolHit]:
        ids = (
            conn.execute(
                select(column_to).where(column_from == symbol_id, symbol_edges.c.kind.in_(kinds))
            )
            .scalars()
            .all()
        )
        return list(get_symbols(conn, list(ids)).values())

    heritage = ("inherits", "implements")
    caller_count = conn.execute(
        select(func.count()).where(refs.c.to_symbol_id == symbol_id, refs.c.kind.in_(CALL_KINDS))
    ).scalar_one()
    return SymbolDetails(
        symbol=hit,
        doc=row.doc,
        visibility=row.visibility,
        modifiers=_json_list(row.modifiers),
        decorators=_json_list(row.decorators),
        members=members,
        bases=edge_targets(symbol_edges.c.src_id, symbol_edges.c.dst_id, heritage),
        derived=edge_targets(symbol_edges.c.dst_id, symbol_edges.c.src_id, heritage),
        overrides=list(get_symbols(conn, _overridden(conn, symbol_id)).values()),
        overridden_by=list(get_symbols(conn, _overriding(conn, symbol_id)).values()),
        caller_count=caller_count,
    )


@dataclass
class OutlineEntry:
    symbol: SymbolHit
    depth: int


def find_files(
    conn: Connection, workspace_id: int, path: str, cwd: Path | None = None
) -> list[tuple[int, str, str]]:
    """Resolve ``repo:path``, a path relative to ``cwd`` or a repo-relative path
    (also as suffix) to ``(file_id, repo, path)`` tuples."""
    base = (
        select(files.c.id, repositories.c.name, files.c.path, repositories.c.root_path)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(repositories.c.workspace_id == workspace_id)
    )
    if ":" in path and not Path(path).exists():
        repo_name, _, rel = path.partition(":")
        rows = conn.execute(base.where(repositories.c.name == repo_name, files.c.path == rel))
        return [(r.id, r.name, r.path) for r in rows]

    absolute = ((cwd or Path.cwd()) / path).resolve()
    if absolute.exists():
        for repo in conn.execute(
            select(repositories.c.name, repositories.c.root_path).where(
                repositories.c.workspace_id == workspace_id
            )
        ):
            root = Path(repo.root_path)
            if absolute.is_relative_to(root):
                rel = absolute.relative_to(root).as_posix()
                rows = conn.execute(
                    base.where(repositories.c.name == repo.name, files.c.path == rel)
                )
                found = [(r.id, r.name, r.path) for r in rows]
                if found:
                    return found

    normalized = path.replace("\\", "/").lstrip("./")
    rows = conn.execute(
        base.where(or_(files.c.path == normalized, files.c.path.like(f"%/{normalized}")))
        .order_by(func.length(files.c.path))
        .limit(20)
    )
    return [(r.id, r.name, r.path) for r in rows]


def outline(conn: Connection, file_id: int) -> list[OutlineEntry]:
    rows = conn.execute(
        _hit_query().where(symbols.c.file_id == file_id).order_by(symbols.c.start_byte)
    ).all()
    hits = [_hit(r) for r in rows]
    depth: dict[int, int] = {}
    entries = []
    for hit in hits:
        depth[hit.id] = depth.get(hit.parent_id, -1) + 1 if hit.parent_id else 0
        entries.append(OutlineEntry(hit, depth[hit.id]))
    return entries


def read_source(conn: Connection, symbol_id: int, max_lines: int = 200) -> str | None:
    row = conn.execute(
        select(repositories.c.root_path, files.c.path, symbols.c.start_line, symbols.c.end_line)
        .select_from(symbols)
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(symbols.c.id == symbol_id)
    ).one_or_none()
    if row is None:
        return None
    try:
        lines = (Path(row.root_path) / row.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    selected = lines.splitlines()[row.start_line - 1 : row.end_line]
    if len(selected) > max_lines:
        omitted = len(selected) - max_lines
        selected = selected[:max_lines] + [f"… ({omitted} more lines)"]
    return "\n".join(selected)


@dataclass
class WorkspaceStats:
    repos: int
    files: int
    symbols: int
    refs: int
    resolved_refs: int


def workspace_stats(conn: Connection, workspace_id: int) -> WorkspaceStats:
    file_ids = (
        select(files.c.id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(repositories.c.workspace_id == workspace_id)
    )
    repo_count = conn.execute(
        select(func.count()).where(repositories.c.workspace_id == workspace_id)
    ).scalar_one()
    file_count = conn.execute(select(func.count()).select_from(file_ids.subquery())).scalar_one()
    symbol_count = conn.execute(
        select(func.count()).where(symbols.c.file_id.in_(file_ids))
    ).scalar_one()
    ref_row = conn.execute(
        select(
            func.count(),
            func.sum(case((refs.c.to_symbol_id.is_not(None), 1), else_=0)),
        ).where(and_(refs.c.from_file_id.in_(file_ids)))
    ).one()
    return WorkspaceStats(repo_count, file_count, symbol_count, ref_row[0], ref_row[1] or 0)


def stable_keys(conn: Connection, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = conn.execute(select(symbols.c.id, symbols.c.stable_key).where(symbols.c.id.in_(ids)))
    return {row.id: row.stable_key for row in rows}


def notes_for_symbols(conn: Connection, workspace_id: int, ids: list[int]) -> dict[int, list[Note]]:
    """Notes attached to the given symbols (matched via ``stable_key``)."""
    keys = stable_keys(conn, ids)
    by_key: dict[str, list[Note]] = {}
    for note in list_notes(conn, workspace_id, list(keys.values())):
        by_key.setdefault(note.target_key, []).append(note)
    return {sid: by_key[key] for sid, key in keys.items() if key in by_key}
