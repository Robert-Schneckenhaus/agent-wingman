from fastapi import APIRouter

from shop import OrderService
from shop.repository import OrderRepository

router = APIRouter(prefix="/api/orders")
service = OrderService(OrderRepository())


@router.post("/{order_id}/cancel")
def cancel(order_id: int):
    """Cancel an order via HTTP."""
    service.cancel_order(order_id)
    return {"ok": True}


@router.get("/{order_id}/total")
def total(order_id: int):
    return {"total": service.order_total(order_id)}
