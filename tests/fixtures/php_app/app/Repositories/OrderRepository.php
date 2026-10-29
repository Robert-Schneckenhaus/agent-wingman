<?php

namespace App\Repositories;

use App\Models\Order;

interface OrderRepository
{
    public function find(int $id): Order;

    public function save(Order $order): void;
}
