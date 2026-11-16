package example;

import java.util.Locale;

public class OrderService {
    public String normalizeReference(String reference) {
        return reference.strip().toUpperCase(Locale.ROOT);
    }
}
