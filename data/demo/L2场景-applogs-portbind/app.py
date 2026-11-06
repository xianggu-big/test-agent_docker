"""端口占用验收 demo（黑盒能力补齐阶段1·环境错误臂 B）。

模块导入时先占住 127.0.0.1:8000（=config.L2_SERVICE_PORT 默认值），uvicorn
随后再绑定该端口即 "Address already in use" 退出——服务启动失败路径（provision
fail-fast）的日志分流演示：起不来的日志正是环境类证据，也必须按轮落 app_logs。

不监听连接、不对外服务：socket 只做占位，leg 在 provision 就终止。
"""
import socket

_s = socket.socket()
_s.bind(("127.0.0.1", 8000))
_s.listen(1)

from fastapi import FastAPI  # noqa: E402

app = FastAPI(title="applogs-portbind-demo")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/boom")
def boom():
    return {"status": "never reached"}
