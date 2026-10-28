import { formatMoney } from "@acme/shared";
import { Invoice } from "../billing/invoice";

/** One-line summary of an invoice. */
export function invoiceSummary(invoice: Invoice): string {
  return formatMoney(invoice.total());
}
