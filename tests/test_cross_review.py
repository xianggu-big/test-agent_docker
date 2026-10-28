"""生成用例 LLM 交叉语义审（V9，vision §3.6 第 3 道闸）：机制与集成单测（fake LLM 注入）。

锁定四件事：
- 审查者角色分离：审查调用走 CROSS_REVIEW_SYSTEM（≠生成器 SYSTEM_PROMPT 自审走过场）
- fail 单案处置不拖累整批：pass 案原样保留，fail 案单独重造 1 次，复审仍 fail 剔除留台账
- fail-open：审查/复审输出不可解析一律放行（辅助闸不因自身故障废掉整批生成）
- 开关默认关：generate_tests 不产生任何额外 LLM 调用、summary.cross_review 为 None（口径不变）
"""
import json
from pathlib import Path

from test_agent import config
from test_agent.skills import gen_tests as gt
from test_agent.skills.gen_tests import (cross_review_gate, generate_tests,
                                         parse_cross_review)
from test_agent.skills.prompt_library import CROSS_REVIEW_SYSTEM
from test_agent.store import db


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _SeqLLM:
    """按序出队 payload 的 fake LLM；记录每次调用的 (system, user) 供断言。"""

    def __init__(self, payloads: list[str]):
        self.queue = list(payloads)
        self.calls: list[tuple[str, str]] = []

    def invoke(self, messages):
        system, user = messages[0], messages[1]
        self.calls.append((system[1], user[1]))
        return _Resp(self.queue.pop(0))


def _cases() -> list[dict]:
    return [
        {"name": "test_add_basic", "expected": "3", "expected_basis": "需求明示",
         "code": "def test_add_basic():\n    assert add(1, 2) == 3\n"},
        {"name": "test_add_weak", "expected": "未标注",
         "code": "def test_add_weak():\n    assert add(0, 0)\n"},
    ]


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("def add(a, b):\n    return a + b\n",
                                   encoding="utf-8")
    return tmp_path


SRC = [("m.py", "def add(a, b):\n    return a + b\n")]


# ---------- parse_cross_review：鲁棒解析 ----------

def test_parse_ok_extracts_verdicts():
    v = parse_cross_review(json.dumps(
        {"reviews": [{"name": "a", "verdict": "pass", "reason": "r1"},
                     {"name": "b", "verdict": "fail", "reason": "r2"}]}))
    assert v == {"a": {"verdict": "pass", "reason": "r1"},
                 "b": {"verdict": "fail", "reason": "r2"}}


def test_parse_garbage_or_empty_returns_none():
    assert parse_cross_review("这不是 JSON") is None
    assert parse_cross_review('{"reviews": "not-a-list"}') is None
    assert parse_cross_review('{"reviews": []}') is None            # 一条判决都没有 = 没审
    assert parse_cross_review('{"reviews": [{"name": 1, "verdict": "pass"}]}') is None


# ---------- cross_review_gate：单案处置语义 ----------

def test_gate_all_pass_returns_unchanged_with_reviewer_role(tmp_path):
    llm = _SeqLLM([json.dumps({"reviews": [
        {"name": "test_add_basic", "verdict": "pass", "reason": "预期3对应需求"},
        {"name": "test_add_weak", "verdict": "pass", "reason": "边界输入已覆盖"}]})])
    out, info = cross_review_gate(llm, _cases(), "add 应返回两数之和", SRC, _ws(tmp_path), set())
    assert out == _cases() and info["failed"] == [] and info["dropped"] == []
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == CROSS_REVIEW_SYSTEM      # 审查者角色分离（≠生成器自审）
    assert "test_add_basic" in llm.calls[0][1]          # 用例清单进审查提示词


def test_gate_fail_case_regenerated_once_others_untouched(tmp_path):
    regen = json.dumps({"cases": [{"name": "test_add_weak",
                                   "code": "from m import add\ndef test_add_weak():\n"
                                           "    assert add(0, 0) == 0\n"}]})
    llm = _SeqLLM([
        json.dumps({"reviews": [
            {"name": "test_add_basic", "verdict": "pass", "reason": "ok"},
            {"name": "test_add_weak", "verdict": "fail", "reason": "裸真值断言无证伪能力"}]}),
        regen,
        json.dumps({"reviews": [{"name": "test_add_weak", "verdict": "pass", "reason": "已修正"}]}),
    ])
    out, info = cross_review_gate(llm, _cases(), "add 应返回两数之和", SRC, _ws(tmp_path), set())
    assert [c["name"] for c in out] == ["test_add_basic", "test_add_weak"]
    assert out[0] == _cases()[0]                        # pass 案原样保留（不拖累整批）
    assert "assert add(0, 0) == 0" in out[1]["code"]    # fail 案被重造产物替换
    assert info["failed"][0]["name"] == "test_add_weak"
    assert info["regenerated"] == 1 and info["dropped"] == []
    assert len(llm.calls) == 3                          # 审 → 重造 → 复审，恰一轮


def test_gate_regen_fail_again_drops_with_reason(tmp_path):
    regen = json.dumps({"cases": [{"name": "test_add_weak",
                                   "code": "from m import add\ndef test_add_weak():\n"
                                           "    assert add(1, 1)\n"}]})
    llm = _SeqLLM([
        json.dumps({"reviews": [{"name": "test_add_basic", "verdict": "pass"},
                                {"name": "test_add_weak", "verdict": "fail", "reason": "弱断言"}]}),
        regen,
        json.dumps({"reviews": [{"name": "test_add_weak", "verdict": "fail", "reason": "仍迁就实现"}]}),
    ])
    out, info = cross_review_gate(llm, _cases(), "add 应返回两数之和", SRC, _ws(tmp_path), set())
    assert [c["name"] for c in out] == ["test_add_basic"]     # 复审仍 fail → 剔除
    assert info["dropped"] == [{"name": "test_add_weak", "reason": "仍迁就实现"}]


def test_gate_unparsable_review_fails_open(tmp_path):
    llm = _SeqLLM(["审查输出坏了"])
    out, info = cross_review_gate(llm, _cases(), "需求", SRC, _ws(tmp_path), set())
    assert out == _cases() and "fail-open" in info["note"] and len(llm.calls) == 1


def test_gate_regen_unusable_output_drops_original(tmp_path):
    llm = _SeqLLM([
        json.dumps({"reviews": [{"name": "test_add_basic", "verdict": "pass"},
                                {"name": "test_add_weak", "verdict": "fail", "reason": "弱断言"}]}),
        "重造输出不是 JSON",                       # 重造不可解析
    ])
    out, info = cross_review_gate(llm, _cases(), "需求", SRC, _ws(tmp_path), set())
    assert [c["name"] for c in out] == ["test_add_basic"]
    assert info["dropped"][0]["reason"].startswith("重造失败")
    assert len(llm.calls) == 2                        # 重造失败无产物 → 不调复审


def test_gate_regen_syntax_error_rejected_by_static_gate(tmp_path):
    llm = _SeqLLM([
        json.dumps({"reviews": [{"name": "test_add_basic", "verdict": "pass"},
                                {"name": "test_add_weak", "verdict": "fail", "reason": "弱"}]}),
        json.dumps({"cases": [{"name": "test_add_weak", "code": "def broken(:"}]}),
    ])
    out, info = cross_review_gate(llm, _cases(), "需求", SRC, _ws(tmp_path), set())
    assert [c["name"] for c in out] == ["test_add_basic"]
    assert "重造失败" in info["dropped"][0]["reason"]


def test_gate_no_requirement_skips_without_llm_call(tmp_path):
    llm = _SeqLLM([])
    out, info = cross_review_gate(llm, _cases(), "", SRC, _ws(tmp_path), set())
    assert out == _cases() and "跳过" in info["note"] and not llm.calls


# ---------- generate_tests 集成：开关分流 + 台账 ----------

class _ChatFactory:
    """跨实例共享出队序列（generate_tests 内部自行 new ChatOpenAI）。带 usage_metadata 供 llm_calls 记账。"""
    queue: list[str] = []
    n_calls = 0

    def __init__(self, **kw):
        pass

    def invoke(self, messages):
        _ChatFactory.n_calls += 1
        return type("R", (), {"content": _ChatFactory.queue.pop(0),
                              "usage_metadata": {"input_tokens": 10,
                                                 "output_tokens": 5,
                                                 "total_tokens": 15}})()


class _FakeRunner:
    def __init__(self):
        self.client = type("C", (), {"containers": type("CS", (), {"get": staticmethod(lambda n: object())})()})()

    def copy_dir_into(self, container, ws):
        pass

    def exec_in(self, container, cmd, timeout_s=60):
        return type("R", (), {"exit_code": 0, "stdout": "", "stderr": ""})()


def _make_env(tmp_path, monkeypatch):
    # 教训：config.DB_PATH 是模块级常量，monkeypatch DATA_DIR 不影响它——
    # generate_tests 内部 get_conn() 无参，必须直接 patch DB_PATH，否则摸真实库
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    c = db.get_conn(db_path)
    db.init_db(db_path)
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ws = tmp_path / "workspaces" / f"task_{tid}"
    ws.mkdir(parents=True)
    (ws / "m.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    c.commit()   # seed 不 commit：连接持有写事务，log_llm_call 短连接写库撞 database is locked
    monkeypatch.setattr(gt, "ChatOpenAI", _ChatFactory)
    _ChatFactory.queue, _ChatFactory.n_calls = [], 0
    args = {"workspace": str(ws), "requirement": "add 应返回两数之和",
            "module_hint": "python", "container_name": "c1", "module_id": None,
            "coverage_gaps": [], "coverage_target": 0.7, "round_no": 1}
    return c, tid, ws, args


def _gen_payload() -> str:
    return json.dumps({"imports": "", "cases": [
        {"name": "test_add_basic", "method": "等价类划分", "expected": "3",
         "expected_basis": "需求明示",
         "code": "from m import add\ndef test_add_basic():\n    assert add(1, 2) == 3\n"},
        {"name": "test_add_weak", "method": "边界值分析",
         "code": "from m import add\ndef test_add_weak():\n    assert add(0, 0)\n"}]},
        ensure_ascii=False)


def test_generate_switch_off_makes_no_extra_llm_call(tmp_path, monkeypatch):
    """开关关（默认）：只有生成这一次 LLM 调用，summary.cross_review 为 None（口径不变）。"""
    c, tid, ws, args = _make_env(tmp_path, monkeypatch)
    assert not config.GEN_CROSS_REVIEW_ENABLED        # 默认关
    _ChatFactory.queue = [_gen_payload()]
    out = generate_tests("key", args, tid, _FakeRunner())
    s = json.loads(out)
    assert s["generated"] == 2 and s["cross_review"] is None
    assert _ChatFactory.n_calls == 1                   # 出队即空 → 若误调审查会 IndexError 炸测试
    assert "test_gen_r1.py" in s["file"]


def test_generate_switch_on_full_flow_regen_and_ledger(tmp_path, monkeypatch):
    """开关开：审→重造→批量复审全链路，重造产物写盘落库，llm_calls 逐次记账。"""
    monkeypatch.setattr(config, "GEN_CROSS_REVIEW_ENABLED", True)
    c, tid, ws, args = _make_env(tmp_path, monkeypatch)
    _ChatFactory.queue = [
        _gen_payload(),
        json.dumps({"reviews": [
            {"name": "test_add_basic", "verdict": "pass", "reason": "预期3对应需求"},
            {"name": "test_add_weak", "verdict": "fail", "reason": "裸真值无证伪能力"}]},
            ensure_ascii=False),
        json.dumps({"cases": [{"name": "test_add_weak", "method": "边界值分析", "expected": "0",
                               "code": "from m import add\ndef test_add_weak():\n"
                                       "    assert add(0, 0) == 0\n"}]}, ensure_ascii=False),
        # 复审批量审重造案；原名在 used_names 已占位，重造案 def 名带 _2 防撞后缀
        json.dumps({"reviews": [{"name": "test_add_weak_2", "verdict": "pass", "reason": "已修正"}]}),
    ]
    out = generate_tests("key", args, tid, _FakeRunner())
    s = json.loads(out)
    assert s["generated"] == 2
    assert s["cross_review"]["regenerated"] == 1
    assert s["cross_review"]["failed"][0]["name"] == "test_add_weak"
    assert s["cross_review"]["failed"][0]["regen_as"] == "test_add_weak_2"
    assert "assert add(0, 0) == 0" in (ws / "test_gen_r1.py").read_text(encoding="utf-8")
    rows = c.execute("SELECT name FROM test_cases WHERE script_id=? ORDER BY id",
                     (s["script_id"],)).fetchall()
    assert [r["name"] for r in rows] == ["test_add_basic", "test_add_weak_2"]
    phases = [r["phase"] for r in c.execute(
        "SELECT phase FROM llm_calls WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
    assert phases == ["gen_tests", "cross_review", "regen_case", "cross_review"]


def test_generate_switch_on_all_dropped_degrades_honestly(tmp_path, monkeypatch):
    """开关开 + 全部用例复审仍 fail：generated=0 诚实降级，台账留 dropped 理由。"""
    monkeypatch.setattr(config, "GEN_CROSS_REVIEW_ENABLED", True)
    c, tid, ws, args = _make_env(tmp_path, monkeypatch)
    _ChatFactory.queue = [
        _gen_payload(),
        json.dumps({"reviews": [
            {"name": "test_add_basic", "verdict": "fail", "reason": "预期无依据"},
            {"name": "test_add_weak", "verdict": "fail", "reason": "裸真值"}]}, ensure_ascii=False),
        json.dumps({"cases": [{"name": "test_add_basic",
                               "code": "from m import add\ndef test_add_basic():\n"
                                       "    assert add(9, 9)\n"}]}, ensure_ascii=False),
        json.dumps({"cases": [{"name": "test_add_weak",
                               "code": "from m import add\ndef test_add_weak():\n"
                                       "    assert add(0, 0)\n"}]}, ensure_ascii=False),
        # 复审批量审两条重造案（重造是逐案循环、复审一次批量——调用序与实现一致）
        json.dumps({"reviews": [
            {"name": "test_add_basic_2", "verdict": "fail", "reason": "仍迁就"},
            {"name": "test_add_weak_2", "verdict": "fail", "reason": "仍弱"}]}),
    ]
    out = generate_tests("key", args, tid, _FakeRunner())
    s = json.loads(out)
    assert s["generated"] == 0 and "交叉语义审" in s["reason"]
    assert len(s["cross_review"]["dropped"]) == 2
    assert not (ws / "test_gen_r1.py").exists()         # 不写盘
    assert c.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == 0
