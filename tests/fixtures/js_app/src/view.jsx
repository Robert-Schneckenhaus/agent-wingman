import { Cart } from "./cart.js";
import { fetchCart } from "./api.mjs";

export function CartView() {
  const cart = new Cart();
  cart.add({ price: 10 });
  return <div>{cart.total()}</div>;
}
