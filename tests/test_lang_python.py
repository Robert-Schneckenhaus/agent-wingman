from conftest import index_fixtures

from wingman import endpoints
from wingman.extract import extract


def test_python_symbols_docs_and_types():
    source = b'''
class Foo(Base):
    """A foo."""
    rate: float = 0.19

    def __init__(self, repo: Repo):
        self.repo = repo

    @property
    def total(self) -> Optional[Decimal]:
        """The total."""
        return self.repo.items.sum()

    @staticmethod
    async def _make() -> "Foo":
        return Foo(None)
'''
    result = extract("foo.py", source)
    found = {s.qualified_name: s for s in result.symbols}
    assert found["Foo"].doc == "A foo." and found["Foo"].kind == "class"
    assert found["Foo.rate"].kind == "property"
    assert found["Foo.__init__"].kind == "constructor"
    assert found["Foo.total"].kind == "property" and found["Foo.total"].type_name == "Decimal"
    assert found["Foo._make"].modifiers == ["async", "static"]
    assert found["Foo._make"].visibility == "protected"
    assert found["Foo._make"].type_name == "Foo"
    calls = {
        (r.target_name, r.qualifier, r.qualifier_type) for r in result.refs if r.kind == "call"
    }
    assert ("sum", "this.repo.items", "Repo") in calls
    assert ("Base", None, None) in {
        (r.target_name, r.qualifier, r.qualifier_type) for r in result.refs if r.kind == "inherits"
    }


def test_python_imports():
    result = extract(
        "a.py",
        b"import os.path\nimport a.b as c\nfrom .m import X, Y as Z\n"
        b"from . import sub\nfrom x import *\n",
    )
    assert [(i.module, i.imported_name, i.alias, i.kind) for i in result.imports] == [
        ("os.path", "*", "os.path", "import"),
        ("a.b", "*", "c", "import"),
        (".m", "X", None, "import"),
        (".m", "Y", "Z", "import"),
        (".", "sub", None, "import"),
        ("x", "*", None, "reexport"),
    ]


def test_python_resolution_across_modules(tmp_path):
    ws = index_fixtures(tmp_path, ["py_app"])
    calls = ws.calls()
    # self attribute typed by the constructor parameter
    assert calls[("OrderService.cancel_order", "get")] == ("OrderRepository.get")
    # typed local variable (return annotation of get)
    assert calls[("OrderService.cancel_order", "cancel")] == "Order.cancel"
    # "from .utils import money" (submodule) and "import shop.utils.money as m"
    assert calls[("Order.total", "round_money")] == "round_money"
    assert calls[("OrderService.order_total", "round_money")] == ("round_money")
    # re-exported through the package __init__, module-level variable type
    assert calls[("cancel", "cancel_order")] == "OrderService.cancel_order"
    assert calls[("test_cancel_order", "OrderService")] == "OrderService"
    assert calls[("test_cancel_order", "Order")] == "Order"
    with ws.engine.connect() as conn:
        routes = {(e.method, e.route) for e in endpoints.list_endpoints(conn, ws.workspace_id)}
    assert routes == {("POST", "api/orders/{}/cancel"), ("GET", "api/orders/{}/total")}
