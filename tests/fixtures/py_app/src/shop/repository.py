from shop.models import Order


class OrderRepository:
    def __init__(self) -> None:
        self._orders: dict[int, Order] = {}

    def get(self, order_id: int) -> Order:
        return self._orders[order_id]

    def save(self, order: Order) -> None:
        self._orders[order.id] = order
