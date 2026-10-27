import { roundMoney, VAT_RATE } from "@shared/money";
import { Invoice } from "./invoice";

export interface TaxLine {
  rate: number;
  amount: number;
}

export interface ITaxCalculator {
  breakdown(invoice: Invoice): TaxLine[];
}

/**
 * Calculates tax per rate.
 * The discount is subtracted before tax is applied.
 */
export class TaxCalculator implements ITaxCalculator {
  breakdown(invoice: Invoice): TaxLine[] {
    const net = invoice.total();
    return [{ rate: VAT_RATE, amount: roundMoney(net * VAT_RATE) }];
  }
}
