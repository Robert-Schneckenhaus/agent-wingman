from wingman.extract import extract
from wingman.extract.csharp import normalize_xml_doc


def _extract(source: str):
    return extract("A.cs", source.encode())


SOURCE = """
global using System.Linq;
using Billing.Tax;
using static Billing.Util.Helpers;
using Model = Billing.Models.Invoice;

namespace Billing.Api;

/// <summary>Invoice endpoints.</summary>
[ApiController]
[Route("api/invoices")]
public partial class InvoicesController : ControllerBase, IAudited
{
    private readonly ITaxCalculator _calc;
    public const int PageSize = 20;
    public string Name { get; set; }

    public InvoicesController(ITaxCalculator calc) { _calc = calc; }

    /// <summary>Gets the tax.</summary>
    /// <param name="id">Invoice id</param>
    /// <returns>Tax lines</returns>
    [HttpGet("{id}")]
    public async Task<IActionResult> GetTax(int id)
    {
        var invoice = new Invoice();
        Invoice other = Load(id);
        var lines = _calc.Breakdown(invoice);
        other.Validate();
        Helper();
        TaxCalculator.Round(1m);
        this.Audit();
        base.Dispose();
        return Ok(lines);
    }

    public void GetTax(string code) { }

    private Invoice Load(int id) => new Invoice();

    public class Nested { }
}

public interface IAudited { void Audit(); }
public record Money(decimal Amount);
public enum Status { Open, Paid }
public struct Point { }
public delegate void Handler(int x);
"""


def test_symbols_and_qualified_names():
    result = _extract(SOURCE)
    kinds = {(s.qualified_name, s.kind) for s in result.symbols}
    prefix = "Billing.Api.InvoicesController"
    assert kinds == {
        (prefix, "class"),
        (f"{prefix}._calc", "field"),
        (f"{prefix}.PageSize", "const"),
        (f"{prefix}.Name", "property"),
        (f"{prefix}.InvoicesController", "constructor"),
        (f"{prefix}.GetTax", "method"),
        (f"{prefix}.Load", "method"),
        (f"{prefix}.Nested", "class"),
        ("Billing.Api.IAudited", "interface"),
        ("Billing.Api.IAudited.Audit", "method"),
        ("Billing.Api.Money", "record"),
        ("Billing.Api.Status", "enum"),
        ("Billing.Api.Point", "struct"),
        ("Billing.Api.Handler", "delegate"),
    }
    overloads = [s for s in result.symbols if s.name == "GetTax"]
    assert len(overloads) == 2
    assert result.namespaces == ["Billing.Api"]


def test_attributes_modifiers_and_docs():
    result = _extract(SOURCE)
    symbols = {s.qualified_name: s for s in result.symbols}
    cls = symbols["Billing.Api.InvoicesController"]
    assert cls.decorators == ["[ApiController]", '[Route("api/invoices")]']
    assert cls.modifiers == ["public", "partial"]
    assert cls.visibility == "public"
    assert cls.doc == "Invoice endpoints."
    assert cls.signature == "public partial class InvoicesController : ControllerBase, IAudited"
    method = next(s for s in result.symbols if s.name == "GetTax")
    assert method.doc == "Gets the tax.\nid: Invoice id\nReturns: Tax lines"
    assert method.decorators == ['[HttpGet("{id}")]']
    assert method.signature == "public async Task<IActionResult> GetTax(int id)"
    assert symbols["Billing.Api.IAudited.Audit"].visibility == "public"
    assert symbols["Billing.Api.InvoicesController._calc"].visibility == "private"


def test_usings():
    result = _extract(SOURCE)
    assert [(i.module, i.alias, i.kind) for i in result.imports] == [
        ("System.Linq", None, "global_using"),
        ("Billing.Tax", None, "using"),
        ("Billing.Util.Helpers", None, "using_static"),
        ("Billing.Models.Invoice", "Model", "using"),
    ]


def test_heritage_and_call_refs():
    result = _extract(SOURCE)
    heritage = {
        (r.kind, r.target_name) for r in result.refs if r.kind in ("inherits", "implements")
    }
    assert heritage == {("inherits", "ControllerBase"), ("implements", "IAudited")}

    calls = {
        r.target_name: (r.qualifier, r.qualifier_type) for r in result.refs if r.kind == "call"
    }
    assert calls["Breakdown"] == ("_calc", "ITaxCalculator")
    assert calls["Validate"] == ("other", "Invoice")
    assert calls["Load"] == (None, None)
    assert calls["Helper"] == (None, None)
    assert calls["Round"] == ("TaxCalculator", None)
    assert calls["Audit"] == ("this", None)
    assert calls["Dispose"] == ("base", None)
    instantiations = [r.target_name for r in result.refs if r.kind == "instantiate"]
    assert instantiations == ["Invoice", "Invoice"]


def test_block_namespaces_nest():
    result = _extract("namespace A { namespace B { class C { void M() {} } } }")
    assert [s.qualified_name for s in result.symbols] == ["A.B.C", "A.B.C.M"]
    assert result.namespaces == ["A", "A.B"]


def test_normalize_xml_doc():
    doc = normalize_xml_doc(
        [
            "/// <summary>",
            '/// Uses <see cref="Billing.TaxCalculator"/> &amp; rounds.',
            "/// </summary>",
            '/// <exception cref="ArgumentException">If empty</exception>',
        ]
    )
    assert doc == "Uses TaxCalculator & rounds.\nThrows ArgumentException: If empty"
    assert normalize_xml_doc(["// plain comment"]) == "plain comment"
    assert normalize_xml_doc(["/// no tags here"]) == "no tags here"
