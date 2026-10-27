"""②定向修复策略族（链路A/B/C）——调研报告 §8 行2/行3 的可切换实现。

- prune  （链路A·基线）= 现行：强信号坏用例剪除，下一轮全量重生成（缺口回灌）
- rewrite（链路B·YATE arXiv:2507.18316）= 剪除坏用例 + 带失败上下文**重写**进新一轮文件
  （坏用例往往是贴近程序逻辑的高价值种子，修复优于丢弃）；全部重写成功时跳过全量重生成
- hybrid （链路C·优缺点合成）= B 的定向重写（REWRITE_BUDGET 预算 + 静态/收集准入闸）
  + A 的缺口回灌全量补测 + 剪除兜底——重写保种子、全量补覆盖、兜底控成本

三策略共用铁律（vision §7.2）：AssertionError 证人永不剪永不重写；仓库自带测试不动
（gen_tests.prune_failing_cases 内建此过滤）。LLM 经 llm_fn 注入（生产=DeepSeek、
单测=fake），机制层可确定性测试；链路参数对比见 benchmarks/compare_pipelines.py。
"""
import hashlib
import logging
import re
import time
from pathlib import Path

from .. import config
from ..graph.repair import CASE_OWN_DEFECT_ERRORS
from .gen_tests import _collect_sources, _parse_cases, _sanitize_name, _static_filter, \
    prune_failing_cases

log = logging.getLogger(__name__)

REWRITE_SYSTEM = (
    "你是测试修复工程师（YATE 思想：坏用例是高价值种子，修复优于丢弃）。"
    "只修复坏用例中的引用错误、签名错误与语法错误，保留其测试意图与断言预期——"
    "预期值不得改为迁就实际输出（预期先于执行）。只输出 JSON。"
)


def build_rewrite_prompt(sources: list[tuple[str, str]], requirement: str,
                         bad_cases: list[dict], fail_summary: str) -> str:
    src_text = "\n\n".join(
        f"### 被测文件：{path}\n```python\n{code}\n```" for path, code in sources)
    bad_text = "\n".join(
        f"- {c['name']}（失败：{c['err']}）\n```python\n{c['code']}\n```" for c in bad_cases)
    return f"""## 被测源码
{src_text}

## 用户需求
{requirement or "（无额外需求，按源码行为设计）"}

## 失败摘要（pytest -rfE）
{fail_summary or "（无）"}

## 待修复坏用例（逐一修复，name 保持原名）
{bad_text}

## 输出格式
只输出一个 JSON 对象：
{{"imports": "所需的全部 import 语句", "cases": [{{"name": "原名", "code": "def 原名():\\n    ..."}}]}}"""


def make_deepseek_fn(api_key: str | None):
    """生产 LLM 注入器；无 Key 返回 None（策略自动降级为纯剪除，诚实）。"""
    if not api_key:
        return None
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL, temperature=0.1,
                     timeout=90, max_retries=2)

    def fn(prompt: str) -> str:
        return llm.invoke([("system", REWRITE_SYSTEM), ("user", prompt)]).content

    return fn


def _strong_pairs(pairs: list[tuple[str, str]], budget: int) -> list[tuple[str, str]]:
    """挑出可重写的强信号坏用例（Agent 生成的、错误类在白名单内、预算内）。"""
    out = []
    for nodeid, err in pairs:
        fname, _, _ = nodeid.partition("::")
        if "::" in nodeid and fname.startswith(("test_gen_r", "test_rewrite_r", "test_imported")) \
                and err in CASE_OWN_DEFECT_ERRORS:
            out.append((nodeid, err))
    return out[:budget]


def rewrite_strategy(ws: Path, pairs: list[tuple[str, str]], task_id: int, conn,
                     round_no: int, llm_fn=None, runner=None, container_name: str = "",
                     requirement: str = "", fail_summary: str = "",
                     budget: int | None = None) -> dict:
    """链路B/C 的重写核心：先剪（复用现行机制）再带上下文重写进 test_rewrite_rN.py。"""
    t0 = time.perf_counter()
    budget = budget or config.REWRITE_BUDGET
    targets = _strong_pairs(pairs, budget)
    prune_info = prune_failing_cases(ws, pairs, task_id, conn)  # 剪除是两策略共同底座

    result = {"strategy": "rewrite", "targets": [n for n, _ in targets],
              "pruned": prune_info["pruned"], "kept": prune_info["kept"],
              "rewritten": [], "llm_calls": 0, "elapsed_ms": 0,
              "skip_full_regen": False}
    if not targets or llm_fn is None:
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    # 重写上下文：被测源码 + 坏用例原码（从停用的 DB 记录取，含完整审计链）
    bad_rows = conn.execute(
        """SELECT name, content FROM test_cases WHERE enabled=0 AND script_id IN
           (SELECT id FROM test_scripts WHERE task_id=?) ORDER BY id DESC LIMIT ?""",
        (task_id, len(targets))).fetchall()
    have = {r["name"]: r["content"] for r in bad_rows}
    bad_cases = [{"name": func, "err": err,
                  "code": have.get(func, f"# 原码缺失（{nodeid}）")}
                 for nodeid, err in targets for func in [nodeid.split("::")[1]]]

    raw = llm_fn(build_rewrite_prompt(_collect_sources(ws), requirement,
                                      bad_cases, fail_summary))
    result["llm_calls"] = 1
    data = _parse_cases(raw)
    if data is None:
        result["fallback"] = "LLM 输出不可解析，重写降级为纯剪除（兜底）"
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    data["cases"], rejected = _static_filter(data["cases"], ws)  # 准入闸第一道
    if not data["cases"]:
        result["fallback"] = f"重写用例全未过静态闸（{rejected}），降级为纯剪除"
        result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return result

    used: set[str] = set()
    for i, c in enumerate(data["cases"]):
        def_name = _sanitize_name(c.get("name"), i, used)
        code = c["code"].strip()
        m = re.search(r"def\s+\w+", code)
        if m:
            code = code[:m.start()] + f"def {def_name}" + code[m.end():]
        c["code"], c["name"] = code.strip(), def_name

    fname = f"test_rewrite_r{round_no}.py"
    imports = (data.get("imports") or "").strip()
    content = (f'"""重写修复用例（第 {round_no} 轮 · YATE 路线）"""\n'
               + (imports + "\n\n" if imports else "")
               + "\n\n".join(c["code"] for c in data["cases"]) + "\n")
    (ws / fname).write_text(content, encoding="utf-8")

    # 落库 + 回灌容器 + 第二道闸（收集失败即整体废案，与主生成闸同款纪律）
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, framework, version, content, content_hash)
           VALUES (?, 'pytest', ?, ?, ?)""",
        (task_id, round_no, content, hashlib.sha256(content.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    for c in data["cases"]:
        conn.execute("INSERT INTO test_cases(script_id, source, name, method, content) "
                     "VALUES (?, 'ai', ?, '重写修复', ?)",
                     (script_id, c["name"], c["code"]))
    conn.commit()

    collect_failed = False
    if runner is not None and container_name:
        container = runner.client.containers.get(container_name)
        runner.copy_dir_into(container, ws)
        r = runner.exec_in(container, ["pytest", "--collect-only", "-q", fname], timeout_s=120)
        collect_failed = r.exit_code != 0
        if collect_failed:
            (ws / fname).unlink(missing_ok=True)
            conn.execute("DELETE FROM test_cases WHERE script_id=?", (script_id,))
            conn.execute("DELETE FROM test_scripts WHERE id=?", (script_id,))
            conn.commit()
            runner.exec_in(container, ["rm", "-f", f"/work/{fname}"], timeout_s=30)

    if collect_failed:
        result["fallback"] = "重写物未过 collect 闸（整体废案），降级为纯剪除"
    else:
        result["rewritten"] = [c["name"] for c in data["cases"]]
        # 全部强信号坏用例都重写成功才跳过全量重生成（预算截断/闸门废案时不跳）
        strong_all = _strong_pairs(pairs, budget=10**9)
        result["skip_full_regen"] = len(result["rewritten"]) == len(strong_all) > 0
    result["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return result


def run_repair_strategy(strategy: str, ws: Path, pairs: list[tuple[str, str]],
                        task_id: int, conn, round_no: int, **kw) -> dict:
    """策略分派器。返回 dict 恒含 strategy/pruned/kept/skip_full_regen/llm_calls/elapsed_ms。"""
    if strategy == "rewrite":
        return rewrite_strategy(ws, pairs, task_id, conn, round_no, **kw)
    if strategy == "hybrid":
        info = rewrite_strategy(ws, pairs, task_id, conn, round_no, **kw)
        info["strategy"] = "hybrid"          # 结合点：重写照做，但全量补测照常（缺口回灌）
        info["skip_full_regen"] = False      # 兜底与覆盖补测永不省略（A 的优点）
        return info
    info = prune_failing_cases(ws, pairs, task_id, conn)   # 链路A基线
    return {"strategy": "prune", "pruned": info["pruned"], "kept": info["kept"],
            "rewritten": [], "llm_calls": 0, "skip_full_regen": False,
            "elapsed_ms": 0}
