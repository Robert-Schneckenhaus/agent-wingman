"""From a bug report or task description to a compact "context pack" (CONCEPT.md 5).

Signals extracted from the text, strongest first:
  1. stack-trace frames (file + line, or C# ``Namespace.Type.Method``)
  2. identifiers mentioned in the text (``TaxCalculator``, ``renderTaxLines()``, ``A.B``)
  3. file paths mentioned without a line number
  4. full-text search over the whole text

Candidates are merged, then expanded by one hop in the call graph, and rendered
with callers, callees, tests and notes so an agent can jump straight to the code.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Connection

from wingman import notes as notes_service
from wingman import query
from wingman.query import SymbolHit
from wingman.storage.store import Note

_SOURCE_EXT = r"(?:tsx?|mts|cts|cs|jsx?|mjs|cjs)"
_CS_FRAME = re.compile(
    r"\bat (?P<qual>[\w.`<>+]+)\.(?P<method>[\w<>]+)\([^)\n]*\)"
    r"(?: in (?P<path>[^\n]+?):line (?P<line>\d+))?"
)
_JS_FRAME = re.compile(
    rf"\bat (?:(?P<fn>[\w$.<>\[\] ]+?) \()?(?:file://)?(?P<path>[^\s()]+?\.{_SOURCE_EXT})"
    r":(?P<line>\d+)(?::\d+)?\)?"
)
_PATH_LINE = re.compile(
    rf"(?P<path>[\w.@/\\-]+\.{_SOURCE_EXT})(?::|\(|:line |, line )(?P<line>\d+)"
)
_PATH = re.compile(rf"(?<![\w/\\.-])(?P<path>[\w.@/\\-]*[\w-]\.{_SOURCE_EXT})\b")
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_IDENTIFIER = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\b")
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(")
_STOPWORDS = frozenset(
    """
    the and for with that this from when then than into onto over under after before not
    but are was were has have had does did can could should would will shall may might
    error exception bug issue fix fixes fixed problem expected actual result results
    line file files code test tests value values null undefined true false none todo der
    die das und oder nicht ist sind wird werden beim bei mit ohne für von zum zur ein
    eine einen einem einer wenn dann aber auch noch schon fehler problem soll sollte
    """.split()  # noqa: SIM905
)


@dataclass
class Frame:
    path: str | None
    line: int | None
    qualified: str | None = None


@dataclass
class TaskSignals:
    frames: list[Frame] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


def _looks_like_code(token: str) -> bool:
    if re.search(r"d__\d|^<", token):  # compiler-generated async state machines
        return False
    if token.isupper() and "_" not in token:  # acronyms such as PDF, VAT
        return False
    if "." in token:
        parts = token.split(".")
        return (
            all(parts)
            and any(p[:1].isupper() or "_" in p for p in parts)
            and not token[0].isdigit()
        )
    if token.lower() in _STOPWORDS or len(token) < 3:
        return False
    has_inner_upper = any(c.isupper() for c in token[1:])
    return has_inner_upper or "_" in token.strip("_")


def analyze_task(text: str) -> TaskSignals:
    signals = TaskSignals()
    seen_frames: set[tuple] = set()

    def add_frame(frame: Frame) -> None:
        if frame.path:  # the same frame may be matched by several patterns
            key = (frame.path.rsplit("/", 1)[-1], frame.line)
        else:
            key = (None, frame.line, frame.qualified)
        if key not in seen_frames:
            seen_frames.add(key)
            signals.frames.append(frame)

    for match in _CS_FRAME.finditer(text):
        qualified = f"{match['qual']}.{match['method']}"
        line = int(match["line"]) if match["line"] else None
        add_frame(Frame(_normalize_path(match["path"]), line, qualified))
    for match in _JS_FRAME.finditer(text):
        add_frame(Frame(_normalize_path(match["path"]), int(match["line"])))
    for match in _PATH_LINE.finditer(text):
        add_frame(Frame(_normalize_path(match["path"]), int(match["line"])))
    framed_paths = {f.path.rsplit("/", 1)[-1] for f in signals.frames if f.path}

    for match in _PATH.finditer(text):
        path = _normalize_path(match["path"])
        if path.rsplit("/", 1)[-1] not in framed_paths and path not in signals.paths:
            signals.paths.append(path)

    def add_identifier(raw: str) -> None:
        token = re.sub(r"\(.*$", "", raw.strip()).strip(" .")
        if re.search(r"d__\d|[<>]", token):  # compiler-generated names
            return
        if token and " " not in token and token not in signals.identifiers:
            signals.identifiers.append(token)

    for match in _BACKTICK.finditer(text):  # `renderTaxLines()`: always code
        add_identifier(match.group(1))
    prose = _PATH.sub(" ", text)
    for match in _CALL.finditer(prose):  # render(): code because of the parentheses
        if len(match.group(1)) >= 3 and match.group(1).lower() not in _STOPWORDS:
            add_identifier(match.group(1))
    for token in _IDENTIFIER.findall(prose):  # CamelCase, snake_case, A.B
        if _looks_like_code(token):
            add_identifier(token)
    return signals


def _normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    path = path.strip().replace("\\", "/")
    path = re.sub(r"^[a-z]+://", "", path)  # webpack://, file://
    path = re.sub(r"^[A-Za-z]:/", "/", path)  # Windows drive
    return path.lstrip("/").removeprefix("./")


def match_files(conn: Connection, workspace_id: int, path: str) -> list[tuple[int, str, str]]:
    """Index files for a path from a report, which may be absolute or from another machine:
    tries ever shorter suffixes (``C:/build/src/Api/X.cs`` -> ``src/Api/X.cs`` -> ...)."""
    parts = path.split("/")
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        matches = query.find_files(conn, workspace_id, candidate)
        if matches:
            return matches
    return []


# --- ranking -----------------------------------------------------------------------


@dataclass
class PackEntry:
    hit: SymbolHit
    score: float
    reasons: list[str]
    called_by: list[SymbolHit] = field(default_factory=list)
    calls: list[SymbolHit] = field(default_factory=list)
    tests: list[SymbolHit] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    code: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.hit.to_dict(),
            "score": round(self.score, 3),
            "reasons": self.reasons,
            "called_by": [h.to_dict() for h in self.called_by],
            "calls": [h.to_dict() for h in self.calls],
            "tests": [h.to_dict() for h in self.tests],
            "notes": [n.text for n in self.notes],
            "code": self.code,
        }


@dataclass
class ContextPack:
    signals: TaskSignals
    entries: list[PackEntry]
    candidates: int
    unknown_paths: list[str] = field(default_factory=list)
    file_notes: list[Note] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "signals": {
                "frames": [f.__dict__ for f in self.signals.frames],
                "identifiers": self.signals.identifiers,
                "paths": self.signals.paths,
            },
            "candidates": self.candidates,
            "entries": [e.to_dict() for e in self.entries],
            "unknown_paths": self.unknown_paths,
            "file_notes": [{"target": n.target_key, "text": n.text} for n in self.file_notes],
        }


class _Scores:
    def __init__(self) -> None:
        self.score: dict[int, float] = defaultdict(float)
        self.reasons: dict[int, list[str]] = defaultdict(list)
        self.hits: dict[int, SymbolHit] = {}

    def add(self, hit: SymbolHit, points: float, reason: str) -> None:
        self.hits[hit.id] = hit
        self.score[hit.id] += points
        if reason not in self.reasons[hit.id]:
            self.reasons[hit.id].append(reason)


def find_relevant(
    conn: Connection,
    workspace_id: int,
    text: str,
    *,
    limit: int = 8,
    include_code: bool = False,
    code_entries: int = 3,
    semantic: Callable[[str], list[tuple[int, float]] | None] | None = None,
) -> ContextPack:
    """``semantic`` maps text to ``(symbol_id, similarity)`` candidates (see
    ``Workspace.semantic_search``); without it only full-text search is used."""
    signals = analyze_task(text)
    scores = _Scores()
    unknown_paths: list[str] = []
    mentioned_files: list[str] = []

    # 1. stack frames: the innermost symbol at the reported line. If the frame also names
    #    the method and that name differs, the trace comes from an older build: trust the
    #    name and keep the symbol at the line only as a weak candidate.
    for position, frame in enumerate(signals.frames[:15]):
        weight = max(10.0 - 2 * position, 4.0)  # the top frame is usually the failure site
        method = _clean_frame_name(frame.qualified) if frame.qualified else None
        found = False
        if frame.path:
            for file_id, repo, path in match_files(conn, workspace_id, frame.path)[:2]:
                mentioned_files.append(f"{repo}:{path}")
                hit = query.symbol_at(conn, file_id, frame.line) if frame.line else None
                if hit is None:
                    continue
                reason = f"stack frame {path.rsplit('/', 1)[-1]}:{frame.line}"
                if method and hit.name != method.rsplit(".", 1)[-1]:
                    scores.add(hit, weight * 0.3, reason + " (line moved?)")
                else:
                    scores.add(hit, weight, reason)
                    found = True
        if not found and method:
            lookup = query.resolve_name(conn, workspace_id, method)
            for hit in lookup.found[:3]:
                scores.add(hit, weight * 0.9, f"stack frame {hit.name}")
                found = True
        if not found and frame.path:
            unknown_paths.append(frame.path)

    # 2. identifiers mentioned in the text (names inside stack frames are already counted)
    frame_names = " ".join(f.qualified for f in signals.frames if f.qualified)
    for identifier in signals.identifiers[:30]:
        if identifier in frame_names:
            continue
        hits = query.find_symbols(conn, workspace_id, identifier, limit=6)
        if not hits:
            continue
        share = 6.0 / len(hits) if len(hits) <= 3 else 1.0
        for hit in hits:
            scores.add(hit, share, f"mentions `{identifier}`")

    # 3. files mentioned without a line
    for raw_path in signals.paths[:10]:
        matches = match_files(conn, workspace_id, raw_path)
        if not matches:
            unknown_paths.append(raw_path)
            continue
        for file_id, repo, path in matches[:2]:
            mentioned_files.append(f"{repo}:{path}")
            top = [e.symbol for e in query.outline(conn, file_id) if e.depth == 0][:4]
            for hit in top:
                scores.add(hit, 3.0 / max(len(top), 1), f"file {path.rsplit('/', 1)[-1]} mentioned")

    # 4. full-text search over the whole text
    prose = "\n".join(line for line in text.splitlines() if not _is_frame_line(line))
    similar = semantic(prose) if semantic is not None and prose.strip() else None
    fts = query.search(conn, workspace_id, _fts_text(prose), limit=25, semantic=similar)
    best = max((hit.score or 0.0 for hit in fts), default=0.0)
    for hit in fts:
        if best > 0:
            reason = {
                "meaning": "similar meaning",
                "text+meaning": "text match + similar meaning",
            }.get(hit.matched_by or "", "text match")
            scores.add(hit, 4.0 * (hit.score or 0.0) / best, reason)

    mentions_tests = bool(re.search(r"\btests?\b|\bspec\b", text, re.IGNORECASE))
    for symbol_id, hit in scores.hits.items():
        if hit.is_test and not mentions_tests:
            scores.score[symbol_id] *= 0.5
        if hit.is_generated:
            scores.score[symbol_id] *= 0.3

    # 5. one hop in the call graph from the strongest candidates; capped so that
    #    neighbours never overtake symbols with direct evidence
    strongest = sorted(scores.score, key=lambda i: -scores.score[i])[:6]
    for symbol_id in strongest:
        bonus = min(scores.score[symbol_id] * 0.15, 1.5)
        name = scores.hits[symbol_id].name
        for ref in query.callers(conn, symbol_id):
            if ref.symbol is not None and not ref.symbol.is_test:
                scores.add(ref.symbol, bonus, f"calls {name}")
        for ref in query.callees(conn, symbol_id):
            if ref.symbol is not None and not ref.symbol.is_test:
                scores.add(ref.symbol, bonus * 0.7, f"called by {name}")

    ranked = sorted(scores.score, key=lambda i: -scores.score[i])
    entries = []
    for position, symbol_id in enumerate(ranked[:limit]):
        hit = scores.hits[symbol_id]
        entry = PackEntry(hit, scores.score[symbol_id], scores.reasons[symbol_id])
        entry.called_by = _unique(
            [r.symbol for r in query.callers(conn, symbol_id) if r.symbol and not r.symbol.is_test]
        )[:3]
        entry.calls = _unique([r.symbol for r in query.callees(conn, symbol_id) if r.symbol])[:4]
        entry.tests = query.tests_for(conn, symbol_id, depth=2)[:3]
        if include_code and position < code_entries:
            entry.code = query.read_source(conn, symbol_id, max_lines=60)
        entries.append(entry)

    notes_by_symbol = query.notes_for_symbols(conn, workspace_id, [e.hit.id for e in entries])
    for entry in entries:
        entry.notes = notes_by_symbol.get(entry.hit.id, [])
    files = sorted({f"{e.hit.repo}:{e.hit.path}" for e in entries} | set(mentioned_files))
    return ContextPack(
        signals=signals,
        entries=entries,
        candidates=len(scores.score),
        unknown_paths=sorted(set(unknown_paths)),
        file_notes=notes_service.for_files(conn, workspace_id, files),
    )


def _is_frame_line(line: str) -> bool:
    return bool(re.match(r"\s*at\s", line)) and bool(
        _CS_FRAME.search(line) or _JS_FRAME.search(line)
    )


def _clean_frame_name(qualified: str) -> str:
    """C# async frames: ``Ns.Controller.<GetTax>d__3.MoveNext`` -> ``Ns.Controller.GetTax``."""
    async_state = re.search(r"\.<(\w+)>d__\d+\.MoveNext$", qualified)
    if async_state:
        qualified = qualified[: async_state.start()] + "." + async_state.group(1)
    return re.sub(r"`\d+", "", qualified)


def _fts_text(text: str, max_terms: int = 60) -> str:
    words = [w for w in re.findall(r"[A-Za-z_$][\w$]{2,}", text) if w.lower() not in _STOPWORDS]
    unique = list(dict.fromkeys(words))
    return " ".join(unique[:max_terms])


def _unique(hits: list[SymbolHit]) -> list[SymbolHit]:
    seen: dict[int, SymbolHit] = {}
    for hit in hits:
        seen.setdefault(hit.id, hit)
    return list(seen.values())


# --- rendering ---------------------------------------------------------------------


def _short(hit: SymbolHit) -> str:
    return f"{hit.qualified_name} ({hit.repo}:{hit.path}:{hit.start_line})"


def render_pack(pack: ContextPack, *, budget: int = 4000) -> str:
    """Markdown context pack, cut to roughly ``budget`` tokens (≈ 4 characters each)."""
    max_chars = budget * 4
    signals = pack.signals
    summary = []
    if signals.frames:
        summary.append(f"{len(signals.frames)} stack frame(s)")
    if signals.identifiers:
        summary.append("identifiers " + ", ".join(f"`{i}`" for i in signals.identifiers[:8]))
    if signals.paths:
        summary.append("files " + ", ".join(signals.paths[:5]))
    header = [f"## Relevant code ({len(pack.entries)} of {pack.candidates} candidates)"]
    if summary:
        header.append("Signals: " + "; ".join(summary))
    if not pack.entries:
        header.append("\nNothing matched. Try other words or `search_code` with specific names.")
        return "\n".join(header)

    blocks: list[str] = []
    used = sum(len(line) + 1 for line in header)
    for number, entry in enumerate(pack.entries, 1):
        hit = entry.hit
        lines = [f"\n{number}. {hit.location}  {hit.kind} {hit.qualified_name}"]
        if hit.signature:
            lines.append(f"   {hit.signature}")
        if hit.summary:
            lines.append(f"   ↳ {hit.summary}")
        lines.append(f"   why: {' · '.join(entry.reasons[:4])}")
        relations = []
        if entry.called_by:
            relations.append("called by: " + ", ".join(_short(h) for h in entry.called_by))
        if entry.calls:
            relations.append("calls: " + ", ".join(h.qualified_name for h in entry.calls))
        lines += [f"   {r}" for r in relations]
        if entry.tests:
            lines.append("   tests: " + ", ".join(_short(t) for t in entry.tests))
        lines += [f"   note: {n.text}" for n in entry.notes]
        block = "\n".join(lines)
        if entry.code:
            with_code = f"{block}\n   ```\n{_indent(entry.code)}\n   ```"
            if used + len(with_code) <= max_chars:
                block = with_code
        if blocks and used + len(block) > max_chars:
            remaining = len(pack.entries) - len(blocks)
            blocks.append(f"\n… {remaining} more (token budget reached)")
            break
        blocks.append(block)
        used += len(block) + 1

    footer = []
    if pack.file_notes:
        footer.append("\nNotes on these files:")
        footer += [f"- {n.target_key}: {n.text}" for n in pack.file_notes]
    if pack.unknown_paths:
        footer.append("\nNot in the index: " + ", ".join(pack.unknown_paths))
    return "\n".join(header + blocks + footer)


def _indent(code: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line for line in code.splitlines())
