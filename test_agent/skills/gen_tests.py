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
    """从 LLM 回复中鲁棒提取 JSON；解析失败返回 None（诚实降级，不硬造用例）。"""
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
    cleaned = [c for c in cases
               if isinstance(c, dict) and isinstance(c.get("code"), str)
               and re.search(r"def\s+test_\w+", c["code"])]
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
                                                  conditions))])
    data = _parse_cases(resp.content)
    if data is None:
        return json.dumps({"generated": 0,
                           "reason": "LLM 返回无法解析为用例（诚实降级，本轮不生成）"},
                          ensure_ascii=False)

    # —— 静态闸（§3.6 第 1 道）：语法 + from-import 符号存在性，坏案在写盘前剔除 ——
    data["cases"], rejected = _static_filter(data["cases"], ws)
    if not data["cases"]:
        return json.dumps({"generated": 0, "reason": "全部用例未过静态审查（诚实降级）",
                           "rejected": rejected}, ensure_ascii=False)

    # —— def 名对齐（A5）：用规整后的 name 重写首个 def，nodeid 从此可反查用例 ——
    used_names: set[str] = set()
    for i, c in enumerate(data["cases"]):
        def_name = _sanitize_name(c.get("name"), i, used_names)
        code = c["code"].strip()
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code, def_name

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
               "methods": [c.get("method") for c in data["cases"]]}
    log.info("gen_tests task=%s → %s", task_id, summary)
    return json.dumps(summary, ensure_ascii=False)
