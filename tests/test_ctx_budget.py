# -*- coding: utf-8 -*-
"""上下文预算管理器 H2/H3（任务卡-H 步骤 3/4）：预算降采样+段落裁剪。

纪律锁：ctx_budget=None 时 build_user_prompt 输出与旧版逐字节一致（云端档零感知）；
降采样排序=入口文件优先；截断必须显式声明（禁止静默裁剪）；裁剪顺序按卡定义。
"""
from test_agent.skills.prompt_library import (build_user_prompt, estimate_tokens,
                                              fit_prompt_sections,
                                              fit_sources_to_budget)

_SRC = [("calc.py", "def add(a, b):\n    return a + b\n")]


# ---------- 估算 ----------

def test_估算系数():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 250) == 113        # chars/2.2（717 真机校准）


# ---------- 对账闸两态（任务卡-H 步骤 2 验收项） ----------

def test_对账闸两态():
    from test_agent.skills.llm_usage import is_context_truncated
    # 未超：8192×0.95=7782，7781 → False
    assert is_context_truncated(7781, ctx_budget=8192) is False
    # 恰好 ≥95% → True（KV 溢出 5 倍掉速边界）
    assert is_context_truncated(7782, ctx_budget=8192) is True
    # 云端档无预算=不限 → 恒 False
    assert is_context_truncated(999999, ctx_budget=None) is False
    assert is_context_truncated(999999, ctx_budget=0) is False


def test_对账闸_台账两态落库(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace as NS
    from test_agent import config
    from test_agent.skills import llm_usage
    from test_agent.store.db import init_db
    p = tmp_path / "t.db"
    init_db(p)
    monkeypatch.setattr(config, "DB_PATH", p)
    conn = sqlite3.connect(p)
    conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')")
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (1, 'r', 'n')")
    conn.execute("INSERT INTO test_tasks(id, project_id, user_id, requirement_text)"
                 " VALUES (1, 1, 1, 'r')")   # FK：llm_calls.task_id → test_tasks
    conn.commit()
    conn.close()
    resp_ok = NS(usage_metadata={"input_tokens": 100, "output_tokens": 5,
                                 "total_tokens": 105})
    resp_over = NS(usage_metadata={"input_tokens": 8000, "output_tokens": 5,
                                   "total_tokens": 8005})
    llm_usage.log_llm_call(1, "gen_tests", resp_ok, model="qwen3-4b", ctx_budget=8192)
    llm_usage.log_llm_call(1, "gen_tests", resp_over, model="qwen3-4b", ctx_budget=8192)
    conn = sqlite3.connect(p)
    rows = conn.execute("SELECT truncated FROM llm_calls WHERE task_id=1"
                        " ORDER BY id DESC LIMIT 2").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 0]           # 超=1，未超=0


def test_台账_model名_未传时取响应元数据(tmp_path, monkeypatch):
    """G 721 实锤：调用点不传 model 时列标签兜底成 DEEPSEEK_MODEL 说谎——
    优先 response_metadata.model_name（路由后真实端点模型）。"""
    import sqlite3
    from types import SimpleNamespace as NS
    from test_agent import config
    from test_agent.skills import llm_usage
    from test_agent.store.db import init_db
    p = tmp_path / "t.db"
    init_db(p)
    monkeypatch.setattr(config, "DB_PATH", p)
    monkeypatch.setattr(config, "DEEPSEEK_MODEL", "deepseek-chat")
    conn = sqlite3.connect(p)
    conn.execute("INSERT INTO users(username, password_hash) VALUES ('u', 'x')")
    conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES (1, 'r', 'n')")
    conn.execute("INSERT INTO test_tasks(id, project_id, user_id, requirement_text)"
                 " VALUES (1, 1, 1, 'r')")
    conn.commit()
    conn.close()
    resp = NS(usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
              response_metadata={"model_name": "qwen3-4b-inst-test"})
    llm_usage.log_llm_call(1, "plan", resp)              # 不传 model
    resp2 = NS(usage_metadata={"input_tokens": 10, "output_tokens": 2,
                               "total_tokens": 12})      # 无元数据：兜底 config
    llm_usage.log_llm_call(1, "plan", resp2)
    resp3 = NS(usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
               response_metadata={"model_name": "qwen3-4b-inst-test"})
    llm_usage.log_llm_call(1, "plan", resp3, model="explicit-model")  # 显式仍优先
    conn = sqlite3.connect(p)
    rows = [r[0] for r in conn.execute("SELECT model FROM llm_calls WHERE task_id=1"
                                       " ORDER BY id").fetchall()]
    conn.close()
    assert rows == ["qwen3-4b-inst-test", "deepseek-chat", "explicit-model"]


# ---------- fit_sources_to_budget ----------

def _mk(n_files: int, chars: int, prefix: str = "mod") -> list[tuple[str, str]]:
    return [(f"{prefix}{i}.py", "x" * chars) for i in range(n_files)]


def test_预算内不动():
    srcs = _mk(3, 500)
    out, decl = fit_sources_to_budget(srcs, 16000)
    assert out == srcs and decl == ""               # 未超预算：零感知


def test_无预算不动():
    srcs = _mk(3, 5000)
    out, decl = fit_sources_to_budget(srcs, None)
    assert out == srcs and decl == ""


def test_超预算_入口文件优先存活():
    srcs = [("utils_deep.py", "u" * 8000), ("main.py", "m" * 6000),
            ("other.py", "o" * 8000)]
    out, decl = fit_sources_to_budget(srcs, 8192)   # 预算≈(8192-2500)*2.5≈14k 字符
    names = [r for r, _ in out]
    assert "main.py" == names[0]                    # 入口文件排首位
    assert len(decl) > 0 and "截断" in decl
    assert "原共" in decl and "你看到的是部分源码" in decl   # 显式声明（禁静默裁剪）


def test_超预算_声明含丢弃清单():
    srcs = [("main.py", "m" * 3000), ("a.py", "a" * 9000), ("b.py", "b" * 9000)]
    out, decl = fit_sources_to_budget(srcs, 4096)
    assert "a.py" in decl and "b.py" in decl        # 丢弃文件留痕
    kept_chars = sum(len(c) for _, c in out)
    assert kept_chars <= 400 + 400                  # 4096<reserve+400：首文件最小段兜底 400+尾标记


def test_全部截断仍给最小源码段():
    srcs = [("big.py", "z" * 50000)]
    out, decl = fit_sources_to_budget(srcs, 4096)
    assert len(out) == 1 and len(out[0][1]) > 400   # 尾段容差：至少留 400 字符
    assert "预算截断" in out[0][0]


# ---------- build_user_prompt 集成 ----------

def test_build_user_prompt_预算内逐字节一致():
    base = build_user_prompt(_SRC, "需求")
    with_budget = build_user_prompt(_SRC, "需求", ctx_budget=16384)
    assert base == with_budget                      # 未超预算：输出不变
    assert "预算截断声明" not in base


def test_build_user_prompt_超预算出现截断声明():
    big = [("big.py", "y" * 60000)]
    p = build_user_prompt(big, "需求", ctx_budget=8192)
    assert "上下文预算截断声明" in p
    assert "部分源码" in p
    # 无预算时同输入不出现声明
    assert "上下文预算截断声明" not in build_user_prompt(big, "需求")


# ---------- fit_prompt_sections（H3 段落裁剪） ----------

def test_段落裁剪_按序砍_留痕():
    prompt = ("HEAD\n" + "## 用例设计方法\n" + "M" * 9000 + "\n"
              + "## 上一轮覆盖缺口\n" + "G" * 9000 + "\n" + "TAIL")
    markers = [("## 用例设计方法", "## 上一轮覆盖缺口"),      # 先砍骨架段
               ("## 上一轮覆盖缺口", "TAIL")]                 # 再砍缺口段
    out, trail = fit_prompt_sections(prompt, 2000, markers)
    assert "用例设计方法" not in out                          # 骨架段被砍
    assert "TAIL" in out                                      # 非目标段存活
    assert "裁段落" in trail


def test_段落裁剪_预算内不动():
    prompt = "HEAD\n## 用例设计方法\nM\nTAIL"
    out, trail = fit_prompt_sections(prompt, 16000, [("## 用例设计方法", "TAIL")])
    assert out == prompt and trail == ""


def test_段落裁剪_全砍仍超_尾部兜底():
    prompt = "X" * 30000
    out, trail = fit_prompt_sections(prompt, 2000, [("NOMARK", "NOMARK")])
    assert len(out) <= 2000 * 2.2 + 1
    assert "头部兜底截断" in trail


def test_段落裁剪_无预算零感知():
    prompt = "X" * 30000
    out, trail = fit_prompt_sections(prompt, None, [("A", "B")])
    assert out == prompt and trail == ""


# ---------- T1 basis 字段补法（治理一致性核对表 T1 ⚠️→✅） ----------

def test_模板展开_basis缺省补齐():
    from test_agent.skills.test_templates import expand_template_cases
    # T2 六点语义要求 min-1/min/min+1/max-1/max/max+1（slots 校验闸）
    case = {"name": "test_div_边界值", "template": "T2",
            "slots": {"mode": "valid", "imports": "from calc import div",
                      "target": "div({v}, 2)",
                      "cases": [{"value": 9, "expected": 4, "label": "min-1"},
                                {"value": 10, "expected": 5, "label": "min"},
                                {"value": 11, "expected": 5, "label": "min+1"},
                                {"value": 19, "expected": 9, "label": "max-1"},
                                {"value": 20, "expected": 10, "label": "max"},
                                {"value": 21, "expected": 10, "label": "max+1"}]}}
    out, rej = expand_template_cases([case])
    assert not rej
    assert out[0]["expected_basis"] == "常识"     # T2 边界值=确定性默认
