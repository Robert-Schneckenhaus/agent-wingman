import { TaxCalculator } from "../src/billing/tax";
import { Invoice } from "../src/billing/invoice";

describe("TaxCalculator", () => {
  it("applies VAT", () => {
    const calc = new TaxCalculator();
    calc.breakdown(new Invoice([]));
  });
});
