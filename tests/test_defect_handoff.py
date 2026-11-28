"""缺陷交接层单测：过滤/合并/置信分层/护栏文本/导出落盘（tmp 库，不碰真实库）。

纪律对应：
- 只收 origin='generated' 的 failed/error（有 expected_basis 证据链才上单）
- 多轮失败合并一条（轮次序列保留，消息取最新轮）
- expected_basis → 置信度分层（需求明示/docstring=高、源码推导=中、常识=低）
- 修复提示词必须含三护栏：禁改断言 / 反驳出口（待人工裁决）/ 验证全绿
- 无缺陷 → export_handoff 返回 None（诚实空手，不造文档）
"""
import json

import pytest

from test_agent.skills.defect_handoff import (confidence_of, export_handoff,
                                              load_defects,
                                              render_defect_report,
                                              render_fix_prompt)
from test_agent.store.db import get_conn, init_db


@pytest.fixture()
def db(tmp_path):
    """最小种子库：1 项目 2 任务；任务 1 有缺陷证据，任务 2 全绿。"""
    path = tmp_path / "handoff.db"
    conn = get_conn(path)
    init_db(path)
    conn.execute("INSERT INTO users(id, username, password_hash, role) VALUES (1,'u','x','admin')")
    conn.execute("INSERT INTO projects(id, user_id, repo_url, name, primary_language) "
                 "VALUES (1, 1, 'local://demo', 'demo', 'python')")
    conn.execute("INSERT INTO test_tasks(id, project_id, user_id, requirement_text, status) "
                 "VALUES (1, 1, 1, '除数非零应报错', 'failed')")
    conn.execute("INSERT INTO test_tasks(id, project_id, user_id, requirement_text, status) "
                 "VALUES (2, 1, 1, '全过任务', 'done')")
    conn.execute("INSERT INTO test_scripts(id, task_id, framework, version, content, content_hash) "
                 "VALUES (11, 1, 'pytest', 1, 'pass', 'h1')")
    conn.execute("INSERT INTO test_scripts(id, task_id, framework, version, content, content_hash) "
                 "VALUES (12, 1, 'pytest', 2, 'pass', 'h2')")
    conn.execute("INSERT INTO test_scripts(id, task_id, framework, version, content, content_hash) "
                 "VALUES (21, 2, 'pytest', 1, 'pass', 'h3')")
    # 用例库：同名两版本（新版本胜出）；优先级/依据分层不同
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content, priority, "
                 "expected, expected_basis) VALUES (11,'ai','test_a','边界值分析','def test_a_v1(): pass','高',"
                 "'抛 ZeroDivisionError','需求明示')")
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content, priority, "
                 "expected, expected_basis) VALUES (12,'ai','test_a','边界值分析','def test_a_v2(): pass','高',"
                 "'抛 ZeroDivisionError（v2）','需求明示')")
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content, priority, "
                 "expected, expected_basis) VALUES (12,'ai','test_b','等价类划分','def test_b(): pass','中',"
                 "'返回 -1','源码推导')")
    conn.execute("INSERT INTO test_cases(script_id, source, name, method, content, priority, "
                 "expected, expected_basis) VALUES (12,'ai','test_c','错误推测法','def test_c(): pass',"
                 "'未标注','不崩溃','常识')")
    # 执行结果：test_a 两轮失败（v2 预期+最新轮消息）；test_b repo 来源不上单；
    # test_c 常识依据仍上单（低置信）；另有一条 passed 不收
    conn.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status, "
                 "message, origin) VALUES (1,1,'L1','t.py::test_a','test_a','failed','old msg','generated')")
    conn.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status, "
                 "message, origin) VALUES (1,2,'L1','t.py::test_a','test_a','failed','new msg','generated')")
    conn.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status, "
                 "message, origin) VALUES (1,2,'L1','t.py::test_b','test_b','failed','repo fail','repo')")
    conn.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status, "
                 "message, origin) VALUES (1,2,'L1','t.py::test_c','test_c','error','boom','generated')")
    conn.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status, "
                 "message, origin) VALUES (1,2,'L1','t.py::test_ok','test_ok','passed',NULL,'generated')")
    # RTM 快照：test_a 挂条件 1
    conn.execute("INSERT INTO rtm_maps(task_id, source, matrix) VALUES (1,'heuristic',?)",
                 (json.dumps([{"condition_id": 1, "condition": "除数为零必须抛错",
                               "priority": "高", "cases": ["test_a"], "status": "gap"}]),))
    conn.commit()
    yield conn
    conn.close()


def test_confidence_of():
    assert confidence_of("需求明示") == "高"
    assert confidence_of("docstring文档") == "高"
    assert confidence_of("源码推导") == "中"
    assert confidence_of("常识") == "低"
    assert confidence_of(None) == "低"
    assert confidence_of("LLM 未标注") == "低"  # 未知措辞按低，不虚高


def test_load_defects_filter_merge(db):
    defects = load_defects(db, 1)
    names = [d["name"] for d in defects]
    assert names == ["test_a", "test_c"]          # repo 来源与 passed 不上单
    a = defects[0]
    assert a["bug_id"] == "BUG-01"
    assert a["rounds"] == [1, 2]                   # 多轮合并，轮次序列保留
    assert a["message"] == "new msg"               # 消息取最新轮
    assert a["content"] == "def test_a_v2(): pass"  # 用例库最新版本胜出
    assert a["condition"] == "除数为零必须抛错"      # RTM 条件挂接
    assert a["confidence"] == "高"


def test_load_defects_sort_by_priority_and_confidence(db):
    defects = load_defects(db, 1)
    assert defects[0]["confidence"] == "高"        # 高优先级+需求明示在前
    assert defects[1]["confidence"] == "低"        # test_c 常识依据仍上单但排后


def test_render_report_fields(db):
    meta = {"task_id": 1, "project": "demo", "repo_url": "local://demo", "language": "python",
            "image": "mta-test:py312", "requirement": "除数非零应报错", "status": "failed",
            "coverage_line": 0.8, "failure_kind": "product_defect", "stop_reason": "证人如实上报",
            "analysis": None}
    text = render_defect_report(meta, load_defects(db, 1))
    for needle in ("# 缺陷报告单（任务 #1）", "疑似产品缺陷 **2** 条", "置信度分布",
                   "BUG-01", "依据：需求明示", "除数为零必须抛错", "局限与免责",
                   "预言误差"):
        assert needle in text, needle


def test_render_fix_prompt_guards(db):
    meta = {"task_id": 1, "project": "demo", "repo_url": "local://demo", "language": "python",
            "image": "mta-test:py312", "requirement": "除数非零应报错", "status": "failed",
            "coverage_line": None, "failure_kind": "product_defect", "stop_reason": None,
            "analysis": {"defects": [{"case": "test_a", "root_cause": "缺零除保护",
                                      "fix": "加 if 分母为零分支", "confidence": "中"}]}}
    text = render_fix_prompt(meta, load_defects(db, 1))
    assert "禁止修改、删除、跳过下列测试用例与断言" in text      # 护栏 1
    assert "预期待人工裁决" in text                              # 护栏 2：反驳出口
    assert "验证全绿" in text                                    # 护栏 3：验证要求
    assert "def test_a_v2(): pass" in text                       # 用例代码在提示词内
    assert "缺零除保护" in text                                  # AI 归因参考已挂接
    assert "行覆盖率 N/A" in text                                # 覆盖缺失不编数字


def test_export_handoff_writes_and_honest_empty(db, tmp_path):
    out = tmp_path / "handoff"
    result = export_handoff(db, 1, out)
    assert result is not None and result["defects"] == 2 and result["high"] == 1
    assert (out / "task_1_缺陷报告单.md").exists()
    assert (out / "task_1_AI修复提示词.md").exists()
    assert export_handoff(db, 2, out) is None     # 全过任务：诚实空手，不造文档


def test_auto_export_writes_files_and_notifies(db, tmp_path):
    """review 节点自动落盘路径：显式目录写两文件 + notify 收到含计数与路径的摘要。"""
    from test_agent.skills.defect_handoff import auto_export_handoff
    notes = []
    out = tmp_path / "handoff"
    built = auto_export_handoff(db, 1, out_dir=out, notify=notes.append)
    assert built is not None and built["defects"] == 2
    assert (out / "task_1_缺陷报告单.md").exists()
    assert (out / "task_1_AI修复提示词.md").exists()
    assert len(notes) == 1 and "[handoff]" in notes[0]
    assert "疑似缺陷 2 条" in notes[0] and "task_1_缺陷报告单.md" in notes[0]


def test_auto_export_default_dir_and_silent_empty(db, tmp_path, monkeypatch):
    """缺省目录=config.DATA_DIR/reports/handoff；无疑似缺陷→None 且不触发 notify（诚实）。"""
    from test_agent import config
    from test_agent.skills.defect_handoff import auto_export_handoff
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    notes = []
    assert auto_export_handoff(db, 2, notify=notes.append) is None   # 全过任务
    assert notes == []
    assert not (tmp_path / "reports" / "handoff" / "task_2_缺陷报告单.md").exists()


def test_render_fix_prompt_clustered_no_crash(db, monkeypatch):
    """开缺陷聚类时 render_fix_prompt 必须可用（2026-11-15 闲时找bug 第一轮）。

    旧实现从 render_defect_report 复制聚类段时漏改变量名 `lines`（本函数作用域只有
    head/clusters/body/last_cluster）——DEFECT_CLUSTERING_ENABLED=1 且存在聚类组时
    首个缺陷即 NameError：/handoff 端点 500、review 节点 auto_export 交付物静默消失。"""
    from test_agent import config as _config
    monkeypatch.setattr(_config, "DEFECT_CLUSTERING_ENABLED", True)
    msg = "Error: TypeError: Assignment to constant variable\n    at evalmachine.<anonymous>:1:9"
    ds = []
    for i, (nid, m) in enumerate(
            [("test_gen_r1.test.js::test_del", msg),
             ("test_gen_r1.test.js::test_add", msg.replace(":1:9", ":1:33"))], 1):
        ds.append({"bug_id": f"BUG-{i:02d}", "name": nid.split("::")[-1], "nodeid": nid,
                   "message": m, "content": f"test('{nid.split('::')[-1]}', () => {{}});",
                   "priority": "高", "basis": "需求明示", "confidence": "高",
                   "rounds": [1], "condition": None, "requirement_ref": None,
                   "method": "边界值分析", "expected": "—", "status": "failed",
                   "level": "L1"})
    meta = {"task_id": 1, "project": "demo", "repo_url": "local://demo",
            "language": "javascript", "image": "mta-test:node20-jest",
            "requirement": "测试添加删除", "status": "failed", "coverage_line": None,
            "failure_kind": "product_defect", "stop_reason": None, "analysis": None,
            "defects_tag": {"failure_kind": "product_defect"}, "conditions": []}
    text = render_fix_prompt(meta, ds)          # 旧代码在此 NameError
    assert "缺陷组" in text                       # 聚类组头进入了交付物
    assert "禁止修改、删除、跳过下列测试用例与断言" in text
    assert "BUG-01" in text and "BUG-02" in text


def test_缺陷清单_无据预期进争议区分账(tmp_path, monkeypatch):
    """阶段 2（2026-11-28）：load_defects 给每条缺陷标 lane——依据为常识/推断且
    预期与需求无可对齐线索 → lane='dispute'（预期存疑待裁决）；其余 'defect'。"""
    from test_agent.skills.defect_handoff import load_defects
    from test_agent import config
    from test_agent.store import db
    dbp = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", dbp)
    c = db.get_conn(dbp)
    db.init_db(dbp)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    req = "优惠券 API：code 长度 2~8、percent 1~100，成功返回 201。"
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,"
                    "confirmed,requirement_text) VALUES(?,?,0.7,'failed',1,?)",
                    (pid, uid, req)).lastrowid
    sid = c.execute("INSERT INTO test_scripts(task_id,framework,version,filename,"
                    "content,content_hash) VALUES(?,'pytest',1,'test_gen_r1.py','c','h')",
                    (tid,)).lastrowid
    # 有据：预期与需求可对齐（code 长度）；无据：惯例断言（重复下单 409——需求未写）
    c.execute("INSERT INTO test_cases(script_id,source,name,content,expected_basis,expected) "
              "VALUES(?,'ai','test_code_下限','c','需求明示','code 长度 2 应返回 201')", (sid,))
    c.execute("INSERT INTO test_cases(script_id,source,name,content,expected_basis,expected) "
              "VALUES(?,'ai','test_repeat_409','c','常识','重复创建同一 code 应返回 409')", (sid,))
    c.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status,"
              " origin) VALUES(?,1,'L1','t::test_code_下限','test_code_下限','failed','generated')",
              (tid,))
    c.execute("INSERT INTO test_case_results(task_id, round_no, level, nodeid, name, status,"
              " origin) VALUES(?,1,'L1','t::test_repeat_409','test_repeat_409','failed','generated')",
              (tid,))
    c.commit()
    ds = load_defects(c, tid)
    lane = {d["name"]: d["lane"] for d in ds}
    assert lane["test_code_下限"] == "defect"
    assert lane["test_repeat_409"] == "dispute", "无据惯例断言必须进争议区"
    c.close()
