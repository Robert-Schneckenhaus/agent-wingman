"""HTTP endpoints: route normalization and client -> server matching (CONCEPT.md 4.3).

Extractors record server routes (ASP.NET, minimal APIs, NestJS, Express) and client
calls (fetch, axios, Angular HttpClient, .NET HttpClient) in the ``endpoints`` table.
After resolution, every client call is matched against the server routes of the
whole workspace and stored as a reference of kind ``http_call``, so callers/callees
and context packs follow requests from frontend to backend and between services.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select

from wingman.storage.schema import endpoints, files, refs, repositories, symbols

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
PARAM = "{}"

_SCHEME_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]*", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{[^}]*\}|^:[\w-]+\??$|^\*$")
_FULL_CONFIDENCE = 0.9
_SUFFIX_CONFIDENCE = 0.6


def normalize_route(raw: str) -> str:
    """``https://h/api/Items/{id:int}?x=1`` -> ``api/items/{}``; ``:id`` / ``${id}`` -> ``{}``."""
    route = _SCHEME_HOST.sub("", raw.strip())
    route = re.sub(r"\{[^}]*\}", PARAM, route)  # before the query: "{id:int?}" has a "?"
    route = re.split(r"[?#]", route, maxsplit=1)[0]
    route = route.removeprefix("~")
    segments = []
    for segment in route.split("/"):
        if not segment:
            continue
        segments.append(PARAM if _PLACEHOLDER.search(segment) else segment.lower())
    return "/".join(segments)


def is_url_path(url: str | None) -> bool:
    """Whether a (partially known) client URL is usable for matching: it must start with
    a path or host and contain at least one literal path segment."""
    if not url or not re.match(r"[\w/~.{$:-]", url):
        return False  # e.g. "&api-version=2" left over from an unknown "{uri}&..."
    return any(s != PARAM for s in normalize_route(url).split("/") if s)


def join_routes(*parts: str | None, slash_is_absolute: bool = False) -> str:
    """Combine route prefixes and a route. ``~/x`` always ignores the prefix; ``/x`` only
    with ``slash_is_absolute`` (ASP.NET attribute routes, not minimal API groups)."""
    result = ""
    for part in parts:
        if not part:
            continue
        if result and (part.startswith("~/") or (slash_is_absolute and part.startswith("/"))):
            result = ""
        part = part.removeprefix("~")
        result = f"{result.rstrip('/')}/{part.lstrip('/')}" if result else part
    return result


@dataclass(frozen=True)
class _Endpoint:
    symbol_id: int
    file_id: int
    repo_id: int
    method: str | None
    route: str
    line: int | None


def _segments(route: str) -> list[str]:
    return route.split("/") if route else []


def _segments_match(client: list[str], server: list[str], *, strict: bool = False) -> bool:
    """A client parameter matches anything; a client literal matches the same literal or
    (unless ``strict``) a server parameter such as ``{id}``."""
    for c, s in zip(client, server, strict=True):
        if c in (s, PARAM):
            continue
        if s == PARAM and not strict:
            continue
        return False
    return True


def match_route(client: str, server: str) -> float | None:
    """Confidence that a client URL addresses a server route, or None.

    A client may use a base URL unknown to the index (``${baseUrl}/items/{}``), so a
    client route matching the *end* of a server route also counts, with less confidence.
    """
    c, s = _segments(client), _segments(server)
    if not c or not s:
        return None
    if len(c) == len(s) and _segments_match(c, s):
        literal = sum(1 for x in c if x != PARAM)
        return _FULL_CONFIDENCE if literal else _FULL_CONFIDENCE / 2
    # partial matches: literals must line up exactly, or "items/{}" would match "{}/tax"
    if len(c) < len(s) and _segments_match(c, s[-len(c) :], strict=True):
        literal = sum(1 for x in c if x != PARAM)
        if literal >= 1 and len(c) >= 2:  # "{}" or a single word is too vague
            return _SUFFIX_CONFIDENCE
    if len(c) > len(s) and _segments_match(c[-len(s) :], s, strict=True):  # gateway prefix
        literal = sum(1 for x in s if x != PARAM)
        if literal >= 1 and len(s) >= 2:
            return _SUFFIX_CONFIDENCE * 0.8
    return None


def specificity(client: str, server: str) -> float:
    """How exactly aligned segments (from the end) agree: equal literals count 1, a
    parameter on both sides 0.5, a literal against a parameter 0."""
    c, s = _segments(client), _segments(server)
    pairs = zip(reversed(c), reversed(s), strict=False)  # lengths may differ
    return sum((1.0 if x != PARAM else 0.5) for x, y in pairs if x == y)


def load_endpoints(conn: Connection, workspace_id: int, role: str) -> list[_Endpoint]:
    rows = conn.execute(
        select(
            endpoints.c.symbol_id,
            symbols.c.file_id,
            files.c.repo_id,
            endpoints.c.http_method,
            endpoints.c.route_pattern,
            endpoints.c.line,
        )
        .join(symbols, symbols.c.id == endpoints.c.symbol_id)
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(repositories.c.workspace_id == workspace_id, endpoints.c.role == role)
    )
    return [_Endpoint(*row) for row in rows]


def clear_http_refs(conn: Connection, workspace_id: int) -> None:
    """Remove all http_call references of the workspace (they are rebuilt by matching)."""
    workspace_files = (
        select(files.c.id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(repositories.c.workspace_id == workspace_id)
    )
    conn.execute(
        delete(refs).where(refs.c.kind == "http_call", refs.c.from_file_id.in_(workspace_files))
    )


def match_endpoints(conn: Connection, workspace_id: int) -> int:
    """Rebuild the http_call references of the workspace; returns their number."""
    clear_http_refs(conn, workspace_id)
    servers = load_endpoints(conn, workspace_id, "server")
    by_method: dict[str | None, list[_Endpoint]] = defaultdict(list)
    for server in servers:
        by_method[server.method].append(server)

    rows = []
    for client in load_endpoints(conn, workspace_id, "client"):
        method = client.method or "GET"
        candidates = by_method.get(method, []) + by_method.get(None, [])  # None: any method
        scored = []
        for server in candidates:
            if server.symbol_id == client.symbol_id:
                continue
            confidence = match_route(client.route, server.route)
            if confidence is not None:
                rank = (confidence, specificity(client.route, server.route))
                scored.append((rank, server))
        if not scored:
            continue
        best_rank = max(rank for rank, _ in scored)
        best = best_rank[0]
        # "items/by" prefers the route "items/by" over "items/{id}"
        winners = [s for rank, s in scored if rank == best_rank][:3]
        for server in winners:  # ambiguous matches share the confidence
            rows.append(
                {
                    "from_symbol_id": client.symbol_id,
                    "from_file_id": client.file_id,
                    "to_symbol_id": server.symbol_id,
                    "target_name": f"{method} /{client.route}",
                    "qualifier": None,
                    "qualifier_type": None,
                    "kind": "http_call",
                    "line": client.line,
                    "col": None,
                    "confidence": round(best / len(winners), 3),
                    "resolver": "http",
                }
            )
    if rows:
        conn.execute(insert(refs), rows)
    return len(rows)


@dataclass
class EndpointInfo:
    role: str
    method: str | None
    route: str
    symbol_id: int
    line: int | None
    matches: int


def list_endpoints(
    conn: Connection, workspace_id: int, *, role: str = "server", contains: str | None = None
) -> list[EndpointInfo]:
    """Endpoints with the number of matched counterparts (clients for a server route,
    servers for a client call)."""
    column = refs.c.to_symbol_id if role == "server" else refs.c.from_symbol_id
    tally: dict[int, int] = defaultdict(int)
    for row in conn.execute(select(column).where(refs.c.kind == "http_call")):
        tally[row[0]] += 1
    result = []
    for e in load_endpoints(conn, workspace_id, role):
        if contains and contains.lower().strip("/") not in e.route:
            continue
        result.append(
            EndpointInfo(role, e.method, e.route, e.symbol_id, e.line, tally[e.symbol_id])
        )
    result.sort(key=lambda e: (e.route, e.method or ""))
    return result
