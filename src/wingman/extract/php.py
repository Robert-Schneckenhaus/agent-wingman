"""PHP extractor.

Namespaces use dots in qualified names (``App\\Models\\Order`` -> ``App.Models.Order``)
like the other languages. ``$this->a->b()`` becomes the qualifier ``this.a``,
``parent::`` -> ``super``, ``self::``/``static::`` -> ``this``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.endpoints import is_url_path, join_routes, normalize_route
from wingman.extract.base import ExtractorBase, collapse, strip_generics
from wingman.extract.java import normalize_javadoc
from wingman.extract.model import Endpoint, Extraction, Import

_TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "class",
    "enum_declaration": "enum",
}
_VISIBILITY = ("public", "private", "protected")
_HTTP_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
_SYMFONY_ROUTE = re.compile(r"\bRoute\s*\(\s*(?:path\s*:\s*)?['\"]([^'\"]*)['\"]([^)]*)\)")
_SYMFONY_METHODS = re.compile(r"methods\s*:\s*\[([^\]]*)\]")
_PRIMITIVES = frozenset(
    {"int", "float", "string", "bool", "array", "void", "mixed", "null", "self", "static"}
)


def php_name(text: str) -> str:
    return text.strip().lstrip("\\").replace("\\", ".")


@dataclass
class _Ctx:
    symbol: int | None = None
    prefix: str | None = None
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    locals: dict[str, str] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)
    route_prefix: str | None = None


class PhpExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        self._namespace: str | None = None
        self.visit_children(root, _Ctx())
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for child in node.named_children:
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

    # --- namespaces & imports -----------------------------------------------------

    def on_namespace_definition(self, node: Node, ctx: _Ctx) -> None:
        name = php_name(self.text(node.child_by_field_name("name")))
        self.out.namespaces.append(name)
        body = node.child_by_field_name("body")
        if body is None:  # "namespace X;" applies to the rest of the file
            self._namespace = name
            ctx.prefix = name
        else:
            self.visit_children(body, _Ctx(prefix=name))

    def on_namespace_use_declaration(self, node: Node, ctx: _Ctx) -> None:
        line = self.line(node)
        is_function = any(
            self.text(c) == "function"
            for part in (node, *node.named_children)
            for c in part.children
            if not c.is_named
        )
        group_prefix = next(
            (php_name(self.text(c)) for c in node.named_children if c.type == "namespace_name"),
            None,
        )
        body = node.child_by_field_name("body")
        clauses = body.named_children if body is not None else node.named_children
        for clause in clauses:
            if clause.type != "namespace_use_clause":
                continue
            target = next(
                (c for c in clause.named_children if c.type in ("qualified_name", "name")), None
            )
            if target is None:
                continue
            module = php_name(self.text(target))
            if group_prefix:
                module = f"{group_prefix}.{module}"
            alias_node = clause.child_by_field_name("alias")
            alias = self.text(alias_node) if alias_node is not None else module.rsplit(".", 1)[-1]
            kind = "using_function" if is_function else "using"
            self.out.imports.append(Import(module, line, alias=alias, kind=kind))

    # --- declarations -----------------------------------------------------------

    def modifiers(self, node: Node) -> list[str]:
        return [
            self.text(c)
            for c in node.children
            if c.type
            in (
                "visibility_modifier",
                "static_modifier",
                "abstract_modifier",
                "final_modifier",
                "readonly_modifier",
            )
        ]

    @staticmethod
    def visibility(modifiers: list[str]) -> str | None:
        found = [m for m in modifiers if m in _VISIBILITY]
        return found[0] if found else None

    def attributes(self, node: Node) -> list[str]:
        return [self.text(c) for c in node.children if c.type == "attribute_list"]

    def header(self, node: Node, body: Node | None) -> str:
        start = node.start_byte
        for child in node.children:
            if child.type in ("attribute_list", "comment"):
                start = child.end_byte
            else:
                break
        end = body.start_byte if body is not None else node.end_byte
        return collapse(self.src[start:end].decode("utf-8", errors="replace").strip())

    def type_declaration(self, node: Node, ctx: _Ctx) -> None:
        kind = _TYPE_DECLARATIONS[node.type]
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        modifiers = self.modifiers(node)
        if node.type == "trait_declaration":
            modifiers.append("trait")
        attributes = self.attributes(node)
        index = self.add_symbol(
            node,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            modifiers=modifiers,
            decorators=attributes,
            doc=normalize_javadoc(self.preceding_comments(node)),
        )
        type_ctx = _Ctx(symbol=index, prefix=self.qualified(index), cls=index)
        route = _SYMFONY_ROUTE.search(" ".join(attributes))
        type_ctx.route_prefix = route.group(1) if route else None
        for child in node.named_children:
            if child.type == "base_clause":
                for base in child.named_children:
                    edge = "inherits"
                    self.add_ref(base, edge, php_name(self.text(base)).rsplit(".", 1)[-1], index)
            elif child.type == "class_interface_clause":
                for base in child.named_children:
                    target = php_name(self.text(base)).rsplit(".", 1)[-1]
                    self.add_ref(base, "implements", target, index)
        if body is None:
            return
        type_ctx.fields.update(self.collect_fields(body))
        for member in body.named_children:
            self.member(member, type_ctx, interface=kind == "interface")

    def collect_fields(self, body: Node) -> dict[str, str]:
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type == "property_declaration":
                type_name = self.type_name(member.child_by_field_name("type"))
                for element in member.named_children:
                    if element.type == "property_element" and type_name:
                        fields[self.variable(element.child_by_field_name("name"))] = type_name
            elif (
                member.type == "method_declaration"
                and self.text(member.child_by_field_name("name")) == "__construct"
            ):
                params = member.child_by_field_name("parameters")
                for param in params.named_children if params is not None else ():
                    if param.type == "property_promotion_parameter":
                        type_name = self.type_name(param.child_by_field_name("type"))
                        if type_name:
                            fields[self.variable(param.child_by_field_name("name"))] = type_name
                body_node = member.child_by_field_name("body")
                params_types = self.parameter_types(params)
                for assignment in self.descendants(body_node, "assignment_expression"):
                    left = assignment.child_by_field_name("left")
                    right = assignment.child_by_field_name("right")
                    if left is None or left.type != "member_access_expression":
                        continue
                    if self.text(left.child_by_field_name("object")) != "$this":
                        continue
                    attr = self.text(left.child_by_field_name("name"))
                    type_name = None
                    if right is not None and right.type == "variable_name":
                        type_name = params_types.get(self.variable(right))
                    elif right is not None and right.type == "object_creation_expression":
                        type_name = self.created_type(right)
                    if attr and type_name and attr not in fields:
                        fields[attr] = type_name
        return fields

    def member(self, node: Node, ctx: _Ctx, *, interface: bool) -> None:
        if node.type == "use_declaration":  # trait use: members come from the trait
            for trait in node.named_children:
                if trait.type in ("name", "qualified_name"):
                    target = php_name(self.text(trait)).rsplit(".", 1)[-1]
                    self.add_ref(trait, "inherits", target, ctx.symbol)
            return
        if node.type == "const_declaration":
            for element in node.named_children:
                if element.type != "const_element":
                    continue
                name_node = next((c for c in element.named_children if c.type == "name"), None)
                self.add_symbol(
                    element,
                    "const",
                    self.text(name_node),
                    ctx.symbol,
                    qualified_prefix=ctx.prefix,
                    signature=collapse(self.text(node), 160),
                    visibility=self.visibility(self.modifiers(node)) or "public",
                )
                value = next((c for c in element.named_children if c.type != "name"), None)
                string = self.string_value(value, ctx)
                if string is not None:
                    ctx.strings[self.text(name_node)] = string
            return
        if node.type == "enum_case":
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
        if node.type == "property_declaration":
            modifiers = self.modifiers(node)
            type_name = self.type_name(node.child_by_field_name("type"))
            for element in node.named_children:
                if element.type != "property_element":
                    continue
                self.add_symbol(
                    element,
                    "property",
                    self.variable(element.child_by_field_name("name")),
                    ctx.symbol,
                    qualified_prefix=ctx.prefix,
                    signature=collapse(self.text(node), 160),
                    visibility=self.visibility(modifiers),
                    modifiers=modifiers,
                    doc=normalize_javadoc(self.preceding_comments(node)),
                    type_name=type_name,
                )
            return
        if node.type != "method_declaration":
            self.visit(node, ctx)
            return
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        modifiers = self.modifiers(node)
        attributes = self.attributes(node)
        index = self.add_symbol(
            node,
            "constructor" if name == "__construct" else "method",
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            visibility=self.visibility(modifiers) or "public",
            modifiers=modifiers,
            decorators=attributes,
            doc=normalize_javadoc(self.preceding_comments(node)),
            type_name=self.type_name(node.child_by_field_name("return_type")),
        )
        self.attribute_routes(" ".join(attributes), index, node, ctx)
        self.function_body(node, ctx, index)

    def on_function_definition(self, node: Node, ctx: _Ctx) -> None:
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        index = self.add_symbol(
            node,
            "function",
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body),
            doc=normalize_javadoc(self.preceding_comments(node)),
            type_name=self.type_name(node.child_by_field_name("return_type")),
        )
        self.function_body(node, ctx, index)

    def function_body(self, node: Node, ctx: _Ctx, index: int) -> None:
        params = node.child_by_field_name("parameters")
        fn_ctx = replace(
            ctx,
            symbol=index,
            prefix=self.qualified(index),
            locals=self.parameter_types(params),
            strings=dict(ctx.strings),
        )
        for param in params.named_children if params is not None else ():
            self.type_refs(param.child_by_field_name("type"), fn_ctx)
        self.type_refs(node.child_by_field_name("return_type"), fn_ctx)
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit_children(body, fn_ctx)

    def parameter_types(self, params: Node | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for param in params.named_children if params is not None else ():
            type_name = self.type_name(param.child_by_field_name("type"))
            name = param.child_by_field_name("name")
            if type_name and name is not None:
                result[self.variable(name)] = type_name
        return result

    def type_name(self, node: Node | None) -> str | None:
        if node is None:
            return None
        text = self.text(node).lstrip("?")
        if "|" in text or "&" in text:
            return None
        name = php_name(strip_generics(text)).rsplit(".", 1)[-1]
        return name if name and name.lower() not in _PRIMITIVES else None

    def variable(self, node: Node | None) -> str:
        return self.text(node).lstrip("$")

    def created_type(self, node: Node) -> str | None:
        target = next(
            (c for c in node.named_children if c.type in ("name", "qualified_name")), None
        )
        return php_name(self.text(target)).rsplit(".", 1)[-1] if target is not None else None

    def descendants(self, node: Node | None, node_type: str):
        stack = [node] if node is not None else []
        while stack:
            current = stack.pop()
            if current.type == node_type:
                yield current
            stack.extend(reversed(current.named_children))

    def type_refs(self, node: Node | None, ctx: _Ctx) -> None:
        if node is None:
            return
        for current in self.descendants(node, "named_type"):
            name = php_name(self.text(current)).rsplit(".", 1)[-1]
            if name.lower() in _PRIMITIVES or len(name) < 2:
                continue
            key = (ctx.symbol, name)
            if key not in self._seen_type_refs:
                self._seen_type_refs.add(key)
                self.add_ref(current, "type_ref", name, ctx.symbol)

    # --- statements & expressions -------------------------------------------------

    def on_assignment_expression(self, node: Node, ctx: _Ctx) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None and left.type == "variable_name" and right is not None:
            name = self.variable(left)
            if right.type == "object_creation_expression":
                type_name = self.created_type(right)
                if type_name:
                    ctx.locals[name] = type_name
            string = self.string_value(right, ctx)
            if string is not None:
                ctx.strings[name] = string
        self.visit_children(node, ctx)

    def on_object_creation_expression(self, node: Node, ctx: _Ctx) -> None:
        type_name = self.created_type(node)
        if type_name:
            self.add_ref(node, "instantiate", type_name, ctx.symbol)
        self.visit_children(node, ctx)

    def on_function_call_expression(self, node: Node, ctx: _Ctx) -> None:
        function = node.child_by_field_name("function")
        if function is not None and function.type in ("name", "qualified_name"):
            self.add_ref(function, "call", php_name(self.text(function)), ctx.symbol)
        self.visit_children(node, ctx)

    def on_member_call_expression(self, node: Node, ctx: _Ctx) -> None:
        obj = node.child_by_field_name("object")
        name = self.text(node.child_by_field_name("name"))
        if obj is not None and name:
            qualifier, root_type = self.receiver(obj, ctx)
            self.add_ref(
                node.child_by_field_name("name"),
                "call",
                name,
                ctx.symbol,
                qualifier=qualifier,
                qualifier_type=root_type,
            )
            self.client_endpoint(node, name, qualifier, ctx)
        self.visit_children(node, ctx)

    on_nullsafe_member_call_expression = on_member_call_expression

    def on_scoped_call_expression(self, node: Node, ctx: _Ctx) -> None:
        scope = node.child_by_field_name("scope")
        name = self.text(node.child_by_field_name("name"))
        if scope is not None and name:
            scope_text = self.text(scope)
            qualifier = {"parent": "super", "self": "this", "static": "this"}.get(
                scope_text, php_name(scope_text).rsplit(".", 1)[-1]
            )
            self.add_ref(
                node.child_by_field_name("name"), "call", name, ctx.symbol, qualifier=qualifier
            )
            self.laravel_route(node, qualifier, name, ctx)
            self.client_endpoint(node, name, qualifier, ctx)
        self.visit_children(node, ctx)

    def receiver(self, obj: Node, ctx: _Ctx) -> tuple[str, str | None]:
        """``$this->repo`` -> (``this.repo``, type of repo); ``$order`` -> (``order``, type)."""
        segments: list[str] = []
        current = obj
        while current.type in ("member_access_expression", "nullsafe_member_access_expression"):
            segments.append(self.text(current.child_by_field_name("name")))
            current = current.child_by_field_name("object")
            if current is None:
                return collapse(self.text(obj), 80), None
        if current.type != "variable_name":
            return collapse(self.text(obj), 80), None
        root = self.variable(current)
        segments.reverse()
        if root == "this":
            qualifier = ".".join(["this", *segments])
            return qualifier, ctx.fields.get(segments[0]) if segments else None
        return ".".join([root, *segments]), ctx.locals.get(root)

    # --- HTTP endpoints -------------------------------------------------------------

    def attribute_routes(self, attributes: str, index: int, node: Node, ctx: _Ctx) -> None:
        """Symfony: #[Route('/x', methods: ['POST'])] on controller methods."""
        for path, rest in _SYMFONY_ROUTE.findall(attributes):
            methods_match = _SYMFONY_METHODS.search(rest)
            methods = (
                [m.strip(" '\"").upper() for m in methods_match.group(1).split(",") if m.strip()]
                if methods_match
                else [None]
            )
            route = normalize_route(join_routes(ctx.route_prefix, path))
            for method in methods:
                self.out.endpoints.append(Endpoint("server", method, route, index, self.line(node)))

    def laravel_route(self, node: Node, scope: str, name: str, ctx: _Ctx) -> None:
        """``Route::post('/x', [OrderController::class, 'cancel'])``: a ``route`` symbol
        that serves the endpoint and calls the controller action."""
        verb = _HTTP_VERBS.get(name)
        if scope != "Route" or verb is None:
            return
        args = self.arguments(node)
        path = self.string_value(args[0], ctx) if args else None
        if path is None:
            return
        route = normalize_route(path)
        index = self.add_symbol(
            node,
            "route",
            f"{verb} /{route}",
            ctx.symbol,
            qualified_prefix=None,
            signature=collapse(self.text(node), 200),
        )
        self.out.endpoints.append(Endpoint("server", verb, route, index, self.line(node)))
        handler = args[1] if len(args) > 1 else None
        if handler is not None and handler.type == "class_constant_access_expression":
            # invokable controller: Route::get('/x', ShowController::class)
            controller = self.text(handler).removesuffix("::class")
            qualifier = php_name(controller).rsplit(".", 1)[-1]
            self.add_ref(handler, "call", "__invoke", index, qualifier=qualifier)
        elif handler is not None and handler.type in ("string", "encapsed_string"):
            # Laravel < 8: Route::get('/x', 'OrderController@show')
            target = self.string_value(handler, ctx) or ""
            controller, _, action = target.partition("@")
            if controller and action:
                qualifier = php_name(controller).rsplit(".", 1)[-1]
                self.add_ref(handler, "call", action, index, qualifier=qualifier)
        if handler is not None and handler.type == "array_creation_expression":
            parts = [e.named_children[0] for e in handler.named_children if e.named_children]
            if len(parts) == 2:
                controller = self.text(parts[0]).removesuffix("::class")
                action = self.string_value(parts[1], ctx)
                if action:
                    self.add_ref(
                        parts[1],
                        "call",
                        action,
                        index,
                        qualifier=php_name(controller).rsplit(".", 1)[-1],
                    )

    def client_endpoint(self, node: Node, name: str, qualifier: str, ctx: _Ctx) -> None:
        """Laravel ``Http::get(url)`` and Guzzle-style ``$client->post(url)``."""
        verb = _HTTP_VERBS.get(name)
        if verb is None or ctx.symbol is None:
            return
        last = qualifier.rsplit(".", 1)[-1].lower()
        if not (last == "http" or "client" in last or "http" in last):
            return
        args = self.arguments(node)
        url = self.string_value(args[0], ctx) if args else None
        if is_url_path(url):
            self.out.endpoints.append(
                Endpoint("client", verb, normalize_route(url), ctx.symbol, self.line(node))
            )

    def arguments(self, node: Node) -> list[Node]:
        arguments = node.child_by_field_name("arguments")
        result = []
        for arg in arguments.named_children if arguments is not None else ():
            if arg.type == "argument" and arg.named_children:
                result.append(arg.named_children[-1])
        return result

    def string_value(self, node: Node | None, ctx: _Ctx) -> str | None:
        if node is None:
            return None
        if node.type in ("string", "encapsed_string"):
            parts = []
            for child in node.named_children:
                if child.type == "string_content":
                    parts.append(self.text(child))
                elif child.type in ("variable_name", "member_access_expression"):
                    parts.append("{}")
            return "".join(parts)
        if node.type == "variable_name":
            return ctx.strings.get(self.variable(node))
        if node.type == "class_constant_access_expression":
            return ctx.strings.get(self.text(node.named_children[-1]))
        if (
            node.type == "binary_expression"
            and self.text(node.child_by_field_name("operator")) == "."
        ):
            left = self.string_value(node.child_by_field_name("left"), ctx)
            right = self.string_value(node.child_by_field_name("right"), ctx)
            if left is None and right is None:
                return None
            return (left if left is not None else "") + (right if right is not None else "{}")
        return None
