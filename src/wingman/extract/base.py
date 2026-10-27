"""Shared helpers for tree-sitter based extractors."""

from __future__ import annotations

import hashlib
import re

from tree_sitter import Node

from wingman.extract.model import Extraction, Ref, Symbol

MAX_SIGNATURE = 300
_WHITESPACE = re.compile(r"\s+")


def short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def collapse(text: str, limit: int = MAX_SIGNATURE) -> str:
    text = _WHITESPACE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ExtractorBase:
    """Walks a tree-sitter tree; subclasses implement the language specifics."""

    def __init__(self, source: bytes) -> None:
        self.src = source
        self.out = Extraction()

    # --- node helpers -----------------------------------------------------------

    def text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return self.src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def line(node: Node) -> int:
        return node.start_point[0] + 1

    def header(self, node: Node, body: Node | None) -> str:
        """Declaration text up to the body, whitespace collapsed."""
        end = body.start_byte if body is not None else node.end_byte
        return collapse(self.src[node.start_byte : end].decode("utf-8", errors="replace"))

    def preceding_comments(self, node: Node) -> list[str]:
        """Comment nodes directly above ``node`` (no blank-line gap), top to bottom."""
        comments: list[str] = []
        current = node
        sibling = node.prev_sibling
        while sibling is not None and sibling.type == "comment":
            if current.start_point[0] - sibling.end_point[0] > 1:
                break
            comments.append(self.text(sibling))
            current = sibling
            sibling = sibling.prev_sibling
        comments.reverse()
        return comments

    # --- output helpers ---------------------------------------------------------

    def add_symbol(
        self,
        node: Node,
        kind: str,
        name: str,
        parent: int | None,
        *,
        qualified_prefix: str | None,
        signature: str | None = None,
        **extra,
    ) -> int:
        qualified = f"{qualified_prefix}.{name}" if qualified_prefix else name
        self.out.symbols.append(
            Symbol(
                kind=kind,
                name=name,
                qualified_name=qualified,
                start_line=self.line(node),
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                parent=parent,
                signature=signature,
                body_hash=short_hash(self.src[node.start_byte : node.end_byte]),
                **extra,
            )
        )
        return len(self.out.symbols) - 1

    def add_ref(self, node: Node, kind: str, name: str, from_symbol: int | None, **extra) -> None:
        if not name:
            return
        self.out.refs.append(
            Ref(
                kind=kind,
                target_name=name,
                line=self.line(node),
                col=node.start_point[1] + 1,
                from_symbol=from_symbol,
                **extra,
            )
        )

    def qualified(self, index: int | None) -> str | None:
        return self.out.symbols[index].qualified_name if index is not None else None


_WRAPPED = re.compile(r"^(?:[\w.]+\.)?(?:Task|ValueTask|Promise|PromiseLike)\s*<(.*)>\s*\??$", re.S)


def value_type(type_text: str | None) -> str | None:
    """Type a member yields, for following call chains: ``Task<Order>`` -> ``Order``."""
    if not type_text:
        return None
    text = type_text.strip().lstrip(":").strip()
    wrapped = _WRAPPED.match(text)
    if wrapped:
        return value_type(wrapped.group(1))
    if not text or "|" in text or "=>" in text or text[0] in "{[(":
        return None
    name = strip_generics(text)
    return name if name and name not in ("void", "Task", "ValueTask", "Promise") else None


def strip_generics(type_text: str) -> str:
    """Bare type name: ``List<Foo>`` -> ``List``, ``a.b.Foo?`` -> ``Foo``, ``Foo[]`` -> ``Foo``."""
    text = type_text.strip()
    text = text.split("<", 1)[0]
    text = text.rstrip("?[]! ")
    return text.rsplit(".", 1)[-1].strip()
