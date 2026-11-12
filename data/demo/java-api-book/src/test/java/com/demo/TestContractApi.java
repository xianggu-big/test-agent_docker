import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import java.net.URI;
import java.net.http.*;

public class TestContractApi {
    static final String BASE = "http://127.0.0.1:8080";
    static final HttpClient CLIENT = HttpClient.newHttpClient();

    @Test
    void test_get__ping_契约黑盒() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(BASE + "/ping"))
                .method("GET", HttpRequest.BodyPublishers.noBody())
                .build();
        HttpResponse<String> res = CLIENT.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, res.statusCode(), "契约断言[接口契约] GET /ping");
    }

    @Test
    void test_get__books_契约黑盒() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(BASE + "/books"))
                .method("GET", HttpRequest.BodyPublishers.noBody())
                .build();
        HttpResponse<String> res = CLIENT.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, res.statusCode(), "契约断言[接口契约] GET /books");
    }

    @Test
    void test_post__books_契约黑盒() throws Exception {
        String payload = "{\"title\": \"xxxx\"}";
        HttpRequest req = HttpRequest.newBuilder(URI.create(BASE + "/books"))
                .header("Content-Type", "application/json")
                .method("POST", HttpRequest.BodyPublishers.ofString(payload))
                .build();
        HttpResponse<String> res = CLIENT.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, res.statusCode(), "契约断言[接口契约] POST /books");
    }

}

