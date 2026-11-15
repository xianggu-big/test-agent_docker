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


def test_env_blocked_not_not_fixed():
    """407 语义：仍失败但消息含环境特征错（ECONNREFUSED）→ env_blocked，不冤枉修复。"""
    rows = [{"nodeid": ORIG[0]["nodeid"], "status": "failed",
             "message": "Error: 清理失败：connect ECONNREFUSED 127.0.0.1:3306"}]
    out = verify_verdict(ORIG, rows)
    g0 = next(g for g in out["groups"] if ORIG[0]["nodeid"] in g["members"])
    assert g0["verdict"] == "env_blocked" and "ECONNREFUSED" in g0["note"]
    assert out["overall"] in ("env_blocked", "regressed")


def test_mixed_fixed_plus_env_blocked():
    orig = [ORIG[0], dict(ORIG[1],
                          message="Error: SyntaxError: Unexpected token 'const'\n"
                                  "    at evalmachine.<anonymous>:3:1")]
    rows = [{"nodeid": orig[0]["nodeid"], "status": "failed",
             "message": "Error: 清理失败：connect ECONNREFUSED 127.0.0.1:3306"},
            {"nodeid": orig[1]["nodeid"], "status": "passed", "message": ""}]
    out = verify_verdict(orig, rows)
    assert {g["verdict"] for g in out["groups"]} == {"fixed_verified", "env_blocked"}
    assert out["overall"] == "fixed_verified"       # 有真实修复且无 not_fixed/regressed


def test_new_signature_survives_when_env_blocked_present():
    """env_blocked 组存在时新失败签名不得被吞（2026-11-15 闲时找bug 第一轮）。

    旧实现的 new_sigs 过滤条件 `not (env_members & {失败行nodeid})` 与被过滤的 s
    无关——只要存在任一 env_blocked 组，交集必非空 → 所有新签名被吞 →
    overall 从应判 regressed 变成假 fixed_verified（新缺陷静默丢失）。"""
    new_msg = MSG.replace("constant variable", "undefined property 'x'")
    rows = [
        # BUG-01 修复后通过
        {"nodeid": ORIG[0]["nodeid"], "status": "passed", "message": ""},
        # BUG-02 仍失败但为环境特征错 → env_blocked（合法组）
        {"nodeid": ORIG[1]["nodeid"], "status": "failed",
         "message": "Error: 清理失败：connect ECONNREFUSED 127.0.0.1:3306"},
        # 验证轮全新签名失败（真新缺陷）——必须如实报出
        {"nodeid": "test_gen_r1.test.js::test_new", "status": "failed",
         "message": new_msg},
    ]
    out = verify_verdict(ORIG, rows)
    assert out["new_signatures"], "新签名被 env_blocked 组的存在性整体吞掉"
    assert out["overall"] == "regressed"


def test_env_noise_new_signature_still_filtered():
    """env_blocked 成员换了签名的失败（如断言错变成连接错）不算新缺陷——
    修复后仍应被过滤（过滤的本意），与上一条构成双向锁定。"""
    rows = [
        {"nodeid": ORIG[0]["nodeid"], "status": "passed", "message": ""},
        # 原 nodeid 以全新签名（环境错）失败：是 env 噪声不是新缺陷
        {"nodeid": ORIG[1]["nodeid"], "status": "failed",
         "message": "Error: 清理失败：connect ECONNREFUSED 127.0.0.1:3306"},
    ]
    out = verify_verdict(ORIG, rows)
    # 两 ORIG 消息数字归一后同签名 → 同组整体 env_blocked（组员之一修好了也整组待复验）
    assert out["new_signatures"] == []
    assert out["overall"] == "env_blocked"
