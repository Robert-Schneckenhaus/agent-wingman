const { Cart } = require("../src/cart");

describe("Cart totals", () => {
  it("adds VAT", () => {
    const cart = new Cart();
    cart.add({ price: 100 });
    expect(cart.total()).toBe(119);
  });
});
