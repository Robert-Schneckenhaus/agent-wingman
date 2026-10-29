from conftest import index_fixtures

from wingman import endpoints


def test_php_resolution(tmp_path):
    ws = index_fixtures(tmp_path, ["php_app"])
    found = ws.symbols()
    assert found["App.Models.Order"] == ("class", "A customer order.")
    assert found["App.Models.Order.cancel"] == (
        "method",
        "Cancels the order unless it was shipped.",
    )
    assert found["App.Support.money_round"] == ("function", "Rounds to cents.")

    calls = ws.calls()
    service = "App.Services.OrderService"
    # constructor property promotion, typed by an interface
    assert calls[(f"{service}.cancelOrder", "find")] == "App.Repositories.OrderRepository.find"
    assert calls[(f"{service}.cancelOrder", "save")] == "App.Repositories.OrderRepository.save"
    # method from a trait via $this, namespaced function via "use function"
    assert calls[("App.Models.Order.cancel", "isOpen")] == "App.Models.HasStatus.isOpen"
    assert calls[("App.Models.Order.total", "money_round")] == "App.Support.money_round"
    assert calls[("App.Http.Controllers.OrderController.cancel", "cancelOrder")] == (
        f"{service}.cancelOrder"
    )
    # Laravel route -> controller action
    route = "POST /api/orders/{}/cancel"
    assert calls[(route, "cancel")] == "App.Http.Controllers.OrderController.cancel"
    invokable = "App.Http.Controllers.ShowOrderController.__invoke"
    assert calls[("GET /api/orders/{}", "__invoke")] == invokable
    assert calls[("OrderServiceTest.testCancel", "InMemoryOrderRepository")] == (
        "App.Repositories.InMemoryOrderRepository"
    )
    assert calls[("OrderServiceTest.testCancel", "save")] == (
        "App.Repositories.InMemoryOrderRepository.save"
    )
    with ws.engine.connect() as conn:
        routes = {(e.method, e.route) for e in endpoints.list_endpoints(conn, ws.workspace_id)}
    assert routes == {("POST", "api/orders/{}/cancel"), ("GET", "api/orders/{}")}
