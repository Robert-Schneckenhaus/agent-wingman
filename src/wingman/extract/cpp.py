"""C and C++ extractor (one visitor for both grammars).

Qualified names use dots (``shop::Repo::find`` -> ``shop.Repo.find``). Out-of-line
member definitions (``Order* Repo::find(...) {}``) become methods of ``Repo``.
Declarations without a body (prototypes, member declarations in a class) get the
modifier ``declaration``; the resolver prefers the definition with the same
qualified name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.extract.base import ExtractorBase, collapse
from wingman.extract.java import normalize_javadoc
from wingman.extract.model import Extraction, Import

_TYPE_SPECIFIERS = {
    "class_specifier": "class",
    "struct_specifier": "struct",
    "union_specifier": "struct",
    "enum_specifier": "enum",
}
_DECLARATOR_WRAPPERS = {
    "pointer_declarator",
    "reference_declarator",
    "array_declarator",
    "init_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
    "function_declarator",
}
_NAME_NODES = {
    "identifier",
    "field_identifier",
    "qualified_identifier",
    "destructor_name",
    "operator_name",
    "type_identifier",
    "template_function",
}
_SMART_POINTER = re.compile(r"^(?:std::)?(?:unique_ptr|shared_ptr|weak_ptr|optional)\s*<(.*)>$")
_COMMENTS = ("comment",)


def cpp_name(text: str) -> str:
    return re.sub(r"<.*>", "", text).replace("::", ".").strip(". ")


def cpp_type(text: str | None) -> str | None:
    """``const std::shared_ptr<Order>&`` -> ``Order``; primitives -> None."""
    if not text:
        return None
    text = re.sub(r"\b(const|volatile|struct|class|enum|typename|static|inline)\b", "", text)
    text = text.replace("*", "").replace("&", "").strip()
    smart = _SMART_POINTER.match(text)
    if smart:
        return cpp_type(smart.group(1))
    name = cpp_name(text).rsplit(".", 1)[-1]
    if not name or not (name[0].isupper() or "_" in name and not name.endswith("_t")):
        return None
    return name


def normalize_cpp_doc(comments: list[str]) -> str | None:
    if not comments:
        return None
    if any(c.startswith(("/**", "/*!")) for c in comments):
        return normalize_javadoc(["/**" + c[3:] for c in comments if c.startswith(("/**", "/*!"))])
    lines = [re.sub(r"^//[/!]?\s?", "", c) for c in comments if c.startswith("//")]
    lines += [c[2:-2].strip() for c in comments if c.startswith("/*") and not c.startswith("/**")]
    return "\n".join(lines).strip() or None


@dataclass
class _Ctx:
    symbol: int | None = None
    prefix: str | None = None
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    locals: dict[str, str] = field(default_factory=dict)
    access: str | None = None
    in_function: bool = False


class CppExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        self._types: dict[str, int] = {}  # qualified type name -> symbol index
        self.visit_children(root, _Ctx())
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for child in node.named_children:
            self.visit(child, ctx)

    def visit(self, node: Node, ctx: _Ctx) -> None:
        if node.type in _TYPE_SPECIFIERS:
            self.type_specifier(node, ctx)
            return
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node, ctx)
        else:
            self.visit_children(node, ctx)

    # --- preprocessor & namespaces ------------------------------------------------

    def on_preproc_include(self, node: Node, ctx: _Ctx) -> None:
        path = node.child_by_field_name("path")
        if path is not None and path.type == "string_literal":
            self.out.imports.append(
                Import(self.text(path).strip('"'), self.line(node), kind="include")
            )

    def on_preproc_function_def(self, node: Node, ctx: _Ctx) -> None:
        self.add_symbol(
            node,
            "macro",
            self.text(node.child_by_field_name("name")),
            None,
            qualified_prefix=None,
            signature=collapse(self.text(node), 160),
            doc=normalize_cpp_doc(self.comments_before(node)),
        )

    def on_namespace_definition(self, node: Node, ctx: _Ctx) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:  # anonymous namespace
            if body is not None:
                self.visit_children(body, ctx)
            return
        name = cpp_name(self.text(name_node))
        full = f"{ctx.prefix}.{name}" if ctx.prefix else name
        self.out.namespaces.append(full)
        if body is not None:
            self.visit_children(body, replace(ctx, prefix=full))

    def on_linkage_specification(self, node: Node, ctx: _Ctx) -> None:  # extern "C" { }
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, ctx)

    on_template_declaration = visit_children

    # --- helpers ------------------------------------------------------------------

    def comments_before(self, node: Node) -> list[str]:
        target = (
            node.parent
            if node.parent is not None and node.parent.type == "template_declaration"
            else node
        )
        comments: list[str] = []
        current = target
        sibling = target.prev_sibling
        while sibling is not None and sibling.type in _COMMENTS:
            if current.start_point[0] - sibling.end_point[0] > 1:
                break
            comments.append(self.text(sibling))
            current = sibling
            sibling = sibling.prev_sibling
        comments.reverse()
        return comments

    def innermost(self, declarator: Node | None) -> Node | None:
        current = declarator
        while current is not None and current.type in _DECLARATOR_WRAPPERS:
            inner = current.child_by_field_name("declarator")
            if inner is None:
                inner = next((c for c in current.named_children if c.type in _NAME_NODES), None)
            current = inner
        return current

    def function_declarator(self, declarator: Node | None) -> Node | None:
        current = declarator
        while current is not None and current.type in _DECLARATOR_WRAPPERS:
            if current.type == "function_declarator":
                return current
            current = current.child_by_field_name("declarator")
        return None

    def storage(self, node: Node) -> list[str]:
        kinds = ("storage_class_specifier", "virtual", "type_qualifier", "virtual_specifier")
        return [self.text(c) for c in node.children if c.type in kinds]

    def type_refs(self, node: Node | None, ctx: _Ctx) -> None:
        if node is None:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "type_identifier":
                name = self.text(current)
                if len(name) > 1 and name[:1].isupper():
                    key = (ctx.symbol, name)
                    if key not in self._seen_type_refs:
                        self._seen_type_refs.add(key)
                        self.add_ref(current, "type_ref", name, ctx.symbol)
            else:
                stack.extend(current.named_children)

    # --- types --------------------------------------------------------------------

    def type_specifier(self, node: Node, ctx: _Ctx, typedef_name: str | None = None) -> None:
        body = node.child_by_field_name("body")
        name_node = node.child_by_field_name("name")
        if body is None:  # a use such as "struct Order *o", not a definition
            self.visit_children(node, ctx)
            return
        name = cpp_name(self.text(name_node)) if name_node is not None else typedef_name
        if not name:
            return
        kind = _TYPE_SPECIFIERS[node.type]
        outer = (
            node.parent
            if node.parent is not None
            and node.parent.type in ("type_definition", "template_declaration")
            else node
        )
        index = self.add_symbol(
            outer,
            kind,
            name.rsplit(".", 1)[-1],
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=collapse(
                self.src[outer.start_byte : body.start_byte].decode("utf-8", errors="replace")
            ),
            visibility=ctx.access,
            doc=normalize_cpp_doc(self.comments_before(outer)),
        )
        self._types[self.qualified(index)] = index
        bases = next((c for c in node.named_children if c.type == "base_class_clause"), None)
        for base in bases.named_children if bases is not None else ():
            if base.type in ("type_identifier", "template_type", "qualified_identifier"):
                self.add_ref(base, "inherits", cpp_name(self.text(base)).rsplit(".", 1)[-1], index)
        if kind == "enum":
            for enumerator in body.named_children:
                if enumerator.type == "enumerator":
                    self.add_symbol(
                        enumerator,
                        "const",
                        self.text(enumerator.child_by_field_name("name")),
                        index,
                        qualified_prefix=self.qualified(index),
                        signature=collapse(self.text(enumerator), 120),
                    )
            return
        default_access = "private" if node.type == "class_specifier" else "public"
        type_ctx = _Ctx(
            symbol=index,
            prefix=self.qualified(index),
            cls=index,
            access=default_access,
            fields=self.collect_fields(body),
        )
        for member in body.named_children:
            if member.type == "access_specifier":
                type_ctx.access = self.text(member).rstrip(":").strip()
            elif member.type in ("field_declaration", "declaration"):
                self.member_declaration(member, type_ctx)
            else:
                self.visit(member, type_ctx)

    def on_type_definition(self, node: Node, ctx: _Ctx) -> None:
        type_node = node.child_by_field_name("type")
        declarator = node.child_by_field_name("declarator")
        if type_node is not None and type_node.type in _TYPE_SPECIFIERS:
            name = self.text(self.innermost(declarator)) if declarator is not None else None
            self.type_specifier(type_node, ctx, typedef_name=name)
            return
        self.visit_children(node, ctx)

    def collect_fields(self, body: Node) -> dict[str, str]:
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type != "field_declaration":
                continue
            type_name = cpp_type(self.text(member.child_by_field_name("type")))
            for declarator in member.children_by_field_name("declarator"):
                if self.function_declarator(declarator) is not None:
                    continue
                name = self.innermost(declarator)
                if name is not None and type_name:
                    fields[self.text(name)] = type_name
        return fields

    def member_declaration(self, node: Node, ctx: _Ctx) -> None:
        """Fields and member function declarations inside a class body."""
        type_node = node.child_by_field_name("type")
        for declarator in node.children_by_field_name("declarator"):
            function = self.function_declarator(declarator)
            name_node = self.innermost(declarator)
            if name_node is None:
                continue
            name = cpp_name(self.text(name_node)).rsplit(".", 1)[-1]
            if function is not None:
                class_name = self.out.symbols[ctx.cls].name if ctx.cls is not None else None
                kind = "constructor" if name == class_name else "method"
                modifiers = self.storage(node) + ["declaration"]
                if any(
                    c.type == "number_literal" and self.text(c) == "0" for c in node.named_children
                ):
                    modifiers.append("abstract")
                index = self.add_symbol(
                    node,
                    kind,
                    name,
                    ctx.symbol,
                    qualified_prefix=ctx.prefix,
                    signature=collapse(self.text(node).rstrip(";")),
                    visibility=ctx.access,
                    modifiers=modifiers,
                    doc=normalize_cpp_doc(self.comments_before(node)),
                    type_name=cpp_type(self.text(type_node)) if type_node is not None else None,
                )
                self.type_refs(
                    function.child_by_field_name("parameters"), replace(ctx, symbol=index)
                )
                self.type_refs(type_node, replace(ctx, symbol=index))
            else:
                index = self.add_symbol(
                    node,
                    "field",
                    name,
                    ctx.symbol,
                    qualified_prefix=ctx.prefix,
                    signature=collapse(self.text(node).rstrip(";")),
                    visibility=ctx.access,
                    modifiers=self.storage(node),
                    doc=normalize_cpp_doc(self.comments_before(node)),
                    type_name=cpp_type(self.text(type_node)) if type_node is not None else None,
                )
                self.type_refs(type_node, replace(ctx, symbol=index))

    # --- functions ----------------------------------------------------------------

    def on_function_definition(self, node: Node, ctx: _Ctx) -> None:
        declarator = node.child_by_field_name("declarator")
        function = self.function_declarator(declarator)
        name_node = self.innermost(declarator)
        body = node.child_by_field_name("body")
        if function is None or name_node is None:
            self.visit_children(node, ctx)
            return
        full = cpp_name(self.text(name_node))
        scope, _, name = full.rpartition(".")
        parent, prefix, cls = ctx.symbol, ctx.prefix, ctx.cls
        if scope:  # out-of-line member: void Repo::find() {}
            qualified_scope = f"{ctx.prefix}.{scope}" if ctx.prefix else scope
            owner = self._types.get(qualified_scope)
            if owner is None:
                owner = next(
                    (i for q, i in self._types.items() if q.endswith("." + scope) or q == scope),
                    None,
                )
            parent = owner if owner is not None else ctx.symbol
            prefix = self.qualified(owner) if owner is not None else qualified_scope
            cls = owner
        in_type = scope != "" or ctx.cls is not None
        type_name = (
            scope.rsplit(".", 1)[-1]
            if scope
            else (self.out.symbols[ctx.cls].name if ctx.cls is not None else None)
        )
        if name == type_name:
            kind = "constructor"
        elif in_type:
            kind = "method"
        else:
            kind = "function"
        type_node = node.child_by_field_name("type")
        modifiers = self.storage(node)
        index = self.add_symbol(
            node.parent
            if node.parent is not None and node.parent.type == "template_declaration"
            else node,
            kind,
            name,
            parent,
            qualified_prefix=prefix,
            signature=self.header(node, body),
            visibility=ctx.access
            if ctx.cls is not None
            else ("private" if "static" in modifiers else None),
            modifiers=modifiers,
            doc=normalize_cpp_doc(self.comments_before(node)),
            type_name=cpp_type(self.text(type_node)) if type_node is not None else None,
        )
        fn_ctx = replace(
            ctx,
            symbol=index,
            prefix=self.qualified(index),
            cls=cls,
            fields=ctx.fields if cls == ctx.cls else self._fields_of(cls),
            locals=self.parameter_types(function.child_by_field_name("parameters")),
            in_function=True,
        )
        self.type_refs(function.child_by_field_name("parameters"), fn_ctx)
        self.type_refs(type_node, fn_ctx)
        if body is not None:
            self.visit_children(body, fn_ctx)

    def _fields_of(self, cls: int | None) -> dict[str, str]:
        if cls is None:
            return {}
        return {
            s.name: s.type_name
            for s in self.out.symbols
            if s.parent == cls and s.kind == "field" and s.type_name
        }

    def parameter_types(self, params: Node | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for param in params.named_children if params is not None else ():
            type_name = cpp_type(self.text(param.child_by_field_name("type")))
            name = self.innermost(param.child_by_field_name("declarator"))
            if type_name and name is not None:
                result[self.text(name)] = type_name
        return result

    def on_declaration(self, node: Node, ctx: _Ctx) -> None:
        type_node = node.child_by_field_name("type")
        declarators = node.children_by_field_name("declarator")
        if not ctx.in_function and any(self.function_declarator(d) for d in declarators):
            # prototype: int helper(Order *o);
            for declarator in declarators:
                name_node = self.innermost(declarator)
                if name_node is None or self.function_declarator(declarator) is None:
                    continue
                full = cpp_name(self.text(name_node))
                scope, _, name = full.rpartition(".")
                prefix = ".".join(p for p in (ctx.prefix, scope) if p) or None
                self.add_symbol(
                    node,
                    "function" if ctx.cls is None else "method",
                    name,
                    ctx.symbol,
                    qualified_prefix=prefix,
                    signature=collapse(self.text(node).rstrip(";")),
                    modifiers=self.storage(node) + ["declaration"],
                    doc=normalize_cpp_doc(self.comments_before(node)),
                    type_name=cpp_type(self.text(type_node)) if type_node is not None else None,
                )
            return
        if type_node is not None and type_node.type in _TYPE_SPECIFIERS:
            self.type_specifier(type_node, ctx)
        declared = cpp_type(self.text(type_node)) if type_node is not None else None
        for declarator in declarators:
            name_node = self.innermost(declarator)
            value = (
                declarator.child_by_field_name("value")
                if declarator.type == "init_declarator"
                else None
            )
            type_name = declared
            if type_name is None and value is not None and value.type == "new_expression":
                type_name = cpp_type(self.text(value.child_by_field_name("type")))
            if ctx.in_function and name_node is not None and type_name:
                ctx.locals[self.text(name_node)] = type_name
            if value is not None:
                self.visit(value, ctx)
            elif declarator.type == "init_declarator":
                self.visit_children(declarator, ctx)
        if ctx.in_function and type_node is not None:
            self.type_refs(type_node, ctx)

    # --- expressions --------------------------------------------------------------

    def on_call_expression(self, node: Node, ctx: _Ctx) -> None:
        function = node.child_by_field_name("function")
        if function is not None:
            if function.type == "template_function":
                function = function.child_by_field_name("name") or function
            if function.type == "identifier":
                self.add_ref(function, "call", self.text(function), ctx.symbol)
            elif function.type == "field_expression":
                self.field_call(function, ctx)
            elif function.type == "qualified_identifier":
                full = cpp_name(self.text(function))
                scope, _, name = full.rpartition(".")
                self.add_ref(function, "call", name, ctx.symbol, qualifier=scope or None)
        self.visit_children(node, ctx)

    def field_call(self, function: Node, ctx: _Ctx) -> None:
        name = self.text(function.child_by_field_name("field"))
        argument = function.child_by_field_name("argument")
        if argument is None or not name:
            return
        segments: list[str] = []
        current = argument
        while current.type == "field_expression":
            segments.append(self.text(current.child_by_field_name("field")))
            current = current.child_by_field_name("argument")
        segments.reverse()
        root_type = None
        if current.type == "this":
            qualifier = ".".join(["this", *segments])
            root_type = ctx.fields.get(segments[0]) if segments else None
        elif current.type == "identifier":
            root = self.text(current)
            qualifier = ".".join([root, *segments])
            root_type = ctx.locals.get(root) or ctx.fields.get(root)
        else:
            qualifier = collapse(self.text(argument).replace("->", "."), 80)
        self.add_ref(
            function.child_by_field_name("field"),
            "call",
            name,
            ctx.symbol,
            qualifier=qualifier,
            qualifier_type=root_type,
        )

    def on_new_expression(self, node: Node, ctx: _Ctx) -> None:
        type_name = cpp_type(self.text(node.child_by_field_name("type")))
        if type_name:
            self.add_ref(node, "instantiate", type_name, ctx.symbol)
        self.visit_children(node, ctx)
