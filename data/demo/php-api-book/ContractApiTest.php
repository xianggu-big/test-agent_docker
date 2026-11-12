<?php
use PHPUnit\Framework\TestCase;

final class ContractApiTest extends TestCase {
    const BASE = "http://127.0.0.1:8080";

    private function http(string $method, string $path, ?array $payload = null): array {
        $ch = curl_init(self::BASE . $path);
        $opts = [CURLOPT_RETURNTRANSFER => true, CURLOPT_CUSTOMREQUEST => $method,
                 CURLOPT_TIMEOUT => 10];
        if ($payload !== null) {
            $opts[CURLOPT_POSTFIELDS] = json_encode($payload);
            $opts[CURLOPT_HTTPHEADER] = ['Content-Type: application/json'];
        }
        curl_setopt_array($ch, $opts);
        $body = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        return [$code, $body];
    }

    public function test_get__ping_契约黑盒(): void {
        [$code] = $this->http("GET", "/ping");
        $this->assertSame(200, $code, "\u5951\u7ea6\u65ad\u8a00[\u63a5\u53e3\u5951\u7ea6] GET /ping");
    }

    public function test_get__books_契约黑盒(): void {
        [$code] = $this->http("GET", "/books");
        $this->assertSame(200, $code, "\u5951\u7ea6\u65ad\u8a00[\u63a5\u53e3\u5951\u7ea6] GET /books");
    }

    public function test_post__books_契约黑盒(): void {
        [$code] = $this->http("POST", "/books", ["title" => "xxxx"]);
        $this->assertSame(200, $code, "\u5951\u7ea6\u65ad\u8a00[\u63a5\u53e3\u5951\u7ea6] POST /books");
    }

}

