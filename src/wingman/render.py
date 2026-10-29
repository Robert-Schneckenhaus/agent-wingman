"""Plain-text rendering of query results, shared by the CLI and the MCP server.

The format is compact and always carries ``repo:path:start-end`` so an agent can
open exactly the right lines.
"""

from __future__ import annotations

from wingman.endpoints import EndpointInfo
from wingman.query import OutlineEntry, RefHit, SymbolDetails, SymbolHit
from wingman.storage.store import Note


def hit_lines(hit: SymbolHit, indent: str = "") -> list[str]:
    lines = [f"{indent}{hit.location}  {hit.kind} {hit.qualified_name}"]
    if hit.signature and hit.kind != "namespace":
        lines.append(f"{indent}    {hit.signature}")
    if hit.summary:
        lines.append(f"{indent}    — {hit.summary}")
    return lines


def hits(results: list[SymbolHit]) -> str:
    if not results:
        return "No matches."
    return "\n".join(line for hit in results for line in hit_lines(hit))


def ambiguous(name: str, found: list[SymbolHit]) -> str:
    lines = [f"'{name}' is ambiguous; use a qualified name or #id:"]
    lines += [f"  #{h.id:<6} {h.kind:<10} {h.qualified_name}  [{h.location}]" for h in found]
    return "\n".join(lines)


def details(
    items: list[SymbolDetails],
    sources: dict[int, str | None] | None = None,
    notes: dict[int, list[Note]] | None = None,
    docs: dict[int, list] | None = None,
) -> str:
    blocks = []
    for d in items:
        s = d.symbol
        lines = [f"{s.kind} {s.qualified_name}  [{s.location}]  #{s.id}"]
        if s.signature:
            lines.append(f"  {s.signature}")
        attributes = list(d.modifiers)
        if d.visibility and d.visibility.split()[0] not in attributes:
            attributes.insert(0, d.visibility)
        if attributes:
            lines.append(f"  modifiers: {' '.join(attributes)}")
        lines += [f"  {decorator}" for decorator in d.decorators]
        if d.doc:
            lines.append("  doc: " + d.doc.replace("\n", "\n       "))
        elif d.generated_summary:
            lines.append(f"  summary (generated): {d.generated_summary}")
        for note in (notes or {}).get(s.id, []):
            author = f" ({note.author})" if note.author else ""
            lines.append(f"  note{author}: {note.text}")
        for doc in (docs or {}).get(s.id, []):
            lines.append(f"  docs: {doc.citation}  [{doc.uri}]  (chunk {doc.chunk_id})")
        for label, related in (
            ("bases", d.bases),
            ("derived", d.derived),
            ("overrides", d.overrides),
            ("overridden by", d.overridden_by),
        ):
            lines += [f"  {label}: {item.qualified_name}  [{item.location}]" for item in related]
        if d.members:
            lines.append(f"  members ({len(d.members)}):")
            lines += [f"    {m.start_line:>5}  {m.kind:<11} {m.name}" for m in d.members]
        lines.append(f"  callers: {d.caller_count}")
        source = (sources or {}).get(s.id)
        if source:
            lines += ["", source]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def ref_tree(refs: list[RefHit], indent: str = "  ") -> list[str]:
    lines = []
    for ref in refs:
        if ref.symbol is not None:
            who = f"{ref.symbol.kind} {ref.symbol.qualified_name}"
        else:
            who = "<top-level code>"
        via = f", via {ref.via}" if ref.via else ""
        test = ", test" if ref.symbol is not None and ref.symbol.is_test else ""
        details = f"{ref.kind}, {ref.confidence:.2f}{via}{test}"
        lines.append(f"{indent}{ref.repo_path}:{ref.line}  {who}  ({details})")
        lines += ref_tree(ref.children, indent + "  ")
    return lines


def callers(target: SymbolHit, refs: list[RefHit]) -> str:
    lines = [f"Callers of {target.kind} {target.qualified_name}  [{target.location}]"]
    lines += ref_tree(refs) or ["  (none found)"]
    return "\n".join(lines)


def callees(target: SymbolHit, refs: list[RefHit]) -> str:
    lines = [f"Callees of {target.kind} {target.qualified_name}  [{target.location}]"]
    for ref in refs:
        if ref.symbol is not None:
            resolved = f"{ref.symbol.qualified_name}  [{ref.symbol.location}]"
            lines.append(
                f"  L{ref.line:<5} {ref.kind:<11} {ref.target_name} -> {resolved} "
                f"({ref.confidence:.2f})"
            )
        else:
            lines.append(f"  L{ref.line:<5} {ref.kind:<11} {ref.target_name}  (unresolved)")
    if not refs:
        lines.append("  (none found)")
    return "\n".join(lines)


def outline(repo: str, path: str, entries: list[OutlineEntry]) -> str:
    lines = [f"{repo}:{path}"]
    for entry in entries:
        s = entry.symbol
        span = f"{s.start_line}-{s.end_line}"
        lines.append(f"  {span:>9}  {'  ' * entry.depth}{s.kind} {s.name}")
    if not entries:
        lines.append("  (no symbols)")
    return "\n".join(lines)


def endpoint_list(items: list[EndpointInfo], hits: dict[int, SymbolHit], role: str) -> str:
    if not items:
        return "No endpoints." if role == "server" else "No HTTP client calls."
    counterpart = "client" if role == "server" else "server"
    lines = []
    for item in items:
        hit = hits.get(item.symbol_id)
        where = f"{hit.qualified_name}  [{hit.location}]" if hit else "?"
        if hit and item.line and not hit.start_line <= item.line <= hit.end_line:
            where += f" (mapped at line {item.line})"
        matches = f"{item.matches} {counterpart}{'s' if item.matches != 1 else ''}"
        lines.append(f"{item.method or 'ANY':<6} /{item.route}  -> {where}  ({matches})")
    return "\n".join(lines)


def doc_hits(hits: list) -> str:
    """Documentation search results (``wingman.docs.DocHit``)."""
    blocks = []
    for hit in hits:
        snippet = " ".join(hit.text.split())
        snippet = snippet if len(snippet) <= 500 else snippet[:499] + "…"
        reason = f"  ({hit.reason})" if hit.reason else ""
        blocks.append(f"[chunk {hit.chunk_id}] {hit.citation}{reason}\n  {hit.uri}\n  {snippet}")
    return "\n\n".join(blocks)


def doc_chunk(hit, links: list[tuple[str, str, float]]) -> str:
    lines = [f"[chunk {hit.chunk_id}] {hit.citation}", hit.uri, "", hit.text]
    if links:
        lines += ["", "Linked to:"]
        lines += [
            f"  {key}  ({source}, {confidence:.2f})" for key, source, confidence in links[:20]
        ]
    return "\n".join(lines)


def doc_add_results(results: list) -> str:
    lines = []
    for r in results:
        if r.error:
            lines.append(f"{r.source}: {r.error}")
            continue
        state = "updated" if r.changed else "unchanged"
        links = r.links
        detail = (
            f"{links.total} links ({links.name_match} by name, {links.route} by route, "
            f"{links.manual} manual)"
            if links
            else ""
        )
        lines.append(f"#{r.document_id} {r.title} ({r.chunks} chunks, {state}): {detail}")
    return "\n".join(lines)
