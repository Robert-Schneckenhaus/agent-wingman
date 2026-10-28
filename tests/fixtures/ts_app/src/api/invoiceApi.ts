const baseUrl = "/api";

/** Loads the tax lines of an invoice from the backend. */
export async function fetchInvoiceTax(id: number) {
  const response = await fetch(`${baseUrl}/invoices/${id}/tax`);
  return response.json();
}

export function createInvoice(data: unknown) {
  return axios.post("/api/invoices", data);
}

export function loadCatalogItem(id: number) {
  return axios.get(`${settings.catalogUrl}/items/${id}`);
}
