namespace Billing.Models;

public partial class Invoice
{
    /// <summary>Net total after discount.</summary>
    public decimal NetTotal() => Items.Sum(i => i.Amount) - Discount;
}
