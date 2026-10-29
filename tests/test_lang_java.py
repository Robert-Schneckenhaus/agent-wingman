from conftest import index_fixtures
from sqlalchemy import select

from wingman import endpoints
from wingman.storage.schema import symbol_edges, symbols

PKG = "com.acme.shop"


def test_java_resolution(tmp_path):
    ws = index_fixtures(tmp_path, ["java_app"])
    found = ws.symbols()
    assert found[f"{PKG}.model.Order"] == ("class", "A customer order.")
    assert found[f"{PKG}.model.Order.cancel"] == (
        "method",
        "Cancels the order unless it was shipped.",
    )
    assert found[f"{PKG}.repo.OrderRepository"][0] == "interface"

    calls = ws.calls()
    service = f"{PKG}.service.OrderService"
    # field typed by an interface, wildcard import, static import
    assert calls[(f"{service}.cancelOrder", "find")] == f"{PKG}.repo.OrderRepository.find"
    assert calls[(f"{service}.cancelOrder", "cancel")] == f"{PKG}.model.Order.cancel"
    assert calls[(f"{service}.cancelOrder", "save")] == f"{PKG}.repo.OrderRepository.save"
    assert calls[(f"{service}.fee", "round")] == f"{PKG}.util.Money.round"
    assert calls[(f"{PKG}.web.OrderController.cancel", "cancelOrder")] == f"{service}.cancelOrder"
    test = f"{PKG}.OrderServiceTest.cancelsOrder"
    assert calls[(test, "InMemoryOrderRepository")] == f"{PKG}.repo.InMemoryOrderRepository"
    assert calls[(test, "save")] == f"{PKG}.repo.InMemoryOrderRepository.save"

    src, dst = symbols.alias(), symbols.alias()
    with ws.engine.connect() as conn:
        edges = {
            (r[0].rsplit(".", 1)[-1], r[1].rsplit(".", 1)[-1], r[2])
            for r in conn.execute(
                select(src.c.qualified_name, dst.c.qualified_name, symbol_edges.c.kind)
                .join(src, src.c.id == symbol_edges.c.src_id)
                .join(dst, dst.c.id == symbol_edges.c.dst_id)
            )
        }
        routes = {(e.method, e.route) for e in endpoints.list_endpoints(conn, ws.workspace_id)}
    assert ("InMemoryOrderRepository", "OrderRepository", "implements") in edges
    assert ("find", "find", "overrides") in edges
    assert routes == {("POST", "api/orders/{}/cancel")}
