// 最小 JS REST（F 卡 C4 契约轨真机载体）：原生 http 模块，books 列表 + 创建
const http = require("http");

const books = [];
let nextId = 1;

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (url.pathname === "/ping" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ message: "pong" }));
    return;
  }
  if (url.pathname === "/books" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(books));
    return;
  }
  if (url.pathname === "/books" && req.method === "POST") {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      let body = {};
      try { body = JSON.parse(raw || "{}"); } catch (e) { body = {}; }
      const b = { id: nextId++, title: body.title || "" };
      books.push(b);
      res.writeHead(201, { "Content-Type": "application/json" });
      res.end(JSON.stringify(b));
    });
    return;
  }
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "not found" }));
});

server.listen(8080, "127.0.0.1");
console.log("listening on 8080");
