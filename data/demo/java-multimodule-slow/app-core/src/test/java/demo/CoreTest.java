package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
public class CoreTest {
    @Test void coreRun() { assertEquals("api:core", new Core().run()); }
    /** G8 慢点（设计稿）：确定性 30s——占 worker 给竞态窗口，离线零插件。 */
    @Test void slowMarker30s() throws InterruptedException { Thread.sleep(30_000L); }
}
