"""Language-independent result of extracting one source file."""

from __future__ import annotations

from dataclasses import dataclass, field

# Symbol kinds that describe types (used for type resolution).
TYPE_KINDS = frozenset(
    {"class", "interface", "struct", "record", "enum", "type_alias", "delegate", "component"}
)
# Symbol kinds that act as containers for qualified names but are not types.
NAMESPACE_KINDS = frozenset({"namespace", "module"})


@dataclass
class Symbol:
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    parent: int | None = None
    """Index of the parent symbol in ``Extraction.symbols``."""
    signature: str | None = None
    visibility: str | None = None
    modifiers: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    doc: str | None = None
    body_hash: str | None = None
    type_name: str | None = None
    """Declared value type: field/property type or (unwrapped) return type."""


@dataclass
class Ref:
    """A use of a name inside the file, resolved to a symbol later.

    ``qualifier`` is the receiver as written (``this``, ``base``, ``ns``, ``_calc``,
    ``TaxCalculator``, ``_repo.UnitOfWork``); ``qualifier_type`` is the declared type
    of the first segment of the receiver chain (``_repo``), if the extractor could
    infer it from a field, parameter or local declaration.
    """

    kind: str  # call|instantiate|type_ref|inherits|implements
    target_name: str
    line: int
    col: int
    from_symbol: int | None = None
    qualifier: str | None = None
    qualifier_type: str | None = None


@dataclass
class Import:
    module: str
    """TS: module specifier (``./tax``, ``@app/x``); C#: namespace or type name."""
    line: int
    imported_name: str | None = None
    """TS: exported name (``default`` for default imports, ``*`` for namespace
    imports / ``export *``); C#: None."""
    alias: str | None = None
    """Local name if different from ``imported_name`` (TS) or the using alias (C#)."""
    kind: str = "import"  # import|reexport|using|using_static|global_using


@dataclass
class Extraction:
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    """C#: namespaces declared in the file."""
