using Billing.Models;
using Billing.Tax;
using Xunit;

namespace Billing.Tests;

public class TaxCalculatorTests
{
    [Fact]
    public void Breakdown_AppliesDefaultRate()
    {
        var calc = new TaxCalculator();
        var lines = calc.Breakdown(new Invoice());
        Assert.Single(lines);
    }
}
