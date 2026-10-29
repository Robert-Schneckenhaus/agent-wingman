<?php

namespace App\Repositories;

use App\Models\Order;

class InMemoryOrderRepository implements OrderRepository
{
    private array $orders = [];

    public function find(int $id): Order
    {
        return $this->orders[$id];
    }

    public function save(Order $order): void
    {
        $this->orders[] = $order;
    }
}
