export async function fetchCart(id) {
  const response = await fetch(`/api/carts/${id}`);
  return response.json();
}
