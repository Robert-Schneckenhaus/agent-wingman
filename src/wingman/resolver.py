"""Resolves references, imports and inheritance (CONCEPT.md 3.4, 4.3).

Lookup tables cover the whole workspace, so references can cross repository borders:
TypeScript imports of packages defined in another repository (or another package of
a monorepo, found via package.json) and C# usings of namespaces from other
repositories. Only the references of the repositories "in scope" are (re)computed;
the indexer puts a repository in scope when it changed or when references from it
pointed into code that was re-indexed. Resolution by name alone (confidence 0.5)
never crosses repository borders.

Everything is loaded into memory; resolution itself is plain dictionary lookups.

Confidence levels:
  1.0  local      same file / same class
  0.9  import     via an import, using directive or namespace
  0.85 typed      receiver type known from a field, parameter or local declaration
  0.8  inherited  member found in a base type
  0.5  name       unique name in the repository (no other evidence)
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, bindparam, delete, insert, select, update

from wingman.extract.model import NAMESPACE_KINDS, TYPE_KINDS
from wingman.storage.schema import files, imports, refs, repositories, symbol_edges, symbols

MAX_DEPTH = 6
TS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# Languages whose imports bind local names to exports of other files (ES modules).
ES_MODULE_LANGUAGES = frozenset({"typescript", "javascript"})
# Languages where imports bind names of other modules (ES modules, Python).
MODULE_LANGUAGES = ES_MODULE_LANGUAGES | {"python"}
# Languages with namespaces/packages and using/import directives (C#, Java).
NAMESPACE_LANGUAGES = frozenset({"csharp", "java", "php"})
# Languages where members of the own type can be used without ``this.``.
IMPLICIT_THIS_LANGUAGES = frozenset({"csharp", "java"})
PYTHON_SUFFIXES = (".py", "/__init__.py", ".pyi", "/__init__.pyi")
# Receivers that are runtime globals, never symbols of the repository.
TS_GLOBALS = frozenset(
    {
        "console",
        "Math",
        "JSON",
        "Object",
        "Array",
        "Promise",
        "Number",
        "String",
        "Date",
        "Reflect",
        "Symbol",
        "Intl",
        "window",
        "document",
        "process",
        "globalThis",
        "localStorage",
        "sessionStorage",
        "navigator",
        "Error",
        "Map",
        "Set",
        "RegExp",
    }
)
CALLABLE_KINDS = frozenset({"function", "method", "constructor", "component", "const", "property"})


@dataclass
class _Sym:
    id: int
    file_id: int
    parent_id: int | None
    kind: str
    name: str
    qualified_name: str
    modifiers: list[str]
    type_name: str | None = None


@dataclass
class _Import:
    id: int
    file_id: int
    module: str
    imported_name: str | None
    alias: str | None
    kind: str
    resolved_file_id: int | None


@dataclass
class _File:
    id: int
    repo_id: int
    path: str
    language: str
    namespaces: list[str] = field(default_factory=list)
    imports: list[_Import] = field(default_factory=list)


@dataclass(frozen=True)
class _Package:
    """A package.json somewhere in the workspace."""

    name: str
    repo_id: int
    directory: str
    """Relative to the repository root ('' for the root)."""
    entries: tuple[str, ...]
    """Entry points from package.json (source, types, module, main), in that order."""


PACKAGE_SKIP_DIRS = frozenset({"node_modules", ".git", "dist", "build", "out", "bin", "obj"})
BUILD_DIRS = ("dist/", "lib/", "build/", "out/", "esm/", "cjs/")


@dataclass(frozen=True)
class Resolution:
    target: int
    confidence: float
    resolver: str


@dataclass
class ResolveStats:
    refs: int = 0
    resolved: int = 0
    edges: int = 0


class WorkspaceResolver:
    def __init__(self, conn: Connection, workspace_id: int, scope: set[int]) -> None:
        """``scope``: ids of the repositories whose references are (re)resolved."""
        self.conn = conn
        self.workspace_id = workspace_id
        self.scope = scope
        self._tsconfig_cache: dict[tuple[int, str], tuple[str, dict] | None] = {}
        self._packages: list[_Package] | None = None
        self._load()

    # --- loading ----------------------------------------------------------------

    def _load(self) -> None:
        c = self.conn
        self.roots: dict[int, Path] = {
            row.id: Path(row.root_path)
            for row in c.execute(
                select(repositories.c.id, repositories.c.root_path).where(
                    repositories.c.workspace_id == self.workspace_id
                )
            )
        }
        self.files: dict[int, _File] = {}
        if self.roots:
            for row in c.execute(
                select(
                    files.c.id, files.c.repo_id, files.c.path, files.c.language, files.c.namespaces
                ).where(files.c.repo_id.in_(list(self.roots)))
            ):
                namespaces = row.namespaces
                if isinstance(namespaces, str):
                    namespaces = json.loads(namespaces)
                self.files[row.id] = _File(
                    row.id, row.repo_id, row.path, row.language or "", namespaces or []
                )
        self.path_to_file = {(f.repo_id, f.path): f.id for f in self.files.values()}
        self._python_modules: dict[str, list[int]] | None = None
        file_ids = list(self.files)
        self.scope_files = [f.id for f in self.files.values() if f.repo_id in self.scope]

        self.symbols: dict[int, _Sym] = {}
        for chunk in _chunks(file_ids):
            for row in c.execute(
                select(
                    symbols.c.id,
                    symbols.c.file_id,
                    symbols.c.parent_id,
                    symbols.c.kind,
                    symbols.c.name,
                    symbols.c.qualified_name,
                    symbols.c.modifiers,
                    symbols.c.type_name,
                ).where(symbols.c.file_id.in_(chunk))
            ):
                modifiers = row.modifiers
                if isinstance(modifiers, str):
                    modifiers = json.loads(modifiers)
                self.symbols[row.id] = _Sym(
                    row.id,
                    row.file_id,
                    row.parent_id,
                    row.kind,
                    row.name,
                    row.qualified_name,
                    modifiers or [],
                    row.type_name,
                )

        self.top_level: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.members: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.types_by_name: dict[str, list[int]] = defaultdict(list)
        self.types_by_qualified: dict[str, list[int]] = defaultdict(list)
        self.callables_by_name: dict[str, list[int]] = defaultdict(list)
        self.functions_by_qualified: dict[str, list[int]] = defaultdict(list)
        for sym in self.symbols.values():
            parent = self.symbols.get(sym.parent_id) if sym.parent_id else None
            if parent is None or parent.kind in NAMESPACE_KINDS:
                self.top_level[sym.file_id][sym.name].append(sym.id)
            if parent is not None:
                self.members[parent.id][sym.name].append(sym.id)
            if sym.kind in TYPE_KINDS:
                self.types_by_name[sym.name].append(sym.id)
                self.types_by_qualified[sym.qualified_name].append(sym.id)
            elif sym.kind in CALLABLE_KINDS:
                self.callables_by_name[sym.name].append(sym.id)
                if sym.kind == "function":
                    self.functions_by_qualified[sym.qualified_name].append(sym.id)

        self.global_usings: dict[int, list[str]] = defaultdict(list)
        for chunk in _chunks(file_ids):
            for row in c.execute(
                select(
                    imports.c.id,
                    imports.c.file_id,
                    imports.c.module,
                    imports.c.imported_name,
                    imports.c.alias,
                    imports.c.kind,
                    imports.c.resolved_file_id,
                ).where(imports.c.file_id.in_(chunk))
            ):
                file = self.files[row.file_id]
                file.imports.append(_Import(*row))
                if row.kind == "global_using":  # applies to the whole project/repository
                    self.global_usings[file.repo_id].append(row.module)

        # Base types of repositories outside the scope come from their stored results.
        self.bases: dict[int, list[int]] = defaultdict(list)
        outside = [f for f in file_ids if self.files[f].repo_id not in self.scope]
        for chunk in _chunks(outside):
            for row in c.execute(
                select(refs.c.from_symbol_id, refs.c.to_symbol_id).where(
                    refs.c.from_file_id.in_(chunk),
                    refs.c.kind.in_(("inherits", "implements")),
                    refs.c.to_symbol_id.is_not(None),
                    refs.c.from_symbol_id.is_not(None),
                )
            ):
                self.bases[row.from_symbol_id].append(row.to_symbol_id)

    def repo_of(self, symbol_id: int) -> int:
        return self.files[self.symbols[symbol_id].file_id].repo_id

    # --- entry point ------------------------------------------------------------

    def run(self) -> ResolveStats:
        stats = ResolveStats()
        self._resolve_imports()
        ref_rows = []
        for chunk in _chunks(self.scope_files):
            ref_rows += self.conn.execute(
                select(
                    refs.c.id,
                    refs.c.from_file_id,
                    refs.c.from_symbol_id,
                    refs.c.target_name,
                    refs.c.qualifier,
                    refs.c.qualifier_type,
                    refs.c.kind,
                    refs.c.to_symbol_id,
                    refs.c.confidence,
                    refs.c.resolver,
                ).where(refs.c.from_file_id.in_(chunk), refs.c.kind != "http_call")
            ).all()
        heritage = [r for r in ref_rows if r.kind in ("inherits", "implements")]
        others = [r for r in ref_rows if r.kind not in ("inherits", "implements")]

        results: dict[int, Resolution | None] = {}
        for row in heritage:  # first, so member lookups can follow base types
            result = self.resolve_type(row.target_name, row.from_file_id, row.from_symbol_id)
            results[row.id] = result
            if result is not None and row.from_symbol_id is not None:
                self.bases[row.from_symbol_id].append(result.target)
        for row in others:
            results[row.id] = self.resolve_ref(row)

        updates = []
        for row in ref_rows:
            result = results[row.id]
            new = (
                (result.target, result.confidence, result.resolver) if result else (None, 0.0, None)
            )
            if (row.to_symbol_id, row.confidence, row.resolver) != new:
                updates.append({"ref_id": row.id, "t": new[0], "c": new[1], "r": new[2]})
        if updates:
            self.conn.execute(
                update(refs)
                .where(refs.c.id == bindparam("ref_id"))
                .values(
                    to_symbol_id=bindparam("t"), confidence=bindparam("c"), resolver=bindparam("r")
                ),
                updates,
            )

        stats.refs = len(ref_rows)
        stats.resolved = sum(1 for r in results.values() if r is not None)
        stats.edges = self._rebuild_edges(heritage, results)
        return stats

    # --- imports ----------------------------------------------------------------

    def _resolve_imports(self) -> None:
        updates = []
        for file_id in self.scope_files:
            file = self.files[file_id]
            if file.language not in MODULE_LANGUAGES:
                continue
            for imp in file.imports:
                if file.language == "python":
                    target = self.resolve_python_import(file, imp)
                else:
                    target = self.resolve_module(file, imp.module)
                if target != imp.resolved_file_id:
                    updates.append({"imp_id": imp.id, "target": target})
                    imp.resolved_file_id = target
        if updates:
            self.conn.execute(
                update(imports)
                .where(imports.c.id == bindparam("imp_id"))
                .values(resolved_file_id=bindparam("target")),
                updates,
            )

    def resolve_module(self, file: _File, spec: str) -> int | None:
        """File a TypeScript module specifier points to, in any repository."""
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(file.path), spec))
            return self._module_file(file.repo_id, base)
        found = self._resolve_tsconfig(file, spec)
        return found if found is not None else self._resolve_package(spec)

    def _resolve_tsconfig(self, file: _File, spec: str) -> int | None:
        config = self._tsconfig_for(file)
        if config is None:
            return None
        config_dir, options = config
        base_url = options.get("baseUrl")
        for pattern, targets in (options.get("paths") or {}).items():
            prefix, star, suffix = pattern.partition("*")
            if star:
                if not (spec.startswith(prefix) and spec.endswith(suffix)):
                    continue
                middle = spec[len(prefix) : len(spec) - len(suffix) if suffix else None]
            elif spec != pattern:
                continue
            else:
                middle = ""
            for target in targets:
                path = target.replace("*", middle)
                base = posixpath.normpath(posixpath.join(config_dir, base_url or ".", path))
                found = self._module_file(file.repo_id, base)
                if found is not None:
                    return found
        if base_url:
            base = posixpath.normpath(posixpath.join(config_dir, base_url, spec))
            return self._module_file(file.repo_id, base)
        return None

    # --- Python modules -------------------------------------------------------------

    def resolve_python_import(self, file: _File, imp) -> int | None:
        """``from pkg import mod`` may name a submodule; otherwise the module itself."""
        if imp.imported_name not in (None, "*"):
            joiner = "" if imp.module.endswith(".") else "."  # "from . import mod"
            submodule = self.python_module(file, f"{imp.module}{joiner}{imp.imported_name}")
            if submodule is not None:
                return submodule
        return self.python_module(file, imp.module)

    def python_module(self, file: _File, module: str) -> int | None:
        level = len(module) - len(module.lstrip("."))
        name = module.lstrip(".")
        if level:
            base = posixpath.dirname(file.path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            rel = "/".join(p for p in (base, name.replace(".", "/")) if p)
            for suffix in PYTHON_SUFFIXES:
                found = self.path_to_file.get((file.repo_id, rel + suffix))
                if found is not None:
                    return found
            return None
        candidates = self.python_modules().get(name, [])
        if not candidates:
            return None
        own = [f for f in candidates if self.files[f].repo_id == file.repo_id]
        return min(own or candidates, key=lambda f: (len(self.files[f].path), f))

    def python_modules(self) -> dict[str, list[int]]:
        """Dotted module name (and every shorter suffix, for source roots such as
        ``src/``) -> Python files."""
        if self._python_modules is None:
            index: dict[str, list[int]] = defaultdict(list)
            for f in self.files.values():
                if f.language != "python":
                    continue
                stem = re.sub(r"(/__init__)?\.pyi?$", "", f.path)
                parts = stem.split("/")
                for start in range(len(parts)):
                    index[".".join(parts[start:])].append(f.id)
            self._python_modules = index
        return self._python_modules

    def _is_module_import(self, imp) -> bool:
        """``from pkg import mod`` that resolved to the submodule ``mod``."""
        if imp.resolved_file_id is None or imp.imported_name in (None, "*"):
            return False
        path = self.files[imp.resolved_file_id].path
        stem = re.sub(r"(/__init__)?\.pyi?$", "", path)
        return stem.rsplit("/", 1)[-1] == imp.imported_name

    # --- packages (package.json anywhere in the workspace) -------------------------

    def packages(self) -> list[_Package]:
        if self._packages is None:
            found = []
            for repo_id, root in self.roots.items():
                for directory, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in PACKAGE_SKIP_DIRS]
                    if "package.json" in filenames:
                        package = _read_package(repo_id, root, Path(directory))
                        if package is not None:
                            found.append(package)
            # longest names first, so "@acme/shared-ui" wins over "@acme/shared"
            self._packages = sorted(found, key=lambda p: -len(p.name))
        return self._packages

    def _resolve_package(self, spec: str) -> int | None:
        for package in self.packages():
            if spec == package.name:
                subpath = ""
            elif spec.startswith(package.name + "/"):
                subpath = spec[len(package.name) + 1 :]
            else:
                continue
            for candidate in _package_candidates(package, subpath):
                found = self._module_file(package.repo_id, candidate)
                if found is not None:
                    return found
        return None

    def _module_file(self, repo_id: int, base: str) -> int | None:
        base = base.removeprefix("./")
        stem = re.sub(r"\.(js|jsx|mjs|cjs)$", "", base)
        candidates = [base, *(stem + ext for ext in TS_EXTENSIONS)]
        candidates += [f"{stem}/index{ext}" for ext in (".ts", ".tsx", ".d.ts", ".js", ".jsx")]
        for candidate in candidates:
            file_id = self.path_to_file.get((repo_id, candidate))
            if file_id is not None:
                return file_id
        return None

    def _tsconfig_for(self, file: _File) -> tuple[str, dict] | None:
        directory = posixpath.dirname(file.path)
        while True:
            key = (file.repo_id, directory)
            if key not in self._tsconfig_cache:
                self._tsconfig_cache[key] = self._read_tsconfig(file.repo_id, directory)
            found = self._tsconfig_cache[key]
            if found is not None:
                return found
            if directory in ("", "."):
                return None
            directory = posixpath.dirname(directory)

    def _read_tsconfig(
        self, repo_id: int, directory: str, name: str = "tsconfig.json", depth: int = 0
    ):
        root = self.roots[repo_id]
        path = root / directory / name if directory not in ("", ".") else root / name
        if not path.is_file() or depth > 3:
            return None
        try:
            data = json.loads(_strip_json_comments(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        options = dict(data.get("compilerOptions") or {})
        config_dir = "" if directory in ("", ".") else directory
        extends = data.get("extends")
        if isinstance(extends, str) and extends.startswith("."):
            parent_path = posixpath.normpath(posixpath.join(config_dir, extends))
            if not parent_path.endswith(".json"):
                parent_path += ".json"
            parent = self._read_tsconfig(
                repo_id, posixpath.dirname(parent_path), posixpath.basename(parent_path), depth + 1
            )
            if parent is not None:
                parent_dir, parent_options = parent
                merged = dict(parent_options)
                if "baseUrl" in parent_options and "baseUrl" not in options:
                    # baseUrl is relative to the config that declares it
                    merged["baseUrl"] = posixpath.relpath(
                        posixpath.join(parent_dir or ".", parent_options["baseUrl"]),
                        config_dir or ".",
                    )
                merged.update(options)
                options = merged
        if "paths" in options or "baseUrl" in options:
            return config_dir, options
        return None

    def find_export(self, file_id: int | None, name: str, depth: int = 0) -> int | None:
        if file_id is None or depth > MAX_DEPTH:
            return None
        top = self.top_level.get(file_id, {})
        if name == "default":
            for ids in top.values():
                for sid in ids:
                    if "default" in self.symbols[sid].modifiers:
                        return sid
        elif top.get(name):
            return top[name][0]
        is_python = self.files[file_id].language == "python"
        for imp in self.files[file_id].imports:
            if is_python and imp.kind == "import" and (imp.alias or imp.imported_name) == name:
                # Python: every imported name is also an attribute of the module
                if imp.imported_name == "*" or self._is_module_import(imp):
                    continue
                found = self.find_export(imp.resolved_file_id, imp.imported_name, depth + 1)
                if found is not None:
                    return found
                continue
            if imp.kind != "reexport":
                continue
            if imp.imported_name == "*":
                found = self.find_export(imp.resolved_file_id, name, depth + 1)
            elif (imp.alias or imp.imported_name) == name:
                found = self.find_export(imp.resolved_file_id, imp.imported_name, depth + 1)
            else:
                continue
            if found is not None:
                return found
        return None

    def _import_for_local(self, file: _File, local: str):
        for imp in file.imports:
            if imp.kind == "import" and (imp.alias or imp.imported_name) == local:
                return imp
        return None

    # --- types ------------------------------------------------------------------

    def enclosing_type(self, symbol_id: int | None) -> int | None:
        current = self.symbols.get(symbol_id) if symbol_id is not None else None
        while current is not None:
            if current.kind in TYPE_KINDS and current.kind != "component":
                return current.id
            current = self.symbols.get(current.parent_id) if current.parent_id else None
        return None

    def resolve_type(self, name: str, file_id: int, from_symbol: int | None) -> Resolution | None:
        name = name.rsplit(".", 1)[-1]
        file = self.files[file_id]
        candidates = self.types_by_name.get(name, [])

        if file.language in MODULE_LANGUAGES:  # imports may rename (aliases, re-exports)
            imp = self._import_for_local(file, name)
            if imp is not None:
                target = self.find_export(imp.resolved_file_id, imp.imported_name or name)
                if target is not None:
                    return Resolution(target, 0.9, "import")
        elif file.language in NAMESPACE_LANGUAGES:
            for imp in file.imports:
                if imp.alias == name:
                    ids = self.types_by_qualified.get(imp.module)
                    if ids:
                        return Resolution(ids[0], 0.9, "import")
        if not candidates:
            return None

        # nested type of an enclosing type
        current = self.enclosing_type(from_symbol)
        while current is not None:
            for sid in self.members.get(current, {}).get(name, []):
                if self.symbols[sid].kind in TYPE_KINDS:
                    return Resolution(sid, 1.0, "local")
            current = self.enclosing_type(self.symbols[current].parent_id)

        same_file = [sid for sid in candidates if self.symbols[sid].file_id == file_id]
        if same_file:
            return Resolution(same_file[0], 1.0, "local")

        if file.language in NAMESPACE_LANGUAGES:
            found = self._resolve_csharp_type(name, file, candidates)
            if found is not None:
                return found

        return self._unique(candidates, 0.5, file.repo_id)

    def _resolve_csharp_type(self, name: str, file: _File, candidates: list[int]):
        namespaces = set()
        for ns in file.namespaces:
            parts = ns.split(".")
            namespaces.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        usings = {imp.module for imp in file.imports if imp.kind == "using" and not imp.alias}
        usings.update(self.global_usings.get(file.repo_id, []))

        def namespace_of(sid: int) -> str:
            return (
                self.symbols[sid].qualified_name.rsplit(".", 1)[0]
                if "." in self.symbols[sid].qualified_name
                else ""
            )

        def prefer_own_repo(ids: list[int]) -> int:
            own = [sid for sid in ids if self.repo_of(sid) == file.repo_id]
            return (own or ids)[0]

        in_namespace = [sid for sid in candidates if namespace_of(sid) in namespaces]
        if in_namespace:
            return Resolution(prefer_own_repo(in_namespace), 0.9, "namespace")
        via_using = [sid for sid in candidates if namespace_of(sid) in usings]
        if via_using:
            return Resolution(prefer_own_repo(via_using), 0.9, "import")
        return None

    def _unique(self, candidates: list[int], confidence: float, repo_id: int) -> Resolution | None:
        """The only candidate (by qualified name) within ``repo_id``."""
        candidates = [sid for sid in candidates if self.repo_of(sid) == repo_id]
        qualified = {self.symbols[sid].qualified_name for sid in candidates}
        if len(qualified) == 1:
            return Resolution(candidates[0], confidence, "name")
        return None

    def type_group(self, type_id: int) -> list[int]:
        """All parts of a type (C# partial classes share the qualified name)."""
        sym = self.symbols[type_id]
        return self.types_by_qualified.get(sym.qualified_name, [type_id])

    def find_member(self, type_id: int, name: str, depth: int = 0) -> tuple[int, bool] | None:
        """Member ``name`` of the type or its bases; returns (symbol, inherited)."""
        if depth > MAX_DEPTH:
            return None
        group = self.type_group(type_id)
        for part in group:
            ids = self.members.get(part, {}).get(name)
            if ids:
                return ids[0], depth > 0
        for part in group:
            for base in self.bases.get(part, []):
                found = self.find_member(base, name, depth + 1)
                if found is not None:
                    return found[0], True
        return None

    def _walk_chain(self, type_id: int, segments: list[str]) -> int | None:
        """Follow ``a.b.c`` through the declared types of members, starting at a type."""
        for segment in segments:
            found = self.find_member(type_id, segment)
            if found is None:
                return None
            member = self.symbols[found[0]]
            if not member.type_name:
                return None
            resolved = self.resolve_type(member.type_name, member.file_id, member.id)
            if resolved is None:
                return None
            type_id = resolved.target
        return type_id

    # --- references -------------------------------------------------------------

    def resolve_ref(self, ref) -> Resolution | None:
        file = self.files[ref.from_file_id]
        kind, name, qualifier = ref.kind, ref.target_name, ref.qualifier

        if kind == "type_ref":
            return self.resolve_type(name, file.id, ref.from_symbol_id)

        if qualifier is None:
            if kind == "instantiate":
                found = self.resolve_type(name, file.id, ref.from_symbol_id)
                if found is not None:
                    return found
            return self._resolve_bare(name, file, ref.from_symbol_id)

        own_type = self.enclosing_type(ref.from_symbol_id)
        if qualifier == "this" and own_type is not None:
            found = self.find_member(own_type, name)
            if found is not None:
                return Resolution(
                    found[0], 0.8 if found[1] else 1.0, "inherited" if found[1] else "local"
                )
        if qualifier in ("base", "super") and own_type is not None:
            for base in self.bases.get(own_type, []):
                found = self.find_member(base, name)
                if found is not None:
                    return Resolution(found[0], 0.9, "inherited")

        segments = _chain_segments(qualifier)
        if ref.qualifier_type and segments:
            owner = self.resolve_type(ref.qualifier_type, file.id, ref.from_symbol_id)
            if owner is not None:
                target_type = self._walk_chain(owner.target, segments[1:])
                found = self.find_member(target_type, name) if target_type else None
                if found is not None:
                    confidence = 0.85 if len(segments) == 1 else 0.8
                    return Resolution(found[0], min(confidence, owner.confidence), "typed")
        elif (
            segments
            and own_type is not None
            and (qualifier.startswith("this.") or file.language in IMPLICIT_THIS_LANGUAGES)
        ):
            # implicit `this`: the chain starts with a member of the enclosing type
            target_type = self._walk_chain(own_type, segments)
            found = self.find_member(target_type, name) if target_type else None
            if found is not None:
                return Resolution(found[0], 0.8, "typed")

        if file.language in MODULE_LANGUAGES and re.fullmatch(r"[A-Za-z_$][\w$.]*", qualifier):
            imp = self._import_for_local(file, qualifier)
            if imp is not None and (imp.imported_name == "*" or self._is_module_import(imp)):
                target = self.find_export(imp.resolved_file_id, name)
                if target is not None:
                    return Resolution(target, 0.9, "import")
            if file.language == "python":
                root = self._import_for_local(file, qualifier.split(".", 1)[0])
                if imp is not None or root is not None:
                    return None  # a module or name from outside the index (os, json, ...)

        if re.fullmatch(r"[A-Z][\w$]*", qualifier):
            owner = self.resolve_type(qualifier, file.id, ref.from_symbol_id)
            if owner is not None:
                found = self.find_member(owner.target, name)
                if found is not None:
                    return Resolution(found[0], min(0.9, owner.confidence), "static")
            is_own_member = own_type is not None and self.find_member(own_type, qualifier)
            if not is_own_member:  # an external type such as Math or Console
                return None

        if file.language in ES_MODULE_LANGUAGES and qualifier.split(".", 1)[0] in TS_GLOBALS:
            return None
        return self._unique_member(name, file.repo_id)

    def _resolve_bare(self, name: str, file: _File, from_symbol: int | None) -> Resolution | None:
        # nested functions of the enclosing function chain
        current = self.symbols.get(from_symbol) if from_symbol is not None else None
        while current is not None and current.kind not in TYPE_KINDS:
            ids = self.members.get(current.id, {}).get(name)
            if ids:
                return Resolution(ids[0], 1.0, "local")
            current = self.symbols.get(current.parent_id) if current.parent_id else None

        if file.language == "php":
            return self._resolve_php_function(name, file)
        if file.language in NAMESPACE_LANGUAGES:
            own_type = self.enclosing_type(from_symbol)
            while own_type is not None:  # also outer types for nested classes
                found = self.find_member(own_type, name)
                if found is not None:
                    return Resolution(
                        found[0], 0.8 if found[1] else 1.0, "inherited" if found[1] else "local"
                    )
                own_type = self.enclosing_type(self.symbols[own_type].parent_id)
            for imp in file.imports:
                if imp.kind == "using_static":
                    ids = self.types_by_qualified.get(imp.module)
                    if ids:
                        found = self.find_member(ids[0], name)
                        if found is not None:
                            return Resolution(found[0], 0.9, "import")
            return self._unique_member(name, file.repo_id)

        # TypeScript
        top = self.top_level.get(file.id, {}).get(name)
        if top:
            return Resolution(top[0], 1.0, "local")
        imp = self._import_for_local(file, name)
        if imp is not None:
            target = self.find_export(imp.resolved_file_id, imp.imported_name or name)
            return Resolution(target, 0.9, "import") if target is not None else None
        top_level_candidates = [
            sid
            for sid in self.callables_by_name.get(name, [])
            if self.symbols[sid].parent_id is None
        ]
        if not top_level_candidates:
            return None
        return self._unique(top_level_candidates, 0.5, file.repo_id)

    def _resolve_php_function(self, name: str, file: _File) -> Resolution | None:
        """PHP: bare calls are functions: imported (``use function``), qualified, in the
        file's namespace, or global."""
        if "." in name:
            ids = self.functions_by_qualified.get(name)
            return Resolution(ids[0], 0.9, "import") if ids else None
        for imp in file.imports:
            if imp.kind == "using_function" and imp.alias == name:
                ids = self.functions_by_qualified.get(imp.module)
                return Resolution(ids[0], 0.9, "import") if ids else None
        for namespace in file.namespaces:
            ids = self.functions_by_qualified.get(f"{namespace}.{name}")
            if ids:
                return Resolution(ids[0], 0.9, "namespace")
        ids = self.functions_by_qualified.get(name)
        if ids:
            return Resolution(
                ids[0], 0.9, "local" if self.symbols[ids[0]].file_id == file.id else "import"
            )
        candidates = [
            sid
            for sid in self.callables_by_name.get(name, [])
            if self.symbols[sid].kind == "function"
        ]
        return self._unique(candidates, 0.5, file.repo_id) if candidates else None

    def _unique_member(self, name: str, repo_id: int) -> Resolution | None:
        """Method/property ``name`` defined in exactly one type of the repository."""
        candidates = [
            sid
            for sid in self.callables_by_name.get(name, [])
            if self.symbols[sid].parent_id is not None
            and self.symbols[self.symbols[sid].parent_id].kind in TYPE_KINDS
        ]
        if not candidates:
            return None
        return self._unique(candidates, 0.5, repo_id)

    # --- edges --------------------------------------------------------------------

    def _rebuild_edges(self, heritage, results) -> int:
        scope_files = set(self.scope_files)
        symbol_ids = [s.id for s in self.symbols.values() if s.file_id in scope_files]
        for chunk in _chunks(symbol_ids):
            self.conn.execute(delete(symbol_edges).where(symbol_edges.c.src_id.in_(chunk)))
        edges: dict[tuple[int, int, str], float] = {}
        for row in heritage:
            result = results.get(row.id)
            if result is None or row.from_symbol_id is None:
                continue
            target_kind = self.symbols[result.target].kind
            kind = "implements" if target_kind == "interface" else "inherits"
            edges[(row.from_symbol_id, result.target, kind)] = result.confidence

        # overrides: a member with the same name in a base type
        for type_id, bases in self.bases.items():
            if (
                self.symbols.get(type_id) is None
                or self.symbols[type_id].file_id not in scope_files
            ):
                continue
            for name, member_ids in self.members.get(type_id, {}).items():
                for member_id in member_ids:
                    if self.symbols[member_id].kind not in ("method", "property"):
                        continue
                    for base in bases:
                        found = self.find_member(base, name)
                        if found is not None:
                            edges[(member_id, found[0], "overrides")] = 0.9

        if edges:
            self.conn.execute(
                insert(symbol_edges),
                [
                    {"src_id": s, "dst_id": d, "kind": k, "confidence": c}
                    for (s, d, k), c in edges.items()
                ],
            )
        return len(edges)


def _chain_segments(qualifier: str) -> list[str] | None:
    """``this._repo.UnitOfWork`` -> ``[_repo, UnitOfWork]``; None for calls/indexers."""
    text = qualifier.replace("?.", ".").replace("!", "")
    if not re.fullmatch(r"[\w$]+(\.[\w$]+)*", text):
        return None
    segments = text.split(".")
    if segments[0] == "this":
        segments = segments[1:]
    return segments or None


def _strip_json_comments(text: str) -> str:
    """tsconfig.json allows comments and trailing commas."""
    result = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            result.append(ch)
            if ch == "\\" and i + 1 < n:
                result.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            result.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        else:
            result.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(result))


def _chunks(ids: list[int], size: int = 900):
    """Keep IN lists below SQLite's variable limit."""
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def _read_package(repo_id: int, root: Path, directory: Path) -> _Package | None:
    try:
        data = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        return None
    entries = [data.get(key) for key in ("source", "types", "typings", "module", "main")]
    rel = directory.relative_to(root).as_posix()
    return _Package(
        name=name,
        repo_id=repo_id,
        directory="" if rel == "." else rel,
        entries=tuple(e for e in entries if isinstance(e, str)),
    )


def _package_candidates(package: _Package, subpath: str) -> list[str]:
    """Module paths (without extension) to try for an import of a package."""

    def join(*parts: str) -> str:
        return posixpath.normpath(posixpath.join(*(p for p in parts if p))).removeprefix("./")

    def source_variants(path: str) -> list[str]:
        path = re.sub(r"(\.d)?\.(ts|tsx|js|jsx|mjs|cjs)$", "", path.removeprefix("./"))
        variants = [path]
        for build in BUILD_DIRS:  # dist/index.js was compiled from src/index.ts
            if path.startswith(build):
                variants += ["src/" + path[len(build) :], path[len(build) :]]
        return variants

    if subpath:
        return [join(package.directory, v) for v in [subpath, "src/" + subpath]]
    candidates = [v for entry in package.entries for v in source_variants(entry)]
    candidates += ["src/index", "index", "lib/index"]
    return [join(package.directory, c) for c in candidates]


def resolve_workspace(conn: Connection, workspace_id: int, scope: set[int]) -> ResolveStats:
    """(Re)resolve the references of the repositories in ``scope``."""
    return WorkspaceResolver(conn, workspace_id, scope).run()
