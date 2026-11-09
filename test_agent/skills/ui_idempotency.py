"""UI 交互幂等路线（期 A2，2026-11-09）：单击/双击/快速三连击 + 状态不变量 oracle。

oracle 语义（不依赖 LLM 判定）：对目标交互元素，**首次交互后的页面状态计数**是
合法基线，其后的重复触发（双击/三连击）不得再改变该计数——变了=重复提交缺陷
（product_defect 证人，沿用 attribution 三分类，不新造 taxonomy）。

统一结果 schema（期 A 纪律④）：容器内渲染 pytest+playwright 脚本执行，junitxml
为权威判定，结果经 merge 转 pytest 风格文本（evaluate/attribution 直接吃），
逐用例行 origin=ai 落 test_case_results——与 ui_assert 同构，不各写一套解析。

案例来源（期 A 最小闭环，确定性优先）：extract_idem_candidates 直接解析 HTML——
button/input[type=submit]/onclick 链接 + 邻近列表容器（table/ul/ol）——零 LLM
即可为 687 这类纯静态页产候选；LLM 精修留任务卡 3。
"""
import base64
import json
import re
from pathlib import Path

from .routes import extract_route_features

_BTN_RE = re.compile(
    r"<button\b[^>]*>(.*?)</button>|<input[^>]+type=[\"'](submit|button)[\"'][^>]*>", re.I | re.S)
_ID_RE = re.compile(r"id=[\"']([\w\-]+)[\"']", re.I)
_LIST_RE = re.compile(r"<(table|ul|ol)\b[^>]*id=[\"']([\w\-]+)[\"']", re.I)
_ROW_RE = re.compile(r"<tr\b|<li\b", re.I)


def extract_idem_candidates(ws: str | Path, max_cases: int = 3) -> list[dict]:
    """确定性提取交互幂等候选：HTML 里可点击元素 + 邻近计数容器。
    优先带 id 的 button 与带 id 的列表/表格容器（selector 三形态的 id 形态最稳）。"""
    ws = Path(ws)
    cases: list[dict] = []
    for p in sorted(ws.rglob("*.htm*")):
        if any(part in (".git", "node_modules", "vendor") for part in p.relative_to(ws).parts):
            continue
        try:
            html = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lists = [(m.group(1), m.group(2)) for m in _LIST_RE.finditer(html)]
        for m in _BTN_RE.finditer(html):
            tag = m.group(0)
            idm = _ID_RE.search(tag)
            if not idm:
                continue                      # v1 只取带 id 的按钮（定位稳定性）
            btn_id = idm.group(1)
            # 计数容器：全文最近的带 id 列表/表格（常在按钮之前或之后）；无则文本长度兜底
            lm = _LIST_RE.search(html)
            count_sel = f"{lm.group(2)}" if lm else ""
            label = re.sub(r"<[^>]+>", "", m.group(1) or "").strip()[:20] or btn_id
            cases.append({
                "name": f"test_交互幂等_{p.stem}_{btn_id}",
                "page": p.name,
                "selector": {"spec": ["id", btn_id], "label": label},
                "count_selector": count_sel,
                "count_kind": "rows" if lm else "text_len",
            })
            if len(cases) >= max_cases:
                return cases
    return cases


def render_idem_script(cases: list[dict], base_url: str, evid_dir: str,
                       junit_path: str) -> str:
    """渲染容器内执行的 pytest+playwright 幂等脚本（与 ui_assert 同构；junit 权威）。"""
    b64 = base64.b64encode(json.dumps(cases, ensure_ascii=False).encode("utf-8")).decode()
    return f'''# UI 交互幂等（ui_idempotency 渲染；oracle=状态不变量，不依赖 LLM 判定）
import base64, json, os, time
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = {json.dumps(base_url, ensure_ascii=False)}
EVID = {json.dumps(evid_dir, ensure_ascii=False)}
CASES = json.loads(base64.b64decode("{b64}").decode("utf-8"))


def _count(page, case):
    if not case.get("count_selector"):
        return len(page.content())
    if case.get("count_kind") == "rows":
        loc = page.locator("#" + case["count_selector"])
        try:
            return loc.locator("tr, li").count()
        except Exception:
            return -1
    return len(page.content())


def _click(page, case, kind):
    el = page.locator("#" + case["selector"]["spec"][1])
    if kind == "single":
        el.click()
    elif kind == "double":
        el.dblclick()
    else:
        el.click(click_count=3)
    page.wait_for_timeout(120)      # 等 DOM 稳定（静态页无网络等待）


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_interaction_idempotent(pw, case):
    ctx = pw.new_context()
    page = ctx.new_page()
    page.goto(f"{{BASE_URL}}/{{case['page']}}")
    page.wait_for_timeout(150)
    base = _count(page, case)
    _click(page, case, "single")
    after1 = _count(page, case)
    # 首次交互合法基线：状态应发生变化（提交生效）；若首击都无效，如实标记
    if after1 == base:
        ctx.close()
        pytest.fail("首次单击后状态未变化（交互未生效，页面或 selector 问题）")
    # —— 状态不变量：重复触发不得再改变计数（变了=重复提交缺陷） ——
    _click(page, case, "double")
    after2 = _count(page, case)
    _click(page, case, "triple")
    after3 = _count(page, case)
    ctx.close()
    assert after2 == after1, (
        f"双击后计数变化：{{after1}} → {{after2}}（重复提交缺陷，oracle=状态不变量）")
    assert after3 == after1, (
        f"三连击后计数变化：{{after1}} → {{after3}}（重复提交缺陷，oracle=状态不变量）")
'''


def run_idempotency(api_key: str, args: dict, task_id: int, runner) -> str:
    """路线执行入口（对齐 skill 契约）：确定性提取候选 → 渲染脚本 → playwright 容器
    真跑 → junitxml 权威判定 → pytest 风格文本（evaluate/attribution 直接吃）。

    结果 schema 对齐（期 A 纪律④）：与 ui_assert 同构（容器内 pytest+junit 权威），
    产出 pytest 计数文本，evaluate/attribution 零新增解析。静态页 file:// 打开，
    无需起服务——687 这类纯 HTML 项目因此有了执行出口。"""
    ws = Path(args["workspace"])
    cases = extract_idem_candidates(ws)
    if not cases:
        return json.dumps({"generated": 0,
                           "reason": "未发现可提取的交互幂等候选（无带 id 的可点击元素）"},
                          ensure_ascii=False)
    # 静态页 file:// 直接打开（容器内 /work 挂载）；有服务型页面留任务卡 3
    script = render_idem_script(cases, "file:///work", "/tmp/idem_evid", "/tmp/idem_junit.xml")
    container = runner.ensure_container("mta-test:playwright", f"mta-ui-{task_id}", fresh=True)
    runner.copy_dir_into(container, ws)
    script_name = "test_idem_r1.py"
    runner.exec_in(container, ["sh", "-c",
                               f"cat > /work/{script_name} << 'MTA_IDEM_EOF'\n"
                               f"{script}\nMTA_IDEM_EOF"],
                   timeout_s=20, workdir="/")
    r = runner.exec_in(container,
                       ["sh", "-c",
                        f"cd /work && python3 -m pytest {script_name} -q --tb=short "
                        f"--junitxml=/tmp/idem_junit.xml 2>&1 | tail -30; "
                        f"echo '=== JUNIT ==='; cat /tmp/idem_junit.xml 2>/dev/null"],
                       timeout_s=420, workdir="/work")
    junit = ""
    if "=== JUNIT ===" in (r.stdout or ""):
        junit = r.stdout.split("=== JUNIT ===", 1)[1]
    # 输出统一为 pytest 风格文本：evaluate/attribution 契约（期 A 纪律④，junit 权威）
    ok_lines, fail_lines, rows = [], [], []
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(junit.strip())
        for tc in root.iter("testcase"):
            name = tc.get("name") or ""
            nodeid = tc.get("classname", "") + "::" + name
            if tc.find("failure") is not None:
                msg = (tc.find("failure").text or "")[:200]
                fail_lines.append(f"FAILED {nodeid} - {msg}")
                rows.append({"nodeid": nodeid, "name": name, "status": "failed",
                             "duration_ms": None, "message": msg})
            elif tc.find("error") is not None:
                msg = (tc.find("error").text or "")[:200]
                fail_lines.append(f"ERROR {nodeid} - {msg}")
                rows.append({"nodeid": nodeid, "name": name, "status": "error",
                             "duration_ms": None, "message": msg})
            else:
                ok_lines.append(f"PASSED {nodeid}")
                rows.append({"nodeid": nodeid, "name": name, "status": "passed",
                             "duration_ms": None, "message": ""})
    except ET.ParseError:
        return json.dumps({"generated": len(cases), "executed": 0,
                           "reason": "junit 不可解析（执行环境异常，诚实留痕）",
                           "raw_tail": (r.stdout or "")[-300:]}, ensure_ascii=False)
    lines = ["=========================== short test summary info ==========================="]
    lines.extend(fail_lines + ok_lines)
    passed, failed = len(ok_lines), len(fail_lines)
    lines.append(f"=========================== {passed} passed, {failed} failed, "
                 f"0 skipped ===========================")
    return json.dumps({"generated": len(cases), "executed": passed + failed,
                       "passed": passed, "failed": failed, "rows": rows,
                       "summary_text": "\n".join(lines)}, ensure_ascii=False)
