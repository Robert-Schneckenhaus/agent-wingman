import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from wingman import endpoints, query
from wingman.endpoints import join_routes, match_route, normalize_route
from wingman.extract import extract
from wingman.indexer import index_repository
from wingman.storage import db, store
from wingman.storage.schema import refs


def _target(indexed, source_qualified: str, target_name: str, kind: str | None = None):
    sql = (
        "SELECT t.qualified_name, r.confidence FROM refs r "
        "JOIN symbols s ON s.id = r.from_symbol_id "
        "LEFT JOIN symbols t ON t.id = r.to_symbol_id "
        "WHERE s.qualified_name = :src AND r.target_name = :target"
    )
    params = {"src": source_qualified, "target": target_name}
    if kind:
        sql += " AND r.kind = :kind"
        params["kind"] = kind
    with indexed.engine.connect() as conn:
        rows = conn.execute(text(sql), params).all()
    assert rows, f"no reference {source_qualified} -> {target_name}"
    return rows


# --- packages and namespaces across repositories --------------------------------------


def test_npm_package_import_resolves_into_other_repository(indexed):
    [(target, confidence)] = _target(indexed, "invoiceSummary", "formatMoney")
    assert target == "formatMoney"  # defined in shared_lib, re-exported by src/index.ts
    assert confidence == 0.9


def test_csharp_using_resolves_into_other_repository(indexed):
    rows = _target(indexed, "Billing.Formatting.InvoiceFormatter.FormatTotal", "Format")
    assert rows[0][0] == "Acme.Shared.MoneyFormatter.Format"


def test_name_only_fallback_stays_within_the_repository(indexed):
    # `total` exists in ts_app (Invoice.total) and as "Total" nowhere else; a call on an
    # untyped receiver must not resolve to a same-named method in another repository.
    with indexed.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT fr.name AS from_repo, tr.name AS to_repo FROM refs r "
                "JOIN files ff ON ff.id = r.from_file_id "
                "JOIN repositories fr ON fr.id = ff.repo_id "
                "JOIN symbols t ON t.id = r.to_symbol_id "
                "JOIN files tf ON tf.id = t.file_id "
                "JOIN repositories tr ON tr.id = tf.repo_id "
                "WHERE r.resolver = 'name' AND fr.id != tr.id"
            )
        ).all()
    assert rows == []


def test_reindexing_a_dependency_re_resolves_its_dependents(indexed):
    shared = indexed.root / "shared_lib/src/format.ts"
    shared.write_text("// moved\n\n" + shared.read_text())
    indexed.reindex("shared_lib")  # new symbol ids in shared_lib
    [(target, _)] = _target(indexed, "invoiceSummary", "formatMoney")
    assert target == "formatMoney"


def test_new_repository_makes_existing_references_resolvable(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"

    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'x.db'}")
    db.upgrade(engine)
    with engine.begin() as conn:
        workspace_id = store.ensure_workspace(conn, "w")
        for name in ("ts_app", "shared_lib"):
            shutil.copytree(fixtures / name, tmp_path / name)
        store.add_repository(conn, workspace_id, "ts_app", (tmp_path / "ts_app").as_posix())
    with engine.connect() as conn:
        [ts_app] = store.list_repositories(conn, workspace_id)
    index_repository(engine, ts_app)  # @acme/shared is unknown yet
    with engine.begin() as conn:
        store.add_repository(conn, workspace_id, "shared", (tmp_path / "shared_lib").as_posix())
    with engine.connect() as conn:
        shared = next(r for r in store.list_repositories(conn, workspace_id) if r.name == "shared")
    index_repository(engine, shared)
    with engine.connect() as conn:
        resolved = conn.execute(
            text(
                "SELECT count(*) FROM refs r JOIN symbols t ON t.id = r.to_symbol_id "
                "WHERE r.target_name = 'formatMoney' AND t.name = 'formatMoney'"
            )
        ).scalar_one()
    assert resolved == 1


# --- HTTP endpoints ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("/api/Items/{id:int}", "api/items/{}"),
        ("/brand/{brandId:int?}", "brand/{}"),
        ("https://shop.example.com/api/items/42?x=1#top", "api/items/42"),
        ("users/:id", "users/{}"),
        ("~/health", "health"),
        ("/", ""),
    ],
)
def test_normalize_route(raw, normalized):
    assert normalize_route(raw) == normalized


def test_join_routes():
    assert join_routes("api/catalog", "/items") == "api/catalog/items"  # minimal API group
    assert join_routes("api/orders", "/health", slash_is_absolute=True) == "/health"
    assert join_routes("api/orders", "~/health") == "/health"  # normalized to "health"
    assert join_routes(None, "x") == "x"


def test_match_route():
    assert match_route("api/invoices/{}/tax", "api/invoices/{}/tax") == 0.9
    assert match_route("api/invoices/5/tax", "api/invoices/{}/tax") == 0.9  # literal value
    assert match_route("items/{}", "api/catalog/items/{}") == 0.6  # unknown base URL
    assert match_route("items/{}", "api/invoices/{}/tax") is None  # literals must line up
    assert match_route("{}", "api/items") is None  # too vague
    assert match_route("bff/api/orders/{}", "api/orders/{}") == pytest.approx(0.48)
    assert match_route("api/orders", "api/invoices") is None


def test_most_specific_route_wins(indexed):
    # client "api/catalog/items/by" must go to the literal route, not to items/{id}
    from wingman.endpoints import specificity

    assert specificity("api/catalog/items/by", "api/catalog/items/by") == 4
    assert specificity("api/catalog/items/by", "api/catalog/items/{}") == 3
    # and a client parameter prefers a server parameter over a literal
    assert specificity("api/catalog/items/{}", "api/catalog/items/{}") == 3.5
    assert specificity("api/catalog/items/{}", "api/catalog/items/by") == 3


def test_csharp_server_and_client_extraction():
    source = b"""
[ApiController]
[Route("api/[controller]")]
public class OrdersController : ControllerBase
{
    [HttpGet("{id:int}")] public IActionResult Get(int id) => Ok();
    [HttpGet("~/health")] public IActionResult Health() => Ok();
    [Route("search")] [HttpGet] public IActionResult Search() => Ok();
}
public static class Api
{
    public static void Map(IEndpointRouteBuilder app)
    {
        var api = app.MapGroup("api/catalog").HasApiVersion(1.0);
        api.MapGet("/items/{id:int}", GetItem);
    }
    public static IResult GetItem(int id) => Results.Ok();
}
public class Client(HttpClient http)
{
    private readonly string baseUrl = "api/catalog/";
    public Task Load(int id) => http.GetFromJsonAsync<string>($"{baseUrl}items/{id}");
    public Task Other() => repository.GetAsync(5);
}
"""
    result = extract("A.cs", source)
    found = {(e.role, e.method, e.route, result.symbols[e.symbol].name) for e in result.endpoints}
    assert found == {
        ("server", "GET", "api/orders/{}", "Get"),
        ("server", "GET", "health", "Health"),
        ("server", "GET", "api/orders/search", "Search"),
        ("server", "GET", "api/catalog/items/{}", "GetItem"),
        ("client", "GET", "api/catalog/items/{}", "Load"),
    }


def test_typescript_server_and_client_extraction():
    source = b"""
const baseUrl = "/api";
export async function load(id: number) {
  await fetch(`${baseUrl}/invoices/${id}/tax`);
  await fetch(`${config.root}/invoices`, { method: "post" });
  return axios.delete(`/api/invoices/${id}`);
}
@Controller('cats')
export class CatsController {
  @Get(':id') findOne() {}
  @Post() create() {}
}
export class Svc {
  constructor(private http: HttpClient) {}
  item(id: string) { return this.http.get(`/api/items/${id}`); }
}
app.get("/users/:id", getUser);
function getUser(req, res) {}
const cache = new Map(); cache.get("key");
"""
    result = extract("a.ts", source)
    found = {(e.role, e.method, e.route, result.symbols[e.symbol].name) for e in result.endpoints}
    assert found == {
        ("client", "GET", "api/invoices/{}/tax", "load"),
        ("client", "POST", "invoices", "load"),
        ("client", "DELETE", "api/invoices/{}", "load"),
        ("server", "GET", "cats/{}", "findOne"),
        ("server", "POST", "cats", "create"),
        ("client", "GET", "api/items/{}", "item"),
        ("server", "GET", "users/{}", "getUser"),
    }
    # method decorators are kept (they precede the method as siblings in the tree)
    assert next(s for s in result.symbols if s.name == "findOne").decorators == ["@Get(':id')"]


def test_http_calls_link_frontend_and_services(indexed):
    def callers_of(name):
        with indexed.engine.connect() as conn:
            [hit] = query.find_symbols(conn, indexed.workspace_id, name)
            return {
                (r.symbol.qualified_name, r.kind, r.confidence) for r in query.callers(conn, hit.id)
            }

    assert callers_of("Billing.Api.InvoicesController.GetTax") == {
        ("fetchInvoiceTax", "http_call", 0.9)
    }
    assert callers_of("Billing.Api.InvoicesController.Create") == {
        ("createInvoice", "http_call", 0.9)
    }
    assert callers_of("Billing.Api.CatalogApi.GetItemById") == {
        ("Billing.Clients.CatalogClient.GetItem", "http_call", 0.9),  # C# -> C#
        ("loadCatalogItem", "http_call", 0.6),  # unknown base URL, suffix match
    }


def test_http_calls_are_rebuilt_after_changes(indexed):
    api = indexed.root / "ts_app/src/api/invoiceApi.ts"
    api.write_text(api.read_text().replace("/tax`", "/taxes`"))
    indexed.reindex("ts_app")
    with indexed.engine.connect() as conn:
        assert endpoints.list_endpoints(conn, indexed.workspace_id, contains="tax")[0].matches == 0
        total = conn.execute(
            select(func.count()).select_from(refs).where(refs.c.kind == "http_call")
        ).scalar_one()
    assert total == 3  # createInvoice, CatalogClient.GetItem, loadCatalogItem


def test_endpoints_cli(ws):
    servers = ws("endpoints").output
    assert "GET    /api/invoices/{}/tax  -> Billing.Api.InvoicesController.GetTax" in servers
    assert "(1 client)" in servers
    assert "(mapped at line 8)" in servers
    clients = ws("endpoints", "--clients").output
    assert "POST   /api/invoices  -> createInvoice" in clients
    unmatched = ws("endpoints", "--unmatched").output
    assert unmatched.strip() == (
        "GET    /api/catalog/items  -> Billing.Api.CatalogApi.MapCatalogApi  "
        "[cs_app:Api/CatalogApi.cs:5-11]  (0 clients)"
    )
    callers = ws("callers", "Billing.Api.InvoicesController.GetTax").output
    assert "ts_app:src/api/invoiceApi.ts:5  function fetchInvoiceTax  (http_call, 0.90)" in callers
