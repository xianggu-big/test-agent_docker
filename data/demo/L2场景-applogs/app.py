"""应用日志分流验收 demo（黑盒能力补齐阶段1·埋错臂 A）。

/health 正常返回；/boom 故意抛 RuntimeError——L2 契约轨一打到它，uvicorn 就把
"ERROR: Exception in ASGI application" + Traceback 写进 /work/logs/app.log，
宿主侧按轮快照并确定性解析入库（app_logs），log_analyze 据此引用真实行号。

埋缺陷语义与 L2场景-coupon-api 同款：需求描述预期行为，实现故意违背——
契约/需求断言失败是证人，不是要修的 bug。
"""
from fastapi import FastAPI

app = FastAPI(title="applogs-demo")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/boom")
def boom():
    raise RuntimeError("planted app error: /boom is intentionally broken")
