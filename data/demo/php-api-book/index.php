<?php
// 最小 PHP REST（F 卡 C4 契约轨/C5 场景轨真机载体）：books 列表 + 创建 + 按 id 读回
// 内置服务器每请求重新执行脚本——存储落临时文件（php -S 单进程，顺序场景读写一致）
header("Content-Type: application/json");
$method = $_SERVER["REQUEST_METHOD"];
$path = parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH);
$storeFile = sys_get_temp_dir() . "/mta_demo_books.json";
$books = is_file($storeFile) ? (json_decode((string)file_get_contents($storeFile), true) ?: []) : [];
if ($path === "/ping" && $method === "GET") {
    echo json_encode(["message" => "pong"]);
    exit;
}
if ($path === "/books" && $method === "GET") {
    echo json_encode($books);
    exit;
}
if ($path === "/books" && $method === "POST") {
    $body = json_decode(file_get_contents("php://input"), true) ?: [];
    $nextId = 1 + ($books ? max(array_column($books, "id")) : 0);
    $b = ["id" => $nextId, "title" => $body["title"] ?? ""];
    $books[] = $b;
    file_put_contents($storeFile, json_encode($books));
    echo json_encode($b);
    exit;
}
if (preg_match("#^/books/(\d+)$#", $path, $m) && $method === "GET") {
    foreach ($books as $b) {
        if ((string)$b["id"] === $m[1]) {
            echo json_encode($b);
            exit;
        }
    }
    http_response_code(404);
    echo json_encode(["error" => "not found"]);
    exit;
}
http_response_code(404);
echo json_encode(["error" => "not found"]);
