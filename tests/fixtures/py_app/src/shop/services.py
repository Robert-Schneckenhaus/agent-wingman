import shop.utils.money as m

from .repository import OrderRepository


class OrderService:
    """Use cases around orders."""

    def __init__(self, repository: OrderRepository):
        self.repository = repository

    def cancel_order(self, order_id: int) -> None:
        order = self.repository.get(order_id)
        order.cancel()
        self.repository.save(order)

    def order_total(self, order_id: int) -> float:
        return m.round_money(self.repository.get(order_id).total())
