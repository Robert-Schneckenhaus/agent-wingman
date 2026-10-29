package com.acme.shop.web;

import com.acme.shop.service.OrderService;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService service;

    public OrderController(OrderService service) {
        this.service = service;
    }

    @PostMapping("/{id}/cancel")
    public void cancel(@PathVariable long id) {
        service.cancelOrder(id);
    }
}
