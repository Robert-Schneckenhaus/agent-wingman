<?php

namespace App\Services;

use App\Repositories\OrderRepository;

/** Use cases around orders. */
class OrderService
{
    public function __construct(private OrderRepository $repository)
    {
    }

    public function cancelOrder(int $id): void
    {
        $order = $this->repository->find($id);
        $order->cancel();
        $this->repository->save($order);
    }
}
