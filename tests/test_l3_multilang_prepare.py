"""F 卡 C5：L3 四语言服务启动接线单测（2026-11-12）。

覆盖：_detect_js_entry 三形态探测 / _start_service_js 命令与就绪语义（假 runner）/
_start_service_php 抽移后行为（假 runner）/ _php_ready_check 轮询脚本自给性。
纪律：不碰真 Docker；python/go 旧路径行为不变由全量回归兜底。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.l2 import (_detect_js_entry, _php_ready_check,
                                 _start_service_js, _start_service_php)


class _ExecRec:
    def __init__(self, exit_code=0, stdout="READY"):
        self.exit_code, self.stdout = exit_code, stdout


class FakeRunner:
    """记录 exec_in 调用；第二次调用（就绪轮询）按 scripted 返回。"""

    def __init__(self, ready=True):
        self.calls = []
        self._ready = ready

    def exec_in(self, container, cmd, timeout_s=None):
        self.calls.append(list(cmd))
        if cmd[:2] == ["python3", "-c"]:
            return _ExecRec(0 if self._ready else 1,
                            "READY" if self._ready else "NOT_READY")
        return _ExecRec(0, "")


# ---------- _detect_js_entry ----------

def test_js_entry_package_json_main_priority(tmp_path):
    (tmp_path / "package.json").write_text('{"main": "src/serve.js"}', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "serve.js").write_text("x", encoding="utf-8")
    # server.js 也存在但 main 优先
    (tmp_path / "server.js").write_text("app.listen(3000)", encoding="utf-8")
    assert _detect_js_entry(tmp_path) == "src/serve.js"


def test_js_entry_listen_feature_detection(tmp_path):
    (tmp_path / "server.js").write_text(
        "const http=require('http');http.createServer().listen(3000);",
        encoding="utf-8")
    assert _detect_js_entry(tmp_path) == "server.js"


def test_js_entry_create_server_no_listen_word(tmp_path):
    (tmp_path / "app.js").write_text(
        "module.exports = http.createServer(handler);", encoding="utf-8")
    assert _detect_js_entry(tmp_path) == "app.js"


def test_js_entry_none_when_no_server(tmp_path):
    (tmp_path / "index.js").write_text("console.log('lib only');", encoding="utf-8")
    assert _detect_js_entry(tmp_path) is None


def test_js_entry_broken_package_json_falls_back(tmp_path):
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "index.js").write_text("x.listen(1)", encoding="utf-8")
    assert _detect_js_entry(tmp_path) == "index.js"


# ---------- _start_service_js ----------

def test_start_service_js_starts_and_waits():
    r = FakeRunner(ready=True)
    ok, why = _start_service_js(r, "c", "server.js", 8123)
    assert ok and why == ""
    assert r.calls[0][:2] == ["sh", "-c"]
    assert "nohup node server.js" in r.calls[0][2]
    assert r.calls[1][:2] == ["python3", "-c"]          # TCP 就绪轮询
    assert "8123" in r.calls[1][2]


def test_start_service_js_not_ready_reports_failure():
    r = FakeRunner(ready=False)
    ok, why = _start_service_js(r, "c", "app.js", 8123)
    assert not ok and "未就绪" in why


# ---------- _start_service_php（l2 抽移：行为等价锁） ----------

def test_start_service_php_commands_unchanged():
    r = FakeRunner(ready=True)
    ok, why = _start_service_php(r, "c", 8000)
    assert ok and why == ""
    assert "php -S 127.0.0.1:8000 index.php" in r.calls[0][2]
    assert r.calls[1][:2] == ["python3", "-c"]
    assert "READY" in r.calls[1][2]


def test_php_ready_check_script_self_contained():
    import subprocess
    src = _php_ready_check(9999)
    # 语法自给性：编译通过（端口未开，不真跑轮询）
    compile(src, "<php_ready_check>", "exec")
    assert "urllib.request" in src and "9999" in src


def test_php_ready_check_404也算就绪():
    # C5 真机实锤（2026-11-13）：api 型项目根路径 404 → 旧脚本 HTTPError 被当未就绪，
    # 服务永远起不来。真行为验证：本地 404 服务器上轮询脚本必须 READY 退出 0。
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _NotFound(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _NotFound)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run([sys.executable, "-c", _php_ready_check(port)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "READY" in r.stdout, f"404 应被判定就绪：{r.stdout}{r.stderr}"
    finally:
        srv.shutdown()

