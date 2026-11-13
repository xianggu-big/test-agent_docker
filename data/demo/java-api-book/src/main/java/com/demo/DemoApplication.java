package com.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

// 最小 Java REST（F 卡 C4 契约轨/C5 场景轨真机载体）：books 列表 + 创建 + 按 id 读回
@SpringBootApplication
@RestController
public class DemoApplication {
    private final Map<Integer, Map<String, Object>> store = new ConcurrentHashMap<>();
    private final AtomicInteger seq = new AtomicInteger();

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/ping")
    public Map<String, String> ping() {
        return Map.of("message", "pong");
    }

    @GetMapping("/books")
    public List<Map<String, Object>> books() {
        return new ArrayList<>(store.values());
    }

    @PostMapping("/books")
    public Map<String, Object> create(@RequestBody Map<String, Object> body) {
        int id = seq.incrementAndGet();
        Map<String, Object> b = Map.of("id", id, "title", String.valueOf(body.getOrDefault("title", "")));
        store.put(id, b);
        return b;
    }

    @GetMapping("/books/{id}")
    public Map<String, Object> get(@PathVariable Integer id) {
        return store.get(id);
    }
}
