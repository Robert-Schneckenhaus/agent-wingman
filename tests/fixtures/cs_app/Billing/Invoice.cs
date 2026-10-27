namespace Billing.Models
{
    /// <summary>An invoice with line items.</summary>
    public partial class Invoice
    {
        public List<LineItem> Items { get; } = new();
        public decimal Discount { get; set; }
    }

    public record LineItem(string Description, decimal Amount, decimal TaxRate);
}
