"""gen_tests —— 按五种方法知识库生成用例：写入工作区 → 回灌容器 → 落库用例库。

流程：
1. 收集被测源码（排除测试文件与噪声目录，限总量防 token 爆炸）
2. 组装"五种方法骨架"提示词，调 DeepSeek 生成 JSON 用例清单
3. 解析 + 清洗（只保留 def test_ 开头的完整函数），写入 test_gen_r<N>.py
4. 重新快照进容器（下一轮 run_tests 直接可跑）
5. test_scripts + test_cases 落库（source='ai'，method=方法标签）—— 论文用例库数据源
"""
import hashlib
import json
import logging
import re
from pathlib import Path

from langchain_openai import ChatOpenAI

from .. import config
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


def generate_tests(api_key: str, args: dict, task_id: int, runner) -> str:
    ws = Path(args["workspace"])
    sources = _collect_sources(ws)
    if not sources:
        return json.dumps({"generated": 0, "reason": "未发现可测源码"}, ensure_ascii=False)

    llm = ChatOpenAI(model=config.DEEPSEEK_MODEL, api_key=api_key,
                     base_url=config.DEEPSEEK_BASE_URL,
                     temperature=0.2, timeout=90, max_retries=2)
    resp = llm.invoke([("system", SYSTEM_PROMPT),
                       ("user", build_user_prompt(sources, args.get("requirement", "")))])
    data = _parse_cases(resp.content)
    if data is None:
        return json.dumps({"generated": 0,
                           "reason": "LLM 返回无法解析为用例（诚实降级，本轮不生成）"},
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

    # —— 写入工作区 ——
    round_no = args.get("round_no", 1)
    fname = f"test_gen_r{round_no}.py"
    imports = (data.get("imports") or "").strip()
    body = "\n\n".join(c["code"].strip() for c in data["cases"])
    content = (f'"""AI 生成用例（第 {round_no} 轮 · 五种方法知识库）"""\n'
               + (imports + "\n\n" if imports else "") + body + "\n")
    (ws / fname).write_text(content, encoding="utf-8")

    # —— 回灌容器（依赖随项目走，生成物也随项目走） ——
    container = runner.client.containers.get(args["container_name"])
    runner.copy_dir_into(container, ws)

    # —— 落库：脚本版本 + 用例清单 ——
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO test_scripts(task_id, module_id, framework, version, content, content_hash)
           VALUES (?, ?, 'pytest', ?, ?, ?)""",
        (task_id, args.get("module_id"), round_no, content,
         hashlib.sha256(content.encode()).hexdigest()[:16]))
    script_id = cur.lastrowid
    for c in data["cases"]:
        conn.execute(
            "INSERT INTO test_cases(script_id, source, name, method, content) VALUES (?,?,?,?,?)",
            (script_id, "ai", c.get("name", "unnamed"), c.get("method"), c["code"]))
    conn.commit()

    summary = {"generated": len(data["cases"]), "file": fname, "script_id": script_id,
               "methods": [c.get("method") for c in data["cases"]]}
    log.info("gen_tests task=%s → %s", task_id, summary)
    return json.dumps(summary, ensure_ascii=False)
