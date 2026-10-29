/** Rounds an amount to cents. */
function roundMoney(value) {
  return Math.round(value * 100) / 100;
}

const VAT_RATE = 0.19;

module.exports = { roundMoney, VAT_RATE };
