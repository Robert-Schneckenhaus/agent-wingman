<?php

use App\Models\Order;
use App\Repositories\InMemoryOrderRepository;
use App\Services\OrderService;

class OrderServiceTest extends TestCase
{
    public function testCancel(): void
    {
        $repository = new InMemoryOrderRepository();
        $repository->save(new Order());
        (new OrderService($repository))->cancelOrder(1);
    }
}
