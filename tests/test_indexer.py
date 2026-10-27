from sqlalchemy import func, select, text

from wingman import query
from wingman.storage import store
from wingman.storage.schema import files, symbols
from wingman.storage.search import backend_for


def _resolved(indexed, source_qualified: str, target_name: str) -> str | None:
    """Qualified name the reference from SOURCE to TARGET_NAME resolved to."""
    with indexed.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT t.qualified_name FROM refs r "
                "JOIN symbols s ON s.id = r.from_symbol_id "
                "LEFT JOIN symbols t ON t.id = r.to_symbol_id "
                "WHERE s.qualified_name = :src AND r.target_name = :target"
            ),
            {"src": source_qualified, "target": target_name},
        ).first()
    assert row is not None, f"no reference {source_qualified} -> {target_name}"
    return row[0]


def _edges(indexed) -> set[tuple[str, str, str]]:
    with indexed.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.qualified_name, e.kind, b.qualified_name FROM symbol_edges e "
                "JOIN symbols a ON a.id = e.src_id JOIN symbols b ON b.id = e.dst_id"
            )
        )
        return {tuple(r) for r in rows}


def test_typescript_resolution(indexed):
    assert (
        _resolved(indexed, "PdfRenderer.render", "renderTaxLines") == "PdfRenderer.renderTaxLines"
    )
    assert _resolved(indexed, "PdfRenderer.render", "log") == "RendererBase.log"  # inherited
    assert _resolved(indexed, "PdfRenderer.render", "total") == "Invoice.total"  # typed param
    # namespace import of a barrel file re-exporting with `export *`
    assert _resolved(indexed, "PdfRenderer.render", "roundMoney") == "roundMoney"
    # field type -> member
    assert (
        _resolved(indexed, "PdfRenderer.renderTaxLines", "breakdown") == "TaxCalculator.breakdown"
    )
    # tsconfig path alias
    assert _resolved(indexed, "TaxCalculator.breakdown", "roundMoney") == "roundMoney"
    # default import + local variable type
    assert _resolved(indexed, "InvoiceView", "PdfRenderer") == "PdfRenderer"
    assert _resolved(indexed, "InvoiceView", "render") == "PdfRenderer.render"
    # aliased re-export: InvoiceModel -> Invoice
    assert _resolved(indexed, "InvoiceView", "InvoiceModel") == "Invoice"
    # JSX component usage
    assert _resolved(indexed, "InvoiceView", "TaxBadge") == "TaxBadge"
    # globals stay unresolved
    assert _resolved(indexed, "RendererBase.log", "log") is None


def test_csharp_resolution(indexed):
    controller = "Billing.Api.InvoicesController"
    assert (
        _resolved(indexed, f"{controller}.GetTax", "Audit") == "Billing.Api.ApiControllerBase.Audit"
    )
    assert _resolved(indexed, f"{controller}.GetTax", "Load") == f"{controller}.Load"
    # DI field typed with an interface
    assert (
        _resolved(indexed, f"{controller}.GetTax", "Breakdown")
        == "Billing.Tax.ITaxCalculator.Breakdown"
    )
    assert _resolved(indexed, f"{controller}.GetTax", "Round") == "Billing.Tax.TaxCalculator.Round"
    # partial class: member defined in the other file
    assert (
        _resolved(indexed, "Billing.Tax.TaxCalculator.Breakdown", "NetTotal")
        == "Billing.Models.Invoice.NetTotal"
    )
    # external static class must not match a repo method of the same name
    assert _resolved(indexed, "Billing.Tax.TaxCalculator.Round", "Round") is None


def test_member_chains_follow_declared_types(indexed):
    handler = "Billing.Data.CloseInvoiceHandler.Handle"
    with indexed.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT r.qualifier, t.qualified_name FROM refs r "
                "JOIN symbols s ON s.id = r.from_symbol_id "
                "LEFT JOIN symbols t ON t.id = r.to_symbol_id "
                "WHERE s.qualified_name = :src AND r.target_name = 'SaveEntitiesAsync'"
            ),
            {"src": handler},
        ).all()
    # explicit field chain and implicit-this property chain
    assert sorted(rows) == [
        ("Repository.UnitOfWork", "Billing.Data.IUnitOfWork.SaveEntitiesAsync"),
        ("_repository.UnitOfWork", "Billing.Data.IUnitOfWork.SaveEntitiesAsync"),
    ]
    # Task<Invoice> is unwrapped for the declared value type
    with indexed.engine.connect() as conn:
        type_name = conn.execute(
            select(symbols.c.type_name).where(
                symbols.c.qualified_name == "Billing.Data.IInvoiceRepository.GetAsync"
            )
        ).scalar_one()
    assert type_name == "Invoice"


def test_callers_include_calls_through_interfaces(indexed):
    with indexed.engine.connect() as conn:
        [impl] = query.find_symbols(
            conn, indexed.workspace_id, "Billing.Data.BillingContext.SaveEntitiesAsync"
        )
        hits = query.callers(conn, impl.id)
    assert [(h.symbol.qualified_name, h.via) for h in hits] == [
        ("Billing.Data.CloseInvoiceHandler.Handle", "Billing.Data.IUnitOfWork.SaveEntitiesAsync"),
        ("Billing.Data.CloseInvoiceHandler.Handle", "Billing.Data.IUnitOfWork.SaveEntitiesAsync"),
    ]


def test_edges(indexed):
    edges = _edges(indexed)
    assert ("TaxCalculator", "implements", "ITaxCalculator") in edges
    assert ("PdfRenderer", "inherits", "RendererBase") in edges
    assert ("TaxCalculator.breakdown", "overrides", "ITaxCalculator.breakdown") in edges
    assert (
        "Billing.Tax.TaxCalculator.Breakdown",
        "overrides",
        "Billing.Tax.ITaxCalculator.Breakdown",
    ) in edges
    assert (
        "Billing.Api.InvoicesController",
        "inherits",
        "Billing.Api.ApiControllerBase",
    ) in edges


def test_incremental_reindex_only_parses_changed_files(indexed):
    stats = indexed.reindex("ts_app")
    assert stats.files_indexed == 0
    assert stats.files_unchanged == 7

    tax = indexed.root / "ts_app/src/billing/tax.ts"
    tax.write_text(tax.read_text().replace("breakdown(", "computeLines("))
    stats = indexed.reindex("ts_app")
    assert stats.files_indexed == 1
    # references from unchanged files into the changed file are re-resolved
    assert _resolved(indexed, "PdfRenderer.renderTaxLines", "breakdown") is None
    assert _resolved(indexed, "InvoiceView", "PdfRenderer") == "PdfRenderer"


def test_reference_into_changed_file_follows_new_symbol_ids(indexed):
    money = indexed.root / "ts_app/src/shared/money.ts"
    money.write_text("// moved down\n\n" + money.read_text())
    indexed.reindex("ts_app")
    assert _resolved(indexed, "TaxCalculator.breakdown", "roundMoney") == "roundMoney"


def test_deleted_files_are_removed(indexed):
    (indexed.root / "ts_app/src/ui/InvoiceView.tsx").unlink()
    stats = indexed.reindex("ts_app")
    assert stats.files_removed == 1
    with indexed.engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).where(files.c.path == "src/ui/InvoiceView.tsx")
            ).scalar_one()
            == 0
        )
        assert not query.find_symbols(conn, indexed.workspace_id, "InvoiceView")
        hits = query.search(conn, indexed.workspace_id, "InvoiceView", repo="ts_app")
        assert "InvoiceView" not in {h.name for h in hits}


def test_full_reindex_keeps_counts_stable(indexed):
    with indexed.engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(symbols)).scalar_one()
    stats = indexed.reindex("cs_app", full=True)
    assert stats.files_indexed == 6
    with indexed.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(symbols)).scalar_one() == before
        fts = conn.execute(text("SELECT count(*) FROM symbols_fts")).scalar_one()
        assert fts == before


def test_removing_repository_cleans_search_index(indexed):
    with indexed.engine.begin() as conn:
        store.remove_repository(conn, indexed.workspace_id, "cs_app")
        backend_for(conn).purge_orphans(conn)
    with indexed.engine.connect() as conn:
        remaining = conn.execute(select(func.count()).select_from(symbols)).scalar_one()
        assert conn.execute(text("SELECT count(*) FROM symbols_fts")).scalar_one() == remaining
        hits = query.search(conn, indexed.workspace_id, "NetTotal")
        assert {h.repo for h in hits} == {"ts_app"}


def test_stable_keys_disambiguate_overloads(indexed):
    controller = indexed.root / "cs_app/Api/InvoicesController.cs"
    controller.write_text(
        controller.read_text().replace(
            "private Invoice Load(int id)",
            "private Invoice Load(string code) => new Invoice();\n    private Invoice Load(int id)",
        )
    )
    indexed.reindex("cs_app")
    with indexed.engine.connect() as conn:
        keys = (
            conn.execute(select(symbols.c.stable_key).where(symbols.c.name == "Load"))
            .scalars()
            .all()
        )
    assert len(keys) == len(set(keys)) == 2
