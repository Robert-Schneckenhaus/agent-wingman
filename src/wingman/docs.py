"""External documentation: ingest, chunking, linking to code, search (CONCEPT.md 3.7).

Sources are Markdown/text files, HTML pages (local or URL), PDFs and OpenAPI specs.
Each document is split at its structure (headings, pages, operations) into chunks
that keep a citable locator (anchor, page, ``GET /route``). Chunks are linked to code:

- **manual**: targets given with ``wingman doc add ... --link`` (symbols, files,
  directories), applied to every chunk of the document;
- **name_match**: identifiers and file paths mentioned in a chunk;
- **route**: OpenAPI operations matched to server endpoints in the index.

Links point to ``stable_key`` / ``repo:path`` targets, so they survive re-indexing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from wingman import context, embeddings, notes, query
from wingman import endpoints as http
from wingman.storage.schema import doc_chunks, doc_links, documents, files, repositories, symbols
from wingman.storage.search import ChunkRow, backend_for

MAX_CHUNK_CHARS = 3000
TARGET_CHUNK_CHARS = 2000
MAX_IDENTIFIERS_PER_CHUNK = 40
MAX_FILE_BYTES = 20_000_000
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
TEXT_SUFFIXES = (".txt", ".rst", ".adoc")
HTML_SUFFIXES = (".html", ".htm")
SPEC_SUFFIXES = (".json", ".yaml", ".yml")
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES + TEXT_SUFFIXES + HTML_SUFFIXES + SPEC_SUFFIXES + (".pdf",)
SKIP_DIRS = frozenset({"node_modules", ".git", "bin", "obj", "dist", "build", ".venv"})


class DocError(Exception):
    pass


@dataclass
class Chunk:
    heading_path: str | None
    locator: str | None
    text: str


@dataclass
class Loaded:
    source_type: str
    uri: str
    title: str
    content_hash: str
    chunks: list[Chunk]
    metadata: dict = field(default_factory=dict)


# --- chunking -----------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def slug(heading: str) -> str:
    """GitHub-style anchor for a heading."""
    text = re.sub(r"[^\w\- ]", "", heading.lower()).strip()
    return "#" + re.sub(r"\s", "-", text)


def _split_long(text: str) -> list[str]:
    """Split a long section at paragraph boundaries into parts of ~TARGET_CHUNK_CHARS."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        while len(paragraph) > MAX_CHUNK_CHARS:  # one huge paragraph
            if current:
                parts.append(current)
                current = ""
            parts.append(paragraph[:TARGET_CHUNK_CHARS])
            paragraph = paragraph[TARGET_CHUNK_CHARS:]
        if current and len(current) + len(paragraph) > TARGET_CHUNK_CHARS:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def chunk_markdown(text: str) -> tuple[str | None, list[Chunk]]:
    """Chunks per heading section; returns the first H1 as title."""
    title = None
    stack: list[str] = []
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            level = len(heading[1])
            name = re.sub(r"<[^>]+>", "", heading[2]).strip() or heading[2].strip()
            if level == 1 and title is None:
                title = name
            stack = stack[: level - 1] + [""] * max(0, level - 1 - len(stack)) + [name]
            sections.append(([s for s in stack if s], []))
        else:
            sections[-1][1].append(line)
    chunks = []
    for path, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        heading_path = " > ".join(path) or None
        locator = slug(path[-1]) if path else None
        for part in _split_long(body):
            chunks.append(Chunk(heading_path, locator, part))
    return title, chunks


def chunk_text(text: str) -> list[Chunk]:
    """Plain text / reStructuredText: underlined headings, else paragraphs."""
    lines = text.splitlines()
    converted = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (
            line.strip()
            and re.fullmatch(r"([=\-~^*#])\1{2,}\s*", nxt)
            and len(nxt.strip()) >= len(line.strip())
        ):
            level = "=-~^*#".index(nxt.strip()[0]) + 1
            converted.append(f"{'#' * min(level, 6)} {line.strip()}")
        elif (
            re.fullmatch(r"([=\-~^*#])\1{2,}\s*", line)
            and i > 0
            and converted
            and (converted[-1].startswith("#"))
        ):
            continue
        else:
            converted.append(line)
    return chunk_markdown("\n".join(converted))[1]


class _HtmlToMarkdown(HTMLParser):
    SKIP = frozenset(
        {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"}
    )
    BLOCK = frozenset({"p", "div", "li", "tr", "section", "article", "main", "table", "ul", "ol"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.main: list[str] = []
        self.title = ""
        self._skip = 0
        self._main = 0
        self._in_title = False
        self._pre = False

    def _emit(self, text: str) -> None:
        self.out.append(text)
        if self._main:
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("main", "article"):
            self._main += 1
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self._pre = True
            self._emit("\n\n```\n")
        elif tag == "br":
            self._emit("\n")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in self.BLOCK:
            self._emit("\n\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self._emit("\n")
        elif tag == "pre":
            self._pre = False
            self._emit("\n```\n")
        if tag in ("main", "article"):
            self._main = max(0, self._main - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._emit(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> tuple[str, str]:
    """``(title, markdown)``; uses only <main>/<article> when the page has one."""
    parser = _HtmlToMarkdown()
    parser.feed(html)
    body = "".join(parser.main if "".join(parser.main).strip() else parser.out)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return parser.title.strip(), body.strip()


def chunk_pdf(data: bytes) -> tuple[str | None, list[Chunk]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocError("PDF support needs pypdf: pip install 'agent-wingman[docs]'") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many types for broken files
        raise DocError(f"Cannot read PDF: {exc}") from exc
    headings = _pdf_outline(reader)
    title = None
    try:
        title = (reader.metadata.title or "").strip() or None if reader.metadata else None
    except Exception:
        title = None
    chunks = []
    current_heading = None
    for number, page_text in enumerate(pages, 1):
        current_heading = headings.get(number, current_heading)
        page_text = page_text.strip()
        if not page_text:
            continue
        for part in _split_long(page_text):
            chunks.append(Chunk(current_heading, f"page {number}", part))
    return title, chunks


def _pdf_outline(reader) -> dict[int, str]:
    """First page number -> outline (bookmark) title, if the PDF has an outline."""
    result: dict[int, str] = {}

    def walk(items, path):
        for item in items:
            if isinstance(item, list):
                walk(item, path)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            result.setdefault(page, " > ".join([*path, item.title]))

    try:
        walk(reader.outline, [])
    except Exception:
        return {}
    return result


HTTP_METHODS = ("get", "post", "put", "delete", "patch")


def chunk_openapi(spec: dict) -> tuple[str | None, list[Chunk]]:
    """One chunk per operation (locator ``GET /route``) plus one for the API overview."""
    info = spec.get("info") or {}
    title = info.get("title")
    base = ""
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict):
        base = re.sub(r"^[a-z]+://[^/]*", "", str(servers[0].get("url", ""))).rstrip("/")
    elif spec.get("basePath"):
        base = str(spec["basePath"]).rstrip("/")
    chunks = []
    overview = "\n".join(str(x) for x in (title, info.get("description")) if x)
    if overview:
        chunks.append(Chunk(None, None, overview))
    for route, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        for method in HTTP_METHODS:
            op = operations.get(method)
            if not isinstance(op, dict):
                continue
            full_route = f"{base}{route}"
            lines = [f"{method.upper()} {full_route}"]
            lines += [str(op[k]) for k in ("summary", "description") if op.get(k)]
            params = [
                f"{p.get('name')} ({p.get('in')}): {p.get('description', '')}".strip()
                for p in op.get("parameters") or []
                if isinstance(p, dict) and p.get("name")
            ]
            if params:
                lines.append("Parameters: " + "; ".join(params))
            responses = op.get("responses") or {}
            if isinstance(responses, dict) and responses:
                lines.append(
                    "Responses: "
                    + "; ".join(
                        f"{code} {(r or {}).get('description', '')}".strip()
                        for code, r in responses.items()
                        if isinstance(r, dict) or r is None
                    )
                )
            tag = (op.get("tags") or [None])[0]
            name = op.get("operationId") or op.get("summary")
            heading = " > ".join(str(x) for x in (tag, name) if x) or None
            chunks.append(Chunk(heading, f"{method.upper()} {full_route}", "\n".join(lines)))
    return title, chunks


def _parse_spec(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            return None
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            return None
    if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
        return data
    return None


# --- loading ------------------------------------------------------------------------


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_url(url: str) -> tuple[bytes, str]:
    """``(body, content type)``; proxies and CA bundles come from the environment."""
    import httpx

    try:
        response = httpx.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocError(f"Cannot fetch {url}: {exc}") from exc
    return response.content, response.headers.get("content-type", "")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def load(source: str, *, fetch: Callable[[str], tuple[bytes, str]] = fetch_url) -> Loaded:
    """Load one file or URL and split it into chunks."""
    if re.match(r"https?://", source):
        data, content_type = fetch(source)
        kind = _kind_for(source.split("?")[0].split("#")[0], content_type, data)
        uri = source
        fallback_title = source
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise DocError(f"Not a file: {source}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DocError(f"File too large: {source}")
        data = path.read_bytes()
        kind = _kind_for(path.name, "", data)
        uri = path.as_posix()
        fallback_title = path.name
    title: str | None
    metadata: dict = {}
    if kind == "pdf":
        title, chunks = chunk_pdf(data)
    elif kind == "html":
        title, markdown = html_to_markdown(_decode(data))
        h1, chunks = chunk_markdown(markdown)
        title = title or h1
    elif kind == "openapi":
        spec = _parse_spec(_decode(data))
        assert spec is not None
        title, chunks = chunk_openapi(spec)
    elif kind == "markdown":
        title, chunks = chunk_markdown(_decode(data))
    elif kind == "text":
        chunks = chunk_text(_decode(data))
        title = None
    else:
        raise DocError(f"Unsupported document type: {source}")
    if not chunks:
        raise DocError(f"No text found in {source}")
    return Loaded(kind, uri, title or fallback_title, _hash(data), chunks, metadata)


def _kind_for(name: str, content_type: str, data: bytes) -> str | None:
    lower = name.lower()
    if lower.endswith(".pdf") or "application/pdf" in content_type or data[:5] == b"%PDF-":
        return "pdf"
    if lower.endswith(SPEC_SUFFIXES) or "json" in content_type or "yaml" in content_type:
        return "openapi" if _parse_spec(_decode(data)) is not None else None
    if lower.endswith(HTML_SUFFIXES) or "text/html" in content_type:
        return "html"
    if lower.endswith(MARKDOWN_SUFFIXES) or "markdown" in content_type:
        return "markdown"
    if lower.endswith(TEXT_SUFFIXES) or content_type.startswith("text/"):
        return "text"
    if re.match(r"https?://", name) or "." not in lower.rsplit("/", 1)[-1]:
        head = _decode(data[:2000]).lstrip().lower()
        return "html" if head.startswith(("<!doctype html", "<html")) else "markdown"
    return None


def expand_sources(sources: Iterable[str]) -> list[str]:
    """Directories become the supported files below them (Markdown, PDF, specs, ...)."""
    result = []
    for source in sources:
        path = Path(source).expanduser()
        if re.match(r"https?://", source) or not path.is_dir():
            result.append(source)
            continue
        for candidate in sorted(path.rglob("*")):
            parts = candidate.relative_to(path).parts
            if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
                continue
            suffix = candidate.suffix.lower()
            if suffix in HTML_SUFFIXES:
                continue  # in source trees these are app templates, not documentation
            if candidate.is_file() and suffix in SUPPORTED_SUFFIXES:
                if (
                    candidate.suffix.lower() in SPEC_SUFFIXES
                    and _parse_spec(_decode(candidate.read_bytes()[:MAX_FILE_BYTES])) is None
                ):
                    continue  # package.json, tsconfig.json, ...
                result.append(candidate.as_posix())
    return result


# --- storing ------------------------------------------------------------------------


@dataclass
class DocInfo:
    id: int
    source_type: str
    uri: str
    title: str | None
    chunks: int
    links: int
    fetched_at: datetime | None
    manual_links: list[str]


def store_document(
    conn: Connection,
    workspace_id: int,
    loaded: Loaded,
    *,
    links: Sequence[str] | None = None,
) -> tuple[int, bool]:
    """Insert or update a document; returns ``(id, changed)``. ``links`` replaces the
    manual link targets (None keeps the existing ones)."""
    existing = conn.execute(
        select(documents.c.id, documents.c.content_hash, documents.c.links).where(
            documents.c.workspace_id == workspace_id, documents.c.uri == loaded.uri
        )
    ).first()
    backend = backend_for(conn)
    now = datetime.now(UTC)
    home = _home_repo(conn, workspace_id, loaded.uri)
    repo_id = home[0] if home else None
    targets = (
        list(links) if links is not None else list((existing.links if existing else None) or [])
    )
    for target in targets:
        notes.resolve_target(conn, workspace_id, target)  # fail early on unknown targets
    if existing is not None:
        document_id = existing.id
        changed = existing.content_hash != loaded.content_hash
        conn.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(title=loaded.title, fetched_at=now, links=targets or None, repo_id=repo_id)
        )
        if not changed:
            return document_id, False
        old = list(
            conn.execute(
                select(doc_chunks.c.id).where(doc_chunks.c.document_id == document_id)
            ).scalars()
        )
        backend.delete_chunks(conn, old)
        conn.execute(delete(doc_chunks).where(doc_chunks.c.document_id == document_id))
        conn.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(content_hash=loaded.content_hash, source_type=loaded.source_type)
        )
    else:
        document_id = conn.execute(
            insert(documents).values(
                workspace_id=workspace_id,
                repo_id=repo_id,
                source_type=loaded.source_type,
                uri=loaded.uri,
                title=loaded.title,
                content_hash=loaded.content_hash,
                fetched_at=now,
                metadata=loaded.metadata or None,
                links=targets or None,
            )
        ).inserted_primary_key[0]
        changed = True
    rows = []
    for ordinal, chunk in enumerate(loaded.chunks):
        chunk_id = conn.execute(
            insert(doc_chunks).values(
                document_id=document_id,
                ordinal=ordinal,
                heading_path=chunk.heading_path,
                locator=chunk.locator,
                text=chunk.text,
                token_count=len(chunk.text) // 4,
                embed_hash=embeddings.text_hash(
                    embeddings.chunk_text_for_embedding(
                        loaded.title, chunk.heading_path, chunk.text
                    )
                ),
            )
        ).inserted_primary_key[0]
        rows.append(ChunkRow(chunk_id, loaded.title, chunk.heading_path, chunk.text))
    backend.index_chunks(conn, rows)
    return document_id, changed


def _home_repo(conn: Connection, workspace_id: int, uri: str) -> tuple[int, str, str] | None:
    """``(repo_id, repo name, directory inside the repo)`` for a file in a repository."""
    if re.match(r"https?://", uri):
        return None
    for repo in conn.execute(
        select(repositories.c.id, repositories.c.name, repositories.c.root_path).where(
            repositories.c.workspace_id == workspace_id
        )
    ):
        root = Path(repo.root_path).resolve().as_posix().rstrip("/") + "/"
        if uri.startswith(root):
            return repo.id, repo.name, uri[len(root) :].rpartition("/")[0]
    return None


def remove_document(conn: Connection, workspace_id: int, document_id: int) -> bool:
    chunk_ids = list(
        conn.execute(
            select(doc_chunks.c.id).where(doc_chunks.c.document_id == document_id)
        ).scalars()
    )
    backend_for(conn).delete_chunks(conn, chunk_ids)
    result = conn.execute(
        delete(documents).where(
            documents.c.id == document_id, documents.c.workspace_id == workspace_id
        )
    )
    return result.rowcount > 0


def find_document(conn: Connection, workspace_id: int, ref: str) -> int | None:
    """A document by id (``3`` or ``#3``), URI, or unique URI suffix."""
    raw = ref.removeprefix("#")
    if raw.isdigit():
        row = conn.execute(
            select(documents.c.id).where(
                documents.c.id == int(raw), documents.c.workspace_id == workspace_id
            )
        ).first()
        return row[0] if row else None
    candidates = [ref]
    if not re.match(r"https?://", ref):
        candidates.append(Path(ref).expanduser().resolve().as_posix())
    rows = conn.execute(
        select(documents.c.id, documents.c.uri).where(documents.c.workspace_id == workspace_id)
    ).all()
    exact = [r.id for r in rows if r.uri in candidates]
    if exact:
        return exact[0]
    suffix = [r.id for r in rows if r.uri.endswith("/" + ref.lstrip("/"))]
    return suffix[0] if len(suffix) == 1 else None


def list_documents(conn: Connection, workspace_id: int) -> list[DocInfo]:
    chunk_counts = dict(
        conn.execute(
            select(doc_chunks.c.document_id, func.count()).group_by(doc_chunks.c.document_id)
        ).all()
    )
    link_counts = dict(
        conn.execute(
            select(doc_chunks.c.document_id, func.count())
            .select_from(doc_links)
            .join(doc_chunks, doc_chunks.c.id == doc_links.c.chunk_id)
            .group_by(doc_chunks.c.document_id)
        ).all()
    )
    rows = conn.execute(
        select(documents).where(documents.c.workspace_id == workspace_id).order_by(documents.c.id)
    )
    return [
        DocInfo(
            r.id,
            r.source_type,
            r.uri,
            r.title,
            chunk_counts.get(r.id, 0),
            link_counts.get(r.id, 0),
            r.fetched_at,
            list(r.links or []),
        )
        for r in rows
    ]


# --- linking ------------------------------------------------------------------------


@dataclass
class LinkStats:
    manual: int = 0
    name_match: int = 0
    route: int = 0

    @property
    def total(self) -> int:
        return self.manual + self.name_match + self.route


def link_document(conn: Connection, workspace_id: int, document_id: int) -> LinkStats:
    """(Re)build all links of a document."""
    stats = LinkStats()
    doc = conn.execute(select(documents).where(documents.c.id == document_id)).one()
    chunks = conn.execute(
        select(doc_chunks.c.id, doc_chunks.c.text, doc_chunks.c.locator)
        .where(doc_chunks.c.document_id == document_id)
        .order_by(doc_chunks.c.ordinal)
    ).all()
    chunk_ids = [c.id for c in chunks]
    for start in range(0, len(chunk_ids), 500):
        conn.execute(
            delete(doc_links).where(doc_links.c.chunk_id.in_(chunk_ids[start : start + 500]))
        )

    home = _home_repo(conn, workspace_id, doc.uri)
    home_repo, home_dir = (home[1], home[2]) if home else (None, "")
    rows: dict[tuple[int, str], dict] = {}

    def add(chunk_id: int, target_type: str, key: str, source: str, confidence: float) -> None:
        current = rows.get((chunk_id, key))
        if current is None or current["confidence"] < confidence:
            rows[(chunk_id, key)] = {
                "chunk_id": chunk_id,
                "target_type": target_type,
                "target_key": key,
                "link_source": source,
                "confidence": confidence,
            }

    manual = []
    for target in doc.links or []:
        try:
            manual.append(notes.resolve_target(conn, workspace_id, target)[:2])
        except notes.TargetError:
            continue  # the target was renamed or deleted; keep the doc, drop the link
    for chunk in chunks:
        for target_type, key in manual:
            add(chunk.id, target_type, key, "manual", 0.7)

    lookups: dict[str, list] = {}
    for chunk in chunks:
        signals = context.analyze_task(chunk.text)
        quoted = set(re.findall(r"`([^`\n]+?)(?:\(\))?`", chunk.text))
        for identifier in signals.identifiers[:MAX_IDENTIFIERS_PER_CHUNK]:
            if identifier not in lookups:
                lookups[identifier] = _link_candidates(conn, workspace_id, identifier, quoted)
            hits = lookups[identifier]
            if home_repo and "." not in identifier:
                # a repository's docs describe its own code; other repositories only
                # through qualified names (``Acme.Shared.MoneyFormatter``)
                hits = [h for h in hits if h.repo == home_repo]
            if not hits or (len(hits) > 3 and "." not in identifier):
                continue  # common names like "Execute" link to everything
            confidence = {1: 0.9, 2: 0.6}.get(len(hits), 0.4)
            for key in query.stable_keys(conn, [h.id for h in hits[:3]]).values():
                add(chunk.id, "symbol", key, "name_match", confidence)
        for raw_path in signals.paths[:10]:
            matches = context.match_files(conn, workspace_id, raw_path)
            if home_repo:
                matches = [m for m in matches if m[1] == home_repo] or matches
                nearby = [m for m in matches if m[1] == home_repo and m[2].startswith(home_dir)]
                matches = nearby or matches
            if len(matches) == 1:  # "src/app.module.ts" exists in every sample
                _, repo, path = matches[0]
                add(chunk.id, "file", f"{repo}:{path}", "name_match", 0.8)

    if doc.source_type == "openapi":
        servers = http.load_endpoints(conn, workspace_id, "server")
        for chunk in chunks:
            if not chunk.locator or " " not in chunk.locator:
                continue
            method, route = chunk.locator.split(" ", 1)
            normalized = http.normalize_route(route)
            scored = []
            for server in servers:
                if server.method not in (method, None):
                    continue
                confidence = http.match_route(normalized, server.route)
                if confidence is not None:
                    scored.append(
                        ((confidence, http.specificity(normalized, server.route)), server)
                    )
            if not scored:
                continue
            best = max(rank for rank, _ in scored)
            winners = [s for rank, s in scored if rank == best][:3]
            keys = query.stable_keys(conn, [s.symbol_id for s in winners])
            for key in keys.values():
                add(chunk.id, "symbol", key, "route", round(best[0], 2))

    if rows:
        conn.execute(insert(doc_links), list(rows.values()))
    for row in rows.values():
        setattr(stats, row["link_source"], getattr(stats, row["link_source"]) + 1)
    return stats


LINK_KINDS = frozenset(
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
        "component",
    }
)
TYPE_LINK_KINDS = frozenset({"class", "interface", "struct", "record", "enum"})


def _link_candidates(conn: Connection, workspace_id: int, identifier: str, quoted: set[str]):
    """Symbols a documentation identifier refers to. Prose is full of words that are
    also member names ("main", "required", "github"), so only code-looking names
    (``renderTaxLines``, ``tax_rate``, ``Tax.Breakdown``) and type names in backticks
    are linked, and only by their exact (case-sensitive) name."""
    last = identifier.rsplit(".", 1)[-1]
    looks_like_code = "." in identifier or context.looks_like_code(identifier)
    quoted_type = identifier in quoted and last[:1].isupper() and len(last) >= 4
    if not (looks_like_code or quoted_type):
        return []
    hits = query.find_symbols(conn, workspace_id, identifier, limit=4)
    return [
        h
        for h in hits
        if h.name == last
        and not h.is_test
        and h.kind in (LINK_KINDS if looks_like_code else TYPE_LINK_KINDS)
        and (
            h.qualified_name == identifier
            or h.qualified_name.endswith("." + identifier)
            or "." not in identifier
        )
    ]


def relink_all(conn: Connection, workspace_id: int) -> LinkStats:
    total = LinkStats()
    for doc_id in conn.execute(
        select(documents.c.id).where(documents.c.workspace_id == workspace_id)
    ).scalars():
        stats = link_document(conn, workspace_id, doc_id)
        total.manual += stats.manual
        total.name_match += stats.name_match
        total.route += stats.route
    return total


# --- querying -----------------------------------------------------------------------


@dataclass
class DocHit:
    chunk_id: int
    document_id: int
    title: str | None
    uri: str
    heading_path: str | None
    locator: str | None
    text: str
    score: float = 0.0
    reason: str | None = None

    @property
    def citation(self) -> str:
        path = self.heading_path or ""
        title = self.title if self.title and not path.startswith(self.title) else None
        where = " > ".join(x for x in (title, path) if x)
        locator = f" ({self.locator})" if self.locator else ""
        return f"{where}{locator}"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()} | {"citation": self.citation}


def _chunk_hits(conn: Connection, chunk_ids: Sequence[int]) -> dict[int, DocHit]:
    result = {}
    ids = list(chunk_ids)
    for start in range(0, len(ids), 500):
        for r in conn.execute(
            select(
                doc_chunks.c.id,
                doc_chunks.c.document_id,
                doc_chunks.c.heading_path,
                doc_chunks.c.locator,
                doc_chunks.c.text,
                documents.c.title,
                documents.c.uri,
            )
            .join(documents, documents.c.id == doc_chunks.c.document_id)
            .where(doc_chunks.c.id.in_(ids[start : start + 500]))
        ):
            result[r.id] = DocHit(
                r.id, r.document_id, r.title, r.uri, r.heading_path, r.locator, r.text
            )
    return result


def has_documents(conn: Connection, workspace_id: int) -> bool:
    return (
        conn.execute(
            select(documents.c.id).where(documents.c.workspace_id == workspace_id).limit(1)
        ).first()
        is not None
    )


def search_docs(
    conn: Connection,
    workspace_id: int,
    text: str,
    *,
    limit: int = 5,
    semantic: list[tuple[int, float]] | None = None,
) -> list[DocHit]:
    """Full-text (and optionally semantic, ``(chunk_id, similarity)``) search over
    documentation chunks; at most two chunks per document."""
    fts = backend_for(conn).search_chunks(conn, workspace_id, text, limit * 6)
    scores: dict[int, float] = defaultdict(float)
    matched: dict[int, set[str]] = defaultdict(set)
    best = max((s for _, s in fts), default=0.0)
    for chunk_id, score in fts:
        if best > 0:
            scores[chunk_id] += score / best
            matched[chunk_id].add("text")
    if semantic:
        top = max(s for _, s in semantic)
        floor = min(s for _, s in semantic)
        span = (top - floor) or 1.0
        for chunk_id, similarity in semantic[: limit * 6]:
            scores[chunk_id] += (similarity - floor) / span
            matched[chunk_id].add("meaning")
    hits = _chunk_hits(conn, list(scores))
    ranked = sorted(hits.values(), key=lambda h: -scores[h.chunk_id])
    result, per_doc = [], defaultdict(int)
    for hit in ranked:
        if per_doc[hit.document_id] >= 2:
            continue
        per_doc[hit.document_id] += 1
        hit.score = round(scores[hit.chunk_id], 3)
        hit.reason = "+".join(sorted(matched[hit.chunk_id], key=["text", "meaning"].index))
        result.append(hit)
        if len(result) >= limit:
            break
    return result


def docs_for_symbols(
    conn: Connection, workspace_id: int, symbol_ids: Sequence[int], *, per_symbol: int = 3
) -> dict[int, list[DocHit]]:
    """Linked chunks per symbol: links to the symbol, its parent type, its file or a
    directory above it; strongest first, one chunk per document."""
    if not symbol_ids:
        return {}
    rows = conn.execute(
        select(
            symbols.c.id,
            symbols.c.stable_key,
            symbols.c.parent_id,
            files.c.path,
            repositories.c.name.label("repo"),
        )
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(symbols.c.id.in_(list(symbol_ids)))
    ).all()
    parents = query.stable_keys(conn, [r.parent_id for r in rows if r.parent_id])
    keys_for: dict[int, dict[str, float]] = {}
    for r in rows:
        keys = {r.stable_key: 1.0}
        if r.parent_id in parents:
            keys[parents[r.parent_id]] = 0.6
        parts = r.path.split("/")
        keys[f"{r.repo}:{r.path}"] = 0.5
        for depth in range(len(parts) - 1, -1, -1):
            keys.setdefault(f"{r.repo}:{'/'.join(parts[:depth])}", 0.3)
        keys_for[r.id] = keys
    all_keys = sorted({k for keys in keys_for.values() for k in keys})
    links_by_key: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for start in range(0, len(all_keys), 500):
        for link in conn.execute(
            select(doc_links.c.target_key, doc_links.c.chunk_id, doc_links.c.confidence)
            .join(doc_chunks, doc_chunks.c.id == doc_links.c.chunk_id)
            .join(documents, documents.c.id == doc_chunks.c.document_id)
            .where(
                doc_links.c.target_key.in_(all_keys[start : start + 500]),
                documents.c.workspace_id == workspace_id,
            )
        ):
            links_by_key[link.target_key].append((link.chunk_id, link.confidence))
    chunk_ids = {c for links in links_by_key.values() for c, _ in links}
    hits = _chunk_hits(conn, list(chunk_ids))
    result: dict[int, list[DocHit]] = {}
    for symbol_id, keys in keys_for.items():
        scored: dict[int, float] = {}
        for key, weight in keys.items():
            for chunk_id, confidence in links_by_key.get(key, []):
                scored[chunk_id] = max(scored.get(chunk_id, 0.0), weight * confidence)
        chosen, seen_docs = [], set()
        for chunk_id in sorted(scored, key=lambda c: (-scored[c], c)):
            hit = hits[chunk_id]
            if hit.document_id in seen_docs:
                continue
            seen_docs.add(hit.document_id)
            chosen.append(
                DocHit(**{**hit.__dict__, "score": round(scored[chunk_id], 3), "reason": "linked"})
            )
            if len(chosen) >= per_symbol:
                break
        if chosen:
            result[symbol_id] = chosen
    return result


def read_chunk(conn: Connection, chunk_id: int) -> DocHit | None:
    return _chunk_hits(conn, [chunk_id]).get(chunk_id)


def linked_symbols(conn: Connection, chunk_id: int) -> list[tuple[str, str, float]]:
    """``(target_key, link_source, confidence)`` of a chunk, strongest first."""
    rows = conn.execute(
        select(doc_links.c.target_key, doc_links.c.link_source, doc_links.c.confidence)
        .where(doc_links.c.chunk_id == chunk_id)
        .order_by(doc_links.c.confidence.desc(), doc_links.c.target_key)
    )
    return [(r.target_key, r.link_source, r.confidence) for r in rows]


# --- service functions for CLI and MCP ----------------------------------------------


@dataclass
class AddResult:
    source: str
    document_id: int | None = None
    title: str | None = None
    chunks: int = 0
    changed: bool = False
    links: LinkStats | None = None
    error: str | None = None


def add_sources(
    engine: Engine,
    workspace_id: int,
    sources: Iterable[str],
    *,
    links: Sequence[str] | None = None,
    fetch: Callable[[str], tuple[bytes, str]] = fetch_url,
) -> list[AddResult]:
    """Load, store and link documents (directories are expanded); errors are reported
    per source instead of aborting the others."""
    results = []
    for source in expand_sources(sources):
        try:
            loaded = load(source, fetch=fetch)
        except DocError as exc:
            results.append(AddResult(source, error=str(exc)))
            continue
        try:
            with engine.begin() as conn:
                document_id, changed = store_document(conn, workspace_id, loaded, links=links)
                stats = link_document(conn, workspace_id, document_id)
        except notes.TargetError as exc:
            results.append(AddResult(source, error=str(exc)))
            continue
        results.append(
            AddResult(source, document_id, loaded.title, len(loaded.chunks), changed, stats)
        )
    return results


def refresh(
    engine: Engine,
    workspace_id: int,
    *,
    fetch: Callable[[str], tuple[bytes, str]] = fetch_url,
) -> list[AddResult]:
    """Re-read every document (files from disk, URLs from the web) and relink all, so
    that new or renamed code is picked up."""
    with engine.connect() as conn:
        uris = [d.uri for d in list_documents(conn, workspace_id)]
    results = []
    for uri in uris:
        try:
            loaded = load(uri, fetch=fetch)
        except DocError as exc:
            results.append(AddResult(uri, error=str(exc)))
            continue
        with engine.begin() as conn:
            document_id, changed = store_document(conn, workspace_id, loaded)
            stats = link_document(conn, workspace_id, document_id)
        results.append(
            AddResult(uri, document_id, loaded.title, len(loaded.chunks), changed, stats)
        )
    return results
