using Acme.Shared;
using Billing.Models;

namespace Billing.Formatting;

public class InvoiceFormatter
{
    public string FormatTotal(Invoice invoice) => MoneyFormatter.Format(invoice.NetTotal());
}
