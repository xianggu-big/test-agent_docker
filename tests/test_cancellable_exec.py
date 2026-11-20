"""0-C 可取消执行测试（2026-11-20 设计稿 §2.3；D7 拍板=全链路包裹）。

对码实锤两层"假超时"：runner.exec_in 的 future.result(timeout) 只停止等待——
容器内进程继续跑还返回伪造 ExecResult；registry 线程池同病。修法两层：
1) exec_in：命令包 coreutils timeout 前缀（进程级真终止，四镜像探针 exit=124 已实测）；
   124/137（TERM/KILL）映射回既有 -1 超时语义与措辞——对外行为逐字节不变，
   stdout 置空与旧假超时一致（防部分输出被 evaluate 正则误计）；
   timeout 本体缺失（127+特征 stderr）诚实降级裸命令重试一次；
2) registry：cancel_event 可选协议——inspect 探测 func 签名，支持则注入，
   FutureTimeout 时 set()（存量 func 签名不变=零破坏）。
铁律：纯函数+假容器+假 skill，零真容器、零 LLM。"""
import threading
import time

from test_agent.skills import registry as reg_mod
from test_agent.skills.registry import Skill, SkillRegistry
from test_agent.sandbox import runner as runner_mod
from test_agent.sandbox.runner import ExecResult


# ---------- exec_in 包裹与映射（纯函数层） ----------

def test_wrap_timeout_prefix():
    w = runner_mod.wrap_timeout(["pytest", "-x"], 300)
    assert w[:4] == ["timeout", "-k", "5", "300"]
    assert w[4:] == ["pytest", "-x"]


class _FakeContainer:
    """记录 exec_run 收到的命令；可脚本化返回码。"""

    def __init__(self, code=0, out=b"", err=b""):
        self.code, self.out, self.err = code, out, err
        self.calls = []

    def exec_run(self, cmd, workdir=None, demux=False):
        self.calls.append(list(cmd))
        return self.code, (self.out, self.err)


def _make_runner():
    return runner_mod.SandboxRunner.__new__(runner_mod.SandboxRunner)  # 跳过 docker 客户端


def test_exec_in_wraps_and_maps_124():
    """真终止 124 → 既有 -1 语义与措辞；stdout 置空（与旧假超时逐字节一致）。"""
    r = _make_runner()
    c = _FakeContainer(code=124, out=b"partial output 3 passed")
    res = r.exec_in(c, ["pytest", "-x"], timeout_s=10)
    assert c.calls[0][:4] == ["timeout", "-k", "5", "10"], "命令必须被 timeout 前缀包裹"
    assert res.exit_code == -1
    assert res.stdout == ""
    assert res.stderr == "timeout after 10s: pytest -x"


def test_exec_in_maps_137():
    r = _make_runner()
    res = r.exec_in(_FakeContainer(code=137), ["sleep", "999"], timeout_s=5)
    assert res.exit_code == -1
    assert "timeout after 5s" in res.stderr


def test_exec_in_normal_path_untouched():
    """非超时码：正常路径原样（exit/解码不变）。"""
    r = _make_runner()
    res = r.exec_in(_FakeContainer(code=1, out=b"3 passed", err=b"1 failed"),
                    ["pytest"], timeout_s=10)
    assert res.exit_code == 1 and res.stdout == "3 passed" and res.stderr == "1 failed"


def test_exec_in_fallback_when_timeout_missing():
    """镜像缺 coreutils timeout（127+特征 stderr）→ 裸命令重试一次。"""
    r = _make_runner()
    c = _FakeContainer(code=127, err=b"/bin/sh: 1: timeout: not found")
    res = r.exec_in(c, ["nonexistent-cmd"], timeout_s=10)
    assert len(c.calls) == 2, "应重试裸命令"
    assert c.calls[1] == ["nonexistent-cmd"]
    assert res.exit_code == 127, "重试结果如实返回（不伪装）"


def test_exec_in_127_without_timeout_signature_no_retry():
    """普通 127（命令真不存在）不重试——只认 stderr 带 timeout 特征的形态。"""
    r = _make_runner()
    c = _FakeContainer(code=127, err=b"bash: nosuchcmd: command not found")
    r.exec_in(c, ["nosuchcmd"], timeout_s=10)
    assert len(c.calls) == 1


def test_exec_in_outer_wait_is_n_plus_15():
    """外层兜底放宽为 timeout_s+15：timeout_s=0 时慢命令（0.6s）仍能正常返回——
    旧代码 wait=timeout_s（0=立即 FutureTimeout 假超时），新契约给真终止留余量。"""
    r = _make_runner()

    class _SlowContainer:
        def exec_run(self, cmd, workdir=None, demux=False):
            time.sleep(0.6)
            return 0, (b"done", b"")

    res = r.exec_in(_SlowContainer(), ["slow"], timeout_s=0)
    assert res.exit_code == 0, f"外层兜底=N+15 下慢命令应正常返回：{res.stderr}"


# ---------- registry cancel_event 协议 ----------

def _make_registry(tmp_path, monkeypatch):
    from test_agent import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    db.init_db()
    conn = db.get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash,role) "
                       "VALUES('u','x','developer')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                       "confirmed,requirement_text) VALUES(?,?,0.7,'ready_for_review',1,'r')",
                       (pid, uid)).lastrowid
    conn.commit()
    return SkillRegistry(conn), conn, tid


def test_cancel_event_injected_and_set_on_timeout(tmp_path, monkeypatch):
    """支持 cancel_event 的 skill：超时被注入 event 且被 set()。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    captured = {}

    def slow_cancelable(task_id, args, cancel_event=None):
        captured["has_event"] = cancel_event is not None
        if cancel_event is not None:
            cancel_event.wait(timeout=1.5)
            captured["set"] = cancel_event.is_set()
        return "never"

    s = Skill(name="slow_cancelable", description="慢技能", func=slow_cancelable,
              timeout_ms=100)
    r = reg.invoke(tid, s, {})
    assert r.status == "timeout"
    time.sleep(1.7)   # 等 skill 线程醒来记录状态
    assert captured.get("has_event") is True
    assert captured.get("set") is True, "FutureTimeout 后 cancel_event 必须 set()"


def test_legacy_func_signature_unchanged(tmp_path, monkeypatch):
    """存量签名 func(task_id, args)：不注入任何 kwargs，行为零变化。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)
    seen = {}

    def legacy(task_id, args):
        seen["args"] = args
        return "ok"

    s = Skill(name="legacy", description="存量形态", func=legacy, timeout_ms=5000)
    assert reg.invoke(tid, s, {"a": 1}).status == "ok"
    assert seen["args"] == {"a": 1}


def test_future_timeout_still_maps(tmp_path, monkeypatch):
    """外层兜底（无 cancel_event 的存量慢 skill）仍走 timeout 语义。"""
    reg, conn, tid = _make_registry(tmp_path, monkeypatch)

    def slow(task_id, args):
        time.sleep(0.8)
        return "late"

    s = Skill(name="slow_legacy", description="慢存量", func=slow, timeout_ms=100)
    r = reg.invoke(tid, s, {})
    assert r.status == "timeout" and "超时" in r.error
