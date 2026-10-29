# Billing Guide

How invoices, taxes and discounts work.

## Tax calculation

VAT is computed per line by `TaxCalculator.Breakdown`. The rate defaults to 19 %.
Amounts are rounded with `roundMoney()` before they are shown.
See src/billing/tax.ts for the client side.

```bash
# not a heading: comment inside a code block
wingman index
```

### Reduced rates

Books and food use a reduced rate of 7 %.

## Discounts

A discount is applied before tax. Customers with a framework contract get it automatically.
