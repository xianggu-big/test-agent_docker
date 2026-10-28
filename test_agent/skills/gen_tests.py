"""gen_tests —— 按五种方法知识库生成用例：写入工作区 → 回灌容器 → 落库用例库。

流程：
1. 收集被测源码（排除测试文件与噪声目录，限总量防 token 爆炸）
2. 组装"五种方法骨架"提示词，调 DeepSeek 生成 JSON 用例清单
3. 解析 + 清洗（只保留 def test_ 开头的完整函数），写入 test_gen_r<N>.py
4. 重新快照进容器（下一轮 run_tests 直接可跑）
5. test_scripts + test_cases 落库（source='ai'，method=方法标签）—— 论文用例库数据源
"""
import ast
import hashlib
import json
import logging
import re
from pathlib import Path

from langchain_openai import ChatOpenAI

from .. import config
from ..graph.repair import CASE_OWN_DEFECT_ERRORS
from ..store.db import get_conn
from .prompt_library import MAX_SOURCE_CHARS, SYSTEM_PROMPT, build_user_prompt
from .test_templates import expand_template_cases

log = logging.getLogger(__name__)


def _sanitize_name(name: str, idx: int, used: set[str]) -> str:
    """把 LLM 自报的用例名规整成合法且唯一的函数名。

    A5 纪律：写文件前统一用这个名字重写 def，保证 test_cases.name 与
    pytest nodeid 一一对应——归因/定向修复（②）按 nodeid 查用例的前提。
    """
    base = re.sub(r"\W+", "_", (name or "").strip()).strip("_") or f"test_case_{idx}"
    if not base.startswith("test_"):
        base = "test_" + base
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _collect_sources(ws: Path) -> list[tuple[str, str]]:
    """被测源码 = 非测试、非噪声的 .py 文件，按路径排序后限量收录。"""
    srcs, used = [], 0
    for p in sorted(ws.rglob("*.py")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip() or used + len(text) > MAX_SOURCE_CHARS:
            continue
        srcs.append((p.relative_to(ws).as_posix(), text))
        used += len(text)
    return srcs


def _parse_cases(text: str) -> dict | None:
    """从 LLM 回复中鲁棒提取 JSON；解析失败返回 None（诚实降级，不硬造用例）。

    用例两种合法形态（双通道）：①自由代码（code 含 def test_）②槽位模板
    （template+slots，由 test_templates 展开）——解析层两种都收，展开在后续步骤。
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    cases = data.get("cases")
    if not isinstance(cases, list):
        return None
    cleaned = [c for c in cases if isinstance(c, dict) and (
        (isinstance(c.get("code"), str) and re.search(r"def\s+test_\w+", c["code"]))
        or (isinstance(c.get("template"), str) and isinstance(c.get("slots"), dict)))]
    if not cleaned:
        return None
    data["cases"] = cleaned
    return data


def _module_top_level(ws: Path) -> dict[str, set[str]]:
    """被测工作区各模块的顶层符号表（静态闸用）：module stem -> {def/class 名}。"""
    table: dict[str, set[str]] = {}
    for p in ws.rglob("*.py"):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        table.setdefault(p.stem, set()).update(names)
    return table


def _static_filter(cases: list[dict], ws: Path) -> tuple[list[dict], list[str]]:
    """生成后静态闸（§3.6 第 1 道，确定性、零成本）：
    语法可解析 + from-import 的符号在被测模块顶层存在。剔除理由如实返回。"""
    table = _module_top_level(ws)
    kept, rejected = [], []
    for c in cases:
        try:
            tree = ast.parse(c["code"])
        except SyntaxError as e:
            rejected.append(f"{c.get('name')}: 语法错误({e.lineno}行)")
            continue
        ok = True
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            mod = node.module.split(".")[0]
            if mod in table:
                for alias in node.names:
                    if alias.name != "*" and alias.name not in table[mod]:
                        rejected.append(f"{c.get('name')}: from {mod} import {alias.name} 不存在")
                        ok = False
                        break
            if not ok:
                break
        if ok:
            kept.append(c)
    return kept, rejected


def _audit_one_assert(test: ast.expr) -> tuple[bool, str | None]:
    """单条断言的强弱判定：(是否强, 弱原因)。强 = 具体值/关系 oracle。"""
    if isinstance(test, ast.Compare):
        comps = [test.left] + list(test.comparators)
        has_rel = any(not isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops)
        if has_rel:
            return True, None                       # ==/!=/</>/in 等值或序关系
        none_check = any(isinstance(c, ast.Constant) and c.value is None for c in comps)
        bool_check = any(isinstance(c, ast.Constant) and isinstance(c.value, bool)
                         for c in comps)
        if bool_check:
            return True, None                       # is True/False：显式布尔 oracle
        if none_check:
            return False, "is (not) None 无具体预期值"
        return True, None
    if isinstance(test, ast.Call):
        fn = test.func
        name = fn.attr if isinstance(fn, ast.Attribute) else \
            (fn.id if isinstance(fn, ast.Name) else "")
        if name == "raises":
            return True, None                       # 异常类型 oracle
        return False, f"裸调用真值断言({name or 'expr'})"
    return False, "裸真值/常量断言"


def assertion_audit(code: str) -> dict:
    """断言质量审计（纯函数，方案A 的度量与闸共用）：统计用例内强/弱断言。

    强 = 具体关系比较（==/!=/序关系/in）、is True/False、pytest.raises(异常类型)；
    弱 = 裸真值断言、is (not) None、isinstance、无断言 —— 不具备证伪具体预期值的能力
    （动机：2412.14137 实证生成器会写"为通过而设计"的断言；UW ASE'25 oracle 实证）。
    返回 {total, strong, weak, reasons}；无断言整案计弱。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["语法不可解析"]}
    strong, weak, reasons = 0, 0, []
    # raises 上下文管理器（with pytest.raises(...)）也是显式 oracle，与 assert 同权统计
    raises_calls = []
    for w in ast.walk(tree):
        if isinstance(w, ast.With):
            for item in w.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else \
                        (fn.id if isinstance(fn, ast.Name) else "")
                    if name == "raises":
                        raises_calls.append(ctx)
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    if not asserts and not raises_calls:
        return {"total": 0, "strong": 0, "weak": 1, "reasons": ["无断言（无 oracle）"]}
    for a in asserts:
        ok, why = _audit_one_assert(a.test)
        if ok:
            strong += 1
        else:
            weak += 1
            reasons.append(why)
    strong += len(raises_calls)
    return {"total": strong + weak, "strong": strong, "weak": weak, "reasons": reasons}


def _condition_gaps(conditions: list[dict], cases: list[dict]) -> list[dict]:
    """方案C：确定性 2-gram 映射（与 map_requirements 启发式同源）找出无用例对准的条件。"""
    from .evaluate import _heuristic_map          # 延迟导入避免环形依赖
    pseudo = [{"id": i + 1, "condition": c["condition"]} for i, c in enumerate(conditions)]
    rows = [{"name": c["name"], "expected": c.get("expected") or "",
             "content": c.get("content") or c.get("code") or ""} for c in cases]
    mapping = _heuristic_map(pseudo, rows)
    return [c for c, p in zip(conditions, pseudo) if not mapping.get(p["id"])]


def _rebuild_imported_file(conn, tid: int, ws: Path) -> str:
    """以库为准重建 test_imported.py（仅 enabled=1），随下次 provision 快照进容器。"""
    imported = conn.execute(
        """SELECT c.content FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id=? AND c.source='imported' AND c.enabled=1 ORDER BY c.id""", (tid,)).fetchall()
    content = '"""人工/AI 插入用例（test_cases.source=imported，enabled=1）"""\n\n' + \
              "\n\n".join(r["content"].strip() for r in imported) + "\n"
    (Path(ws) / "test_imported.py").write_text(content, encoding="utf-8")
    return content


def prune_failing_cases(ws: Path, pairs: list[tuple[str, str]],
                        task_id: int, conn) -> dict:
    """②定向修复（TestGen-LLM 准入思想）：上一轮失败用例按归因分流——

    - 用例自身坏掉的强信号（ImportError/SyntaxError/AttributeError 等）→ 剪除：
      生成文件走 AST 摘除函数，imported 文件以库为准重建（enabled=0 保留审计记录）；
    - AssertionError（疑似产品缺陷）→ 一律保留为证人（§7.2 防造假铁律）；
    - 仓库自带测试永远不动（那是被测项目的一部分，不是 Agent 的产物）。
    """
    pruned, kept = [], []
    for nodeid, err in pairs:
        fname, _, func = nodeid.partition("::")
        agent_file = fname.startswith(("test_gen_r", "test_imported"))
        if not agent_file or err not in CASE_OWN_DEFECT_ERRORS:
            kept.append({"nodeid": nodeid, "err": err or "unknown"})
            continue
        # DB 记录保留但停用（论文审计台账），文件层面真正摘除
        conn.execute(
            """UPDATE test_cases SET enabled=0 WHERE name=? AND enabled=1 AND script_id IN
               (SELECT id FROM test_scripts WHERE task_id=?)""", (func, task_id))
        conn.commit()
        path = Path(ws) / fname
        if fname == "test_imported.py":
            _rebuild_imported_file(conn, task_id, ws)
        elif path.exists():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                tree.body = [n for n in tree.body
                             if not (isinstance(n, ast.FunctionDef) and n.name == func)]
                path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
            except SyntaxError:
                path.unlink(missing_ok=True)  # 整文件已不可解析，整体废案
        pruned.append({"nodeid": nodeid, "err": err})
    return {"pruned": pruned, "kept": kept}


def refill_condition_gaps(llm, sources: list[tuple[str, str]], requirement: str,
                          conditions: list[dict], cases: list[dict], ws: Path,
                          used_names: set[str], include_methods: bool, pbt: bool
                          ) -> tuple[list[dict], str, dict]:
    """方案C 核心（llm 注入可单测）：查条件缺口 → 定向补生成恰 1 次 → 返回 (新用例, imports, 信息)。"""
    gaps = _condition_gaps(conditions, cases)
    info: dict = {"uncovered_before": len(gaps), "refill_generated": 0,
                  "uncovered_after": len(gaps)}
    if not gaps:
        return [], "", info
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_user_prompt(
                           sources, requirement, None, None, gaps,
                           include_methods=include_methods, pbt=pbt, cond_forced=True,
                           templates=False,   # 补生成轮保持自由通道（定向补缺不做结构先验）
                           existing_cases=[c["name"] for c in cases]))])
    data = _parse_cases(resp.content)
    if not data:
        info["note"] = "补生成轮 LLM 输出不可解析（诚实放弃）"
        return [], "", info
    data["cases"], rejected = _static_filter(data["cases"], ws)
    info["static_rejected"] = rejected
    out = []
    for c in data["cases"]:
        def_name = _sanitize_name(c.get("name"), len(used_names), used_names)
        code = c["code"].strip()
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name
        out.append(c)
    info["refill_generated"] = len(out)
    info["uncovered_after"] = len(_condition_gaps(conditions, cases + out))
    return out, (data.get("imports") or "").strip(), info


def apply_assertion_gate(cases: list[dict], gate: bool = False
                         ) -> tuple[list[dict], list[str], dict]:
    """方案A 核心（纯函数）：断言质量审计恒算；gate=True 时剔除全弱断言用例（无证伪能力）。
    返回 (保留用例, 剔除理由列表, 统计)。混合断言（含 ≥1 强）的用例保留。"""
    audits = [assertion_audit(c["code"]) for c in cases]
    stats = {"cases": len(audits),
             "weak_cases": sum(1 for a in audits if a["strong"] == 0),
             "assert_total": sum(a["total"] for a in audits),
             "assert_weak": sum(a["weak"] for a in audits)}
    rejected: list[str] = []
    if not gate:
        return cases, rejected, stats
    kept = []
    for c, a in zip(cases, audits):
        if a["strong"] == 0:
            rejected.append(f"{c.get('name')}: {a['reasons'][0]}")
        else:
            kept.append(c)
    stats["gate_rejected"] = len(rejected)
    return kept, rejected, stats


def generate_tests(api_key: str, args: dict, task_id: int, runner) -> str:
    ws = Path(args["workspace"])
    sources = _collect_sources(ws)
    if not sources:
        return json.dumps({"generated": 0, "reason": "未发现可测源码"}, ensure_ascii=False)

    conn = get_conn()
    conditions = [dict(r) for r in conn.execute(
        "SELECT condition, priority, method_hint FROM test_conditions "
        "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()] or None

    llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL,
                     temperature=0.2, timeout=90, max_retries=2)
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_user_prompt(sources, args.get("requirement", ""),
                                                  args.get("coverage_gaps"),
                                                  args.get("coverage_target"),
                                                  conditions,
                                                  include_methods=config.GEN_METHODS_ENABLED,
                                                  boundary_attack=config.GEN_BOUNDARY_ATTACK,
                                                  pbt=config.PBT_ENABLED,
                                                  templates=config.TEMPLATES_ENABLED,
                                                  state_transition=config.STATE_TRANSITION_ENABLED,
                                                  metamorphic=config.METAMORPHIC_ENABLED))])
    data = _parse_cases(resp.content)
    if data is None:
        return json.dumps({"generated": 0,
                           "reason": "LLM 返回无法解析为用例（诚实降级，本轮不生成）"},
                          ensure_ascii=False)

    # —— 槽位模板展开（双通道）：基础方法 T1/T2/T5 渲染成代码，失败案留台账理由剔除 ——
    data["cases"], template_rejected = expand_template_cases(data["cases"])
    if template_rejected:
        log.info("gen_tests task=%s 模板展开剔除: %s", task_id, template_rejected)
    if not data["cases"] and template_rejected:
        return json.dumps({"generated": 0, "reason": "全部用例模板展开失败（槽位无效，诚实降级）",
                           "template_rejected": template_rejected}, ensure_ascii=False)

    # —— 静态闸（§3.6 第 1 道）：语法 + from-import 符号存在性，坏案在写盘前剔除 ——
    data["cases"], rejected = _static_filter(data["cases"], ws)
    if not data["cases"]:
        return json.dumps({"generated": 0, "reason": "全部用例未过静态审查（诚实降级）",
                           "rejected": rejected, "template_rejected": template_rejected},
                          ensure_ascii=False)

    # —— def 名对齐（A5）：用规整后的 name 重写首个 def，nodeid 从此可反查用例 ——
    used_names: set[str] = set()
    for i, c in enumerate(data["cases"]):
        def_name = _sanitize_name(c.get("name"), i, used_names)
        code = c["code"].strip()
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name

    # —— 方案C·条件缺口回灌（Gotel&Finkelstein post-traceability 的生成侧闭环）：
    # 确定性 2-gram 映射查无用例对准的条件 → 定向补生成恰 1 轮（预算纪律）——
    # 与 V4 行覆盖回灌不同层：这里是 RTM 条件层的"覆盖了≠测到了"信号。 ——
    cond_feedback: dict | None = None
    if config.COND_FEEDBACK_ENABLED and conditions:
        new_cases, extra_imports, cond_feedback = refill_condition_gaps(
            llm, sources, args.get("requirement", ""), conditions, data["cases"], ws,
            used_names, config.GEN_METHODS_ENABLED, config.PBT_ENABLED)
        if new_cases:
            data["cases"] += new_cases
            if extra_imports:
                old = (data.get("imports") or "").strip()
                data["imports"] = (old + "\n" + extra_imports).strip()

    # —— 方案A·断言质量审计（度量恒算）+ 可选闸：全弱断言用例 = 无证伪能力 ——
    data["cases"], gate_rejected, assertion_stats = apply_assertion_gate(
        data["cases"], gate=config.ASSERTION_GATE_ENABLED)
    if gate_rejected:
        rejected += gate_rejected
    if not data["cases"]:
        return json.dumps({"generated": 0,
                           "reason": "全部用例未过断言质量闸（全弱断言，诚实降级）",
                           "rejected": rejected, "assertion": assertion_stats,
                           "cond_feedback": cond_feedback}, ensure_ascii=False)

    # —— 写入工作区 ——
    round_no = args.get("round_no", 1)
    fname = f"test_gen_r{round_no}.py"
    imports = (data.get("imports") or "").strip()
    body = "\n\n".join(c["code"].strip() for c in data["cases"])
    content = (f'"""AI 生成用例（第 {round_no} 轮 · 五种方法知识库）"""\n'
               + (imports + "\n\n" if imports else "") + body + "\n")
    (ws / fname).write_text(content, encoding="utf-8")

    # —— 落库：脚本版本 + 用例清单（先于闸门，收集失败时可整体废案删除） ——
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, module_id, framework, version, content, content_hash)
           VALUES (?, ?, 'pytest', ?, ?, ?)""",
        (task_id, args.get("module_id"), round_no, content,
         hashlib.sha256(content.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    for c in data["cases"]:
        conn.execute(
            """INSERT INTO test_cases(script_id, source, name, method, content,
                                      priority, input, steps, expected, expected_basis)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (script_id, "ai", c.get("name", "unnamed"), c.get("method"), c["code"],
             c.get("priority"), c.get("input"), c.get("steps"),
             c.get("expected"), c.get("expected_basis")))
    conn.commit()

    # —— 回灌容器（依赖随项目走，生成物也随项目走） ——
    container = runner.client.containers.get(args["container_name"])
    runner.copy_dir_into(container, ws)

    # —— 第二道闸（§3.6 第 2 道）：容器内 --collect-only 可收集性检查 ——
    # 收集失败是文件级问题（模块级 import 等），无法按用例归责 → 整体废案（诚实降级），
    # 绝不把注定 ERROR 的生成物交给执行轮。
    r = runner.exec_in(container, ["pytest", "--collect-only", "-q", fname], timeout_s=120)
    if r.exit_code != 0:
        (ws / fname).unlink(missing_ok=True)
        conn.execute("DELETE FROM test_cases WHERE script_id=?", (script_id,))
        conn.execute("DELETE FROM test_scripts WHERE id=?", (script_id,))
        conn.commit()
        runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)
        log.warning("gen_tests task=%s collect 失败，本轮生成物废案: %s",
                    task_id, r.stderr[-200:])
        return json.dumps({"generated": 0,
                           "reason": "生成物未过容器内 --collect-only 可收集性检查（整体废案）",
                           "detail": (r.stdout + r.stderr)[-300:]}, ensure_ascii=False)

    summary = {"generated": len(data["cases"]), "file": fname, "script_id": script_id,
               "static_rejected": rejected,
               "methods": [c.get("method") for c in data["cases"]],
               "template_rejected": template_rejected,
               "assertion": assertion_stats, "cond_feedback": cond_feedback}
    log.info("gen_tests task=%s → %s", task_id, summary)
    return json.dumps(summary, ensure_ascii=False)
