package com.acme.shop.util;

public final class Money {
    private Money() {}

    /** Rounds to cents. */
    public static double round(double value) {
        return Math.round(value * 100) / 100.0;
    }
}
