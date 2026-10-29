"""Language registry and entry point for symbol extraction."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from tree_sitter import Language, Parser

from wingman.extract.model import Extraction

# Bump when extractors (or the indexer) change what they produce: it is part of every
# file's content hash, so the next `wingman index` re-parses all files.
EXTRACTOR_VERSION = "7"


def content_hash(data: bytes) -> str:
    """Hash stored in ``files.content_hash``: file content + extractor version."""
    return hashlib.sha256(EXTRACTOR_VERSION.encode() + b"\0" + data).hexdigest()


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    """Language name stored in ``files.language``."""
    grammar: Callable[[], object]
    extractor: str


def _ts():
    import tree_sitter_typescript

    return tree_sitter_typescript.language_typescript()


def _tsx():
    import tree_sitter_typescript

    return tree_sitter_typescript.language_tsx()


def _csharp():
    import tree_sitter_c_sharp

    return tree_sitter_c_sharp.language()


_TS = LanguageSpec("typescript", _ts, "typescript")
_TSX = LanguageSpec("typescript", _tsx, "typescript")
# JavaScript is parsed with the TSX grammar (a superset that also covers JSX), so the
# TypeScript extractor handles both.
_JS = LanguageSpec("javascript", _tsx, "typescript")
_CS = LanguageSpec("csharp", _csharp, "csharp")

EXTENSIONS: dict[str, LanguageSpec] = {
    ".ts": _TS,
    ".mts": _TS,
    ".cts": _TS,
    ".tsx": _TSX,
    ".js": _JS,
    ".jsx": _JS,
    ".mjs": _JS,
    ".cjs": _JS,
    ".cs": _CS,
}


def spec_for(path: str) -> LanguageSpec | None:
    dot = path.rfind(".")
    return EXTENSIONS.get(path[dot:].lower()) if dot != -1 else None


@cache
def _parser(spec: LanguageSpec) -> Parser:
    return Parser(Language(spec.grammar()))


def extract(path: str, source: bytes) -> Extraction:
    spec = spec_for(path)
    if spec is None:
        raise ValueError(f"Unsupported file type: {path}")
    tree = _parser(spec).parse(source)
    if spec.extractor == "typescript":
        from wingman.extract.typescript import TypeScriptExtractor

        return TypeScriptExtractor(source).extract(tree.root_node)
    from wingman.extract.csharp import CSharpExtractor

    return CSharpExtractor(source).extract(tree.root_node)
