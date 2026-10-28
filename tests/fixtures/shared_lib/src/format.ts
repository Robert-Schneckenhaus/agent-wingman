/** Formats an amount of money for display. */
export function formatMoney(value: number, currency = "EUR"): string {
  return `${value.toFixed(2)} ${currency}`;
}
