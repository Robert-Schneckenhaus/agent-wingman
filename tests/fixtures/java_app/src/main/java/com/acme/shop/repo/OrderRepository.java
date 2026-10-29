package com.acme.shop.repo;

import com.acme.shop.model.Order;

public interface OrderRepository {
    Order find(long id);

    void save(Order order);
}
