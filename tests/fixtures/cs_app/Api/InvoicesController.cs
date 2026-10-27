using Billing.Models;
using Billing.Tax;
using Microsoft.AspNetCore.Mvc;

namespace Billing.Api;

public abstract class ApiControllerBase : ControllerBase
{
    protected void Audit(string action) { }
}

[ApiController]
[Route("api/invoices")]
public class InvoicesController : ApiControllerBase
{
    private readonly ITaxCalculator _taxCalculator;

    public InvoicesController(ITaxCalculator taxCalculator)
    {
        _taxCalculator = taxCalculator;
    }

    /// <summary>Returns the tax lines of an invoice.</summary>
    /// <param name="id">Invoice id</param>
    [HttpGet("{id}/tax")]
    public IActionResult GetTax(int id)
    {
        Audit("tax");
        var invoice = Load(id);
        var lines = _taxCalculator.Breakdown(invoice);
        var rounded = TaxCalculator.Round(1.234m);
        return Ok(lines);
    }

    private Invoice Load(int id) => new Invoice();
}
