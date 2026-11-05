"""F（任务卡-369 缺陷修复-20261105）回归验证闭环：三态判定 + 验证任务创建幂等。

fail-to-pass 口径（复用归因三闸 Track A / SWT-bench 语义）：
- fixed_verified：原缺陷组成员 nodeid 修复后全 passed
- not_fixed：仍 failed 且签名相同
- regressed/new：验证轮出现原组之外的新签名（如实报新缺陷）
用例冻结：验证任务不跑 gen（阶段二执行图原样）——修复验证不可能被迁就用例洗白。
"""
import pytest

from test_agent import config
from test_agent.skills.verify_fix import create_verify_task, verify_verdict


def _d(bug_id, nodeid, msg):
    return {"bug_id": bug_id, "nodeid": nodeid, "message": msg, "confidence": "中",
            "name": nodeid.split("::")[-1], "basis": "源码推导", "priority": "中",
            "condition": None, "requirement_ref": None, "method": "未标注",
            "expected": "—", "content": "—", "rounds": [1], "status": "failed",
            "level": "L1"}


MSG = "Error: TypeError: Assignment to constant variable\n    at evalmachine.<anonymous>:1:9"
ORIG = [_d("BUG-01", "test_gen_r1.test.js::test_del", MSG),
        _d("BUG-02", "test_gen_r1.test.js::test_add", MSG.replace(":1:9", ":1:33"))]


def test_fixed_verified_when_all_members_pass():
    rows = [{"nodeid": ORIG[0]["nodeid"], "status": "passed", "message": ""},
            {"nodeid": ORIG[1]["nodeid"], "status": "passed", "message": ""}]
    out = verify_verdict(ORIG, rows)
    assert out["overall"] == "fixed_verified"
    assert all(g["verdict"] == "fixed_verified" for g in out["groups"])


def test_not_fixed_when_same_signature_still_fails():
    rows = [{"nodeid": ORIG[0]["nodeid"], "status": "failed", "message": MSG}]
    out = verify_verdict(ORIG, rows)
    assert out["overall"] == "not_fixed"
    g0 = next(g for g in out["groups"] if ORIG[0]["nodeid"] in g["members"])
    assert g0["verdict"] == "not_fixed"


def test_regressed_new_signature_reported_separately():
    new_msg = MSG.replace("constant variable", "undefined property 'x'")
    rows = [{"nodeid": ORIG[0]["nodeid"], "status": "passed", "message": ""},
            {"nodeid": ORIG[1]["nodeid"], "status": "passed", "message": ""},
            {"nodeid": "test_gen_r1.test.js::test_new", "status": "failed",
             "message": new_msg}]
    out = verify_verdict(ORIG, rows)
    assert out["overall"] == "regressed"
    assert len(out["new_signatures"]) == 1
    assert "新失败签名" in out["new_defect_note"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = config  # noqa: F841  noqa 占位保持结构
    from test_agent.store import db as dbmod
    conn = dbmod.get_conn(tmp_path / "t.db")
    dbmod.init_db(tmp_path / "t.db")
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,requirement_text,"
                       "coverage_target,status,confirmed) VALUES(?,?,?,?,?,1)",
                       (pid, uid, "测试添加删除", 0.7, "failed")).lastrowid
    sid = conn.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                       "content,content_hash) VALUES(?,?,?,?,?,?)",
                       (tid, "jest", 1, "test_gen_r1.test.js", "// 原文", "h1")).lastrowid
    conn.execute("INSERT INTO test_cases(script_id,source,name,method,content,enabled) "
                 "VALUES(?,?,?,?,?,1)",
                 (sid, "ai", "test_del", "等价类划分", "// 用例原文"))
    conn.commit()
    yield conn, tid
    conn.close()


def test_create_verify_task_copies_and_links(env):
    conn, tid = env
    vid = create_verify_task(conn, tid)
    assert vid != tid
    v = conn.execute("SELECT * FROM test_tasks WHERE id=?", (vid,)).fetchone()
    assert v["requirement_text"].startswith(f"[回归验证 of task {tid}]")
    assert v["status"] == "ready_for_review" and v["confirmed"] == 1
    n_cases = conn.execute(
        """SELECT COUNT(*) n FROM test_cases tc JOIN test_scripts ts ON ts.id=tc.script_id
           WHERE ts.task_id=?""", (vid,)).fetchone()["n"]
    assert n_cases == 1                              # 用例集复制（enabled 原样）


def test_create_verify_task_idempotent(env):
    conn, tid = env
    v1 = create_verify_task(conn, tid)
    v2 = create_verify_task(conn, tid)
    assert v1 == v2                                  # 未执行验证任务复用，不重复复制
