"""LLM summaries for undocumented code (CONCEPT.md 3.5).

Symbols without a doc comment get a one- or two-sentence description plus a few
search keywords, written by an LLM (Anthropic by default). The result is stored in
``symbols.summary`` (``summary_source = 'generated'``), added to the full-text index
and used as the doc part of the embedding text, so undocumented code becomes findable
by what it does.

- **Opt-in and budgeted:** nothing runs unless ``[summaries] enabled = true``; a run
  stops when ``max_tokens_per_run`` (input + output) is spent.
- **Content-addressed:** ``summary_cache`` is keyed by ``hash(prompt version + model +
  body_hash)``, so unchanged code is never summarized twice, also not after
  re-indexing or on another branch. Re-indexed symbols get their summary back from the
  cache for free (:func:`apply_cached`).
- **Bottom-up:** callees and members are summarized before their callers and classes,
  so a prompt can include the summaries of what a symbol calls.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from wingman import embeddings
from wingman.config import SummaryConfig
from wingman.extract import content_hash
from wingman.storage.schema import files, refs, repositories, summary_cache, symbols
from wingman.storage.search import FtsRow, backend_for

PROMPT_VERSION = "1"
SUMMARY_KINDS = frozenset(
    {"class", "interface", "struct", "record", "function", "method", "component"}
)
TYPE_KINDS = embeddings.TYPE_KINDS
DEPENDENCY_KINDS = ("call", "instantiate")
MAX_BODY_CHARS = 6000
MAX_TYPE_HEAD_CHARS = 1500
MAX_CONTEXT_ITEMS = 12
MAX_BATCH_SYMBOLS = 8
MAX_BATCH_CHARS = 24_000
OUTPUT_TOKENS_PER_SYMBOL = 80  # estimate for --dry-run
CHARS_PER_TOKEN = 3.5

SYSTEM_PROMPT = """\
You write short descriptions of source code symbols for a code search index that
coding agents use to find the code relevant to a bug report or task.

For each symbol, write one or two sentences (at most about 40 words, in English)
saying what it does or what it is responsible for, in domain terms: which business
concept it handles, its important side effects and conditions. Do not repeat the
signature mechanically and do not start with "This method" or "This class".
Also give up to 5 keywords: domain terms and synonyms a developer might search for
that are not already part of the name.

Summaries of called code and members are provided as context where known. Answer
for every symbol id in the request."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "summary", "keywords"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["summaries"],
    "additionalProperties": False,
}


class SummaryError(Exception):
    def __init__(self, message: str, *, fatal: bool = False, tokens: int = 0) -> None:
        super().__init__(message)
        self.fatal = fatal
        """Stop the run (no credentials, unknown model) instead of skipping one batch."""
        self.tokens = tokens


def cache_key(body_hash: str, model: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{model}\0{body_hash}".encode()).hexdigest()


# --- providers ----------------------------------------------------------------------


class Summarizer(Protocol):
    model: str

    def summarize(self, prompt: str) -> tuple[dict, int]:
        """Return the parsed JSON answer (see ``OUTPUT_SCHEMA``) and the tokens used."""
        ...


class AnthropicSummarizer:
    """Claude via the official SDK; credentials come from the environment
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an ``ant auth login`` profile)."""

    def __init__(self, model: str, max_output_tokens: int = 8000) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise SummaryError(
                "The Anthropic SDK is not installed: pip install 'agent-wingman[summaries]'",
                fatal=True,
            ) from exc
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def summarize(self, prompt: str) -> tuple[dict, int]:
        api = self._anthropic
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_output_tokens,
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except TypeError as exc:  # raised by the SDK when no credentials are configured
            raise SummaryError(f"No Anthropic credentials: {exc}", fatal=True) from exc
        except (api.AuthenticationError, api.PermissionDeniedError) as exc:
            raise SummaryError(f"Anthropic API access denied: {exc}", fatal=True) from exc
        except (api.NotFoundError, api.BadRequestError) as exc:
            raise SummaryError(f"Anthropic API rejected the request: {exc}", fatal=True) from exc
        except (api.APIStatusError, api.APIConnectionError) as exc:  # retried by the SDK
            raise SummaryError(f"Anthropic API error: {exc}") from exc
        used = response.usage.input_tokens + response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            raise SummaryError(
                f"no usable answer (stop reason {response.stop_reason})", tokens=used
            )
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text), used
        except json.JSONDecodeError as exc:
            raise SummaryError(f"invalid JSON in answer: {exc}", tokens=used) from exc


# --- candidates ---------------------------------------------------------------------


def _candidate_query(workspace_id: int, config: SummaryConfig, repo_ids: Sequence[int] | None):
    lines = symbols.c.end_line - symbols.c.start_line + 1
    query = (
        select(
            symbols.c.id,
            symbols.c.parent_id,
            symbols.c.kind,
            symbols.c.name,
            symbols.c.qualified_name,
            symbols.c.signature,
            symbols.c.body_hash,
            symbols.c.start_line,
            symbols.c.end_line,
            symbols.c.start_byte,
            symbols.c.end_byte,
            files.c.id.label("file_id"),
            files.c.path,
            files.c.content_hash,
            repositories.c.name.label("repo"),
            repositories.c.root_path,
        )
        .join(files, files.c.id == symbols.c.file_id)
        .join(repositories, repositories.c.id == files.c.repo_id)
        .where(
            repositories.c.workspace_id == workspace_id,
            symbols.c.kind.in_(SUMMARY_KINDS),
            symbols.c.doc.is_(None),
            symbols.c.summary_source.is_(None),
            symbols.c.body_hash.is_not(None),
            files.c.is_test.is_(False),
            files.c.is_generated.is_(False),
            symbols.c.kind.in_(TYPE_KINDS) | (lines >= config.min_lines),
        )
        .order_by(files.c.id, symbols.c.start_byte)
    )
    if repo_ids is not None:
        query = query.where(repositories.c.id.in_(list(repo_ids)))
    return query


@dataclass
class _Item:
    row: object
    body: str
    data: bytes


def _load_items(rows) -> tuple[list[_Item], int]:
    """Read the source of each candidate; skips files that changed since indexing."""
    items: list[_Item] = []
    skipped = 0
    by_file: dict[int, list] = defaultdict(list)
    for row in rows:
        by_file[row.file_id].append(row)
    for file_rows in by_file.values():
        first = file_rows[0]
        try:
            data = (Path(first.root_path) / first.path).read_bytes()
        except OSError:
            skipped += len(file_rows)
            continue
        if content_hash(data) != first.content_hash:
            skipped += len(file_rows)
            continue
        for row in file_rows:
            body = embeddings.body_text(data, row.start_byte, row.end_byte)
            items.append(_Item(row, body, data))
    return items, skipped


# --- applying summaries -------------------------------------------------------------


def _apply(conn: Connection, items: list[tuple[_Item, str, list[str]]]) -> None:
    """Store summaries on symbols and refresh their full-text and embedding entries."""
    if not items:
        return
    type_ids = [i.row.id for i, _, _ in items if i.row.kind in TYPE_KINDS]
    members = embeddings.member_names(conn, type_ids) if type_ids else {}
    fts_rows = []
    for item, summary, keywords in items:
        row = item.row
        embed_hash = None
        if row.kind in embeddings.EMBED_KINDS:
            embed_hash = embeddings.text_hash(
                embeddings.embedding_text(
                    kind=row.kind,
                    qualified_name=row.qualified_name,
                    signature=row.signature,
                    doc=summary,
                    path=row.path,
                    body=item.body,
                    members=members.get(row.id, ()),
                )
            )
        conn.execute(
            update(symbols)
            .where(symbols.c.id == row.id)
            .values(summary=summary, summary_source="generated", embed_hash=embed_hash)
        )
        searchable = f"{summary} {' '.join(keywords)}".strip()
        fts_rows.append(
            FtsRow(row.id, row.name, row.qualified_name, row.signature, searchable, row.path)
        )
    backend_for(conn).replace(conn, fts_rows)


def _store_cache(conn: Connection, model: str, entries: list[tuple[str, str, list[str]]]) -> None:
    if not entries:
        return
    rows = [{"key": k, "summary": s, "keywords": kw, "model": model} for k, s, kw in entries]
    if conn.dialect.name == "postgresql":
        statement = pg_insert(summary_cache).on_conflict_do_nothing()
    else:
        statement = insert(summary_cache).prefix_with("OR IGNORE")
    conn.execute(statement, rows)


def apply_cached(
    engine: Engine,
    workspace_id: int,
    config: SummaryConfig,
    repo_ids: Sequence[int] | None = None,
) -> int:
    """Give symbols without a summary the cached one for their code; returns how many."""
    with engine.connect() as conn:
        rows = conn.execute(_candidate_query(workspace_id, config, repo_ids)).all()
        if not rows:
            return 0
        keys = {row.id: cache_key(row.body_hash, config.model) for row in rows}
        cached: dict[str, tuple[str, list[str]]] = {}
        unique = sorted(set(keys.values()))
        for start in range(0, len(unique), 500):
            for entry in conn.execute(
                select(
                    summary_cache.c.key, summary_cache.c.summary, summary_cache.c.keywords
                ).where(summary_cache.c.key.in_(unique[start : start + 500]))
            ):
                cached[entry.key] = (entry.summary, entry.keywords or [])
    hits = [row for row in rows if keys[row.id] in cached]
    if not hits:
        return 0
    items, _ = _load_items(hits)
    with engine.begin() as conn:
        _apply(conn, [(i, *cached[keys[i.row.id]]) for i in items])
    return len(items)


# --- ordering and prompts -----------------------------------------------------------


def _levels(ids: list[int], deps: dict[int, set[int]]) -> dict[int, int]:
    """Level 0 = depends on no other candidate; symbols in a cycle share a level, one
    above everything the cycle depends on. ``deps`` must only contain ``ids``."""
    component = _components(ids, deps)
    by_component: dict[int, list[int]] = defaultdict(list)
    for s in ids:
        by_component[component[s]].append(s)
    level_of: dict[int, int] = {}
    for c in sorted(by_component):  # dependencies have lower component numbers
        level_of[c] = 1 + max(
            (
                level_of[component[d]]
                for s in by_component[c]
                for d in deps.get(s, ())
                if component[d] != c
            ),
            default=-1,
        )
    return {s: level_of[component[s]] for s in ids}


def _components(ids: list[int], deps: dict[int, set[int]]) -> dict[int, int]:
    """Strongly connected components (iterative Tarjan), numbered so that a component
    comes after every component it depends on."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    component: dict[int, int] = {}
    components = 0

    def visit(node: int, work: list) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        work.append((node, iter(sorted(deps.get(node, ())))))

    for root in ids:
        if root in index:
            continue
        work: list = []
        visit(root, work)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    visit(child, work)
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component[member] = components
                    if member == node:
                        break
                components += 1
    return component


def _dependencies(conn: Connection, items: list[_Item]) -> tuple[dict, dict]:
    """Candidate -> candidates it depends on (callees, members), and candidate -> all
    resolved callee ids (for context)."""
    ids = [i.row.id for i in items]
    id_set = set(ids)
    callees: dict[int, list[int]] = defaultdict(list)
    for start in range(0, len(ids), 500):
        for r in conn.execute(
            select(refs.c.from_symbol_id, refs.c.to_symbol_id)
            .where(
                refs.c.from_symbol_id.in_(ids[start : start + 500]),
                refs.c.kind.in_(DEPENDENCY_KINDS),
                refs.c.to_symbol_id.is_not(None),
            )
            .distinct()
        ):
            if r.to_symbol_id != r.from_symbol_id:
                callees[r.from_symbol_id].append(r.to_symbol_id)
    deps: dict[int, set[int]] = defaultdict(set)
    for s, targets in callees.items():
        deps[s].update(t for t in targets if t in id_set)
    for item in items:
        parent = item.row.parent_id
        if parent in id_set:
            deps[parent].add(item.row.id)  # a class after its members
    return deps, callees


def _context(conn: Connection, ids: set[int]) -> dict[int, tuple[str, str | None]]:
    """qualified name and known description (doc summary or generated) per symbol."""
    result = {}
    id_list = sorted(ids)
    for start in range(0, len(id_list), 500):
        for r in conn.execute(
            select(symbols.c.id, symbols.c.qualified_name, symbols.c.summary).where(
                symbols.c.id.in_(id_list[start : start + 500])
            )
        ):
            result[r.id] = (r.qualified_name, r.summary)
    return result


def _member_rows(conn: Connection, parent_ids: list[int]) -> dict[int, list[int]]:
    members: dict[int, list[int]] = defaultdict(list)
    for start in range(0, len(parent_ids), 500):
        for r in conn.execute(
            select(symbols.c.parent_id, symbols.c.id)
            .where(symbols.c.parent_id.in_(parent_ids[start : start + 500]))
            .order_by(symbols.c.parent_id, symbols.c.start_byte)
        ):
            members[r.parent_id].append(r.id)
    return members


def _symbol_block(
    number: int,
    item: _Item,
    callees: list[int],
    members: list[int],
    known: dict[int, tuple[str, str | None]],
) -> str:
    row = item.row
    lines = [
        f'<symbol id="{number}">',
        f"kind: {row.kind}",
        f"name: {row.qualified_name}",
        f"file: {row.repo}:{row.path}",
    ]
    if row.signature:
        lines.append(f"signature: {row.signature}")
    if row.kind in TYPE_KINDS:
        if members:
            lines.append("members:")
            for m in members[:40]:
                name, summary = known.get(m, ("?", None))
                lines.append(f"- {name.rsplit('.', 1)[-1]}" + (f": {summary}" if summary else ""))
        code = item.body[:MAX_TYPE_HEAD_CHARS]
    else:
        context = [known[c] for c in dict.fromkeys(callees) if c in known][:MAX_CONTEXT_ITEMS]
        if context:
            lines.append("calls:")
            lines += [
                f"- {name}" + (f": {summary}" if summary else "") for name, summary in context
            ]
        code = item.body[:MAX_BODY_CHARS]
    lines += ["code:", code.strip(), "</symbol>"]
    return "\n".join(lines)


# --- running ------------------------------------------------------------------------


@dataclass
class SummaryStats:
    candidates: int = 0
    from_cache: int = 0
    summarized: int = 0
    failed: int = 0
    skipped: int = 0
    """Symbols whose file changed since indexing (re-index first)."""
    tokens: int = 0
    """Tokens used, or estimated with ``dry_run``."""
    requests: int = 0
    budget_exhausted: bool = False
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.candidates - self.summarized - self.failed - self.skipped


def summarize_workspace(
    engine: Engine,
    workspace_id: int,
    config: SummaryConfig,
    summarizer: Summarizer | None,
    *,
    repo_ids: Sequence[int] | None = None,
    limit: int | None = None,
    max_tokens: int | None = None,
    dry_run: bool = False,
    workers: int = 4,
    progress: Callable[[str], None] | None = None,
) -> SummaryStats:
    """Summarize undocumented symbols bottom-up until done or the budget is spent.

    With ``dry_run`` no request is sent; ``tokens`` is then an estimate for all
    candidates (or ``limit`` of them)."""
    started = time.monotonic()
    stats = SummaryStats()
    model = summarizer.model if summarizer is not None else config.model
    budget = max_tokens if max_tokens is not None else config.max_tokens_per_run
    if not dry_run:
        stats.from_cache = apply_cached(engine, workspace_id, config, repo_ids)

    with engine.connect() as conn:
        rows = conn.execute(_candidate_query(workspace_id, config, repo_ids)).all()
        items, stats.skipped = _load_items(rows)
        stats.candidates = len(items) + stats.skipped
        deps, callees = _dependencies(conn, items)
        type_ids = [i.row.id for i in items if i.row.kind in TYPE_KINDS]
        members = _member_rows(conn, type_ids) if type_ids else {}
        context_ids = {c for cs in callees.values() for c in cs}
        context_ids |= {m for ms in members.values() for m in ms}
        known = _context(conn, context_ids)

    levels = _levels([i.row.id for i in items], deps)
    by_level: dict[int, list[_Item]] = defaultdict(list)
    for item in items:
        by_level[levels[item.row.id]].append(item)
    if limit is not None:
        ordered = [i for lvl in sorted(by_level) for i in by_level[lvl]][:limit]
        keep = {i.row.id for i in ordered}
        by_level = {lvl: [i for i in its if i.row.id in keep] for lvl, its in by_level.items()}

    def batches(level_items: list[_Item]) -> list[list[_Item]]:
        result, current, size = [], [], 0
        for item in sorted(level_items, key=lambda i: (i.row.file_id, i.row.start_byte)):
            length = min(len(item.body), MAX_BODY_CHARS) + 500
            if current and (len(current) >= MAX_BATCH_SYMBOLS or size + length > MAX_BATCH_CHARS):
                result.append(current)
                current, size = [], 0
            current.append(item)
            size += length
        if current:
            result.append(current)
        return result

    def prompt_for(batch: list[_Item]) -> str:
        blocks = [
            _symbol_block(
                n, item, callees.get(item.row.id, []), members.get(item.row.id, []), known
            )
            for n, item in enumerate(batch, 1)
        ]
        return "Describe these symbols.\n\n" + "\n\n".join(blocks)

    total = sum(len(v) for v in by_level.values())
    done = 0
    for lvl in sorted(by_level):
        level_batches = batches(by_level[lvl])
        if dry_run:
            for batch in level_batches:
                stats.tokens += int(len(SYSTEM_PROMPT + prompt_for(batch)) / CHARS_PER_TOKEN)
                stats.tokens += OUTPUT_TOKENS_PER_SYMBOL * len(batch)
                stats.requests += 1
            continue
        assert summarizer is not None
        for start in range(0, len(level_batches), workers):
            if stats.tokens >= budget:
                stats.budget_exhausted = True
                break
            group = level_batches[start : start + workers]
            prompts = [prompt_for(b) for b in group]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(lambda p: _call(summarizer, p), prompts))
            results, cache_entries = [], []
            for batch, outcome in zip(group, outcomes, strict=True):
                stats.requests += 1
                if isinstance(outcome, SummaryError):
                    stats.tokens += outcome.tokens
                    stats.failed += len(batch)
                    stats.errors.append(str(outcome))
                    if outcome.fatal:
                        stats.seconds = round(time.monotonic() - started, 2)
                        return stats
                    continue
                answer, used = outcome
                stats.tokens += used
                by_number = {
                    str(entry.get("id", "")).strip(): entry
                    for entry in answer.get("summaries", [])
                    if isinstance(entry, dict)
                }
                for n, item in enumerate(batch, 1):
                    entry = by_number.get(str(n))
                    summary = (entry or {}).get("summary", "").strip()
                    if not summary:
                        stats.failed += 1
                        continue
                    keywords = [str(k) for k in entry.get("keywords", [])][:5]
                    results.append((item, summary, keywords))
                    cache_entries.append((cache_key(item.row.body_hash, model), summary, keywords))
                    known[item.row.id] = (item.row.qualified_name, summary)
            with engine.begin() as conn:
                _apply(conn, results)
                _store_cache(conn, model, cache_entries)
            stats.summarized += len(results)
            done += sum(len(b) for b in group)
            if progress:
                progress(f"summarized {done}/{total} symbols, {stats.tokens} tokens")
        if stats.budget_exhausted:
            break
    stats.seconds = round(time.monotonic() - started, 2)
    return stats


def _call(summarizer: Summarizer, prompt: str):
    try:
        return summarizer.summarize(prompt)
    except SummaryError as exc:
        return exc
