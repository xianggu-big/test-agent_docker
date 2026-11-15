"""逐用例执行结果（任务C/2026-11-01：实时执行看板）。

pytest --junitxml / jest --json 的确定性解析 → test_case_results 表（第 24 张）。
设计取舍（研究先行，见任务C报告）：方案 A 新表 vs 方案 B 复用 JSON 列——选 A，
与 evaluation_rounds 同先例（P1-③ 教训：正则解析叙述消息脆，结构化表才是可审计真源）。

诚实边界：粒度=轮次级——每轮跑完才出现该轮逐用例结果（容器内单测流式 stdout 逐行
拉取需 attach 日志流，v1 不做）；前端如实标注"按轮次更新"，不伪装成逐用例实时流。
"""
import json
import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


def parse_junit_xml(text: str) -> list[dict]:
    """pytest --junitxml（junit_family=legacy 默认）→ [{nodeid,name,status,duration_ms,message}]。

    解析失败返回 []（诚实降级：看板退轮次汇总，不炸主链路）。
    error 级别（收集错误/fixture 炸）在 junit 里 classname 常为空、message 在 <error>。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "") or ""
        name = tc.get("name", "") or ""
        nodeid = f"{classname}::{name}" if classname else name
        duration = tc.get("time")
        dur_ms = int(float(duration) * 1000) if duration else None
        kids = list(tc)
        if not kids:
            status = "passed"
            message = ""
        else:
            tag = kids[0].tag                       # failure / error / skipped
            status = {"failure": "failed", "error": "error",
                      "skipped": "skipped"}.get(tag, "error")
            # 真 pytest junit 实测：message 属性=断言表达式（无错误类）、type 属性常缺、
            # 错误类在 traceback 尾行（"test_x.py:2: AssertionError"）——三处合成可读消息。
            # phpunit junit（2026-11-15 L1 服务编排实锤）：message 属性为空、type 属性=
            # 异常类、text 里才是断言详情（"Failed asserting that 0 is identical to
            # 200."）——断言详情行优先于尾行（尾行只有 file:line；curl code=0 的服务
            # 未起指纹全在断言行里，归因特征靠它）
            short = (kids[0].get("message") or "").strip()
            txt_lines = [ln for ln in (kids[0].text or "").splitlines() if ln.strip()]
            tail = txt_lines[-1].strip() if txt_lines else ""
            head = (kids[0].get("type") or "").strip()
            if not head and tail:
                m2 = re.search(r"(\w+Error|\w+Exception|\w+Failed)\b", tail)
                head = m2.group(1) if m2 else ""
            assertion_line = next(
                (ln.strip() for ln in txt_lines
                 if "asserting" in ln.lower() or "expecting" in ln.lower()), "")
            body = short or assertion_line or tail
            message = (body[:300] if not head or body.startswith(head)
                       else f"{head}: {body}")[:300]
        base = name.split("[")[0]                   # 参数化后缀剥除，对齐 test_cases.name
        out.append({"nodeid": nodeid, "name": base, "status": status,
                    "duration_ms": dur_ms, "message": message})
    return out


def parse_jest_results(stdout: str) -> list[dict]:
    """jest --json stdout → 逐用例（assertionResults：fullName/status/duration/failureMessages）。

    与 js_adapter 同源数据但视角不同：那边转 pytest 文本给 evaluate，这边抽逐用例明细。"""
    from .js_adapter import _extract_trailing_json  # 已有鲁棒 JSON 提取（前缀噪声容忍）
    data = _extract_trailing_json(stdout or "")
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for tr in data.get("testResults", []):
        for ar in tr.get("assertionResults", []):
            full = ar.get("fullName") or ar.get("title") or ""
            status = {"passed": "passed", "failed": "failed"}.get(ar.get("status"), "error")
            msgs = ar.get("failureMessages") or []
            # name 取 title（jest 的用例名字段）——fullName 是 describe 链空格拼接，
            # 取"最后一个空格分词"在 describe/标题含空格时取错词 → 与 test_cases.name
            # 反查失败 → origin 误归 repo（2026-11-15 闲时找bug；js_adapter._nodeid 同口径）
            out.append({"nodeid": full,
                        "name": (ar.get("title") or (full.split(" ")[-1] if full else "")),
                        "status": status,
                        "duration_ms": int(ar.get("duration") or 0) or None,
                        "message": (msgs[0][:300] if msgs else "")})
    return out


def normalize_case_name(n: str) -> str:
    """用例名规范化（origin 匹配用，2026-11-15 卡 3）：LLM 登记名带习惯前缀
    （test_TestX_方法），go 渲染层跨轮改名（Test→TestR{round}_）后执行名与登记名
    失配 → AI 用例成绩全错标 repo（1138 实锤 16 条）。剥 test_ 前缀/轮次标记
    （TestR1_/TestFb2_）/Test 前缀后小写比对——python/js/php 登记名与执行名
    原样相等形式归一后仍相等，零行为变化。"""
    n = (n or "").strip()
    n = re.sub(r"^test_", "", n, flags=re.I)
    n = re.sub(r"^Test(?:R\d+|Fb\d+)?_", "", n)
    n = re.sub(r"^Test", "", n)
    return n.lower()


def store_case_results(conn, task_id: int, round_no: int, rows: list[dict],
                       level: str = "L1") -> int:
    """逐用例结果落库（delete-first per round，与 evaluation_rounds 同幂等语义）。
    origin：name 规范化后与任务生成用例名匹配 → generated，否则 repo（自带测试）。
    返回落库条数。"""
    if round_no <= 1:
        conn.execute("DELETE FROM test_case_results WHERE task_id=? AND level=?",
                     (task_id, level))    # 重跑再进图 round 从 1 起：以本轮为准
    gen_names = {normalize_case_name(r["name"]) for r in conn.execute(
        """SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
           WHERE s.task_id=?""", (task_id,))}
    n = 0
    for r in rows:
        # origin 细化（2026-11-10）：rows 可自带 origin（如 ui_idempotency 路线的
        # 交互用例不走 gen_tests 落库，名字匹配不到 gen_names——行级自带 origin
        # 优先，否则按规范化名匹配归 generated/repo 旧口径）
        origin = r.get("origin") or ("generated"
                                     if normalize_case_name(r["name"]) in gen_names
                                     else "repo")
        conn.execute(
            """INSERT INTO test_case_results(task_id, round_no, level, nodeid, name,
                                            status, duration_ms, message, origin)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task_id, round_no, level, r["nodeid"], r["name"], r["status"],
             r.get("duration_ms"), r.get("message") or None, origin))
        n += 1
    conn.commit()
    return n


def execution_snapshot(conn, task_id: int) -> dict:
    """/api/tasks/{tid}/execution 的数据组装（纯 SQL 确定性）：任务态 + 逐用例结果 + 日志尾。"""
    t = conn.execute("SELECT status FROM test_tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        return {"error": "task not found"}
    rows = [dict(r) for r in conn.execute(
        """SELECT round_no, nodeid, name, status, duration_ms, message, origin
           FROM test_case_results WHERE task_id=? ORDER BY round_no, id""", (task_id,))]
    rounds = [dict(r) for r in conn.execute(
        """SELECT round_no, passed, failed, skipped, coverage_line, verdict
           FROM evaluation_rounds WHERE task_id=? AND level='L1' ORDER BY round_no""",
        (task_id,))]
    tail = [r["content"] for r in conn.execute(
        """SELECT m.content FROM agent_messages m
           JOIN agent_sessions s ON s.id=m.session_id
           WHERE s.task_id=? ORDER BY m.id DESC LIMIT 6""", (task_id,))][::-1]
    return {"status": t["status"], "rounds": rounds, "cases": rows,
            "log_tail": tail,
            "granularity": "round",       # 诚实标注：按轮次更新，非逐用例实时流
            "note": "每轮执行完毕后出现该轮逐用例结果（轮次级更新）"}
