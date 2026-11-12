<?php
// 最小 PHP REST（F 卡 C4 契约轨真机载体）：books 列表 + 创建
header("Content-Type: application/json");
$method = $_SERVER["REQUEST_METHOD"];
$path = parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH);
if ($path === "/ping" && $method === "GET") {
    echo json_encode(["message" => "pong"]);
    exit;
}
if ($path === "/books" && $method === "GET") {
    echo json_encode([["id" => 1, "title" => "demo"]]);
    exit;
}
if ($path === "/books" && $method === "POST") {
    $body = json_decode(file_get_contents("php://input"), true) ?: [];
    echo json_encode(["id" => 1, "title" => $body["title"] ?? ""]);
    exit;
}
http_response_code(404);
echo json_encode(["error" => "not found"]);
