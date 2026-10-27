/** Rounds a monetary amount to two decimals. */
export function roundMoney(value: number): number {
  return Math.round(value * 100) / 100;
}

export const VAT_RATE = 0.19;
