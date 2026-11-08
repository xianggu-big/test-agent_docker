"""ui_assert：UI 黑盒验证 skill（黑盒能力补齐阶段3，docs/黑盒能力补齐任务包-20261106.md）。

五步模子对应：②提取层=DOM 快照采集（collect_dom_snapshot——可达性树 role/name 两元组
+ DOM [id] 清单，落盘即「UI 版端点清单」）；③生成层=LLM 只按快照真值清单生成操作序列，
**双闸**：动作 schema 闸（未知动作/缺字段拒绝）+ selector 真值闸（三形态语法
role/text/#id，必须命中快照元素，虚构整案剔除留痕）；④执行层=Playwright 容器内
pytest+chromium 真实执行（每步截图+trace.zip，junitxml 权威判定）；⑤落库层=ui_asserts
第 30 表（步骤级 verdict/截图路径/trace 路径/拒绝理由，幂等口径与 app_logs/db_asserts 一致），
失败场景出缺陷报告单（失败步骤+预期 vs 实际+证据路径）。

安全模型：双闸在 skill 内部（不占确认门——闸挡在执行之前，与 db_assert 只读闸同哲学）；
LLM 生成（gen_ui_scenarios）在调用方侧完成，skill 只收已生成 scenarios 复闸（纵深防御）。
"""
import base64
import json
import logging
import re
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

# —— 操作序列 schema：动作白名单 + 每个动作的必填字段（任务卡「未知动作类型拒绝」） ——
_ACTIONS = {
    "goto": ("url",),
    "click": ("selector",),
    "fill": ("selector", "value"),
    "expect_visible": ("selector",),
    "expect_text": ("selector", "text"),
    "expect_hidden": ("selector",),   # 删除/移除后应消失（验收「按钮失效」场景必需）
}

# —— selector 三形态语法（可确定性解析；语法外形态一律不可解析=虚构） ——
_SEL_ROLE = re.compile(r'^role=([a-z][a-z_0-9]*)\s+name="([^"]+)"$')
_SEL_TEXT = re.compile(r"^text=(.+)$")
_SEL_ID = re.compile(r"^#([A-Za-z][\w-]*)$")

# 快照排除的角色（无交互语义/容器节点，不进真值清单）
_SNAP_SKIP_ROLES = {"generic", "text", "StaticText", "InlineTextBox", "WebArea"}

CONT_EVID = "/work/ui_evidence"          # 容器内证据目录（截图/trace/快照）


def parse_selector(sel):
    """selector 三形态解析：('role', role, name) | ('text', text) | ('id', id) | None。

    语法外形态（xpath/CSS/裸类名/缺 name）返回 None——虚构闸的第一道：连语法都不合法。"""
    if not isinstance(sel, str):
        return None
    sel = sel.strip()
    m = _SEL_ROLE.match(sel)
    if m:
        return ("role", m.group(1), m.group(2))
    m = _SEL_TEXT.match(sel)
    if m:
        return ("text", m.group(1))
    m = _SEL_ID.match(sel)
    if m:
        return ("id", m.group(1))
    return None


def _truth_hit(parsed, snapshot) -> bool:
    """selector 是否命中快照真值清单（role/name 精确、text 精确、id 精确）。"""
    if parsed is None:
        return False
    roles = {(r.get("role"), r.get("name")) for r in snapshot.get("roles") or []}
    ids = set(snapshot.get("ids") or [])
    texts = set(snapshot.get("texts") or [])
    if parsed[0] == "role":
        return (parsed[1], parsed[2]) in roles
    if parsed[0] == "text":
        return parsed[1] in texts
    return parsed[1] in ids


def _derived_allow(parsed, fill_values, snapshot) -> bool:
    """运行时派生元素放行（真值闸的确定性扩展，2026-11-07 真机实锤：新增项断言元素
    在执行前快照里必然不存在——测试自己创建的元素，拒掉=合法添加流全灭）。

    放行条件（全部确定性可审计）：
    - text=<v>：v 是本场景先前 fill 引入的值（断言对象=自己创建的项）；
    - role=<r> name=<v>：精确等于 fill 值且该 role 在快照中存在；
    - role=button name=<模板+v>：模板从快照自动发现（既有按钮名 "删除 X" 与可见文本 X
      的成对结构 → 前缀模板 "删除 "），派生名=模板+fill 值。
    除此之外的未命中 selector 仍=虚构，整案剔除。"""
    if not fill_values:
        return False
    names = [r.get("name") or "" for r in (snapshot.get("roles") or [])]
    snap_roles = {r.get("role") for r in (snapshot.get("roles") or [])}
    texts = [t for t in (snapshot.get("texts") or []) if t]
    templates = set()
    for s in names:
        for t in texts:
            if t and t in s and s != t:
                templates.add(s[:s.index(t)])
    for v in fill_values:
        if not v:
            continue
        if parsed[0] == "text" and parsed[1] == v:
            return True
        if parsed[0] == "role" and parsed[1] in snap_roles and parsed[2] == v:
            return True
        if parsed[0] == "role" and any(tpl + v == parsed[2] for tpl in templates):
            return True
    return False


def _rej_row(scenario: str, reason: str) -> dict:
    """拒绝行（与 ui_asserts 表列同形的完整行，gate 即留痕）。"""
    return {"scenario": scenario, "step_no": 0, "action": "gate", "selector": "",
            "verdict": "rejected", "error": "", "screenshot_path": "", "trace_path": "",
            "rejected_reason": reason, "elapsed_ms": 0}


def scenario_gate(scenarios, snapshot):
    """双闸：动作 schema 闸 + selector 真值闸。返回 (kept_scenarios, rejected_rows)。

    整案语义（同 db_assert 只读闸/端点铁律）：一个场景内任一步违规 → 整案剔除，
    一条都不执行；拒绝理由指明步骤序号/动作/selector 原文（可审计）。"""
    if not isinstance(scenarios, list):
        return [], [_rej_row("<input>", "scenarios 必须是场景数组")]
    kept, rejected = [], []
    for sc in scenarios:
        if not isinstance(sc, dict):
            rejected.append(_rej_row("<input>", "场景必须是对象"))
            continue
        name = sc.get("name")
        steps = sc.get("steps")
        if not isinstance(name, str) or not name.strip():
            rejected.append(_rej_row("<input>", "场景缺 name（字符串）——整案剔除"))
            continue
        name = name.strip()
        if not isinstance(steps, list) or not steps:
            rejected.append(_rej_row(name, "steps 为空或不是数组——整案剔除"))
            continue
        reason = None
        fill_values: list[str] = []
        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                reason = f"步骤 #{i} 不是对象——整案剔除"
                break
            act = st.get("action")
            if act not in _ACTIONS:
                reason = (f"步骤 #{i} 动作类型 {act!r} 不在白名单 "
                          f"{sorted(_ACTIONS)}——整案剔除")
                break
            missing = [f for f in _ACTIONS[act]
                       if not isinstance(st.get(f), str) or not st.get(f)]
            if missing:
                reason = f"步骤 #{i} {act} 缺字段 {missing}——整案剔除"
                break
            if "selector" in _ACTIONS[act]:
                parsed = parse_selector(st["selector"])
                if parsed is None:
                    reason = (f"步骤 #{i} selector {st['selector']!r} 语法不合法"
                              f"（仅支持 role=<r> name=\"<n>\" / text=<t> / #<id>）——整案剔除")
                    break
                if (not _truth_hit(parsed, snapshot or {})
                        and not _derived_allow(parsed, fill_values, snapshot or {})):
                    reason = (f"步骤 #{i} selector {st['selector']!r} 未命中 DOM 快照真值清单"
                              f"（既不在快照、也非本场景运行时派生元素=虚构）——整案剔除")
                    break
            if act == "fill" and st.get("value"):
                fill_values.append(st["value"])
        if reason:
            rejected.append(_rej_row(name, reason))
        else:
            kept.append({"name": name, "steps": steps})
    return kept, rejected


def sanitize_name(name: str) -> str:
    """场景名 → pytest 节点/目录安全名（\\w 保留中文；引号/斜杠/尖括号折叠成 _）。"""
    s = re.sub(r"[^\w]+", "_", name).strip("_")
    return s or "case"


def render_pytest(kept, base_url: str, evid_dir: str = CONT_EVID,
                  steps_path: str = "/work/ui_steps.json") -> str:
    """渲染 pytest 文件（步骤数据 base64+json 载入——值里的引号/换行不破坏代码文本）。"""
    payload = []
    for idx, sc in enumerate(kept):
        steps = []
        for i, st in enumerate(sc["steps"]):
            parsed = (parse_selector(st["selector"])
                      if "selector" in _ACTIONS[st["action"]] else None)
            steps.append({"i": i, "action": st["action"], "selector": st.get("selector", ""),
                          "sel": list(parsed) if parsed else None, "url": st.get("url", ""),
                          "value": st.get("value", ""), "text": st.get("text", "")})
        payload.append({"name": sc["name"], "dir": sanitize_name(sc["name"]),
                        "steps": steps})
    b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    return f'''# -*- coding: utf-8 -*-
# UI 黑盒场景（ui_assert 渲染；每步截图 + 每场景 trace.zip；判定权威=junit 报告）
import base64
import json
import os
import time

import pytest
from playwright.sync_api import sync_playwright, expect

BASE_URL = {json.dumps(base_url, ensure_ascii=False)}
EVID = {json.dumps(evid_dir, ensure_ascii=False)}
_STEPS_PATH = {json.dumps(steps_path, ensure_ascii=False)}
SCENARIOS = json.loads(base64.b64decode("{b64}").decode("utf-8"))


def _el(page, spec):
    # selector 三形态 → playwright 定位（与真值闸同源映射）
    if spec[0] == "role":
        return page.get_by_role(spec[1], name=spec[2], exact=True)
    if spec[0] == "text":
        return page.get_by_text(spec[1], exact=True)
    return page.locator('#' + spec[1])


def _save_record(rec, name):
    try:
        data = []
        if os.path.exists(_STEPS_PATH):
            with open(_STEPS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        data = [d for d in data if d.get("scenario") != name]
        data.append(rec)
        with open(_STEPS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture()
def page(pw):
    ctx = pw.new_context()
    ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
    pg = ctx.new_page()
    yield pg, ctx
    ctx.close()


def _run_steps(pg, ctx, sc):
    name, sdir = sc["name"], os.path.join(EVID, sc["dir"])
    os.makedirs(sdir, exist_ok=True)
    rec = {{"scenario": name, "trace_path": os.path.join(sdir, "trace.zip"), "steps": []}}
    try:
        for st in sc["steps"]:
            act = st["action"]
            t0 = time.perf_counter()
            err = ""
            try:
                if act == "goto":
                    pg.goto(st["url"], wait_until="networkidle", timeout=15000)
                elif act == "click":
                    _el(pg, st["sel"]).click(timeout=5000)
                elif act == "fill":
                    _el(pg, st["sel"]).fill(st["value"], timeout=5000)
                elif act == "expect_visible":
                    expect(_el(pg, st["sel"])).to_be_visible(timeout=5000)
                elif act == "expect_text":
                    expect(_el(pg, st["sel"])).to_have_text(st["text"], timeout=5000)
                elif act == "expect_hidden":
                    expect(_el(pg, st["sel"])).not_to_be_visible(timeout=5000)
                pg.wait_for_timeout(150)
            except Exception as e:  # noqa: BLE001 —— 步骤失败如实记录后整场景判失败
                err = (f"{{type(e).__name__}}: {{e}}")[:300]
            shot = os.path.join(sdir, f"step_{{st['i']}}_{{act}}.png")
            try:
                pg.screenshot(path=shot, full_page=True)
            except Exception:  # noqa: BLE001 —— 截图失败不掩盖步骤结果
                shot = ""
            rec["steps"].append({{"i": st["i"], "action": act,
                                  "selector": st.get("selector", ""),
                                  "verdict": "failed" if err else "passed",
                                  "error": err, "screenshot": shot,
                                  "elapsed_ms": int((time.perf_counter() - t0) * 1000)}})
            if err:
                rec["verdict"] = "failed"
                rec["error"] = err
                rec["screenshot_final"] = os.path.join(sdir, "failure.png")
                try:
                    pg.screenshot(path=rec["screenshot_final"], full_page=True)
                except Exception:  # noqa: BLE001
                    pass
                _save_record(rec, name)
                raise AssertionError(f"步骤 {{st['i']}} {{act}} 失败: {{err}}")
        pg.wait_for_timeout(200)
        rec["verdict"] = "passed"
        rec["error"] = ""
        rec["screenshot_final"] = os.path.join(sdir, "final.png")
        pg.screenshot(path=rec["screenshot_final"], full_page=True)
        _save_record(rec, name)
    finally:
        try:
            ctx.tracing.stop(path=rec["trace_path"])
        except Exception:  # noqa: BLE001 —— trace 缺席不掩盖步骤结果
            pass


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_ui_scenario(sc, page):
    # 场景清单：{ "、".join(s["name"] for s in payload) }（步骤数据经真值闸核验，虚构元素已在闸层剔除）
    pg, ctx = page
    _run_steps(pg, ctx, sc)
'''


# —— ②提取层：DOM 快照采集（可达性树 + DOM id 清单 → UI 版端点清单） ——

_SNAP_SCRIPT = r'''
import json
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
SKIP = {"generic", "text", "StaticText", "InlineTextBox", "WebArea"}


def walk(node, out):
    if not isinstance(node, dict):
        return
    role, name = node.get("role"), (node.get("name") or "").strip()
    if role and name and role not in SKIP:
        out["roles"].append({"role": role, "name": name})
        if name not in out["texts"]:
            out["texts"].append(name)
    for ch in node.get("children") or []:
        walk(ch, out)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    page = b.new_page()
    page.goto(URL, wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(300)
    out = {"url": URL, "title": page.title(), "roles": [], "ids": [], "texts": []}
    walk(page.accessibility.snapshot(), out)
    out["ids"] = page.evaluate(
        "() => Array.from(document.querySelectorAll('[id]')).map(e => e.id)")
    print("@@SNAP_JSON@@" + json.dumps(out, ensure_ascii=False))
    b.close()
'''


def collect_dom_snapshot(runner, container, url: str, timeout_s: int = 60) -> dict:
    """容器内跑 playwright 采集可达性树快照（真值清单单一真源）。"""
    runner.exec_in(container, ["sh", "-c",
                               f"cat > /tmp/.mta_snap.py << 'MTA_SNAP_EOF'\n{_SNAP_SCRIPT}\nMTA_SNAP_EOF"],
                   timeout_s=15, workdir="/")
    r = runner.exec_in(container, ["python3", "/tmp/.mta_snap.py", url],
                       timeout_s=timeout_s, workdir="/work")
    marker = "@@SNAP_JSON@@"
    line = next((ln for ln in (r.stdout or "").splitlines()
                 if ln.startswith(marker)), None)
    if line is None:
        raise RuntimeError(f"DOM 快照采集失败（exit={r.exit_code}）："
                           f"{(r.stderr or r.stdout)[-300:]}")
    return json.loads(line[len(marker):])


# —— ③生成层：LLM 按真值清单生成操作序列（调用方侧；输出即过闸） ——

from .llm_client import make_chat as _make_chat  # noqa: E402 —— 测试 monkeypatch 此符号


def _parse_llm_json(text: str):
    """剥 ```json 围栏后解析 {scenarios:[...]}；失败返回 None（诚实，不猜）。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data.get("scenarios") if isinstance(data, dict) else None


def gen_ui_scenarios(api_key: str, snapshot: dict, requirement: str,
                     base_url: str = "", attempts: int = 3, min_scenarios: int = 3):
    """LLM 生成操作序列（只读注入快照事实=事实注入，无经验性语言 prompt）；输出即过闸。

    最少场景数在代码层强制（X2 需求显式化结论复现：LLM 会合并/省略功能覆盖，
    prompt 祈祷无效→闸层反馈重试）；LLM 输出完全不可解析 → RuntimeError（诚实失败）。
    返回 (kept, rejected_rows)。"""
    roles_desc = "\n".join(f'- role={r["role"]} name="{r["name"]}"'
                           for r in (snapshot.get("roles") or [])[:60])
    ids_desc = "、".join(snapshot.get("ids") or []) or "（无）"
    texts_desc = "、".join(snapshot.get("texts") or [])[:600] or "（无）"
    sysmsg = (
        "你是 UI 黑盒测试用例生成器。铁律：每步 selector 只能引用给定真值清单中真实存在的元素，"
        "虚构任何元素=整案作废。selector 只允许三种语法：role=<role> name=\"<精确名称>\"、"
        "text=<精确文本>、#<元素id>。动作只允许：goto/click/fill/expect_visible/expect_text/expect_hidden。"
        "断言纪律（正例锚定）：新增项的存在性用 expect_visible（text=<新增文本> 或 "
        "role=button name=\"删除 <新增文本>\"）；expect_text 只用于单个叶子元素的精确文案，"
        "禁止对列表/容器元素使用（容器文本包含多项，精确匹配必失败）；"
        "删除后的消失断言用 expect_hidden。场景覆盖纪律：至少 3 条场景，"
        "需求中的每条功能（渲染/添加/删除等）各至少 1 条场景覆盖，不得合并省略。只输出 JSON："
        "{\"scenarios\":[{\"name\":\"场景名\",\"steps\":[...]}]}，不要任何解释。")
    feedback = ""
    last_parse_text = ""
    example = ('字段示例（goto 用 url 字段，不用 selector；fill 用 value；expect_text 用 text）：\n'
               '{"scenarios":[{"name":"示例","steps":['
               '{"action":"goto","url":"' + (base_url or snapshot.get("url", "http://x/")) + '"},'
               '{"action":"fill","selector":"role=textbox name=\\"输入框\\"","value":"内容"},'
               '{"action":"click","selector":"role=button name=\\"按钮\\""},'
               '{"action":"expect_visible","selector":"text=\\"结果\\""}]}]}')
    for attempt in range(1, attempts + 1):
        usr = (f"需求：\n{requirement}\n\n页面基址：{base_url or snapshot.get('url', '')}\n\n"
               f"真值清单（唯一合法元素来源）——\n角色元素：\n{roles_desc}\n\n"
               f"id 清单：{ids_desc}\n可见文本：{texts_desc}\n\n{example}\n")
        if feedback:
            usr += (f"\n上一轮输出存在以下问题，必须修正后重新输出全部场景：\n{feedback}")
        chat = _make_chat(api_key=api_key, temperature=0,
                          timeout=120, max_tokens=4096, phase="ui_gen")
        resp = chat.invoke([("system", sysmsg), ("user", usr)])
        last_parse_text = getattr(resp, "content", "") or ""
        scens = _parse_llm_json(last_parse_text)
        if scens is None:
            feedback = ("输出不是完整可解析的 {\"scenarios\":[...]} JSON"
                        "（可能被截断——请精简输出，每个场景只保留 name/steps 必要字段）")
            continue
        kept, rejected = scenario_gate(scens, snapshot)
        if kept and len(kept) >= min_scenarios:
            return kept, rejected
        probs = []
        if kept and len(kept) < min_scenarios:
            probs.append(f"仅 {len(kept)} 条场景（要求 ≥{min_scenarios} 条）："
                         "需求中的每条功能必须各出 1 条场景，不得合并进一条场景")
        if rejected:
            probs.append("以下内容被真值闸剔除：\n"
                         + "\n".join(r["rejected_reason"] for r in rejected[:5]))
        feedback = "\n".join(probs)
    if last_parse_text and _parse_llm_json(last_parse_text) is None:
        raise RuntimeError("LLM 场景生成失败：输出不可解析为场景 JSON "
                           f"（最后输出片段：{last_parse_text[:200]}）")
    raise RuntimeError(f"LLM 场景生成未能在 {attempts} 轮内产出 ≥{min_scenarios} 条过闸场景"
                       f"（最后一轮问题：{feedback[:200]}）")


# —— ⑤落库层：junit+步骤记录 → ui_asserts 行 ——

def merge_ui_rows(junit_text: str, steps_payload, trace_dir: str = "") -> list[dict]:
    """合成落库行：场景级（junitxml 权威 verdict）+ 步骤级（渲染文件逐步骤记录）。

    junit 不可解析 → 场景级行如实缺席（不伪造 verdict），步骤行保留。"""
    from .case_results import parse_junit_xml
    tcs = parse_junit_xml(junit_text or "")
    if not isinstance(steps_payload, list):
        steps_payload = []
    # 场景名映射从 nodeid 取（parse_junit_xml 的 name 已剥参数化后缀，"test_ui_scenario[场景名]"
    # → "test_ui_scenario"，直接用会全部失配——真机实锤 2026-11-07）；
    # pytest junitxml 把中文转义成 \uXXXX 字面量（真机实锤二连），需反转义后匹配
    by_scen = {}
    for tc in tcs:
        nm = (tc.get("nodeid") or tc["name"]).split("::")[-1]
        if "[" in nm:
            cand = nm.split("[", 1)[1].rsplit("]", 1)[0]
            cand = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), cand)
        else:
            cand = re.sub(r"^test_\d+_", "", nm)
        by_scen.setdefault(cand, tc)
    rows: list[dict] = []
    for rec in steps_payload:
        scen = rec.get("scenario", "")
        trace = rec.get("trace_path") or (
            f"{trace_dir}/{scen}/trace.zip" if trace_dir else "")
        tc = by_scen.get(scen)
        if tc is not None:
            failed = tc["status"] != "passed"
            shot = rec.get("screenshot_final") or (
                f"{trace_dir}/{scen}/{'failure' if failed else 'final'}.png" if trace_dir else "")
            rows.append({"scenario": scen, "step_no": 0, "action": "scene", "selector": "",
                         "verdict": tc["status"], "error": (tc.get("message") or "")[:500],
                         "screenshot_path": shot, "trace_path": trace,
                         "rejected_reason": "", "elapsed_ms": int(tc.get("duration_ms") or 0)})
        for st in rec.get("steps") or []:
            rows.append({"scenario": scen, "step_no": int(st.get("i", 0)) + 1,
                         "action": st.get("action", ""), "selector": st.get("selector", ""),
                         "verdict": st.get("verdict", "failed"),
                         "error": (st.get("error") or "")[:500],
                         "screenshot_path": st.get("screenshot") or (
                             f"{trace_dir}/{scen}/step_{st.get('i', 0)}_{st.get('action', '')}.png"
                             if trace_dir else ""),
                         "trace_path": "", "rejected_reason": "",
                         "elapsed_ms": int(st.get("elapsed_ms") or 0)})
    return rows


def store_ui_asserts(conn, task_id: int, round_no: int, rows: list[dict]) -> None:
    """ui_asserts 第 30 表落库；幂等口径与 app_logs/db_asserts 一致
    （round≤1 清全任务、round>1 清同轮）。"""
    if round_no <= 1:
        conn.execute("DELETE FROM ui_asserts WHERE task_id=?", (task_id,))
    else:
        conn.execute("DELETE FROM ui_asserts WHERE task_id=? AND round_no=?", (task_id, round_no))
    for r in rows:
        conn.execute(
            """INSERT INTO ui_asserts(task_id, round_no, scenario, step_no, action, selector,
                                      verdict, error, screenshot_path, trace_path,
                                      rejected_reason, elapsed_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, round_no, r.get("scenario", ""), int(r.get("step_no") or 0),
             r.get("action", ""), r.get("selector", ""), r.get("verdict", "failed"),
             (r.get("error") or "")[:500], (r.get("screenshot_path") or "")[:300],
             (r.get("trace_path") or "")[:300], (r.get("rejected_reason") or "")[:500],
             int(r.get("elapsed_ms") or 0)))
    conn.commit()


# —— 缺陷报告单（失败证据进报告；与 defect_handoff 同纪律：引用证据、不叙述 vibes） ——

def build_defect_report(requirement: str, rows: list[dict], evidence_root: str,
                        out_dir: str):
    """失败场景 → 缺陷报告单（markdown，证据=截图/trace 路径）；无疑似缺陷返回 None 不造文档。"""
    scen_rows = [r for r in rows if r.get("step_no") == 0 and r.get("verdict") == "failed"]
    if not scen_rows:
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    lines = [
        f"# 缺陷报告单（UI 黑盒）— {ts}",
        "",
        "> 口径：UI 场景真实执行（pytest+playwright）失败的场景；证据=逐步截图/trace.zip/"
        "可达性快照。通过场景不进清单。",
        "",
        "## 1. 元信息",
        f"- 需求：{(requirement or '').strip()[:200]}",
        f"- 证据目录：{evidence_root}",
        "",
        "## 2. 汇总",
        f"- 执行场景 {len([r for r in rows if r.get('step_no') == 0])} 个，"
        f"失败 {len(scen_rows)} 个",
        "",
        "## 3. 缺陷清单",
    ]
    for i, r in enumerate(scen_rows, 1):
        scen = r["scenario"]
        fail_steps = [s for s in rows
                      if s.get("scenario") == scen and s.get("step_no", 0) > 0
                      and s.get("verdict") == "failed"]
        lines += ["", f"### 缺陷 {i}　{scen}",
                  f"- 判定：场景真实执行失败（verdict={r.get('verdict')}）"]
        for s in fail_steps:
            lines.append(f"- 失败步骤 #{s['step_no']} {s['action']} `{s['selector']}`"
                         f"（耗时 {s.get('elapsed_ms', 0)}ms）")
        if r.get("error"):
            lines.append(f"- 错误：{r['error'][:300]}")
        lines.append("- 预期 vs 实际：按需求文本该场景应成立，实际执行失败"
                     "（UI 反馈未按预期发生——按钮失效/元素缺失/文案不符）")
        shots = [s.get("screenshot_path") for s in fail_steps if s.get("screenshot_path")]
        if r.get("screenshot_path"):
            shots.append(r["screenshot_path"])
        lines.append(f"- 证据：trace=`{r.get('trace_path')}`"
                     + (f"｜截图={ '、'.join(shots[:6]) }" if shots else ""))
        lines.append("- 修复提示：请优先核对失败步骤对应的交互处理器（onclick/handler）"
                     "是否抛错或未绑定，再核对元素 id/文案是否与需求一致。")
    lines += ["", "## 4. 局限与免责",
              "- 本单仅收「真实执行失败」的场景，判定依据=junitxml 与逐步截图/trace，"
              "不含任何推测性叙述；用例自身错误（如 selector 语义写偏）不属于产品缺陷。",
              ""]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fname = f"缺陷报告单-{ts}.md"
    (out / fname).write_text("\n".join(lines), encoding="utf-8")
    return fname


# —— ④执行层编排：渲染→写入容器→pytest→采集证据→合成行 ——

def run_ui_scenarios(runner, container, kept: list[dict], base_url: str,
                     round_no: int, evidence_dir: str):
    """执行编排；返回 (rows, meta)。证据从容器拉回宿主 evidence_dir（即 ui_evidence 目录）。"""
    import io
    import shutil
    import tarfile
    import tempfile

    steps_path = f"/work/ui_steps_r{round_no}.json"
    rendered = render_pytest(kept, base_url, CONT_EVID, steps_path=steps_path)
    runner.exec_in(container, ["sh", "-c",
                               f"rm -rf {CONT_EVID} && mkdir -p {CONT_EVID} && "
                               f"rm -f {steps_path} && "
                               f"cat > /work/ui_test_r{round_no}.py << 'MTA_UI_EOF'\n"
                               f"{rendered}\nMTA_UI_EOF"],
                   timeout_s=20, workdir="/")
    r = runner.exec_in(container,
                       ["python3", "-m", "pytest", f"/work/ui_test_r{round_no}.py",
                        f"--junitxml=/tmp/ui_junit_r{round_no}.xml", "-q", "--tb=short"],
                       timeout_s=420, workdir="/work")
    jr = runner.exec_in(container, ["sh", "-c",
                                    f"cat /tmp/ui_junit_r{round_no}.xml 2>/dev/null"],
                        timeout_s=10, workdir="/")
    sr = runner.exec_in(container, ["sh", "-c", f"cat {steps_path} 2>/dev/null"],
                        timeout_s=10, workdir="/")
    try:
        steps_payload = json.loads(sr.stdout) if (sr.stdout or "").strip() else []
    except json.JSONDecodeError:
        steps_payload = []
    # 证据拉回宿主（截图/trace.zip——容器 /work/ui_evidence → 宿主 evidence_dir）
    # 只按场景子目录刷新，不清整个宿主证据目录（多轮调用时 junit 留档/快照/缺陷单保留）
    host_dir = Path(evidence_dir)
    res, _ = container.get_archive(CONT_EVID)
    bits = res if isinstance(res, bytes) else b"".join(res)
    host_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(fileobj=io.BytesIO(bits)) as tf:
            tf.extractall(td)
        src = Path(td) / "ui_evidence"
        if src.exists():
            for item in src.iterdir():
                dest = host_dir / item.name
                if dest.exists():
                    shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                shutil.move(str(item), str(dest))
    rows = merge_ui_rows(jr.stdout, steps_payload, trace_dir=CONT_EVID)
    # 容器路径 → 宿主证据目录路径（截图/trace 落库可追溯）
    for row in rows:
        for k in ("screenshot_path", "trace_path"):
            v = row.get(k) or ""
            if v.startswith(CONT_EVID):
                row[k] = str(host_dir / v[len(CONT_EVID):].lstrip("/"))
    meta = {"pytest_exit": r.exit_code, "junit_found": bool((jr.stdout or "").strip()),
            "steps_found": bool(steps_payload)}
    return rows, meta


# —— SkillRegistry 入口（生成在调用方侧；本 skill 复闸+执行+落库+缺陷单） ——

def ui_assert(api_key: str, args: dict, task_id: int, runner) -> str:
    container = runner.client.containers.get(args["container_name"])
    round_no = int(args.get("round_no") or 1)
    url = args["url"]
    evidence_dir = args["evidence_dir"]
    snapshot = args.get("snapshot")
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    scenarios = args.get("scenarios")
    if isinstance(scenarios, str):
        scenarios = json.loads(scenarios)

    kept, rejected = scenario_gate(scenarios, snapshot)
    rows = list(rejected)
    meta: dict = {}
    if kept:
        exec_rows, meta = run_ui_scenarios(runner, container, kept, url,
                                           round_no, evidence_dir)
        rows += exec_rows
    from ..store.db import get_conn
    conn = get_conn()
    try:
        store_ui_asserts(conn, task_id, round_no, rows)
    finally:
        conn.close()
    n_scene = [r for r in rows if r.get("step_no") == 0]
    defect_md = None
    if any(r["verdict"] == "failed" for r in n_scene):
        defect_md = build_defect_report(args.get("requirement", ""), rows,
                                        evidence_root=evidence_dir, out_dir=evidence_dir)
    return json.dumps({
        "executed": len([r for r in n_scene if r["verdict"] != "rejected"]),
        "passed": len([r for r in n_scene if r["verdict"] == "passed"]),
        "failed": len([r for r in n_scene if r["verdict"] == "failed"]),
        "rejected": len([r for r in n_scene if r["verdict"] == "rejected"]),
        "pytest_exit": meta.get("pytest_exit"), "defect_report": defect_md,
        "evidence_dir": evidence_dir,
        "rows": rows[:40],
    }, ensure_ascii=False)
