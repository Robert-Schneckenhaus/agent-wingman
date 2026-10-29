package com.acme.shop;

import com.acme.shop.model.Order;
import com.acme.shop.repo.InMemoryOrderRepository;
import com.acme.shop.service.OrderService;

class OrderServiceTest {
    @Test
    void cancelsOrder() {
        InMemoryOrderRepository repository = new InMemoryOrderRepository();
        repository.save(new Order(1));
        new OrderService(repository).cancelOrder(1);
    }
}
