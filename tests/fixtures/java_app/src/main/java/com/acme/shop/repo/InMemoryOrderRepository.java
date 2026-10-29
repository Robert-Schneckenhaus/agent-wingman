package com.acme.shop.repo;

import java.util.HashMap;
import java.util.Map;
import com.acme.shop.model.Order;

public class InMemoryOrderRepository implements OrderRepository {
    private final Map<Long, Order> orders = new HashMap<>();

    @Override
    public Order find(long id) {
        return orders.get(id);
    }

    @Override
    public void save(Order order) {
        orders.put(order.getId(), order);
    }
}
