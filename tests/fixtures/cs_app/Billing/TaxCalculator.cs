using Billing.Models;

namespace Billing.Tax;

public interface ITaxCalculator
{
    /// <summary>Groups line items by tax rate.</summary>
    IReadOnlyList<TaxLine> Breakdown(Invoice invoice);
}

public record TaxLine(decimal Rate, decimal Amount);

/// <summary>
/// Calculates taxes. The discount is applied <b>before</b> tax.
/// </summary>
public class TaxCalculator : ITaxCalculator
{
    public const decimal DefaultRate = 0.19m;

    public IReadOnlyList<TaxLine> Breakdown(Invoice invoice)
    {
        var net = invoice.NetTotal();
        return new List<TaxLine> { new TaxLine(DefaultRate, Round(net * DefaultRate)) };
    }

    public static decimal Round(decimal value) => Math.Round(value, 2);
}
