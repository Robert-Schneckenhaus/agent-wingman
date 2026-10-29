package com.acme.shop.service;

import com.acme.shop.model.*;
import com.acme.shop.repo.OrderRepository;
import static com.acme.shop.util.Money.round;

/** Use cases around orders. */
public class OrderService {
    private final OrderRepository repository;

    public OrderService(OrderRepository repository) {
        this.repository = repository;
    }

    public void cancelOrder(long id) {
        Order order = repository.find(id);
        order.cancel();
        this.repository.save(order);
    }

    public double fee(double amount) {
        return round(amount * 0.02);
    }
}
