package com.demo;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class OrderServiceTest {
    @Test
    void tierBoundary() {
        assertEquals("A", new OrderService().getTier(90));
    }

    @Test
    void discount() {
        assertEquals(50.0, new OrderService().applyDiscount(100, 50));
    }
}
