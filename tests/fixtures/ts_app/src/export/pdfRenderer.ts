import * as shared from "@shared/index";
import { TaxCalculator } from "../billing/tax";
import type { Invoice } from "../billing/invoice";

abstract class RendererBase {
  protected log(message: string): void {
    console.log(message);
  }
}

export default class PdfRenderer extends RendererBase {
  private calculator: TaxCalculator = new TaxCalculator();

  render(invoice: Invoice): string {
    this.log("rendering");
    const lines = this.renderTaxLines(invoice);
    return lines.join("\n") + shared.roundMoney(invoice.total());
  }

  /** Renders one line per tax rate (VAT lines). */
  renderTaxLines(invoice: Invoice): string[] {
    return this.calculator.breakdown(invoice).map((l) => `${l.rate}: ${l.amount}`);
  }
}
