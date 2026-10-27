export interface LineItem {
  description: string;
  amount: number;
  taxRate: number;
}

/** An invoice with line items and an optional discount. */
export class Invoice {
  constructor(public readonly items: LineItem[], public discount = 0) {}

  total(): number {
    return this.items.reduce((sum, item) => sum + item.amount, 0) - this.discount;
  }
}
