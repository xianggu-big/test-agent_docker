// AI 契约黑盒用例（L2 · JS supertest 形态）
const request = require("supertest");
const BASE = "http://127.0.0.1:8080";

describe("GET /ping", () => {
  it('test_get__ping_契约黑盒', async () => {
    const res = await request(BASE).get("/ping");
    expect(res.status).toBe(200);   // 契约断言[接口契约]
  });
});

describe("GET /books", () => {
  it('test_get__books_契约黑盒', async () => {
    const res = await request(BASE).get("/books");
    expect(res.status).toBe(200);   // 契约断言[接口契约]
  });
});

describe("POST /books", () => {
  it('test_post__books_契约黑盒', async () => {
    const payload = {"title": "xxxx"};
    const res = await request(BASE).post("/books").send(payload);
    expect(res.status).toBe(201);   // 契约断言[接口契约]
  });
});

