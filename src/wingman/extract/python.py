"""Python extractor."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from tree_sitter import Node

from wingman.endpoints import is_url_path, join_routes, normalize_route
from wingman.extract.base import ExtractorBase, collapse, strip_generics
from wingman.extract.model import Endpoint, Extraction, Import

_HTTP_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
# FastAPI/Flask/Starlette route decorators: @app.get("/x"), @router.post(...), @bp.route(...)
_ROUTE_RECEIVERS = re.compile(r"(app|router|api|bp|blueprint|routes)$", re.IGNORECASE)
_CLIENT_RECEIVERS = {"requests", "httpx", "session", "client", "http", "aiohttp"}
_WRAPPERS = re.compile(r"^(?:typing\.)?(?:Optional|Awaitable|Coroutine|Annotated|Final)\[(.*)\]$")
_BUILTINS = frozenset(
    {
        "print",
        "len",
        "range",
        "isinstance",
        "str",
        "int",
        "float",
        "bool",
        "dict",
        "list",
        "set",
        "tuple",
        "super",
        "open",
        "sorted",
        "enumerate",
        "zip",
        "getattr",
        "setattr",
        "hasattr",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "map",
        "filter",
        "repr",
        "type",
        "iter",
        "next",
        "round",
        "abs",
    }
)


def python_type(annotation: str | None) -> str | None:
    """``Optional[Order]`` -> ``Order``; ``list[Order]``/``str``/unions -> None."""
    if not annotation:
        return None
    text = annotation.strip().strip("'\"")
    wrapped = _WRAPPERS.match(text)
    if wrapped:
        return python_type(wrapped.group(1).split(",")[0])
    text = re.sub(r"\s*\|\s*None$", "", text)
    if not text or "|" in text or "[" in text or text[0] in "({":
        return None
    name = strip_generics(text)
    return name if name and name[0].isupper() and name not in ("None", "Any") else None


def normalize_docstring(raw: str) -> str | None:
    text = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        prefix = re.match(r"^[rRuUbBfF]*", text).group(0)
        body = text[len(prefix) :]
        if body.startswith(quote) and body.endswith(quote) and len(body) >= 2 * len(quote):
            text = body[len(quote) : -len(quote)]
            break
    lines = text.strip().splitlines()
    if not lines:
        return None
    indent = min((len(line) - len(line.lstrip()) for line in lines[1:] if line.strip()), default=0)
    cleaned = [lines[0].strip()] + [line[indent:].rstrip() for line in lines[1:]]
    return "\n".join(cleaned).strip() or None


@dataclass
class _Ctx:
    symbol: int | None = None
    prefix: str | None = None
    cls: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    """Enclosing class: attribute name -> type."""
    locals: dict[str, str] = field(default_factory=dict)
    in_function: bool = False
    route_prefix: str | None = None


class PythonExtractor(ExtractorBase):
    def extract(self, root: Node) -> Extraction:
        self._seen_type_refs: set[tuple[int | None, str]] = set()
        self._strings: dict[str, str] = {}
        self._prefixes: dict[str, str] = {}  # router = APIRouter(prefix="/api/orders")
        self._module_types: dict[str, str] = {}  # service = OrderService(...)
        self.visit_children(root, _Ctx())
        return self.out

    # --- traversal --------------------------------------------------------------

    def visit_children(self, node: Node, ctx: _Ctx) -> None:
        for child in node.named_children:
            self.visit(child, ctx)

    def visit(self, node: Node, ctx: _Ctx) -> None:
        handler = getattr(self, f"on_{node.type}", None)
        if handler is not None:
            handler(node, ctx)
        else:
            self.visit_children(node, ctx)

    # --- imports ----------------------------------------------------------------

    def on_import_statement(self, node: Node, ctx: _Ctx) -> None:
        line = self.line(node)
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                module = self.text(child.child_by_field_name("name"))
                alias = self.text(child.child_by_field_name("alias"))
            else:
                module, alias = self.text(child), None
            # "import a.b" binds "a.b" (used as a.b.f()); "import a.b as c" binds "c"
            self.out.imports.append(Import(module, line, "*", alias or module))

    def on_import_from_statement(self, node: Node, ctx: _Ctx) -> None:
        line = self.line(node)
        module = self.text(node.child_by_field_name("module_name"))
        if any(c.type == "wildcard_import" for c in node.named_children):
            self.out.imports.append(Import(module, line, "*", None, "reexport"))
            return
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                name = self.text(child.child_by_field_name("name"))
                alias = self.text(child.child_by_field_name("alias"))
                self.out.imports.append(Import(module, line, name, alias))
            else:
                self.out.imports.append(Import(module, line, self.text(child)))

    # --- declarations -----------------------------------------------------------

    def on_decorated_definition(self, node: Node, ctx: _Ctx) -> None:
        decorators = [c for c in node.named_children if c.type == "decorator"]
        definition = node.child_by_field_name("definition")
        if definition is None:
            return
        if definition.type == "class_definition":
            self.class_definition(definition, ctx, decorators, outer=node)
        elif definition.type == "function_definition":
            self.function_definition(definition, ctx, decorators, outer=node)

    def on_class_definition(self, node: Node, ctx: _Ctx) -> None:
        self.class_definition(node, ctx, [], outer=node)

    def on_function_definition(self, node: Node, ctx: _Ctx) -> None:
        self.function_definition(node, ctx, [], outer=node)

    def docstring(self, body: Node | None) -> str | None:
        if body is None or not body.named_children:
            return None
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_children:
            expr = first.named_children[0]
            if expr.type == "string":
                return normalize_docstring(self.text(expr))
        return None

    def class_definition(self, node: Node, ctx: _Ctx, decorators: list[Node], *, outer: Node):
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        index = self.add_symbol(
            outer,
            "class",
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body).rstrip(":"),
            visibility="private" if name.startswith("_") else None,
            decorators=[self.text(d) for d in decorators],
            doc=self.docstring(body),
        )
        self.decorator_refs(decorators, ctx, index)
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for base in superclasses.named_children:
                if base.type in ("identifier", "attribute"):
                    base_name = self.text(base).rsplit(".", 1)[-1]
                    if base_name not in ("object",):
                        self.add_ref(base, "inherits", base_name, index)
        fields = self.collect_fields(body)
        class_ctx = replace(
            ctx,
            symbol=index,
            prefix=self.qualified(index),
            cls=index,
            fields=fields,
            locals={},
            in_function=False,
        )
        if body is not None:
            for statement in body.named_children:
                if statement.type == "expression_statement":
                    self.class_attribute(statement, class_ctx)
                else:
                    self.visit(statement, class_ctx)

    def collect_fields(self, body: Node | None) -> dict[str, str]:
        """Attribute types from class-level annotations and ``self.x = ...`` in methods."""
        fields: dict[str, str] = {}
        if body is None:
            return fields
        for statement in body.named_children:
            for assignment in statement.named_children:
                if assignment.type == "assignment":
                    left = assignment.child_by_field_name("left")
                    type_name = python_type(self.text(assignment.child_by_field_name("type")))
                    if left is not None and left.type == "identifier" and type_name:
                        fields[self.text(left)] = type_name
            function = statement
            if statement.type == "decorated_definition":
                function = statement.child_by_field_name("definition")
            if function is None or function.type != "function_definition":
                continue
            params = self.parameter_types(function.child_by_field_name("parameters"))
            for assignment in self.descendants(function.child_by_field_name("body"), "assignment"):
                left = assignment.child_by_field_name("left")
                if left is None or left.type != "attribute":
                    continue
                if self.text(left.child_by_field_name("object")) != "self":
                    continue
                attr = self.text(left.child_by_field_name("attribute"))
                type_name = python_type(self.text(assignment.child_by_field_name("type")))
                type_name = type_name or self.value_type(
                    assignment.child_by_field_name("right"), params
                )
                if type_name and attr not in fields:
                    fields[attr] = type_name
        return fields

    def class_attribute(self, statement: Node, ctx: _Ctx) -> None:
        for assignment in statement.named_children:
            if assignment.type != "assignment":
                self.visit(assignment, ctx)
                continue
            left = assignment.child_by_field_name("left")
            if left is None or left.type != "identifier":
                self.visit(assignment, ctx)
                continue
            name = self.text(left)
            type_text = self.text(assignment.child_by_field_name("type"))
            index = self.add_symbol(
                assignment,
                "property",
                name,
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=collapse(self.text(assignment), 160),
                visibility="private" if name.startswith("_") else None,
                type_name=python_type(type_text)
                or self.value_type(assignment.child_by_field_name("right"), {}),
            )
            right = assignment.child_by_field_name("right")
            if right is not None:
                self.visit(right, replace(ctx, symbol=index))

    def function_definition(
        self, node: Node, ctx: _Ctx, decorators: list[Node], *, outer: Node
    ) -> None:
        name = self.text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        decorator_names = [self.text(d).lstrip("@").split("(", 1)[0] for d in decorators]
        in_class = ctx.cls is not None and ctx.symbol == ctx.cls
        if in_class:
            if name == "__init__":
                kind = "constructor"
            elif any(
                d in ("property", "cached_property") or d.endswith(".setter")
                for d in decorator_names
            ):
                kind = "property"
            else:
                kind = "method"
        else:
            kind = "function"
        modifiers = []
        if any(c.type == "async" for c in node.children):
            modifiers.append("async")
        for decorator, token in (
            ("staticmethod", "static"),
            ("classmethod", "classmethod"),
            ("abstractmethod", "abstract"),
            ("abc.abstractmethod", "abstract"),
        ):
            if decorator in decorator_names:
                modifiers.append(token)
        visibility = None
        if name.startswith("__") and not name.endswith("__"):
            visibility = "private"
        elif name.startswith("_") and not name.endswith("__"):
            visibility = "protected"
        index = self.add_symbol(
            outer,
            kind,
            name,
            ctx.symbol,
            qualified_prefix=ctx.prefix,
            signature=self.header(node, body).rstrip(":"),
            visibility=visibility,
            modifiers=modifiers,
            decorators=[self.text(d) for d in decorators],
            doc=self.docstring(body),
            type_name=python_type(self.text(node.child_by_field_name("return_type"))),
        )
        self.decorator_refs(decorators, ctx, index)
        self.route_decorators(decorators, index, outer)
        for annotation in (node.child_by_field_name("return_type"),):
            self.type_refs(annotation, index)
        params_node = node.child_by_field_name("parameters")
        locals_ = dict(ctx.locals)
        locals_.update(self.parameter_types(params_node))
        if params_node is not None:
            for param in params_node.named_children:
                self.type_refs(param.child_by_field_name("type"), index)
                default = param.child_by_field_name("value")
                if default is not None:
                    self.visit(default, replace(ctx, symbol=index))
        fn_ctx = replace(
            ctx, symbol=index, prefix=self.qualified(index), locals=locals_, in_function=True
        )
        if body is not None:
            self.visit_children(body, fn_ctx)

    def parameter_types(self, params: Node | None) -> dict[str, str]:
        result: dict[str, str] = {}
        if params is None:
            return result
        for param in params.named_children:
            if param.type in ("typed_parameter", "typed_default_parameter"):
                name_node = param.child_by_field_name("name")
                if name_node is None:
                    name_node = next(
                        (c for c in param.named_children if c.type == "identifier"), None
                    )
                type_name = python_type(self.text(param.child_by_field_name("type")))
                if name_node is not None and type_name:
                    result[self.text(name_node)] = type_name
        return result

    def value_type(self, value: Node | None, known: dict[str, str]) -> str | None:
        """``Foo(...)`` -> Foo; a parameter -> its annotation."""
        if value is None:
            return None
        if value.type == "call":
            function = value.child_by_field_name("function")
            name = self.text(function).rsplit(".", 1)[-1]
            return name if name[:1].isupper() else None
        if value.type == "identifier":
            return known.get(self.text(value))
        return None

    def descendants(self, node: Node | None, node_type: str):
        if node is None:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == node_type:
                yield current
            if current.type in ("function_definition", "class_definition") and current is not node:
                continue
            stack.extend(reversed(current.named_children))

    def decorator_refs(self, decorators: list[Node], ctx: _Ctx, index: int) -> None:
        for decorator in decorators:
            expr = decorator.named_children[0] if decorator.named_children else None
            if expr is None:
                continue
            target = expr.child_by_field_name("function") if expr.type == "call" else expr
            if (
                target is not None
                and target.type == "identifier"
                and self.text(target) not in ("property", "staticmethod", "classmethod")
            ):
                self.add_ref(target, "call", self.text(target), index)
            if expr.type == "call":
                arguments = expr.child_by_field_name("arguments")
                if arguments is not None:
                    self.visit(arguments, replace(ctx, symbol=index))

    def type_refs(self, annotation: Node | None, index: int) -> None:
        if annotation is None:
            return
        for name in re.findall(r"\b[A-Z]\w*", self.text(annotation)):
            if name in ("Optional", "List", "Dict", "Set", "Tuple", "Union", "Any", "Callable"):
                continue
            key = (index, name)
            if key not in self._seen_type_refs:
                self._seen_type_refs.add(key)
                self.add_ref(annotation, "type_ref", name, index)

    # --- module-level assignments -----------------------------------------------

    def on_expression_statement(self, node: Node, ctx: _Ctx) -> None:
        if ctx.in_function or ctx.cls is not None:
            self.visit_children(node, ctx)
            return
        for child in node.named_children:
            if child.type != "assignment":
                self.visit(child, ctx)
                continue
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if left is None or left.type != "identifier":
                self.visit(child, ctx)
                continue
            name = self.text(left)
            value = self.string_value(right)
            if value is not None:
                self._strings[name] = value
            module_type = self.value_type(right, {})
            if module_type:
                self._module_types[name] = module_type
            prefix = self.keyword_string(right, ("prefix", "url_prefix"))
            if prefix:
                self._prefixes[name] = prefix
            index = self.add_symbol(
                child,
                "const",
                name,
                ctx.symbol,
                qualified_prefix=ctx.prefix,
                signature=collapse(self.text(child), 160),
                type_name=python_type(self.text(child.child_by_field_name("type")))
                or self.value_type(right, {}),
            )
            if right is not None:
                self.visit(right, replace(ctx, symbol=index))

    def on_assignment(self, node: Node, ctx: _Ctx) -> None:
        if ctx.in_function:
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            type_name = python_type(self.text(node.child_by_field_name("type")))
            type_name = type_name or self.value_type(right, ctx.locals)
            if left is not None and left.type == "identifier" and type_name:
                ctx.locals[self.text(left)] = type_name
        self.visit_children(node, ctx)

    # --- calls ------------------------------------------------------------------

    def on_call(self, node: Node, ctx: _Ctx) -> None:
        function = node.child_by_field_name("function")
        if function is not None:
            if function.type == "identifier":
                name = self.text(function)
                if name not in _BUILTINS:
                    kind = "instantiate" if name[:1].isupper() else "call"
                    self.add_ref(function, kind, name, ctx.symbol)
            elif function.type == "attribute":
                self.attribute_call(function, ctx)
            self.client_endpoint(node, function, ctx)
        self.visit_children(node, ctx)

    def attribute_call(self, function: Node, ctx: _Ctx) -> None:
        obj = function.child_by_field_name("object")
        name = self.text(function.child_by_field_name("attribute"))
        if obj is None or not name:
            return
        if obj.type == "call" and self.text(obj.child_by_field_name("function")) == "super":
            self.add_ref(function, "call", name, ctx.symbol, qualifier="super")
            return
        qualifier = self.text(obj)
        if not re.fullmatch(r"[\w.]+", qualifier):
            self.add_ref(function, "call", name, ctx.symbol, qualifier=collapse(qualifier, 80))
            return
        parts = qualifier.split(".")
        if parts[0] in ("self", "cls"):
            parts[0] = "this"
            root_type = ctx.fields.get(parts[1]) if len(parts) > 1 else None
            qualifier = ".".join(parts)
        else:
            root_type = ctx.locals.get(parts[0]) or self._module_types.get(parts[0])
        self.add_ref(
            function, "call", name, ctx.symbol, qualifier=qualifier, qualifier_type=root_type
        )

    # --- HTTP endpoints -----------------------------------------------------------

    def route_decorators(self, decorators: list[Node], index: int, node: Node) -> None:
        for decorator in decorators:
            expr = decorator.named_children[0] if decorator.named_children else None
            if expr is None or expr.type != "call":
                continue
            function = expr.child_by_field_name("function")
            if function is None or function.type != "attribute":
                continue
            receiver = self.text(function.child_by_field_name("object")).rsplit(".", 1)[-1]
            verb_name = self.text(function.child_by_field_name("attribute"))
            if not _ROUTE_RECEIVERS.search(receiver) and receiver not in self._prefixes:
                continue
            args = [a for a in expr.child_by_field_name("arguments").named_children]
            path = self.string_value(args[0]) if args else None
            if path is None or not path.startswith("/"):
                continue
            path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)  # Flask <int:id>
            methods: list[str | None]
            if verb_name in _HTTP_VERBS:
                methods = [_HTTP_VERBS[verb_name]]
            elif verb_name in ("route", "api_route"):
                methods = self.methods_argument(args) or ["GET"]
            else:
                continue
            for method in methods:
                route = normalize_route(join_routes(self._prefixes.get(receiver), path))
                self.out.endpoints.append(Endpoint("server", method, route, index, self.line(node)))

    def keyword_string(self, call: Node | None, names: tuple[str, ...]) -> str | None:
        if call is None or call.type != "call":
            return None
        arguments = call.child_by_field_name("arguments")
        for arg in arguments.named_children if arguments else []:
            name = self.text(arg.child_by_field_name("name"))
            if arg.type == "keyword_argument" and name in names:
                return self.string_value(arg.child_by_field_name("value"))
        return None

    def methods_argument(self, args: list[Node]) -> list[str | None]:
        for arg in args:
            if (
                arg.type == "keyword_argument"
                and self.text(arg.child_by_field_name("name")) == "methods"
            ):
                value = arg.child_by_field_name("value")
                return [
                    m.upper()
                    for m in (self.string_value(v) for v in (value.named_children if value else []))
                    if m
                ]
        return []

    def client_endpoint(self, node: Node, function: Node, ctx: _Ctx) -> None:
        """requests.get(url), httpx.post(url), session.get(url), client.get(url)."""
        if function.type != "attribute" or ctx.symbol is None:
            return
        verb = _HTTP_VERBS.get(self.text(function.child_by_field_name("attribute")))
        receiver = self.text(function.child_by_field_name("object")).rsplit(".", 1)[-1]
        if verb is None or receiver.lower().lstrip("_") not in _CLIENT_RECEIVERS:
            return
        arguments = node.child_by_field_name("arguments")
        args = arguments.named_children if arguments else []
        url = self.string_value(args[0]) if args else None
        if is_url_path(url):
            self.out.endpoints.append(
                Endpoint("client", verb, normalize_route(url), ctx.symbol, self.line(node))
            )

    def string_value(self, node: Node | None) -> str | None:
        """Value of a string literal or f-string (placeholders become ``{}``), a known
        module-level string constant, or a concatenation of those."""
        if node is None:
            return None
        if node.type == "string":
            parts = []
            for child in node.named_children:
                if child.type == "string_content":
                    parts.append(self.text(child))
                elif child.type == "interpolation":
                    parts.append("{}")
            return "".join(parts)
        if node.type == "identifier":
            return self._strings.get(self.text(node))
        if (
            node.type == "binary_operator"
            and self.text(node.child_by_field_name("operator")) == "+"
        ):
            left = self.string_value(node.child_by_field_name("left"))
            right = self.string_value(node.child_by_field_name("right"))
            if left is None and right is not None:
                return right if right.startswith("/") else None  # unknown base URL
            if left is not None and right is not None:
                return left + right
        return None
