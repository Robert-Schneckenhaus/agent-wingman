"""TypeScript / TSX extractor."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.extract.base import ExtractorBase, collapse, strip_generics, value_type
from wingman.extract.model import Extraction, Import

_MODIFIER_TOKENS = {"static", "async", "readonly", "abstract", "override", "declare", "get", "set"}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_JSX_NODES = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
_TS_TYPE_SKIP = re.compile(r"^[A-Z]$")  # generic type parameters like T


@dataclass
class _Ctx:
    symbol: int | None = None
    """Symbol that owns references found here."""
    prefix: str | None = None
    """Qualified-name prefix for new symbols."""
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    """Enclosing class: member name -> declared type."""
    locals: dict[str, str] = field(default_factory=dict)
    """Enclosing function: variable/parameter name -> declared type."""
    in_function: bool = False


def normalize_jsdoc(comments: list[str]) -> str | None:
    blocks = [c for c in comments if c.startswith("/**")]
    if blocks:
        body = blocks[-1][3:]
        body = body[:-2] if body.endswith("*/") else body
        lines = [re.sub(r"^\s*\*\s?", "", line) for line in body.splitlines()]
    else:
        lines = [c[2:].strip() for c in comments if c.startswith("//")]
    text = "\n".join(line.rstrip() for line in lines).strip()
    return text or None


class TypeScriptExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        self.visit_children(root, _Ctx())
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for index, child in enumerate(node.children):
            if child.is_named:
                self.visit(child, ctx, node.field_name_for_child(index))

    def visit(self, node: Node, ctx: _Ctx, field_name: str | None = None) -> None:
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node, ctx)
        elif node.type == "type_identifier":
            if field_name != "name":
                self.type_ref(node, ctx)
        else:
            self.visit_children(node, ctx)

    # --- imports / exports ------------------------------------------------------

    def on_import_statement(self, node: Node, ctx: _Ctx) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            return
        module = self.text(source).strip("'\"`")
        line = self.line(node)
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:  # side-effect import
            self.out.imports.append(Import(module=module, line=line))
            return
        for part in clause.named_children:
            if part.type == "identifier":
                self.out.imports.append(
                    Import(module, line, imported_name="default", alias=self.text(part))
                )
            elif part.type == "namespace_import":
                ident = next((c for c in part.named_children if c.type == "identifier"), None)
                self.out.imports.append(Import(module, line, "*", self.text(ident)))
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type == "import_specifier":
                        name = self.text(spec.child_by_field_name("name"))
                        alias = self.text(spec.child_by_field_name("alias")) or None
                        self.out.imports.append(Import(module, line, name, alias))

    def on_export_statement(self, node: Node, ctx: _Ctx) -> None:
        source = node.child_by_field_name("source")
        if source is not None:
            module = self.text(source).strip("'\"`")
            line = self.line(node)
            clause = next((c for c in node.named_children if c.type == "export_clause"), None)
            if clause is None:
                self.out.imports.append(Import(module, line, "*", kind="reexport"))
            else:
                for spec in clause.named_children:
                    if spec.type == "export_specifier":
                        name = self.text(spec.child_by_field_name("name"))
                        alias = self.text(spec.child_by_field_name("alias")) or None
                        self.out.imports.append(Import(module, line, name, alias, "reexport"))
            return

        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            self.visit_children(node, ctx)
            return
        is_default = any(c.type == "default" for c in node.children)
        modifiers = ["export", "default"] if is_default else ["export"]
        decorators = [self.text(c) for c in node.children if c.type == "decorator"]
        doc = normalize_jsdoc(self.preceding_comments(node))
        self.declaration(declaration, ctx, modifiers=modifiers, decorators=decorators, doc=doc)

    # --- declarations -----------------------------------------------------------

    def declaration(
        self,
        node: Node,
        ctx: _Ctx,
        *,
        modifiers: list[str] | None = None,
        decorators: list[str] | None = None,
        doc: str | None = None,
    ) -> None:
        kwargs = {"modifiers": modifiers or [], "decorators": decorators or [], "doc": doc}
        handler = {
            "class_declaration": self.class_like,
            "abstract_class_declaration": self.class_like,
            "interface_declaration": self.class_like,
            "enum_declaration": self.simple_type,
            "type_alias_declaration": self.simple_type,
            "function_declaration": self.function,
            "generator_function_declaration": self.function,
            "lexical_declaration": self.variables,
            "variable_declaration": self.variables,
            "internal_module": self.namespace,
            "module": self.namespace,
            "ambient_declaration": None,
        }.get(node.type)
        if handler is None:
            self.visit(node, ctx)
        else:
            handler(node, ctx, **kwargs)

    def _own_doc(self, node: Node, doc: str | None) -> str | None:
        return doc if doc is not None else normalize_jsdoc(self.preceding_comments(node))

    def on_class_declaration(self, node: Node, ctx: _Ctx) -> None:
        self.class_like(node, ctx)

    on_abstract_class_declaration = on_class_declaration
    on_interface_declaration = on_class_declaration

    def on_enum_declaration(self, node: Node, ctx: _Ctx) -> None:
        self.simple_type(node, ctx)

    on_type_alias_declaration = on_enum_declaration

    def on_function_declaration(self, node: Node, ctx: _Ctx) -> None:
        self.function(node, ctx)

    on_generator_function_declaration = on_function_declaration

    def on_lexical_declaration(self, node: Node, ctx: _Ctx) -> None:
        self.variables(node, ctx)

    on_variable_declaration = on_lexical_declaration

    def on_internal_module(self, node: Node, ctx: _Ctx) -> None:
        self.namespace(node, ctx)

    on_module = on_internal_module

    def class_like(self, node: Node, ctx: _Ctx, *, modifiers=(), decorators=(), doc=None):
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        kind = "interface" if node.type == "interface_declaration" else "class"
        mods = list(modifiers)
        if node.type == "abstract_class_declaration":
            mods.append("abstract")
        decorators = list(decorators) + [
            self.text(c) for c in node.children if c.type == "decorator"
        ]
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=mods,
            decorators=decorators,
            doc=self._own_doc(node, doc),
        )
        for child in node.named_children:
            if child.type == "class_heritage":
                self.heritage(child, index)
            elif child.type == "extends_type_clause":  # interface extends
                for base in child.named_children:
                    self.add_ref(base, "inherits", strip_generics(self.text(base)), index)
            elif child.type == "type_parameters":
                continue

        if body is None:
            return
        class_ctx = _Ctx(symbol=index, prefix=self.qualified(index), cls=index)
        class_ctx.fields = self.collect_fields(body)
        for member in body.named_children:
            self.member(member, class_ctx, interface=kind == "interface")

    def heritage(self, node: Node, class_index: int) -> None:
        for clause in node.named_children:
            if clause.type == "extends_clause":
                value = clause.child_by_field_name("value")
                if value is not None:
                    self.add_ref(value, "inherits", strip_generics(self.text(value)), class_index)
            elif clause.type == "implements_clause":
                for base in clause.named_children:
                    self.add_ref(base, "implements", strip_generics(self.text(base)), class_index)

    def collect_fields(self, body: Node) -> dict[str, str]:
        """Member name -> declared type, including constructor parameter properties."""
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type in ("public_field_definition", "property_signature"):
                name = self.text(member.child_by_field_name("name"))
                type_name = self.annotation_type(member) or self.new_type(
                    member.child_by_field_name("value")
                )
                if name and type_name:
                    fields[name] = type_name
            elif member.type == "method_definition":
                if self.text(member.child_by_field_name("name")) != "constructor":
                    continue
                params = member.child_by_field_name("parameters")
                for param in params.named_children if params else ():
                    is_property = any(
                        c.type == "accessibility_modifier" or c.type == "readonly"
                        for c in param.children
                    )
                    if is_property:
                        name = self.text(param.child_by_field_name("pattern"))
                        type_name = self.annotation_type(param)
                        if name and type_name:
                            fields[name] = type_name
        return fields

    def member(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        if node.type in ("method_definition", "method_signature", "abstract_method_signature"):
            name = self.text(node.child_by_field_name("name"))
            body = node.child_by_field_name("body")
            modifiers = self.modifier_tokens(node)
            kind = "constructor" if name == "constructor" else "method"
            if "get" in modifiers or "set" in modifiers:
                kind = "property"
            index = self.add_symbol(
                node,
                kind,
                name,
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=self.header(node, body),
                visibility=self.accessibility(node) or ("public" if interface else None),
                modifiers=modifiers,
                decorators=[self.text(c) for c in node.children if c.type == "decorator"],
                doc=normalize_jsdoc(self.preceding_comments(node)),
                type_name=self.return_type(node),
            )
            self.function_body(node, ctx, index)
        elif node.type in ("public_field_definition", "property_signature"):
            name = self.text(node.child_by_field_name("name"))
            value = node.child_by_field_name("value")
            is_function = value is not None and value.type in _FUNCTION_VALUES
            index = self.add_symbol(
                node,
                "method" if is_function else "property",
                name,
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=self.header(
                    node, value.child_by_field_name("body") if is_function else None
                ),
                visibility=self.accessibility(node) or ("public" if interface else None),
                modifiers=self.modifier_tokens(node),
                decorators=[self.text(c) for c in node.children if c.type == "decorator"],
                doc=normalize_jsdoc(self.preceding_comments(node)),
                type_name=(
                    self.return_type(value)
                    if is_function
                    else ctx.fields.get(name) or self.annotation_type(node)
                ),
            )
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                self.visit(type_node, replace(ctx, symbol=index))
            if value is not None:
                if is_function:
                    self.function_body(value, ctx, index)
                else:
                    self.visit(value, replace(ctx, symbol=index))
        elif node.type == "comment" or node.type == "decorator":
            return
        else:
            self.visit(node, ctx)

    def simple_type(self, node: Node, ctx: _Ctx, *, modifiers=(), decorators=(), doc=None):
        kind = "enum" if node.type == "enum_declaration" else "type_alias"
        body = node.child_by_field_name("body") if kind == "enum" else None
        index = self.add_symbol(
            node,
            kind,
            self.text(node.child_by_field_name("name")),
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=list(modifiers),
            doc=self._own_doc(node, doc),
        )
        if kind == "type_alias":
            value = node.child_by_field_name("value")
            if value is not None:
                self.visit(value, replace(ctx, symbol=index))

    def function(self, node: Node, ctx: _Ctx, *, modifiers=(), decorators=(), doc=None):
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        kind = "component" if self.is_component(name, body) else "function"
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=list(modifiers) + self.modifier_tokens(node),
            decorators=list(decorators),
            doc=self._own_doc(node, doc),
            type_name=self.return_type(node),
        )
        self.function_body(node, ctx, index)

    def variables(self, node: Node, ctx: _Ctx, *, modifiers=(), decorators=(), doc=None):
        is_const = any(c.type == "const" for c in node.children)
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            name = self.text(name_node)

            if ctx.in_function:
                type_name = self.annotation_type(declarator) or self.new_type(value)
                if name_node is not None and name_node.type == "identifier" and type_name:
                    ctx.locals[name] = type_name
                self.visit_children(declarator, ctx)
                continue

            is_function = value is not None and value.type in _FUNCTION_VALUES
            if not is_function and not (is_const and name_node.type == "identifier"):
                self.visit_children(declarator, ctx)
                continue

            if is_function:
                fn_body = value.child_by_field_name("body")
                kind = "component" if self.is_component(name, fn_body) else "function"
                signature = self.header(node, fn_body)
            else:
                kind = "const"
                signature = collapse(self.text(node), 160)
            index = self.add_symbol(
                declarator,
                kind,
                name,
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=signature,
                modifiers=list(modifiers) + (self.modifier_tokens(value) if is_function else []),
                decorators=list(decorators),
                doc=self._own_doc(node, doc),
                type_name=(
                    self.return_type(value)
                    if is_function
                    else self.annotation_type(declarator) or self.new_type(value)
                ),
            )
            if is_function:
                self.function_body(value, ctx, index)
            else:
                self.visit_children(declarator, replace(ctx, symbol=index))

    def namespace(self, node: Node, ctx: _Ctx, *, modifiers=(), decorators=(), doc=None):
        name = self.text(node.child_by_field_name("name")).strip("'\"")
        body = node.child_by_field_name("body")
        index = self.add_symbol(
            node,
            "namespace",
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=list(modifiers),
            doc=self._own_doc(node, doc),
        )
        if body is not None:
            self.visit_children(body, _Ctx(symbol=index, prefix=self.qualified(index)))

    def function_body(self, node: Node, ctx: _Ctx, index: int) -> None:
        """Visit parameters and body of a function-like node owned by symbol ``index``."""
        fn_ctx = _Ctx(
            symbol=index,
            prefix=self.qualified(index),
            cls=ctx.cls,
            fields=ctx.fields,
            locals=dict(ctx.locals),
            in_function=True,
        )
        for child_index, child in enumerate(node.children):
            if not child.is_named:
                continue
            field_name = node.field_name_for_child(child_index)
            if field_name == "name" or child.type in ("decorator", "accessibility_modifier"):
                continue
            if field_name == "parameters":
                for param in child.named_children:
                    pattern = param.child_by_field_name("pattern")
                    type_name = self.annotation_type(param)
                    if pattern is not None and pattern.type == "identifier" and type_name:
                        fn_ctx.locals[self.text(pattern)] = type_name
            self.visit(child, fn_ctx, field_name)

    # --- expressions --------------------------------------------------------------

    def on_arrow_function(self, node: Node, ctx: _Ctx) -> None:
        nested = replace(ctx, locals=dict(ctx.locals), in_function=True)
        self.visit_children(node, nested)

    on_function_expression = on_arrow_function

    def on_call_expression(self, node: Node, ctx: _Ctx) -> None:
        function = node.child_by_field_name("function")
        if function is not None:
            if function.type == "identifier":
                self.add_ref(function, "call", self.text(function), ctx.symbol)
            elif function.type == "member_expression":
                self.member_ref(function, "call", ctx)
        self.visit_children(node, ctx)

    def on_new_expression(self, node: Node, ctx: _Ctx) -> None:
        constructor = node.child_by_field_name("constructor")
        if constructor is not None:
            if constructor.type == "identifier":
                self.add_ref(constructor, "instantiate", self.text(constructor), ctx.symbol)
            elif constructor.type == "member_expression":
                self.member_ref(constructor, "instantiate", ctx)
        self.visit_children(node, ctx)

    def on_jsx_opening_element(self, node: Node, ctx: _Ctx) -> None:
        name = node.child_by_field_name("name")
        tag = self.text(name)
        if tag[:1].isupper():
            self.add_ref(name, "instantiate", tag.rsplit(".", 1)[-1], ctx.symbol)
        self.visit_children(node, ctx)

    on_jsx_self_closing_element = on_jsx_opening_element

    def on_generic_type(self, node: Node, ctx: _Ctx) -> None:
        name = node.child_by_field_name("name")
        if name is not None and name.type == "type_identifier":
            self.type_ref(name, ctx)
        arguments = node.child_by_field_name("type_arguments")
        if arguments is not None:
            self.visit_children(arguments, ctx)

    def member_ref(self, node: Node, kind: str, ctx: _Ctx) -> None:
        prop = node.child_by_field_name("property")
        obj = node.child_by_field_name("object")
        if prop is None or obj is None:
            return
        qualifier = collapse(self.text(obj), 80)
        self.add_ref(
            prop,
            kind,
            self.text(prop),
            ctx.symbol,
            qualifier=qualifier,
            qualifier_type=self.chain_root_type(obj, ctx),
        )

    def chain_root_type(self, obj: Node, ctx: _Ctx) -> str | None:
        """Declared type of the first segment of ``a.b.c`` / ``this.a.b``."""
        segments: list[str] = []
        current = obj
        while current.type in ("member_expression", "non_null_expression"):
            if current.type == "member_expression":
                segments.append(self.text(current.child_by_field_name("property")))
                current = current.child_by_field_name("object")
            else:
                current = current.named_children[0] if current.named_children else current
                if current.type == "non_null_expression":
                    break
        if current.type == "this":
            return ctx.fields.get(segments[-1]) if segments else None
        if current.type == "identifier":
            return ctx.locals.get(self.text(current))
        return None

    def return_type(self, node: Node | None) -> str | None:
        if node is None:
            return None
        annotation = node.child_by_field_name("return_type")
        return value_type(self.text(annotation)) if annotation is not None else None

    def type_ref(self, node: Node, ctx: _Ctx) -> None:
        name = self.text(node)
        if _TS_TYPE_SKIP.match(name):
            return
        key = (ctx.symbol, name)
        if key in self._seen_type_refs:
            return
        self._seen_type_refs.add(key)
        self.add_ref(node, "type_ref", name, ctx.symbol)

    # --- small helpers ------------------------------------------------------------

    def annotation_type(self, node: Node) -> str | None:
        annotation = node.child_by_field_name("type")
        if annotation is None:
            return None
        text = self.text(annotation).lstrip(":").strip()
        if not text or "|" in text or "=>" in text or text[0] in "{[(":
            return None
        return strip_generics(text) or None

    def new_type(self, value: Node | None) -> str | None:
        if value is None or value.type != "new_expression":
            return None
        constructor = value.child_by_field_name("constructor")
        return strip_generics(self.text(constructor)) if constructor is not None else None

    def modifier_tokens(self, node: Node | None) -> list[str]:
        if node is None:
            return []
        return [c.type for c in node.children if c.type in _MODIFIER_TOKENS]

    def accessibility(self, node: Node) -> str | None:
        modifier = next((c for c in node.children if c.type == "accessibility_modifier"), None)
        return self.text(modifier) if modifier is not None else None

    def is_component(self, name: str, body: Node | None) -> bool:
        if not name[:1].isupper() or body is None:
            return False
        stack = [body]
        while stack:
            current = stack.pop()
            if current.type in _JSX_NODES:
                return True
            stack.extend(current.named_children)
        return False
