package com.acme.shop.model;

/** A customer order. */
public class Order {
    private final long id;
    private String status = "open";

    public Order(long id) {
        this.id = id;
    }

    public long getId() {
        return id;
    }

    /** Cancels the order unless it was shipped. */
    public void cancel() {
        if ("shipped".equals(status)) {
            throw new IllegalStateException("already shipped");
        }
        status = "cancelled";
    }
}
