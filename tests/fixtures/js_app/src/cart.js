const { roundMoney, VAT_RATE } = require("./money");

/** A shopping cart with line items. */
class Cart {
  constructor() {
    this.items = [];
  }

  add(item) {
    this.items.push(item);
  }

  /** Gross total including VAT. */
  total() {
    const net = this.items.reduce((sum, item) => sum + item.price, 0);
    return roundMoney(net * (1 + VAT_RATE));
  }
}

module.exports = { Cart };
