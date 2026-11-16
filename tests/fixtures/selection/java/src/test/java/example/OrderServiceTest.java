package example;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class OrderServiceTest {
    @Test
    void normalizesReference() {
        assertEquals("ABC", new OrderService().normalizeReference(" abc "));
    }
}
