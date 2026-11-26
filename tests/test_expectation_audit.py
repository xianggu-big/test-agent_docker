# -*- coding: utf-8 -*-
"""C7（2026-11-27 总包）预期依据审计单测：逐条标注与全无据降级判定。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _env(tmp_path):
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "test_agent.db"
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    req = ("会议室预约 API：POST /rooms 创建会议室；预约时长必须在 1~4 小时闭区间内"
           "（否则 400）；同一会议室同一天时段重叠返回 409。")
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,requirement_text) VALUES(?,?,0.7,'running',1,?)",
                    (pid, uid, req)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                    "content,content_hash) VALUES(?,'pytest',1,'test_gen_r1.py','c','h')",
                    (tid,)).lastrowid
    cases = [
        # 植入缺陷 D1（时长边界）——expected 有需求线索 + basis=源码推导 → 有据
        ("test_create_booking_时长4小时", "源码推导", "预约时长 4 小时应创建成功（需求闭区间 1~4 小时）"),
        # 常识预期 + 需求不提 title → 无据（39 字需求实锤同型）
        ("test_create_room_空name应400", "常识", "空 name 应被拒绝且不落库"),
        # 模型推断 + 需求不提 → 无据
        ("test_delete_room_不存在应404", "模型推断", "删除不存在的会议室应给出明确的资源缺失错误"),
    ]
    for name, basis, expected in cases:
        c.execute("INSERT INTO test_cases(script_id,source,name,content,expected_basis,"
                  "expected) VALUES(?,'ai',?,'c',?,?)", (sid, name, basis, expected))
    c.commit()
    return c, tid


def test_逐条标注依据与可追溯(tmp_path):
    from test_agent.skills.expectation_audit import audit_retained_failures
    c, tid = _env(tmp_path)
    stdout = ("FAILED test_gen_r1.py::test_create_booking_时长4小时 - assert\n"
              "FAILED test_gen_r1.py::test_create_room_空name应400 - assert")
    a = audit_retained_failures(c, tid, stdout, failing_nodeids=lambda s: [
        "test_gen_r1.py::test_create_booking_时长4小时",
        "test_gen_r1.py::test_create_room_空name应400"])
    assert a["n_failed"] == 2
    by_name = {r["name"]: r for r in a["rows"]}
    assert by_name["test_create_booking_时长4小时"]["req_traceable"] is True
    assert by_name["test_create_booking_时长4小时"]["unfounded"] is False
    assert by_name["test_create_room_空name应400"]["unfounded"] is True
    assert a["all_unfounded"] is False      # 部分有据 → 维持 product_defect


def test_全无据降级判定(tmp_path):
    from test_agent.skills.expectation_audit import audit_retained_failures
    c, tid = _env(tmp_path)
    stdout = ("FAILED test_gen_r1.py::test_create_room_空name应400\n"
              "FAILED test_gen_r1.py::test_delete_room_不存在应404")
    a = audit_retained_failures(c, tid, stdout, failing_nodeids=lambda s: [
        "test_gen_r1.py::test_create_room_空name应400",
        "test_gen_r1.py::test_delete_room_不存在应404"])
    assert a["n_failed"] == 2 and a["n_unfounded"] == 2
    assert a["all_unfounded"] is True       # → nodes 侧降级 expectation_dispute
