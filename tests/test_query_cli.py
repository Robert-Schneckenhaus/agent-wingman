import json


def test_index_is_incremental(ws):
    result = ws("index", "ts_app")
    assert "0 indexed, 7 unchanged" in result.output
    result = ws("index", "unknown", ok=False)
    assert result.exit_code != 0
    assert "Unknown repositories" in result.output


def test_status_shows_index_counts(ws):
    result = ws("status")
    assert "13 files" in result.output
    assert "references resolved" in result.output


def test_search(ws):
    result = ws("search", "render", "tax", "lines")
    first = result.output.splitlines()[0]
    assert "PdfRenderer.renderTaxLines" in first
    assert "ts_app:src/export/pdfRenderer.ts:21-23" in first

    data = json.loads(ws("search", "net total", "--json", "--repo", "cs_app").output)
    assert data[0]["qualified_name"] == "Billing.Models.Invoice.NetTotal"
    assert all(hit["repo"] == "cs_app" for hit in data)

    data = json.loads(ws("search", "TaxCalculator", "--json", "--kind", "class").output)
    assert {hit["kind"] for hit in data} == {"class"}

    data = json.loads(ws("search", "breakdown applies", "--json", "--no-tests").output)
    assert not any(hit["is_test"] for hit in data)


def test_symbol(ws):
    result = ws("symbol", "Billing.Tax.TaxCalculator")
    assert (
        "class Billing.Tax.TaxCalculator  [cs_app:Billing/TaxCalculator.cs:16-27]" in result.output
    )
    assert "bases: Billing.Tax.ITaxCalculator" in result.output
    assert "doc: Calculates taxes. The discount is applied before tax." in result.output
    assert "method      Breakdown" in result.output

    result = ws("symbol", "roundMoney", "--code")
    assert "return Math.round(value * 100) / 100;" in result.output

    data = json.loads(ws("symbol", "ITaxCalculator.breakdown", "--json").output)
    assert data[0]["overridden_by"][0]["qualified_name"] == "TaxCalculator.breakdown"


def test_symbol_ambiguous_and_unknown(ws):
    result = ws("symbol", "Breakdown", ok=False)
    assert result.exit_code == 1
    assert "ambiguous" in result.output
    assert "Billing.Tax.TaxCalculator.Breakdown" in result.output

    result = ws("symbol", "DoesNotExist", ok=False)
    assert "Try: wingman search DoesNotExist" in result.output


def test_symbol_by_id(ws):
    data = json.loads(ws("symbol", "roundMoney", "--json").output)
    symbol_id = data[0]["symbol"]["id"]
    result = ws("symbol", f"#{symbol_id}")
    assert "function roundMoney" in result.output


def test_callers_through_interface_and_depth(ws):
    result = ws("callers", "Billing.Tax.TaxCalculator.Breakdown")
    assert "Billing.Api.InvoicesController.GetTax" in result.output
    assert "via Billing.Tax.ITaxCalculator.Breakdown" in result.output
    assert "TaxCalculatorTests.Breakdown_AppliesDefaultRate" in result.output

    result = ws("callers", "renderTaxLines", "--depth", "2")
    lines = result.output.splitlines()
    assert "method PdfRenderer.render" in lines[1]
    assert lines[2].startswith("    ") and "component InvoiceView" in lines[2]


def test_callees(ws):
    result = ws("callees", "PdfRenderer.render")
    assert "this.log -> RendererBase.log" in result.output
    assert "lines.join  (unresolved)" in result.output

    data = json.loads(ws("callees", "Billing.Api.InvoicesController.GetTax", "--json").output)
    targets = {c["symbol"]["qualified_name"] for c in data[0]["callees"] if c["symbol"]}
    assert "Billing.Tax.TaxCalculator.Round" in targets


def test_outline(ws):
    result = ws("outline", "ts_app/src/export/pdfRenderer.ts")
    assert result.output.splitlines()[0] == "ts_app:src/export/pdfRenderer.ts"
    assert "class PdfRenderer" in result.output
    assert "    method renderTaxLines" in result.output

    assert "record LineItem" in ws("outline", "cs_app:Billing/Invoice.cs").output
    assert "class Invoice" in ws("outline", "Invoice.cs").output  # suffix match

    result = ws("outline", "nope.ts", ok=False)
    assert "File not in index" in result.output


def test_repo_remove_cleans_index(ws):
    ws("repo", "remove", "cs_app", "--yes")
    data = json.loads(ws("search", "NetTotal", "--json").output)
    assert {hit["repo"] for hit in data} == {"ts_app"}
