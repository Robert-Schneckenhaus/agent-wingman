import PdfRenderer from "../export/pdfRenderer";
import { InvoiceModel } from "@shared/index";

export function TaxBadge({ rate }: { rate: number }) {
  return <span>{rate}</span>;
}

export const InvoiceView = ({ invoice }: { invoice: InvoiceModel }) => {
  const renderer = new PdfRenderer();
  const text = renderer.render(invoice);
  return (
    <div>
      <TaxBadge rate={0.19} />
      {text}
    </div>
  );
};
