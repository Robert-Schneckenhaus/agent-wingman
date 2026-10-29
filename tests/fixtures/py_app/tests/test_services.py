from shop import Order, OrderService
from shop.repository import OrderRepository


def test_cancel_order():
    repository = OrderRepository()
    repository.save(Order(id=1))
    OrderService(repository).cancel_order(1)
    assert repository.get(1).status == "cancelled"
