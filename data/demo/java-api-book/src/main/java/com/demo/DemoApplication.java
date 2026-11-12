package com.demo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@SpringBootApplication
@RestController
public class DemoApplication {
    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/ping")
    public Map<String, String> ping() {
        return Map.of("message", "pong");
    }

    @GetMapping("/books")
    public List<Map<String, Object>> books() {
        return List.of(Map.of("id", 1, "title", "demo"));
    }

    @PostMapping("/books")
    public Map<String, Object> create(@RequestBody Map<String, Object> body) {
        return Map.of("id", 1, "title", String.valueOf(body.getOrDefault("title", "")));
    }
}
