package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
public class ApiTest {
    @Test void apiName() { assertEquals("api", new Api().name()); }
}
