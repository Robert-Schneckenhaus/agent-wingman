from wingman import context
from wingman.context import analyze_task

BUG = """\
PDF export: VAT line missing when the invoice has a discount.
Seen in `PdfRenderer.renderTaxLines()` and maybe TaxCalculator; called from render().
   at Billing.Api.InvoicesController.<GetTax>d__3.MoveNext() in C:\\build\\src\\Api\\InvoicesController.cs:line 30
   at Billing.Tax.TaxCalculator.Breakdown(Invoice invoice)
TypeError: Cannot read properties of undefined (reading 'rate')
    at PdfRenderer.renderTaxLines (webpack:///src/export/pdfRenderer.ts:22:41)
    at InvoiceView (src/ui/InvoiceView.tsx:10:5)
See also src/billing/tax.ts and the tax_rate field.
"""


def test_stack_frames_are_extracted_and_normalized():
    frames = analyze_task(BUG).frames
    assert [(f.path, f.line, f.qualified) for f in frames] == [
        (
            "build/src/Api/InvoicesController.cs",
            30,
            "Billing.Api.InvoicesController.<GetTax>d__3.MoveNext",
        ),
        (None, None, "Billing.Tax.TaxCalculator.Breakdown"),
        ("src/export/pdfRenderer.ts", 22, None),
        ("src/ui/InvoiceView.tsx", 10, None),
    ]


def test_identifiers_and_paths():
    signals = analyze_task(BUG)
    assert signals.paths == ["src/billing/tax.ts"]
    for expected in ("PdfRenderer.renderTaxLines", "TaxCalculator", "render", "tax_rate"):
        assert expected in signals.identifiers
    # acronyms, prose and compiler-generated names are not identifiers
    for unexpected in ("PDF", "VAT", "missing", "d__3.MoveNext", "the"):
        assert unexpected not in signals.identifiers


def test_clean_frame_name():
    assert (
        context._clean_frame_name("A.B.Controller.<GetTax>d__3.MoveNext") == "A.B.Controller.GetTax"
    )
    assert context._clean_frame_name("A.Repo`1.Find") == "A.Repo.Find"


def _ranked(indexed, text, **kwargs):
    with indexed.engine.connect() as conn:
        pack = context.find_relevant(conn, indexed.workspace_id, text, **kwargs)
    return pack, [e.hit.qualified_name for e in pack.entries]


def test_stack_trace_finds_failing_methods(indexed):
    report = (
        "Tax endpoint returns wrong amounts.\n"
        "   at Billing.Tax.TaxCalculator.Breakdown(Invoice invoice) in "
        "D:\\ci\\work\\Billing\\TaxCalculator.cs:line 22\n"
        "   at Billing.Api.InvoicesController.GetTax(Int32 id) in "
        "D:\\ci\\work\\Api\\InvoicesController.cs:line 30\n"
    )
    pack, names = _ranked(indexed, report, limit=5)
    assert set(names[:2]) == {
        "Billing.Tax.TaxCalculator.Breakdown",
        "Billing.Api.InvoicesController.GetTax",
    }
    entry = next(e for e in pack.entries if e.hit.name == "Breakdown")
    assert any(r.startswith("stack frame TaxCalculator.cs:22") for r in entry.reasons)
    assert [t.name for t in entry.tests] == ["Breakdown_AppliesDefaultRate"]
    assert "Billing.Api.InvoicesController.GetTax" in [h.qualified_name for h in entry.called_by]


def test_prose_task_uses_full_text_and_docs(indexed):
    _, names = _ranked(indexed, "VAT line missing in PDF export when discount is set", limit=3)
    assert names[0] == "PdfRenderer.renderTaxLines"


def test_mentioned_identifier(indexed):
    _, names = _ranked(indexed, "Please make `roundMoney` configurable", limit=3)
    assert names[0] == "roundMoney"


def test_unknown_paths_are_reported(indexed):
    pack, _ = _ranked(indexed, "crash in src/legacy/old.ts:12")
    assert pack.unknown_paths == ["src/legacy/old.ts"]


def test_render_pack_respects_budget(indexed):
    pack, _ = _ranked(indexed, "tax invoice total discount render", limit=8, include_code=True)
    full = context.render_pack(pack, budget=10_000)
    small = context.render_pack(pack, budget=150)
    assert full.startswith("## Relevant code (8 of")
    assert "```" in full
    assert len(small) < len(full)
    assert "token budget reached" in small
    assert "1. " in small  # always at least one entry


def test_render_pack_without_results(indexed):
    pack, _ = _ranked(indexed, "zzzz qqqq")
    assert "Nothing matched" in context.render_pack(pack)


def test_text_matches_outside_the_failing_repositories_rank_lower(indexed):
    prose = "Money values are formatted wrong for display.\n"
    frame = (
        "   at Billing.Tax.TaxCalculator.Breakdown(Invoice invoice) in "
        "D:\\ci\\work\\Billing\\TaxCalculator.cs:line 22\n"
    )

    def score(text):
        pack, _ = _ranked(indexed, text, limit=20)
        return next(e.score for e in pack.entries if e.hit.name == "formatMoney")

    assert score(prose + frame) < 0.5 * score(prose)
