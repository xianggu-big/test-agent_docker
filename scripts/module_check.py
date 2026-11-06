"""全系统机制面完成度巡检与边界探针（2026-10-28 三方案轮创建；第十一轮起契约扩至全机制面）。

用法：python scripts/module_check.py [--full]
三段式：
  PART1 完成度 = 静态契约检查（开关/函数/提示词段/集成点/仪器锚点逐一核对；
       第十一轮补 L5 表契约（现 22 表）/消融开关默认值/V8V9 机制函数/重跑幂等与台账语义/
       评估链单快照/X 格消融入口/阅卷 decoy 协议/新机制单测文件）；
  PART2 单测回归 = 三方案机制单测 + review 节点 + 四轮新机制单测文件；
  PART3 边界探针 = 单测未覆盖的边界输入逐个喂给方案A/B/C核心函数与变异算子，
       每条探针结论三态：PASS（符合契约）/ FAIL（契约违约，exit 1）/ FINDING（可辩护的
       边界行为，记录不动死刑，留给 v2 决策）。
--full 额外跑全量 pytest 回归（299 过 1 跳基线）。

诚实边界：本脚本离线确定性（不触网/不依赖 Docker/不调 LLM）；方案B 的容器侧
hypothesis 安装只做源码契约检查（真容器行为由 run_grid B1 格真机实验覆盖）。
报告落 data/reports/module_check_<ts>.json（运行产物，不入库）。
"""
import ast
import importlib.util
import inspect
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config
from test_agent.graph import builder as builder_mod  # noqa: E402
from test_agent.skills import ai_reviewer as ai_reviewer_mod  # noqa: E402
from test_agent.skills import scenario_gen as scenario_gen_mod  # noqa: E402
from test_agent.skills import (builtin, callgraph, contract_gen, endpoint_extract,
                               gen_tests, llm_usage, prompt_library,
                               traceability)  # noqa: E402

PASS, FAIL, FINDING = "PASS", "FAIL", "FINDING"


@dataclass
class Item:
    name: str
    status: str          # OK / MISS / PASS / FAIL / FINDING
    detail: str = ""


def part1_completion() -> list[Item]:
    """静态契约：今日该有的构件逐一核对（存在性 + 关键内容）。"""
    out: list[Item] = []

    def ok(name: str, detail: str = ""):
        out.append(Item(name, "OK", detail))

    def miss(name: str, detail: str):
        out.append(Item(name, "MISS", detail))

    # 1) config 三开关，默认 False（保证基线格不受污染）
    for key in ("ASSERTION_GATE_ENABLED", "PBT_ENABLED", "COND_FEEDBACK_ENABLED"):
        v = getattr(config, key, None)
        (ok if v is False else miss)(f"config.{key}", f"默认值={v}（应为 False）")

    # 2) 方案A/B/C 核心函数存在
    for fn in ("assertion_audit", "apply_assertion_gate", "_condition_gaps",
               "refill_condition_gaps"):
        (ok if callable(getattr(gen_tests, fn, None)) else miss)(
            f"gen_tests.{fn}", "缺失" if not callable(getattr(gen_tests, fn, None)) else "")

    # 3) 提示词库：PBT 段 + build_user_prompt 新参数
    has_pbt = getattr(prompt_library, "PBT_SECTION", None)
    (ok if has_pbt else miss)("prompt_library.PBT_SECTION", "缺失")
    sig = inspect.signature(prompt_library.build_user_prompt)
    for p in ("pbt", "cond_forced", "existing_cases"):
        (ok if p in sig.parameters else miss)(
            f"build_user_prompt({p}=...)", "签名缺少该参数")

    # 4) 方案B 容器侧：install_deps 受 PBT_ENABLED 约束装 hypothesis（源码契约）
    src_builtin = inspect.getsource(builtin.build_skills)
    if "hypothesis" in src_builtin and "PBT_ENABLED" in src_builtin:
        ok("builtin.install_deps → hypothesis", "源码契约在位")
    else:
        miss("builtin.install_deps → hypothesis", "源码未引用 hypothesis/PBT_ENABLED")

    # 5) generate_tests 集成三方案（生成主链路挂点）
    src_gen = inspect.getsource(gen_tests.generate_tests)
    for key in ("ASSERTION_GATE_ENABLED", "PBT_ENABLED", "COND_FEEDBACK_ENABLED"):
        (ok if key in src_gen else miss)(
            f"generate_tests 集成 {key}", "主链路未挂该方案")

    # 6) 变异仪器契约：tmp 固定 D 盘（C 盘 Temp junction 坑）+ 核心函数 + 变异上限
    mk = _load("mutation_kill.py")
    if mk is None:
        miss("scripts/mutation_kill.py", "无法加载")
    else:
        mk_src = (ROOT / "scripts" / "mutation_kill.py").read_text(encoding="utf-8")
        (ok if "tmp_mut" in mk_src else miss)("mutation_kill tmp 固定 data/tmp_mut",
                                             "C 盘 junction 修复不在位")
        (ok if mk.MAX_MUTANTS == 12 else miss)("mutation_kill MAX_MUTANTS",
                                               f"={mk.MAX_MUTANTS}（应为 12）")
        for fn in ("_candidates", "_mutate", "source_modules"):
            (ok if callable(getattr(mk, fn, None)) else miss)(f"mutation_kill.{fn}", "缺失")

    # 7) run_grid 三方案格（文本契约，不 import——其顶层 import graph 链路是重依赖）
    rg = (ROOT / "scripts" / "run_grid.py").read_text(encoding="utf-8")
    for cell, key in (("A1", "ASSERTION_GATE_ENABLED"), ("B1", "PBT_ENABLED"),
                      ("C1", "COND_FEEDBACK_ENABLED")):
        (ok if f'"{cell}": {{"{key}": True}}' in rg else miss)(
            f"run_grid.GRID[{cell}]", f"应为 {{{key}: True}}")

    # 8) 今日其他产物在位（阅卷根修/review 终态恢复/聚合脚本的静态锚点）
    for p in ("scripts/grade_corpus.py", "scripts/aggregate_grid.py",
              "scripts/compare_proposals.py", "tests/test_review_node.py"):
        (ok if (ROOT / p).exists() else miss)(p, "文件缺失")

    # 11b) E6 消融三开关（S0：默认 True，L2E1/L2E2/L2E3 格逐个关）
    for key in ("L2_BLACKBOX_ENABLED", "L2_WHITEBOX_ENABLED", "TRACE_ENABLED"):
        v = getattr(config, key, None)
        (ok if v is True else miss)(f"config.{key}", f"默认值={v}（应为 True）")

    # 12) 2026-10-31 质检修复+自主循环新机制（loop-1/2/6/8/9：judge_l2/容器复核/URL 校验/
    #     收集错误容忍/生成物限定）
    _nodes_src = inspect.getsource(__import__("test_agent.graph.nodes",
                                          fromlist=["build_run_cmd"]))
    (ok if callable(getattr(__import__("test_agent.graph.l2", fromlist=["judge_l2"]),
                            "judge_l2", None)) else miss)("l2.judge_l2 纯函数", "缺失")
    (ok if "--continue-on-collection-errors" in inspect.getsource(
        __import__("test_agent.graph.nodes", fromlist=["build_run_cmd"]).build_run_cmd)
     else miss)("build_run_cmd 收集错误容忍", "缺 flag")
    _nodes_mod = __import__("test_agent.graph.nodes", fromlist=["_validate_repo_url"])
    (ok if callable(getattr(_nodes_mod, "_validate_repo_url", None))
     else miss)("nodes._validate_repo_url", "缺失")
    _runner_src = inspect.getsource(__import__("test_agent.sandbox.runner",
                                               fromlist=["SandboxRunner"]).SandboxRunner)
    (ok if "销毁重建" in _runner_src else miss)("ensure_container 死容器重建", "缺复核语义")
    _sgen_src = inspect.getsource(__import__("test_agent.skills.scenario_gen",
                                             fromlist=["apply_smoke_results"]).apply_smoke_results)
    (ok if "duplicate_titles_manual" in _sgen_src else miss)("apply_smoke_results 同名转人工", "缺防线")

    # 9) 机制单测文件在位
    (ok if (ROOT / "tests/test_design_proposals.py").exists()
     else miss)("tests/test_design_proposals.py", "文件缺失")

    # —— 第十一轮补齐：以下为第四至十轮新机制的静态契约（此前契约集合停在 2026-10-28 三方案轮）——

    # 10) L5 表契约：28 表齐备（15-18 rtm_maps/evaluation_rounds/complexity_hotspots/llm_calls
    #     + 19-22 四级双轨：test_levels/interface_endpoints/call_edges/scenario_cases
    #     + 23 review_decisions：AI 代审/人工审查决策台账，决策必须结构化+可解释
    #     + 24 test_case_results：逐用例执行结果，实时执行看板数据源（任务C/2026-11-01）
    #     + 25 exec_queue：执行队列（B1：单 worker 串行消费，并发下发不再互挤）
    #     + 28 app_logs：应用运行时日志分档（黑盒能力补齐阶段1，log_analyze 证据真源））
    ddl = config.SCHEMA_PATH.read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", ddl))
    expected_tables = {
        "users", "projects", "modules", "test_tasks", "agent_sessions", "agent_messages",
        "skills", "skill_invocations", "container_jobs", "test_scripts", "test_cases",
        "test_conditions", "test_reports", "feedback_annotations", "rtm_maps",
        "evaluation_rounds", "complexity_hotspots", "llm_calls",
        "test_levels", "interface_endpoints", "call_edges", "scenario_cases",
        "review_decisions", "test_case_results", "exec_queue", "image_recipes", "login_events",
        "app_logs"}
    (ok if created == expected_tables else miss)(
        "L5 schema 28 表契约",
        "28 表齐备" if created == expected_tables else
        f"缺失={sorted(expected_tables - created)} 多余={sorted(created - expected_tables)}")
    tasks_ddl = re.search(r"CREATE TABLE IF NOT EXISTS test_tasks\s*\(.*?\n\)", ddl, re.S)
    (ok if tasks_ddl and "ready_for_review" in tasks_ddl.group(0) else miss)(
        "test_tasks.status 含 ready_for_review（两阶段停等态）", "缺失")
    lvl_col = re.search(r"CREATE TABLE IF NOT EXISTS evaluation_rounds\s*\(.*?\n\)", ddl, re.S)
    (ok if lvl_col and "level" in lvl_col.group(0) else miss)(
        "evaluation_rounds.level 列", "级别维度在位" if lvl_col else "DDL 缺失")

    # 11) 消融开关默认值契约：新机制默认关（基线格零污染、老口径逐字节不变）；
    #     骨架四开关默认开（系统默认行为）。A/B/C 三开关默认值见上方第 1 组。
    for key in ("GEN_BOUNDARY_ATTACK", "WITNESS_CONTINUE_ENABLED", "GEN_CROSS_REVIEW_ENABLED",
                "STATE_TRANSITION_ENABLED", "METAMORPHIC_ENABLED", "AUTO_PREPARE_ENV",
                "LEVELS_ENABLED", "LEVELS_L3_ENABLED", "LEVELS_L4_ENABLED",
                "USAGE_EXAMPLES_ENABLED", "PITFALL_KB_ENABLED"):
        v = getattr(config, key, None)
        (ok if v is False else miss)(f"config.{key}", f"默认值={v}（应为 False）")
    for name in ("parse_scenarios", "confirm_scenario", "l3_metrics", "apply_smoke_results",
                 "parse_acceptance", "render_acceptance_module", "l4_metrics"):
        (ok if callable(getattr(scenario_gen_mod, name, None)) else miss)(
            f"scenario_gen.{name}", "L3/L4 纯函数在位")
    for key in ("GEN_METHODS_ENABLED", "PLAN_ENABLED", "COVERAGE_FEEDBACK_ENABLED",
                "TEMPLATES_ENABLED"):
        v = getattr(config, key, None)
        (ok if v is True else miss)(f"config.{key}", f"默认值={v}（应为 True）")

    # 12) 第四至九轮机制函数 + V9 角色分离锚点（审查者 ≠ 生成器提示词）
    for mod, fn in ((llm_usage, "extract_usage"), (llm_usage, "log_llm_call"),
                    (gen_tests, "cross_review_gate"), (builtin, "probe_deps_manifest"),
                    (endpoint_extract, "probe_endpoint_source"), (endpoint_extract, "parse_openapi"),
                    (callgraph, "build_call_edges"), (callgraph, "sequence_scenarios"),
                    (contract_gen, "render_contract_module"), (contract_gen, "_safe_expr")):
        (ok if callable(getattr(mod, fn, None)) else miss)(
            f"{fn}（{mod.__name__}）", "缺失")
    # 四级双轨 P1 锚点：L2 skill 注册 + level_supervisor 接力 + demo 项目可提取
    src_nodes = inspect.getsource(__import__("test_agent.graph.nodes", fromlist=["make_deps"]).make_deps)
    (ok if "build_l2_skills" in src_nodes else miss)(
        "L2 skill 注册（make_deps 引 build_l2_skills）", "未注册")
    (ok if (config.ROOT / "test_agent" / "graph" / "levels.py").exists() else miss)(
        "level_supervisor（graph/levels.py）", "缺失")

    # D8 轮机制锚点（2026-10-30）：模型漂移容错解析 / 阅卷语义修正 / 级别过滤
    (ok if callable(getattr(gen_tests, "_loads_tolerant", None)) else miss)(
        "gen_tests._loads_tolerant（JSON 内 Python 字面量宽容解析）", "缺失")
    _gc = __import__("scripts.grade_corpus", fromlist=["witnesses_of"])         if (config.ROOT / "scripts" / "grade_corpus.py").exists() else None
    src_wit = inspect.getsource(_gc.witnesses_of) if _gc else ""
    (ok if 'split("[", 1)' in src_wit else miss)(
        "grade_corpus 参数化 nodeid 剥后缀（witnesses_of）", "缺失")
    src_grade = inspect.getsource(_gc.grade) if _gc else ""
    (ok if "套件全过" in src_grade and "recall\": 0.0" in src_grade or 'recall": 0.0' in src_grade
        or "recall=0.0" in src_grade else miss)(
        "grade_corpus 套件全过=recall 0.0 语义", "缺失")
    (ok if "recall\": 0.0" in src_grade or "recall=0.0" in src_grade
        or 'recall": 0.0' in src_grade else miss)(
        "grade_corpus 套件全过=recall 0.0 语义", "缺失")
    src_nodes_lvl = inspect.getsource(__import__("test_agent.graph.nodes",
                                                 fromlist=["make_nodes"]).make_nodes)
    (ok if "AND level='L1'" in src_nodes_lvl else miss)(
        "L1 evaluate 轮次删除级别过滤", "缺失")
    (ok if callable(getattr(gen_tests, "_extract_api_docs", None)) else miss)(
        "gen_tests._extract_api_docs（C源 docstring grounding）", "缺失")
    (ok if callable(getattr(gen_tests, "_generate_seed", None)) else miss)(
        "gen_tests._generate_seed（种子自举）", "缺失")
    (ok if getattr(config, "SEED_BOOTSTRAP_ENABLED", None) is False else miss)(
        "config.SEED_BOOTSTRAP_ENABLED 默认关", "缺失")
    (ok if getattr(config, "DOCSTRING_GROUNDING_ENABLED", None) is False else miss)(
        "config.DOCSTRING_GROUNDING_ENABLED 默认关", "缺失")
    (ok if callable(getattr(gen_tests, "_extract_usage_examples", None))
     and callable(getattr(gen_tests, "rebuild_gen_files_from_db", None))
     and callable(getattr(gen_tests, "collect_gate_files", None)) else miss)(
        "gen_tests A源提取+布考场重建+顺延收集闸", "缺失")
    (ok if getattr(prompt_library, "PITFALL_KB", None)
     and callable(getattr(prompt_library, "select_pitfalls", None))
     and callable(getattr(prompt_library, "build_pitfall_section", None)) else miss)(
        "prompt_library PITFALL_KB（B源陷阱库）", "缺失")

    # 12b) 两阶段人机协作 + AI 代审（2026-10-31）：两图入口/状态机/代审机制/角色分离/红线
    (ok if callable(getattr(builder_mod, "build_prep_graph", None))
     and callable(getattr(builder_mod, "build_exec_graph", None)) else miss)(
        "builder 两图入口（prep/exec）", "缺失")
    (ok if all(callable(getattr(builder_mod, n, None)) for n in
               ("run_task_prep", "run_task_exec", "run_task_auto", "run_feedback_round")) else miss)(
        "两阶段+代审入口（prep/exec/auto/feedback）", "缺失")
    src_builder = inspect.getsource(builder_mod)
    (ok if "two_stage_rebuild" in src_builder else miss)(
        "布考场按审查结论重写（two_stage_rebuild 钩子）", "缺失")
    (ok if all(callable(getattr(ai_reviewer_mod, n, None)) for n in
               ("run_review_once", "run_d1_review", "validate_decisions",
                "witness_protected_names", "spotcheck_sample", "record_spotcheck")) else miss)(
        "ai_reviewer 机制函数在位", "缺失")
    src_ar = inspect.getsource(ai_reviewer_mod)
    (ok if "证人保护" in src_ar else miss)(
        "AI 代审证人保护红线（AI 无权删 product_defect 证人）", "缺失")
    sys_rev = getattr(prompt_library, "REVIEWER_SYSTEM", "")
    sys_d1 = getattr(prompt_library, "D1_SYSTEM", "")
    sys_base = getattr(prompt_library, "SYSTEM_PROMPT", "")
    (ok if sys_base and sys_rev and sys_d1 and len({sys_base, sys_rev, sys_d1}) == 3 else miss)(
        "代审角色分离（D1/REVIEWER ≠ 生成器 SYSTEM，三份独立提示词）", "缺失")
    (ok if getattr(config, "AI_REVIEWER_TOKEN_BUDGET", 0) > 0 else miss)(
        "代审 token 停车线", "缺失")
    (ok if getattr(config, "FEEDBACK_MAX_ROUNDS", 0) == 5 else miss)(
        "对话增补预算 5 轮", "缺失")

    # 12c) 多语言分级（2026-11-01 用户拍板）：Tier3 静态扫描兜底 + JS 执行链路 Tier2
    from test_agent.skills import js_adapter as js_adapter_mod, static_scan as static_scan_mod
    (ok if all(callable(getattr(static_scan_mod, n, None)) for n in
               ("scan_workspace", "generate_static_drafts", "parse_drafts", "_static_scan_skill")) else miss)(
        "static_scan 机制函数（Tier3 扫描+草稿）", "缺失")
    (ok if callable(getattr(js_adapter_mod, "jest_json_to_pytest_text", None)) else miss)(
        "js_adapter（jest→pytest 文本，下游零改动）", "缺失")
    src_nodes_tier = inspect.getsource(__import__("test_agent.graph.nodes",
                                                  fromlist=["make_nodes"]).make_nodes)
    (ok if 'strategy = "full" if primary in ("python", "javascript") else "static_only"' in src_nodes_tier
     else miss)("分级名单 full=python/javascript（java 移出假 full，typescript 暂静态）", "漂移")
    (ok if "static_draft" in src_nodes_tier and "[static-draft]" in src_nodes_tier else miss)(
        "static_draft 节点在位", "缺失")
    exec_langs = getattr(config, "EXEC_LANGUAGES", {})
    (ok if exec_langs.get("javascript") == getattr(config, "IMAGE_JS", None)
     and "typescript" not in exec_langs else miss)("EXEC_LANGUAGES 镜像映射契约", "漂移")
    (ok if (config.ROOT / "docker" / "js-test" / "Dockerfile").exists() else miss)(
        "docker/js-test/Dockerfile", "缺失")
    state_src = (config.ROOT / "test_agent" / "graph" / "state.py").read_text(encoding="utf-8")
    (ok if "static_only: bool" in state_src else miss)(
        "TaskState.static_only 声明（LangGraph 按 schema 过滤未声明键——真机实锤）", "缺失")
    (ok if "[evaluate·failed] FAILED" in src_nodes_tier else miss)(
        "evaluate per-case FAILED 行进日志（/defects 数据源）", "缺失")
    demo_spec = config.ROOT / "data" / "demo" / "L2场景-coupon-api" / "openapi.json"
    (ok if demo_spec.exists() else miss)("L2 冒烟 demo 项目（openapi.json）", "缺失")
    sys_gen = getattr(prompt_library, "SYSTEM_PROMPT", None)
    sys_cross = getattr(prompt_library, "CROSS_REVIEW_SYSTEM", None)
    (ok if (sys_gen and sys_cross and sys_gen != sys_cross) else miss)(
        "V9 角色分离（SYSTEM_PROMPT ≠ CROSS_REVIEW_SYSTEM）",
        "生成器与交叉审必须是两份不同系统提示词（同模型角色分离口径的根基）")

    # 13) 跨级 RTM 溯源链（P2，2026-10-30）：链函数/DDL 位置列/API 入口/语料与标定入口
    for fn in ("build_trace_chain", "build_task_traces", "route_key", "script_level"):
        (ok if callable(getattr(traceability, fn, None)) else miss)(
            f"traceability.{fn}", "缺失")
    (ok if callable(getattr(scenario_gen_mod, "inject_body_refs", None)) else miss)(
        "scenario_gen.inject_body_refs（正例外键占位符注入）", "缺失")
    ce_cols = re.search(r"CREATE TABLE IF NOT EXISTS call_edges\s*\(.*?\n\)", ddl, re.S)
    (ok if ce_cols and "file" in ce_cols.group(0) and "lineno" in ce_cols.group(0) else miss)(
        "call_edges.file/lineno（溯源代码位置列）", "DDL 缺失")
    src_server = (config.ROOT / "test_agent" / "api" / "server.py").read_text(encoding="utf-8")
    (ok if '"/api/tasks/{tid}/trace"' in src_server and "build_task_traces" in src_server
        else miss)("GET /api/tasks/{tid}/trace 入口", "缺失")
    (ok if (config.ROOT / "tests" / "test_traceability.py").exists() else miss)(
        "tests/test_traceability.py", "缺失")
    c3 = config.ROOT / "data" / "corpus" / "corpus-003"
    (ok if (c3 / "ground_truth.json").exists() and (c3 / "project" / "openapi.json").exists()
        else miss)("corpus-003 语料（ground_truth+openapi）", "缺失")
    (ok if (config.ROOT / "scripts" / "calibrate_corpus.py").exists()
        and (config.ROOT / "scripts" / "run_e7_corpus003.py").exists() else miss)(
        "corpus 标定 + E7 跑数入口", "缺失")

    # 14) 重跑幂等与台账语义锚点：轨迹/热点表 delete-first（重跑以本轮为准）；
    #     llm_calls 反向契约=追加不删（跨轮次/回炉成本累积即口径）
    nodes_src = (ROOT / "test_agent" / "graph" / "nodes.py").read_text(encoding="utf-8")
    (ok if "DELETE FROM evaluation_rounds WHERE task_id" in nodes_src else miss)(
        "evaluation_rounds 重跑 delete-first", "P1-③ 幂等语义不在位（重跑轨迹会翻倍）")
    (ok if "DELETE FROM complexity_hotspots WHERE task_id" in nodes_src else miss)(
        "complexity_hotspots 重跑 delete-first", "W4 幂等语义不在位（主语言变化会残留旧热点）")
    all_src = "\n".join(p.read_text(encoding="utf-8")
                        for p in (ROOT / "test_agent").rglob("*.py") if "__pycache__" not in str(p))
    (ok if "DELETE FROM llm_calls" not in all_src else miss)(
        "llm_calls 追加不删", "出现 delete-first，违反跨轮累积成本口径")

    # 14) 评估链单快照语义（run46 根修）：叙述/审查/图表同源零漂移，
    #     gen_charts 的 metrics/rtm 必须从 args 收（签名锚点），不得自 collect
    ev_src = (ROOT / "test_agent" / "skills" / "evaluate.py").read_text(encoding="utf-8")
    (ok if "def gen_charts(task_id: int, metrics: dict, rtm: dict" in ev_src else miss)(
        "gen_charts 从 args 收 metrics/rtm", "签名漂移：图表可能回到自取指标（快照漂移回归）")

    # 15) run_grid X 格消融入口全表（第十轮 X6/X7 补齐后 10 格齐；A1/B1/C1 见上方第 7 组）
    for cell, anchor in (
            ("X1", '"X1": {}'),
            ("X2", '"X2": {"GEN_METHODS_ENABLED": False}'),
            ("X3", '"X3": {"PLAN_ENABLED": False}'),
            ("X4", '"X4": {"COVERAGE_FEEDBACK_ENABLED": False}'),
            ("X5", '"X5": {"GEN_BOUNDARY_ATTACK": True}'),
            ("X6", '"X6": {"WITNESS_CONTINUE_ENABLED": True}'),
            ("X7", '"X7": {"GEN_CROSS_REVIEW_ENABLED": True}')):
        (ok if anchor in rg else miss)(f"run_grid.GRID[{cell}]", f"锚点缺失：{anchor}")

    # 16) 第四至十轮机制单测文件在位（机制行为锁，PART2 逐文件回归）
    for p in ("tests/test_witness_continue.py", "tests/test_cross_review.py",
              "tests/test_llm_usage.py", "tests/test_hotspot_metrics.py",
              "tests/test_grade_corpus.py", "tests/test_install_deps.py",
              "tests/test_run_grid_grid.py", "tests/test_rerun_idempotent.py",
              "tests/test_traceability.py"):
        (ok if (ROOT / p).exists() else miss)(p, "文件缺失")

    # 17) 阅卷协议 decoy 根修锚点（第七轮：占位缺陷不计 recall 分母 + 离线重算入口）
    gc_src = (ROOT / "scripts" / "grade_corpus.py").read_text(encoding="utf-8")
    for anchor in ("def split_planted", "def compute_metrics", "--recompute"):
        (ok if anchor in gc_src else miss)(f"grade_corpus『{anchor}』", "decoy 协议锚点缺失")
    return out


def part2_pytest(extra: list[str] | None = None) -> list[Item]:
    """单测回归：三方案+review+四轮新机制单测必查；--full 时全量。"""
    out: list[Item] = []
    runs = [("三方案机制单测", ["tests/test_design_proposals.py"], 13),
            ("review 节点单测（终态恢复+受控重写）", ["tests/test_review_node.py"], None),
            ("多轮新机制单测（V8证人/V9交叉审/token台账/W4热点/阅卷decoy/依赖清单/消融入口/重跑幂等/跨级溯源）",
             ["tests/test_witness_continue.py", "tests/test_cross_review.py",
              "tests/test_llm_usage.py", "tests/test_hotspot_metrics.py",
              "tests/test_grade_corpus.py", "tests/test_install_deps.py",
              "tests/test_run_grid_grid.py", "tests/test_rerun_idempotent.py",
              "tests/test_traceability.py"], None)]
    if extra:
        runs.append(("全量回归", extra, None))
    for name, paths, expect in runs:
        r = subprocess.run([sys.executable, "-m", "pytest", *paths, "-q", "--no-header"],
                           cwd=ROOT, capture_output=True, text=True, timeout=600)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(无输出)"
        n_pass, n_fail = _parse_pytest(last)
        good = r.returncode == 0 and n_fail == 0 and (expect is None or n_pass == expect)
        out.append(Item(f"pytest {name}", "OK" if good else "FAIL",
                        f"{last}（期望 {'≥1 过 0 败' if expect is None else f'{expect} 过'}）"
                        if good else f"returncode={r.returncode} tail={last}"))
    return out


def _parse_pytest(tail: str) -> tuple[int, int]:
    import re
    m = re.search(r"(\d+) passed(?:, (\d+) failed)?", tail)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def part3_probes() -> list[Item]:
    """边界探针：单测没覆盖的输入，逐条对契约。"""
    from test_agent.skills.gen_tests import (_condition_gaps, apply_assertion_gate,
                                             assertion_audit, refill_condition_gaps)
    from test_agent.skills.prompt_library import build_user_prompt
    out: list[Item] = []

    def check(name: str, cond: bool, detail: str, finding: bool = False):
        out.append(Item(name, (FINDING if finding else (PASS if cond else FAIL)), detail))

    src = [("m.py", "def check(p):\n    return bool(p)\n")]

    # —— 方案A：断言审计边界 ——
    a = assertion_audit("def t():\n    assert add(1, 2) == 3\n")
    check("A·等值比较=强", a["strong"] == 1 and a["weak"] == 0, f"{a}")

    a = assertion_audit("def t():\n    assert isinstance(v, int)\n")
    check("A·isinstance=弱（docstring 口径）", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert True\n")
    check("A·常量断言=弱", a["weak"] == 1 and "裸真值" in a["reasons"][0], f"{a}")

    a = assertion_audit("def t():\n    assert 0 < x <= 10\n")
    check("A·链式比较=强", a["strong"] == 1 and a["weak"] == 0, f"{a}")

    a = assertion_audit("def t():\n    assert x is None\n")
    check("A·is None=弱（与 is not None 同口径）", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert x in [1, 2]\n")
    check("A·in 成员比较=强", a["strong"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert not ok_flag\n")
    check("A·not 裸真值=弱", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    def broken(:\n")
    check("A·语法坏码不炸且计弱", a["weak"] == 1 and "语法" in a["reasons"][0], f"{a}")

    a = assertion_audit("def t():\n    assert a == 1 and b == 2\n")
    check("A·BoolOp 联接逐臂审计（两臂皆强比较=强，e908213 修复后回归守卫）",
          a["strong"] == 1 and a["weak"] == 0, f"{a}——两臂皆具体比较，逐臂审计不再误杀")

    a = assertion_audit("def t():\n    assert a == 1 and flag\n")
    check("A·BoolOp 含弱臂（裸真值）仍判弱", a["weak"] == 1 and "第2臂" in a["reasons"][0],
          f"{a}")

    a = assertion_audit("def t():\n    assert (a, b) == (1, 2)\n")
    check("A·元组整体比较=强（BoolOp 的正确写法对照组）", a["strong"] == 1, f"{a}")

    cases = [{"name": "w1", "code": "def w1():\n    assert f()\n"},
             {"name": "s1", "code": "def s1():\n    assert f() == 1\n"}]
    kept, rejected, stats = apply_assertion_gate(cases, gate=True)
    check("A·闸剔除数与 weak_cases 一致（stats 自洽）",
          stats["gate_rejected"] == stats["weak_cases"] == 1 and len(kept) == 1,
          f"kept={[c['name'] for c in kept]} stats={stats}")

    # —— 方案B：提示词边界 ——
    on = build_user_prompt(src, "需求", pbt=True)
    elems = ["@given", "hypothesis", "settings(max_examples", "属性测试", "不变量"]
    missing = [e for e in elems if e not in on]
    check("B·PBT 段要素齐备（骨架/采样上限/不变量纪律）", not missing,
          f"缺要素：{missing}" if missing else "5/5 要素在位")
    off = build_user_prompt(src, "需求", pbt=False)
    check("B·关时无 PBT 段", "属性测试" not in off and "@given" not in off, "开关隔离正常")
    both = build_user_prompt(src, "需求", include_methods=False, pbt=True)
    check("B·与五方法开关独立（方法关 PBT 开仍注入）", "@given" in both, "独立性成立")

    forced = build_user_prompt(src, "需求", conditions=[{"condition": "空密码应判为无效"}],
                               cond_forced=True, existing_cases=["test_a", "test_b"])
    check("C·强制模式措辞+已有用例名单注入",
          "强制逐条覆盖" in forced and "test_a" in forced, "措辞与防重复名单都在")
    forced_no = build_user_prompt(src, "需求", conditions=[{"condition": "空密码应判为无效"}],
                                  cond_forced=True, existing_cases=None)
    # 注意区分：静态措辞「不得与已有用例重复：」恒在；动态名单行「已有用例（禁止重复，
    # 可直接跳过…）：」才是 existing_cases 注入的产物——None 时不得出现、不得崩
    check("C·强制模式无已有用例时不崩不注入名单",
          "禁止重复，可直接跳过" not in forced_no and "test_a" not in forced_no,
          "None 路径干净（静态措辞『不得与已有用例重复』恒在，属正常）")

    # —— 方案C：缺口映射与回灌边界 ——
    conds = [{"condition": "空密码应判为无效"}, {"condition": "长度恰为上限的密码应有效"}]
    both_covered = [
        {"name": "test_空密码应判为无效_边界值分析", "expected": "无效",
         "code": "def t1():\n    assert check('') is False\n"},
        {"name": "test_长度恰为上限的密码应有效_边界值分析", "expected": "有效",
         "code": "def t2():\n    assert check('x' * 10) is True\n"}]
    check("C·2-gram 全覆盖→零缺口（回灌不触发的正路）",
          _condition_gaps(conds, both_covered) == [], "两条条件均有对准用例")
    check("C·空条件列表不炸", _condition_gaps([], both_covered) == [], "空表安全")

    covered = [both_covered[0]]     # 只盖住条件1 → 条件2 是真实缺口，回灌才会调 LLM
    dup = _FakeLLM(json.dumps({"cases": [
        {"name": "test_空密码应判为无效_边界值分析",
         "code": "def test_空密码应判为无效_边界值分析():\n    assert check('x') is True\n"}]}))
    tmp = _tmp_ws()
    new, _, info = refill_condition_gaps(
        dup, src, "", conds, covered, tmp, {c["name"] for c in covered}, True, False)
    check("C·LLM 回传重名被去重（nodeid 唯一性前提）",
          bool(new) and new[0]["name"] != covered[0]["name"],
          f"原名={covered[0]['name']} 新名={new[0]['name'] if new else '—'}")

    # —— 变异仪器边界 ——
    mk = _load("mutation_kill.py")
    got = mk._mutate("def f(x):\n    return x + 1\n", 0)
    check("M·算术翻转 Add→Sub", got is not None and "x - 1" in got[0], f"{got}")
    n_cand = len(mk._candidates(ast.parse("def f(x):\n    return 0 < x <= 10\n")))
    check("M·链式比较逐 op 计点", n_cand == 2, f"候选点={n_cand}（应 2：Lt/LtE）")
    check("M·越界索引返回 None", mk._mutate("def f(x):\n    return x + 1\n", 99) is None,
          "越界安全")
    check("M·无可变点返回 None", mk._mutate("def f():\n    return 1\n", 0) is None,
          "纯常量安全")
    probe_ws = _tmp_ws()
    (probe_ws / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (probe_ws / "test_mod.py").write_text("def t():\n    assert True\n", encoding="utf-8")
    mods = [p.name for p in mk.source_modules(probe_ws)]
    check("M·source_modules 跳过测试文件", mods == ["mod.py"], f"{mods}")
    return out


@dataclass
class _Resp:
    content: str


class _FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return _Resp(self.payload)


_TMP_DIRS: list[Path] = []


def _tmp_ws() -> Path:
    import tempfile
    root = ROOT / "data" / "tmp_check"
    root.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="probe_", dir=str(root)))
    _TMP_DIRS.append(ws)
    return ws


def _cleanup_tmp():
    import shutil
    for ws in _TMP_DIRS:
        shutil.rmtree(ws, ignore_errors=True)


_MK = None


def _load(name: str):
    global _MK
    if name == "mutation_kill.py" and _MK is not None:
        return _MK
    p = ROOT / "scripts" / name
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if name == "mutation_kill.py":
        _MK = mod
    return mod


def main() -> int:
    full = "--full" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")
    print("=" * 72)
    print("PART 1/3 完成度（静态契约）")
    items = part1_completion()
    _print(items)
    print("\n" + "=" * 72)
    print("PART 2/3 单测回归")
    boundary = len(items)
    items += part2_pytest(["-q"] if full else None)
    _print(items, since=boundary)
    print("\n" + "=" * 72)
    print("PART 3/3 边界探针（单测未覆盖输入）")
    boundary = len(items)
    try:
        items += part3_probes()
    finally:
        _cleanup_tmp()
    _print(items, since=boundary)

    n = {s: sum(1 for i in items if i.status == s)
         for s in ("OK", "MISS", "PASS", "FAIL", "FINDING")}
    print("\n" + "=" * 72)
    print(f"汇总：完成度 {n['OK']} OK / {n['MISS']} MISS；探针 {n['PASS']} PASS / "
          f"{n['FAIL']} FAIL / {n['FINDING']} FINDING")
    bad = [i for i in items if i.status in ("MISS", "FAIL")]
    for i in bad:
        print(f"  ✗ {i.name}: {i.detail}")
    for i in items:
        if i.status == FINDING:
            print(f"  ⚠ FINDING {i.name}: {i.detail}")

    rep = ROOT / "data" / "reports" / f"module_check_{ts}.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({"ts": ts, "summary": n,
                               "items": [vars(i) for i in items]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已落 {rep.relative_to(ROOT)}")
    return 1 if bad else 0


def _print(items: list[Item], since: int = 0):
    for i in items[since:]:
        mark = {"OK": "✓", "PASS": "✓", "MISS": "✗", "FAIL": "✗",
                "FINDING": "⚠"}.get(i.status, "?")
        line = f"  [{mark} {i.status:7}] {i.name}"
        if i.detail and i.status in ("MISS", "FAIL", "FINDING"):
            line += f" —— {i.detail}"
        print(line)


if __name__ == "__main__":
    sys.exit(main())
