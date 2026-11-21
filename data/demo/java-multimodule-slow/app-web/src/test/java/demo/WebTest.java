package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
public class WebTest {
    @Test void webPage() { assertTrue(new Web().page().endsWith(":web")); }
    /** G8 慢点：15s——合计 45s 可预测慢链。 */
    @Test void slowMarker15s() throws InterruptedException { Thread.sleep(15_000L); }
}
