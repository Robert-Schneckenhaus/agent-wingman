"""Language registry and entry point for symbol extraction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from tree_sitter import Language, Parser

from wingman.extract.model import Extraction

# Bump when extractors change what they produce: it is part of every file's content
# hash, so the next `wingman index` re-parses all files.
EXTRACTOR_VERSION = "2"


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
_CS = LanguageSpec("csharp", _csharp, "csharp")

EXTENSIONS: dict[str, LanguageSpec] = {
    ".ts": _TS,
    ".mts": _TS,
    ".cts": _TS,
    ".tsx": _TSX,
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
