"""Incremental resolution must give exactly the result of a full resolution."""

import pytest
from sqlalchemy import select

from wingman.resolver import resolve_workspace
from wingman.storage.schema import files, imports, refs, repositories, symbol_edges, symbols


def snapshot(engine) -> dict:
    """Resolution state keyed by stable names (ids change on re-index)."""
    source, target = symbols.alias("source"), symbols.alias("target")
    src_file, dst_file = files.alias("src_file"), files.alias("dst_file")
    src_repo, dst_repo = repositories.alias("src_repo"), repositories.alias("dst_repo")
    with engine.connect() as conn:
        ref_rows = conn.execute(
            select(
                src_repo.c.name,
                src_file.c.path,
                source.c.stable_key,
                refs.c.kind,
                refs.c.target_name,
                refs.c.line,
                refs.c.col,
                target.c.stable_key,
                refs.c.confidence,
                refs.c.resolver,
            )
            .select_from(refs)
            .join(src_file, src_file.c.id == refs.c.from_file_id)
            .join(src_repo, src_repo.c.id == src_file.c.repo_id)
            .outerjoin(source, source.c.id == refs.c.from_symbol_id)
            .outerjoin(target, target.c.id == refs.c.to_symbol_id)
        ).all()
        edge_rows = conn.execute(
            select(
                source.c.stable_key,
                target.c.stable_key,
                symbol_edges.c.kind,
                symbol_edges.c.confidence,
            )
            .select_from(symbol_edges)
            .join(source, source.c.id == symbol_edges.c.src_id)
            .join(target, target.c.id == symbol_edges.c.dst_id)
        ).all()
        import_rows = conn.execute(
            select(
                src_repo.c.name,
                src_file.c.path,
                imports.c.module,
                imports.c.imported_name,
                imports.c.line,
                dst_repo.c.name,
                dst_file.c.path,
            )
            .select_from(imports)
            .join(src_file, src_file.c.id == imports.c.file_id)
            .join(src_repo, src_repo.c.id == src_file.c.repo_id)
            .outerjoin(dst_file, dst_file.c.id == imports.c.resolved_file_id)
            .outerjoin(dst_repo, dst_repo.c.id == dst_file.c.repo_id)
        ).all()
    return {
        "refs": {tuple(r[:7]): tuple(r[7:]) for r in ref_rows},
        "edges": {tuple(r) for r in edge_rows},
        "imports": {tuple(r[:5]): tuple(r[5:]) for r in import_rows},
    }


def full_resolution(ws) -> dict:
    with ws.engine.begin() as conn:
        repo_ids = set(conn.execute(select(repositories.c.id)).scalars())
        resolve_workspace(conn, ws.workspace_id, repo_ids)
    return snapshot(ws.engine)


def diff(a: dict, b: dict) -> list:
    problems = []
    for key in ("refs", "imports"):
        for k in a[key].keys() | b[key].keys():
            if a[key].get(k) != b[key].get(k):
                problems.append((key, k, a[key].get(k), b[key].get(k)))
    for edge in a["edges"] ^ b["edges"]:
        problems.append(("edge", edge, edge in a["edges"], edge in b["edges"]))
    return sorted(problems, key=str)[:10]


def replace_in(path, old, new):
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new))


# name -> phases; each phase is a list of edits followed by an incremental re-index.
# (repo, path, old, new): old None deletes the file, old "" creates it.
SCENARIOS = {
    "body change adds a call": [
        [
            (
                "cs_app",
                "Billing/TaxCalculator.cs",
                "var net = invoice.NetTotal();",
                "var net = invoice.NetTotal(); Round(net);",
            )
        ],
    ],
    "renamed class": [
        [("ts_app", "src/billing/invoice.ts", "export class Invoice", "export class Bill")]
    ],
    "deleted module that others import": [[("ts_app", "src/shared/money.ts", None, None)]],
    "barrel re-export removed": [
        [("ts_app", "src/shared/index.ts", 'export * from "./money";\n', "")]
    ],
    "base class changed": [
        [
            (
                "cs_app",
                "Data/InvoiceRepository.cs",
                "public class InvoiceRepository : IInvoiceRepository",
                "public class InvoiceRepository",
            )
        ],
    ],
    "shared library change in another repository": [
        [
            (
                "shared_lib",
                "src/format.ts",
                "export function formatMoney(",
                "export function formatAmount(",
            )
        ],
    ],
    "C# type moved to another namespace": [
        [
            (
                "cs_app",
                "Billing/TaxCalculator.cs",
                "namespace Billing.Tax;",
                "namespace Billing.Taxes;",
            )
        ],
    ],
    # In the following, the file with the reference does not change in the last phase.
    "a second method makes a unique name ambiguous": [
        [
            (
                "cs_app",
                "Billing/Formatting.cs",
                "public string FormatTotal",
                "public void Audit(dynamic x) { x.Archive(); }\n    public string FormatTotal",
            ),
            (
                "cs_app",
                "Billing/Invoice.cs",
                "public decimal Discount { get; set; }",
                "public decimal Discount { get; set; }\n        public void Archive() {}",
            ),
        ],
        [
            (
                "cs_app",
                "Data/InvoiceRepository.cs",
                "private readonly BillingContext _context = new();",
                "private readonly BillingContext _context = new();\n    public void Archive() {}",
            )
        ],
    ],
    "a new type resolves an unresolved instantiation": [
        [
            (
                "cs_app",
                "Billing/TaxCalculator.cs",
                "var net = invoice.NetTotal();",
                "var net = invoice.NetTotal(); var log = new AuditLog();",
            )
        ],
        [
            (
                "cs_app",
                "Billing/AuditLog.cs",
                "",
                "namespace Billing.Tax;\n\npublic class AuditLog { }\n",
            )
        ],
    ],
    "a new TS function resolves a bare call": [
        [
            (
                "ts_app",
                "src/billing/tax.ts",
                "const net = invoice.total();",
                "const net = applyDiscount(invoice.total());",
            )
        ],
        [
            (
                "ts_app",
                "src/billing/discount.ts",
                "",
                "export function applyDiscount(x: number) { return x; }\n",
            )
        ],
    ],
    "a changed member type redirects a call chain elsewhere": [
        [
            (
                "cs_app",
                "Billing/Closer.cs",
                "",
                "using Billing.Data;\nnamespace Billing.Closing;\npublic class Closer\n{\n    private readonly IInvoiceRepository _repo;\n    public void Close() => _repo.UnitOfWork.SaveEntitiesAsync();\n}\n",
            )
        ],
        [
            (
                "cs_app",
                "Data/InvoiceRepository.cs",
                "IUnitOfWork UnitOfWork { get; }",
                "BillingContext UnitOfWork { get; }",
            )
        ],
    ],
    "an intermediate member type in a call chain changes": [
        [
            (
                "cs_app",
                "Billing/Ledger.cs",
                "",
                "namespace Billing.Data;\npublic interface ILedger { void Post(); }\npublic class Books { public IInvoiceRepository Repo { get; } }\n",
            ),
            (
                "cs_app",
                "Billing/Poster.cs",
                "",
                "using Billing.Data;\nnamespace Billing.Posting;\npublic class Poster\n{\n    private readonly Books _books;\n    public void Run() => _books.Repo.UnitOfWork.Post();\n}\n",
            ),
        ],
        [
            (
                "cs_app",
                "Data/InvoiceRepository.cs",
                "IUnitOfWork UnitOfWork { get; }",
                "ILedger UnitOfWork { get; }",
            )
        ],
    ],
    "a new base member creates an override in a derived type elsewhere": [
        [
            (
                "cs_app",
                "Billing/SpecialCalculator.cs",
                "",
                "namespace Billing.Tax;\npublic class SpecialCalculator : TaxCalculator\n{\n    public decimal Extra() => 0;\n}\n",
            ),
            # a grandchild in a third file: only reached through the base type chain
            (
                "cs_app",
                "Billing/SuperCalculator.cs",
                "",
                "namespace Billing.Tax;\npublic class SuperCalculator : SpecialCalculator\n{\n    public decimal Bonus() => 0;\n}\n",
            ),
        ],
        [
            (
                "cs_app",
                "Billing/TaxCalculator.cs",
                "public const decimal DefaultRate = 0.19m;",
                "public const decimal DefaultRate = 0.19m;\n    public virtual decimal Extra() => 1;\n    public virtual decimal Bonus() => 1;",
            )
        ],
    ],
}


def apply_edit(root, repo, path, old, new):
    file = root / repo / path
    if old is None:
        file.unlink()
    elif old == "" and not file.exists():
        file.write_text(new)
    else:
        replace_in(file, old, new)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_incremental_equals_full_resolution(indexed, name):
    for phase in SCENARIOS[name]:
        for edit in phase:
            apply_edit(indexed.root, *edit)
        for repo in sorted({repo for repo, *_ in phase}):
            stats = indexed.reindex(repo)
            assert stats.files_indexed or stats.files_removed
    incremental = snapshot(indexed.engine)
    assert diff(incremental, full_resolution(indexed)) == []


def test_single_path_reindex_equals_full_resolution(indexed):
    file = indexed.root / "cs_app" / "Billing/Invoice.cs"
    replace_in(file, "public decimal Discount { get; set; }", "public decimal Rebate { get; set; }")
    indexed.reindex("cs_app", paths=["Billing/Invoice.cs"])
    incremental = snapshot(indexed.engine)
    assert diff(incremental, full_resolution(indexed)) == []


LANGUAGE_SCENARIOS = {
    "py_app": [("src/shop/utils/money.py", "def round_money(", "def round_amount(")],
    "java_app": [
        (
            "src/main/java/com/acme/shop/repo/OrderRepository.java",
            "Order find(long id);",
            "Order lookup(long id);",
        ),
    ],
    "php_app": [
        ("app/Models/HasStatus.php", "public function isOpen()", "public function isActive()")
    ],
    "cpp_app": [
        (
            "src/util.cpp",
            "double round(double value) {",
            "double round(double value, int digits) {",
        ),
        ("include/shop/order.h", "void cancel();", "void cancel();\n    void archive();"),
    ],
}


@pytest.mark.parametrize("repo", list(LANGUAGE_SCENARIOS))
def test_incremental_equals_full_resolution_per_language(tmp_path, repo):
    from conftest import index_fixtures

    ws = index_fixtures(tmp_path, [repo])
    for path, old, new in LANGUAGE_SCENARIOS[repo]:
        replace_in(ws.root / repo / path, old, new)
    assert ws.reindex(repo).files_indexed
    assert diff(snapshot(ws.engine), full_resolution(ws)) == []
