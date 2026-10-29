"""Java extractor."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.endpoints import is_url_path, join_routes, normalize_route
from wingman.extract.base import ExtractorBase, collapse, strip_generics, value_type
from wingman.extract.model import Endpoint, Extraction, Import

_TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "interface",
}
_VISIBILITY = ("public", "private", "protected")
_COMMENTS = ("block_comment", "line_comment", "comment")
# Spring MVC / JAX-RS
_SPRING_MAPPING = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b\s*(?:\(([^)]*)\))?", re.DOTALL
)
_JAXRS_VERB = re.compile(r"@(GET|POST|PUT|DELETE|PATCH)\b")
_JAXRS_PATH = re.compile(r"@Path\s*\(\s*\"([^\"]*)\"")
_FIRST_STRING = re.compile(r"(?:(?:value|path)\s*=\s*)?\{?\s*\"([^\"]*)\"")
_REQUEST_METHOD = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)")
# RestTemplate / WebClient style clients
_CLIENT_METHODS = {
    "getForObject": "GET",
    "getForEntity": "GET",
    "postForObject": "POST",
    "postForEntity": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patchForObject": "PATCH",
}


def normalize_javadoc(comments: list[str]) -> str | None:
    """``/** ... */`` without ``*`` prefixes; ``@param``/``@return`` tags kept as text."""
    docs = [c for c in comments if c.startswith("/**")]
    if not docs:
        plain = [re.sub(r"^//\s?", "", c) for c in comments if c.startswith("//")]
        return "\n".join(plain).strip() or None
    text = docs[-1][3:-2]
    lines = [re.sub(r"^\s*\*\s?", "", line).rstrip() for line in text.splitlines()]
    result = "\n".join(lines).strip()
    result = re.sub(r"\{@(?:link|code|linkplain)\s+([^}]*)\}", r"\1", result)
    result = re.sub(r"<[^>]+>", "", result)
    result = re.sub(r"^@return\s+", "Returns: ", result, flags=re.MULTILINE)
    result = re.sub(r"^@param\s+(\w+)\s+", r"\1: ", result, flags=re.MULTILINE)
    return result.strip() or None


@dataclass
class _Ctx:
    symbol: int | None = None
    prefix: str | None = None
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    locals: dict[str, str] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)
    route_prefix: str | None = None


class JavaExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        ctx = _Ctx()
        for child in root.named_children:
            if child.type == "package_declaration":
                name = self.text(
                    next((c for c in child.named_children if c.type != "annotation"), None)
                )
                self.out.namespaces.append(name)
                ctx.prefix = name
        self.visit_children(root, ctx)
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for index, child in enumerate(node.children):
            if not child.is_named:
                continue
            if node.field_name_for_child(index) == "type":
                self.type_refs(child, ctx)
            else:
                self.visit(child, ctx)

    def visit(self, node: Node, ctx: _Ctx) -> None:
        if node.type in _TYPE_DECLARATIONS:
            self.type_declaration(node, ctx)
            return
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node, ctx)
        else:
            self.visit_children(node, ctx)

    def on_package_declaration(self, node: Node, ctx: _Ctx) -> None:
        pass

    def on_import_declaration(self, node: Node, ctx: _Ctx) -> None:
        tokens = {c.type for c in node.children}
        target = next(
            (c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None
        )
        if target is None:
            return
        module = self.text(target)
        wildcard = "asterisk" in tokens
        line = self.line(node)
        if "static" in tokens:
            # import static a.b.C.m -> members of C; import static a.b.C.* -> all of C
            owner = module if wildcard else module.rsplit(".", 1)[0]
            self.out.imports.append(Import(owner, line, kind="using_static"))
        elif wildcard:
            self.out.imports.append(Import(module, line, kind="using"))
        else:  # single-type import: like a C# alias of the simple name
            self.out.imports.append(
                Import(module, line, alias=module.rsplit(".", 1)[-1], kind="using")
            )

    # --- declarations -----------------------------------------------------------

    def comments_before(self, node: Node) -> list[str]:
        comments: list[str] = []
        current = node
        sibling = node.prev_sibling
        while sibling is not None and sibling.type in _COMMENTS:
            if current.start_point[0] - sibling.end_point[0] > 1:
                break
            comments.append(self.text(sibling))
            current = sibling
            sibling = sibling.prev_sibling
        comments.reverse()
        return comments

    def modifiers_node(self, node: Node) -> Node | None:
        return next((c for c in node.children if c.type == "modifiers"), None)

    def modifiers(self, node: Node) -> list[str]:
        mods = self.modifiers_node(node)
        if mods is None:
            return []
        return [
            self.text(c)
            for c in mods.children
            if not c.is_named or c.type not in ("annotation", "marker_annotation")
        ]

    def annotations(self, node: Node) -> list[str]:
        mods = self.modifiers_node(node)
        if mods is None:
            return []
        return [
            self.text(c)
            for c in mods.named_children
            if c.type in ("annotation", "marker_annotation")
        ]

    def header_without_annotations(self, node: Node, body: Node | None) -> str:
        start = node.start_byte
        mods = self.modifiers_node(node)
        if mods is not None:
            annotations = [
                c for c in mods.named_children if c.type in ("annotation", "marker_annotation")
            ]
            if annotations:
                keywords = [
                    c
                    for c in mods.children
                    if c not in annotations and c.start_byte > annotations[-1].end_byte
                ]
                start = keywords[0].start_byte if keywords else mods.end_byte
        end = body.start_byte if body is not None else node.end_byte
        return collapse(self.src[start:end].decode("utf-8", errors="replace").strip())

    @staticmethod
    def visibility(modifiers: list[str]) -> str | None:
        found = [m for m in modifiers if m in _VISIBILITY]
        return found[0] if found else None

    def type_declaration(self, node: Node, ctx: _Ctx) -> None:
        kind = _TYPE_DECLARATIONS[node.type]
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        modifiers = self.modifiers(node)
        annotations = self.annotations(node)
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header_without_annotations(node, body),
            visibility=self.visibility(modifiers),
            modifiers=modifiers,
            decorators=annotations,
            doc=normalize_javadoc(self.comments_before(node)),
        )
        self.annotation_refs(node, ctx, index)
        type_ctx = _Ctx(
            symbol=index, prefix=self.qualified(index), cls=index, strings=dict(ctx.strings)
        )
        type_ctx.route_prefix = self.class_route(" ".join(annotations))

        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            for base in superclass.named_children:
                self.add_ref(base, "inherits", strip_generics(self.text(base)), index)
        for child in node.named_children:
            if child.type not in ("super_interfaces", "extends_interfaces"):
                continue
            edge = "inherits" if kind == "interface" else "implements"
            for type_list in child.named_children:
                for base in type_list.named_children:
                    self.add_ref(base, edge, strip_generics(self.text(base)), index)
        params = node.child_by_field_name("parameters")  # record components
        for param in params.named_children if params is not None else ():
            type_node = param.child_by_field_name("type")
            param_name = self.text(param.child_by_field_name("name"))
            if type_node is not None and param_name:
                type_ctx.fields[param_name] = strip_generics(self.text(type_node))
                self.add_symbol(
                    param,
                    "property",
                    param_name,
                    index,
                    qualified_prefix=type_ctx.prefix,
                    signature=collapse(self.text(param)),
                    visibility="public",
                    type_name=value_type(self.text(type_node)),
                )
        if body is None:
            return
        type_ctx.fields.update(self.collect_fields(body))
        type_ctx.strings.update(self.collect_strings(body))
        for member in body.named_children:
            self.member(member, type_ctx, interface=kind == "interface")

    def class_route(self, annotations: str) -> str | None:
        mapping = re.search(r"@RequestMapping\b\s*\(([^)]*)\)", annotations)
        if mapping:
            path = _FIRST_STRING.search(mapping.group(1))
            return path.group(1) if path else None
        jaxrs = _JAXRS_PATH.search(annotations)
        return jaxrs.group(1) if jaxrs else None

    def collect_fields(self, body: Node) -> dict[str, str]:
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type not in ("field_declaration", "constant_declaration"):
                continue
            type_name = strip_generics(self.text(member.child_by_field_name("type")))
            for declarator in member.children_by_field_name("declarator"):
                fields[self.text(declarator.child_by_field_name("name"))] = type_name
        return fields

    def collect_strings(self, body: Node) -> dict[str, str]:
        values: dict[str, str] = {}
        for member in body.named_children:
            if member.type not in ("field_declaration", "constant_declaration"):
                continue
            for declarator in member.children_by_field_name("declarator"):
                value = self.string_value(declarator.child_by_field_name("value"), _Ctx())
                if value is not None:
                    values[self.text(declarator.child_by_field_name("name"))] = value
        return values

    def member(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        if node.type in ("field_declaration", "constant_declaration"):
            self.field(node, ctx, interface=interface)
            return
        if node.type == "enum_body_declarations":
            for child in node.named_children:
                self.member(child, ctx, interface=interface)
            return
        if node.type == "enum_constant":
            self.add_symbol(
                node,
                "const",
                self.text(node.child_by_field_name("name")),
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=collapse(self.text(node), 120),
                visibility="public",
            )
            return
        kind = {
            "method_declaration": "method",
            "constructor_declaration": "constructor",
            "compact_constructor_declaration": "constructor",
        }.get(node.type)
        if kind is None:
            self.visit(node, ctx)
            return
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        modifiers = self.modifiers(node)
        annotations = self.annotations(node)
        declared = node.child_by_field_name("type")
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header_without_annotations(node, body),
            visibility=self.visibility(modifiers) or ("public" if interface else None),
            modifiers=modifiers,
            decorators=annotations,
            doc=normalize_javadoc(self.comments_before(node)),
            type_name=value_type(self.text(declared)) if declared is not None else None,
        )
        self.annotation_refs(node, ctx, index)
        self.mapping_endpoints(node, index, " ".join(annotations), ctx)
        member_ctx = replace(ctx, symbol=index, prefix=self.qualified(index), locals={})
        params = node.child_by_field_name("parameters")
        for param in params.named_children if params is not None else ():
            type_node = param.child_by_field_name("type")
            param_name = param.child_by_field_name("name")
            if type_node is not None and param_name is not None:
                member_ctx.locals[self.text(param_name)] = strip_generics(self.text(type_node))
                self.type_refs(type_node, member_ctx)
        if declared is not None:
            self.type_refs(declared, member_ctx)
        if body is not None:
            self.visit_children(body, member_ctx)

    def field(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        modifiers = self.modifiers(node)
        type_node = node.child_by_field_name("type")
        is_constant = interface or ("static" in modifiers and "final" in modifiers)
        declarators = node.children_by_field_name("declarator")
        for declarator in declarators:
            index = self.add_symbol(
                declarator if len(declarators) > 1 else node,
                "const" if is_constant else "field",
                self.text(declarator.child_by_field_name("name")),
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=collapse(self.text(node), 200),
                visibility=self.visibility(modifiers) or ("public" if interface else None),
                modifiers=modifiers,
                decorators=self.annotations(node),
                doc=normalize_javadoc(self.comments_before(node)),
                type_name=value_type(self.text(type_node)) if type_node is not None else None,
            )
            field_ctx = replace(ctx, symbol=index)
            if type_node is not None:
                self.type_refs(type_node, field_ctx)
            value = declarator.child_by_field_name("value")
            if value is not None:
                self.visit(value, field_ctx)

    def annotation_refs(self, node: Node, ctx: _Ctx, index: int) -> None:
        mods = self.modifiers_node(node)
        if mods is None:
            return
        for annotation in mods.named_children:
            if annotation.type in ("annotation", "marker_annotation"):
                name = self.text(annotation.child_by_field_name("name")).rsplit(".", 1)[-1]
                self.add_ref(annotation, "type_ref", name, index)

    # --- statements & expressions -------------------------------------------------

    def on_local_variable_declaration(self, node: Node, ctx: _Ctx) -> None:
        type_node = node.child_by_field_name("type")
        declared = None
        if type_node is not None and self.text(type_node) != "var":
            declared = strip_generics(self.text(type_node))
        for declarator in node.children_by_field_name("declarator"):
            name = self.text(declarator.child_by_field_name("name"))
            value = declarator.child_by_field_name("value")
            type_name = declared
            if (
                type_name is None
                and value is not None
                and value.type == "object_creation_expression"
            ):
                type_name = strip_generics(self.text(value.child_by_field_name("type")))
            if name and type_name:
                ctx.locals[name] = type_name
            string = self.string_value(value, ctx)
            if name and string is not None:
                ctx.strings[name] = string
        self.visit_children(node, ctx)

    def on_lambda_expression(self, node: Node, ctx: _Ctx) -> None:
        self.visit_children(node, replace(ctx, locals=dict(ctx.locals)))

    def on_method_invocation(self, node: Node, ctx: _Ctx) -> None:
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        if name_node is not None:
            name = self.text(name_node)
            if obj is None:
                self.add_ref(name_node, "call", name, ctx.symbol)
            else:
                qualifier = collapse(self.text(obj), 80)
                if obj.type == "super":
                    qualifier = "super"
                self.add_ref(
                    name_node,
                    "call",
                    name,
                    ctx.symbol,
                    qualifier=qualifier,
                    qualifier_type=self.chain_root_type(obj, ctx),
                )
                self.client_endpoint(node, name, obj, ctx)
        self.visit_children(node, ctx)

    def on_object_creation_expression(self, node: Node, ctx: _Ctx) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            self.add_ref(type_node, "instantiate", strip_generics(self.text(type_node)), ctx.symbol)
        for index, child in enumerate(node.children):
            if child.is_named and node.field_name_for_child(index) != "type":
                self.visit(child, ctx)

    def chain_root_type(self, expression: Node, ctx: _Ctx) -> str | None:
        segments: list[str] = []
        current = expression
        while current.type == "field_access":
            segments.append(self.text(current.child_by_field_name("field")))
            current = current.child_by_field_name("object")
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
            if current.type == "type_identifier":
                name = self.text(current)
                if len(name) > 1 and name[:1].isupper():
                    key = (ctx.symbol, name)
                    if key not in self._seen_type_refs:
                        self._seen_type_refs.add(key)
                        self.add_ref(current, "type_ref", name, ctx.symbol)
            elif current.type == "scoped_type_identifier":
                stack.append(current.named_children[-1])
            else:
                stack.extend(current.named_children)

    # --- HTTP endpoints -------------------------------------------------------------

    def mapping_endpoints(self, node: Node, index: int, annotations: str, ctx: _Ctx) -> None:
        for verb, arguments in _SPRING_MAPPING.findall(annotations):
            path_match = _FIRST_STRING.search(arguments or "")
            path = path_match.group(1) if path_match else ""
            methods: list[str | None]
            if verb == "Request":
                methods = list(_REQUEST_METHOD.findall(arguments or "")) or [None]
            else:
                methods = [verb.upper()]
            for method in methods:
                route = normalize_route(join_routes(ctx.route_prefix, path))
                self.out.endpoints.append(Endpoint("server", method, route, index, self.line(node)))
        jaxrs = _JAXRS_VERB.search(annotations)
        if jaxrs:
            path = _JAXRS_PATH.search(annotations)
            route = normalize_route(join_routes(ctx.route_prefix, path.group(1) if path else ""))
            self.out.endpoints.append(
                Endpoint("server", jaxrs.group(1), route, index, self.line(node))
            )

    def client_endpoint(self, node: Node, name: str, obj: Node, ctx: _Ctx) -> None:
        method = _CLIENT_METHODS.get(name)
        receiver_type = self.chain_root_type(obj, ctx) or ""
        if method is None or ctx.symbol is None:
            return
        if "RestTemplate" not in receiver_type and "rest" not in self.text(obj).lower():
            return
        arguments = node.child_by_field_name("arguments")
        args = arguments.named_children if arguments else []
        url = self.string_value(args[0], ctx) if args else None
        if is_url_path(url):
            self.out.endpoints.append(
                Endpoint("client", method, normalize_route(url), ctx.symbol, self.line(node))
            )

    def string_value(self, node: Node | None, ctx: _Ctx) -> str | None:
        if node is None:
            return None
        if node.type == "string_literal":
            return "".join(self.text(c) for c in node.named_children if c.type == "string_fragment")
        if node.type == "identifier":
            return ctx.strings.get(self.text(node))
        if (
            node.type == "binary_expression"
            and self.text(node.child_by_field_name("operator")) == "+"
        ):
            left = self.string_value(node.child_by_field_name("left"), ctx)
            right = self.string_value(node.child_by_field_name("right"), ctx)
            if left is None and right is None:
                return None
            return (left if left is not None else "") + (right if right is not None else "{}")
        return None
