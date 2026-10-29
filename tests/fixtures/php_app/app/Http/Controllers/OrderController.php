<?php

namespace App\Http\Controllers;

use App\Services\OrderService;

class OrderController
{
    public function __construct(private OrderService $orders)
    {
    }

    public function cancel(int $id)
    {
        $this->orders->cancelOrder($id);
        return ['ok' => true];
    }
}
