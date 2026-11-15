"""卡 3 修复锁（2026-11-15）：origin 规范化名匹配 + 收集闸三语言扩展。

实锤一（1138）：go 渲染层跨轮改名（Test→TestR{round}_）后执行名（TestR1_X_方法）
与登记名（test_TestX_方法）失配 → 16 条 AI 用例成绩错标 origin=repo——八指标
3/4 数据前置修复。修=匹配层规范化（剥 test_/轮次标记/Test 前缀小写比对），
登记名不动（RTM/评审显示零影响）。
实锤二（1142）：collect_gate_files 清单只有 python/js 形态——go/java/php 生成
文件不进顺延闸清单 → 闸对三语言空转 → run 时 [build failed] 无人拦截。
修=php 文件级闸（php -l）+ go/java 项目级闸（整组一次，失败整组废案+闸证据留痕）。
"""
from pathlib import Path

import pytest

from test_agent import config
from test_agent.skills.case_results import normalize_case_name, store_case_results
from test_agent.skills.gen_tests import collect_gate_files


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')")
    conn.commit()
    yield conn
    conn.close()


# ---------- ① origin 规范化匹配 ----------

def test_normalize_case_name_forms():
    assert normalize_case_name("test_TestAuthLogin_等价类划分") == \
        normalize_case_name("TestR1_AuthLogin_等价类划分") == "authlogin_等价类划分"
    assert normalize_case_name("TestFb2_CreateX") == normalize_case_name("test_TestCreateX")
    assert normalize_case_name("test_create_album") == "create_album"
    assert normalize_case_name("") == ""


def test_store_origin_go_normalized(tmp_db):
    """go 形态端到端：登记 test_TestX_方法、执行 TestR1_X_方法 → generated（旧=repo）。"""
    pid = tmp_db.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = tmp_db.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,1)",
                         (pid,)).lastrowid
    sid = tmp_db.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash)"
        " VALUES(?,1,1,'TestGenR1_test.go','c','h')", (tid,)).lastrowid
    for n in ("test_TestAuthLogin_等价类划分", "test_TestCreateAlbum_边界值分析"):
        tmp_db.execute("INSERT INTO test_cases(script_id,source,name,content)"
                       " VALUES(?,'ai',?,'code')", (sid, n))
    tmp_db.commit()
    rows = [{"nodeid": f"pkg::{n}", "name": n, "status": "passed"}
            for n in ("TestR1_AuthLogin_等价类划分", "TestR1_CreateAlbum_边界值分析",
                      "Test_logDBExec", "TestAPI")]
    n = store_case_results(tmp_db, tid, 1, rows)
    assert n == 4
    got = {r["name"]: r["origin"] for r in tmp_db.execute(
        "SELECT name, origin FROM test_case_results WHERE task_id=?", (tid,)).fetchall()}
    assert got["TestR1_AuthLogin_等价类划分"] == "generated", "规范化匹配后必须归 AI"
    assert got["TestR1_CreateAlbum_边界值分析"] == "generated"
    assert got["Test_logDBExec"] == "repo" and got["TestAPI"] == "repo", "自带测试不误归"


# ---------- ② collect_gate_files 三语言扩展 ----------

class _R:
    def __init__(self, exit_code, stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, "", stderr


class _FakeRunner:
    """按命令首词分流返回：php -l 失败 / go 项目级闸失败 / mvn 失败可配置。"""

    def __init__(self, fail_prefixes):
        self.fail_prefixes = fail_prefixes   # tuple of first-token strings
        self.commands = []

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        self.commands.append((cmd[0] if cmd else "", " ".join(cmd)[:80]))
        first = cmd[0] if cmd else ""
        joined = " ".join(cmd)
        if first == "sh" and "rm -f" in joined:
            return _R(0)
        if "go build" in joined:
            first = "go-build-gate"
        if any(first.startswith(p) or p in first for p in self.fail_prefixes):
            return _R(1, stderr="compile error: undefined: Fake")
        return _R(0)


def _mk_task(conn) -> int:
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(1,'r','n')").lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id) VALUES(?,1)",
                       (pid,)).lastrowid
    conn.commit()
    return tid


def _seed_script(conn, tid, filename):
    sid = conn.execute(
        "INSERT INTO test_scripts(task_id,framework,version,filename,content,content_hash)"
        " VALUES(?,1,1,?,'c','h')", (tid, filename)).lastrowid
    conn.execute("INSERT INTO test_cases(script_id,source,name,content)"
                 " VALUES(?,'ai','x','code')", (sid,))
    conn.commit()
    return sid


def test_collect_gate_go_project_level(tmp_db, tmp_path):
    """go 生成文件进顺延闸：闸失败 → 整组废案（文件+DB 行删）。旧形态=清单不收 go。"""
    (tmp_path / "TestGenR1_test.go").write_text("package main", encoding="utf-8")
    (tmp_path / "TestGenR2_test.go").write_text("package main", encoding="utf-8")
    tid = _mk_task(tmp_db)
    for f in ("TestGenR1_test.go", "TestGenR2_test.go"):
        _seed_script(tmp_db, tid, f)
    runner = _FakeRunner(fail_prefixes=("go-build-gate",))
    dropped = collect_gate_files(runner, object(), tmp_db, tid, tmp_path)
    assert sorted(dropped) == ["TestGenR1_test.go", "TestGenR2_test.go"], \
        "go 生成文件必须进顺延闸且失败整组废案（旧形态根本不进清单）"
    assert not (tmp_path / "TestGenR1_test.go").exists()
    n = tmp_db.execute("SELECT COUNT(*) AS n FROM test_scripts WHERE task_id=?", (tid,)).fetchone()["n"]
    assert n == 0, "废案必须连带删 DB 行"


def test_collect_gate_go_pass_keeps(tmp_db, tmp_path):
    (tmp_path / "TestGenR1_test.go").write_text("package main", encoding="utf-8")
    tid = _mk_task(tmp_db)
    _seed_script(tmp_db, tid, "TestGenR1_test.go")
    runner = _FakeRunner(fail_prefixes=())
    dropped = collect_gate_files(runner, object(), tmp_db, tid, tmp_path)
    assert dropped == [] and (tmp_path / "TestGenR1_test.go").exists()


def test_collect_gate_php_lint_file_level(tmp_db, tmp_path):
    """php 逐文件闸：语法错文件废案、好文件保留。"""
    (tmp_path / "TestGenR1.php").write_text("<?php echo 1;", encoding="utf-8")
    (tmp_path / "TestGenR2.php").write_text("<?php echo 2;", encoding="utf-8")
    _mk_task(tmp_db)
    _seed_script(tmp_db, 1, "TestGenR1.php")
    _seed_script(tmp_db, 1, "TestGenR2.php")
    # 只让第二个文件失败（按命令内容分流）
    class _Sel(_FakeRunner):
        def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
            r = super().exec_in(container, cmd, workdir, timeout_s)
            if cmd[0] == "php" and "TestGenR2" in " ".join(cmd):
                return _R(1, stderr="Parse error")
            return _R(0)
    dropped = collect_gate_files(_Sel(fail_prefixes=()), object(), tmp_db, 1, tmp_path)
    assert dropped == ["TestGenR2.php"], "php 闸必须逐文件归责"
    assert (tmp_path / "TestGenR1.php").exists() and not (tmp_path / "TestGenR2.php").exists()


def test_collect_gate_java_project_level(tmp_db, tmp_path):
    d = tmp_path / "src" / "test" / "java"
    d.mkdir(parents=True)
    (d / "TestGenR1.java").write_text("class TestGenR1 {}", encoding="utf-8")
    _mk_task(tmp_db)
    _seed_script(tmp_db, 1, "src/test/java/TestGenR1.java")
    runner = _FakeRunner(fail_prefixes=("mvn",))
    dropped = collect_gate_files(runner, object(), tmp_db, 1, tmp_path)
    assert dropped == ["src/test/java/TestGenR1.java"]
    assert not (d / "TestGenR1.java").exists()
