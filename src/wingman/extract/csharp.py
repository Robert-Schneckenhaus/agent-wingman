"""C# extractor."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.extract.base import ExtractorBase, collapse, strip_generics, value_type
from wingman.extract.model import Extraction, Import

_TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "record_declaration": "record",
    "record_struct_declaration": "record",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}
_VISIBILITY = ("public", "private", "protected", "internal")
_INTERFACE_NAME = re.compile(r"^I[A-Z]")
_XML_TAG = re.compile(r"<(\w+)([^>]*)>(.*?)</\1>", re.DOTALL)
_XML_EMPTY_TAG = re.compile(r"<(?:see|seealso|paramref|typeparamref)\s+\w+=\"([^\"]*)\"\s*/>")


@dataclass
class _Ctx:
    symbol: int | None = None
    prefix: str | None = None
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    locals: dict[str, str] = field(default_factory=dict)


def normalize_xml_doc(comments: list[str]) -> str | None:
    lines = [c[3:].strip() for c in comments if c.startswith("///")]
    if not lines:
        plain = [c[2:].strip() for c in comments if c.startswith("//")]
        return "\n".join(plain).strip() or None
    xml = _XML_EMPTY_TAG.sub(lambda m: m.group(1).rsplit(".", 1)[-1], " ".join(lines))
    parts: list[str] = []
    for tag, attrs, content in _XML_TAG.findall(xml):
        content = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
        content = re.sub(r"\s+", " ", content)
        if not content:
            continue
        if tag in ("summary", "remarks"):
            parts.append(content)
        elif tag == "param":
            name = re.search(r'name="([^"]*)"', attrs)
            parts.append(f"{name.group(1) if name else 'param'}: {content}")
        elif tag == "returns":
            parts.append(f"Returns: {content}")
        elif tag == "exception":
            ref = re.search(r'cref="([^"]*)"', attrs)
            parts.append(f"Throws {ref.group(1) if ref else ''}: {content}".replace(" :", ":"))
    if not parts:  # no XML tags, plain text after ///
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", xml))).strip() or None
    return "\n".join(parts)


class CSharpExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        self.visit_children(root, _Ctx())
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for index, child in enumerate(node.children):
            if not child.is_named:
                continue
            field_name = node.field_name_for_child(index)
            if field_name in ("type", "returns"):
                self.type_refs(child, ctx)
            else:
                self.visit(child, ctx)

    def visit(self, node: Node, ctx: _Ctx) -> None:
        if node.type == "file_scoped_namespace_declaration":
            # applies to all following siblings
            name = self.text(node.child_by_field_name("name"))
            self.out.namespaces.append(name)
            ctx.prefix = name
            return
        if node.type in _TYPE_DECLARATIONS:
            self.type_declaration(node, ctx)
            return
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node, ctx)
        else:
            self.visit_children(node, ctx)

    # --- usings & namespaces ------------------------------------------------------

    def on_using_directive(self, node: Node, ctx: _Ctx) -> None:
        tokens = {c.type for c in node.children if not c.is_named}
        alias_node = node.child_by_field_name("name")
        target = next(
            (c for c in node.named_children if c != alias_node and c.type != "comment"),
            None,
        )
        if target is None:
            return
        if "static" in tokens:
            kind = "using_static"
        elif "global" in tokens:
            kind = "global_using"
        else:
            kind = "using"
        self.out.imports.append(
            Import(
                module=self.text(target),
                line=self.line(node),
                alias=self.text(alias_node) or None,
                kind=kind,
            )
        )

    def on_namespace_declaration(self, node: Node, ctx: _Ctx) -> None:
        name = self.text(node.child_by_field_name("name"))
        full = f"{ctx.prefix}.{name}" if ctx.prefix else name
        self.out.namespaces.append(full)
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit_children(body, _Ctx(prefix=full))

    # --- declarations -------------------------------------------------------------

    def type_declaration(self, node: Node, ctx: _Ctx) -> None:
        kind = _TYPE_DECLARATIONS[node.type]
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        modifiers = self.modifiers(node)
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header_without_attributes(node, body),
            visibility=self.visibility(modifiers),
            modifiers=modifiers,
            decorators=self.attributes(node),
            doc=normalize_xml_doc(self.preceding_comments(node)),
        )
        type_ctx = _Ctx(symbol=index, prefix=self.qualified(index), cls=index)

        for child in node.named_children:
            if child.type == "base_list":
                for base in child.named_children:
                    if base.type == "argument_list":  # primary constructor base call
                        continue
                    base_name = strip_generics(self.text(base).split("(", 1)[0])
                    edge = "implements" if _INTERFACE_NAME.match(base_name) else "inherits"
                    self.add_ref(base, edge, base_name, index)
            elif child.type == "parameter_list":  # record / primary constructor
                for param in child.named_children:
                    type_node = param.child_by_field_name("type")
                    param_name = self.text(param.child_by_field_name("name"))
                    if type_node is not None and param_name:
                        type_ctx.fields[param_name] = strip_generics(self.text(type_node))
                        self.type_refs(type_node, type_ctx)

        if body is None or kind == "enum":
            return
        type_ctx.fields.update(self.collect_fields(body))
        for member in body.named_children:
            self.member(member, type_ctx, interface=kind == "interface")

    def on_delegate_declaration(self, node: Node, ctx: _Ctx) -> None:
        modifiers = self.modifiers(node)
        index = self.add_symbol(
            node,
            "delegate",
            self.text(node.child_by_field_name("name")),
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header_without_attributes(node, None),
            visibility=self.visibility(modifiers),
            modifiers=modifiers,
            doc=normalize_xml_doc(self.preceding_comments(node)),
        )
        self.visit_children(node, replace(ctx, symbol=index))

    def collect_fields(self, body: Node) -> dict[str, str]:
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type == "field_declaration":
                declaration = next(
                    (c for c in member.named_children if c.type == "variable_declaration"), None
                )
                if declaration is None:
                    continue
                type_name = strip_generics(self.text(declaration.child_by_field_name("type")))
                for declarator in declaration.named_children:
                    if declarator.type == "variable_declarator":
                        fields[self.text(declarator.child_by_field_name("name"))] = type_name
            elif member.type == "property_declaration":
                type_node = member.child_by_field_name("type")
                fields[self.text(member.child_by_field_name("name"))] = strip_generics(
                    self.text(type_node)
                )
        return fields

    def member(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        kind = {
            "method_declaration": "method",
            "constructor_declaration": "constructor",
            "property_declaration": "property",
            "destructor_declaration": "method",
            "operator_declaration": "method",
            "indexer_declaration": "property",
        }.get(node.type)

        if kind is None and node.type == "field_declaration":
            self.field(node, ctx, interface=interface)
            return
        if kind is None:
            self.visit(node, ctx)
            return

        name_node = node.child_by_field_name("name")
        name = self.text(name_node) if name_node is not None else node.type.split("_")[0]
        if node.type == "indexer_declaration":
            name = "this[]"
        body = node.child_by_field_name("body") or node.child_by_field_name("accessors")
        modifiers = self.modifiers(node)
        declared = node.child_by_field_name("returns") or node.child_by_field_name("type")
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header_without_attributes(node, body),
            visibility=self.visibility(modifiers) or ("public" if interface else "private"),
            modifiers=modifiers,
            decorators=self.attributes(node),
            doc=normalize_xml_doc(self.preceding_comments(node)),
            type_name=value_type(self.text(declared)) if declared is not None else None,
        )
        member_ctx = replace(ctx, symbol=index, prefix=self.qualified(index), locals={})
        params = node.child_by_field_name("parameters")
        for param in params.named_children if params is not None else ():
            type_node = param.child_by_field_name("type")
            param_name = param.child_by_field_name("name")
            if type_node is not None and param_name is not None:
                member_ctx.locals[self.text(param_name)] = strip_generics(self.text(type_node))
        for child_index, child in enumerate(node.children):
            if not child.is_named or child.type in ("attribute_list", "modifier", "identifier"):
                continue
            field_name = node.field_name_for_child(child_index)
            if field_name in ("type", "returns"):
                self.type_refs(child, member_ctx)
            elif field_name != "name":
                self.visit(child, member_ctx)

    def field(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        modifiers = self.modifiers(node)
        declaration = next(
            (c for c in node.named_children if c.type == "variable_declaration"), None
        )
        if declaration is None:
            return
        kind = "const" if "const" in modifiers else "field"
        type_node = declaration.child_by_field_name("type")
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            index = self.add_symbol(
                declarator if len(declaration.named_children) > 2 else node,
                kind,
                self.text(declarator.child_by_field_name("name")),
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=collapse(self.text(node), 200),
                visibility=self.visibility(modifiers) or ("public" if interface else "private"),
                modifiers=modifiers,
                decorators=self.attributes(node),
                doc=normalize_xml_doc(self.preceding_comments(node)),
                type_name=value_type(self.text(type_node)) if type_node is not None else None,
            )
            field_ctx = replace(ctx, symbol=index)
            if type_node is not None:
                self.type_refs(type_node, field_ctx)
            for child in declarator.named_children:
                if child.type != "identifier":  # skip the declarator's own name
                    self.visit(child, field_ctx)

    # --- statements & expressions -------------------------------------------------

    def on_local_function_statement(self, node: Node, ctx: _Ctx) -> None:
        body = node.child_by_field_name("body")
        index = self.add_symbol(
            node,
            "function",
            self.text(node.child_by_field_name("name")),
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=self.modifiers(node),
        )
        self.visit_children(node, replace(ctx, symbol=index, locals=dict(ctx.locals)))

    def on_local_declaration_statement(self, node: Node, ctx: _Ctx) -> None:
        declaration = next(
            (c for c in node.named_children if c.type == "variable_declaration"), None
        )
        if declaration is not None:
            type_node = declaration.child_by_field_name("type")
            declared = None
            if type_node is not None and type_node.type != "implicit_type":
                declared = strip_generics(self.text(type_node))
            for declarator in declaration.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = self.text(declarator.child_by_field_name("name"))
                type_name = declared or self.creation_type(declarator)
                if name and type_name:
                    ctx.locals[name] = type_name
        self.visit_children(node, ctx)

    def creation_type(self, declarator: Node) -> str | None:
        for child in declarator.named_children:
            if child.type == "object_creation_expression":
                return strip_generics(self.text(child.child_by_field_name("type")))
        return None

    def on_invocation_expression(self, node: Node, ctx: _Ctx) -> None:
        function = node.child_by_field_name("function")
        if function is not None:
            if function.type in ("identifier", "generic_name"):
                self.add_ref(function, "call", self.simple_name(function), ctx.symbol)
            elif function.type == "member_access_expression":
                self.member_ref(function, "call", ctx)
        self.visit_children(node, ctx)

    def on_object_creation_expression(self, node: Node, ctx: _Ctx) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            self.add_ref(type_node, "instantiate", strip_generics(self.text(type_node)), ctx.symbol)
        for index, child in enumerate(node.children):
            if child.is_named and node.field_name_for_child(index) != "type":
                self.visit(child, ctx)

    def member_ref(self, node: Node, kind: str, ctx: _Ctx) -> None:
        name_node = node.child_by_field_name("name")
        expression = node.child_by_field_name("expression")
        if name_node is None or expression is None:
            return
        qualifier = collapse(self.text(expression), 80)
        self.add_ref(
            name_node,
            kind,
            self.simple_name(name_node),
            ctx.symbol,
            qualifier=qualifier,
            qualifier_type=self.chain_root_type(expression, ctx),
        )
        if expression.type not in ("identifier", "this", "base"):
            self.visit(expression, ctx)

    def chain_root_type(self, expression: Node, ctx: _Ctx) -> str | None:
        """Declared type of the first segment of ``a.b.c`` / ``this.a.b``."""
        segments: list[str] = []
        current = expression
        while current.type == "member_access_expression":
            segments.append(self.simple_name(current.child_by_field_name("name")))
            current = current.child_by_field_name("expression")
            if current is None:
                return None
        if current.type == "this":
            return ctx.fields.get(segments[-1]) if segments else None
        if current.type == "identifier":
            name = self.text(current)
            return ctx.locals.get(name) or ctx.fields.get(name)
        return None

    def type_refs(self, node: Node, ctx: _Ctx) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier":
                self._type_ref(current, ctx)
            elif current.type == "qualified_name":
                name = current.child_by_field_name("name")
                if name is not None:
                    stack.append(name)
            else:
                stack.extend(current.named_children)

    def _type_ref(self, node: Node, ctx: _Ctx) -> None:
        name = self.text(node)
        # single letters are type parameters; lowercase names come from grammar
        # ambiguities such as `Round(net * rate)` parsed as a pointer type
        if len(name) == 1 or not name[:1].isupper():
            return
        key = (ctx.symbol, name)
        if key not in self._seen_type_refs:
            self._seen_type_refs.add(key)
            self.add_ref(node, "type_ref", name, ctx.symbol)

    # --- helpers ------------------------------------------------------------------

    def simple_name(self, node: Node) -> str:
        if node.type == "generic_name":
            ident = next((c for c in node.named_children if c.type == "identifier"), None)
            return self.text(ident)
        return self.text(node)

    def modifiers(self, node: Node) -> list[str]:
        return [self.text(c) for c in node.children if c.type == "modifier"]

    @staticmethod
    def visibility(modifiers: list[str]) -> str | None:
        found = [m for m in modifiers if m in _VISIBILITY]
        return " ".join(found) if found else None

    def attributes(self, node: Node) -> list[str]:
        return [self.text(c) for c in node.children if c.type == "attribute_list"]

    def header_without_attributes(self, node: Node, body: Node | None) -> str:
        start = node.start_byte
        for child in node.children:
            if child.type in ("attribute_list", "comment"):
                start = child.end_byte
            else:
                break
        end = body.start_byte if body is not None else node.end_byte
        return collapse(self.src[start:end].decode("utf-8", errors="replace"))
