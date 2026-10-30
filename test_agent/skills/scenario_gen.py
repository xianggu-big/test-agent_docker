"""L3 业务流场景生成与冒烟（D5 半自动：冒烟自动判定 + 失败人工介入）。

纯函数 + LLM 调用薄层，图节点在 graph/l3.py。
静态闸铁律：场景每一步的 method+path 必须命中端点清单（来自 L2 D4 降级链），
LLM 猜的接口一律剔除——端到端场景不允许虚构 API。
人工确认语义：冒烟全步骤通过=auto_pass；任一步失败=pending_confirm（人工介入
判断是缺陷还是场景问题），确认结论三选一（confirmed_pass/confirmed_defect/
confirmed_scenario）回库参与评估；分母口径（P3-3 对齐）：pending（未生成）剔出
通过率分母，pending_confirm（待人工定性）计入分母——保守方向，通过率不虚高。
"""
import json
import logging
import re

from ..skills.llm_usage import log_llm_call

log = logging.getLogger(__name__)

SCENARIO_SYSTEM = (
    "你是资深系统测试工程师（L3 业务流场景设计）。依据需求与可用接口清单，"
    "设计端到端业务流场景：每个场景是一串有序的 API 调用（method+path 只能来自"
    "可用接口清单，禁止虚构接口），覆盖关键业务路径。只输出 JSON。"
)


def _schema_digest(ep: dict) -> str:
    """请求体 schema 的紧凑摘要 + 具体示例 body。

    模型抄示例远比遵循字段清单忠实（POST /orders 三连 422 的实锤修法）：
    示例值确定性生成——integer 取 minimum（无则 100/1 按 id 类区分），string 取短字面值。
    """
    schema = ep.get("request_schema")
    if not schema:
        return "（无请求体）"
    try:
        if isinstance(schema, str):
            schema = json.loads(schema)
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if not props:
            return "（无请求体字段）"
        fields = ", ".join(
            f"{k}:{(v or {}).get('type', '?')}" + ("*" if k in required else "")
            for k, v in list(props.items())[:8])
        example = {}
        for i, (k, v) in enumerate(props.items()):
            v = v or {}
            typ = v.get("type", "string")
            if k.endswith("_id") or k == "id":
                # 外键/主键字段不能给字面示例：模型会照抄（corpus-003 实测 room_id=100
                # 导致创建预约 404）。给占位符，冒烟执行器从 ctx/上一步响应替换。
                example[k] = f"{{{k}}}"
            elif typ == "integer":
                example[k] = v.get("minimum", 0) if k == "amount" else (100 if i == 0 else 1)
            elif typ == "number":
                example[k] = v.get("minimum", 0) or 10
            elif typ == "boolean":
                example[k] = True
            else:
                example[k] = f"{k[:8]}{i + 1}"
        ordered = {k: example[k] for k in example if k in required} or example
        return (f"请求体字段: {fields}（* =必填）｜"
                f"示例body: {json.dumps(ordered, ensure_ascii=False)}")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return "（schema 不可解析）"


def build_scenario_prompt(requirement: str, endpoints: list[dict],
                          max_scenarios: int = 5) -> str:
    eps = "\n".join(
        f"- {e['method']} {e['path']}｜{_schema_digest(e)}" for e in endpoints)
    return f"""## 需求
{requirement}

## 可用接口清单（steps 中的 method+path 只能取自这里；body 必须满足请求体字段）
{eps}

## 任务
设计不超过 {max_scenarios} 个端到端业务流场景（关键路径冒烟）：每个场景是 2~4 步
有序 API 调用（前一步的输出可作为后一步的输入），说明场景预期（业务叙述）。
需求含糊处按最保守业务语义设计并在 expected 注明假设。

## 负例纪律（强制）
期望 4xx/拒绝 的负例场景：path 直接写字面上不存在/非法的字面值（如 /coupons/999999），
禁止用 {{占位符}}——{{占位符}} 只用于引用前一步创建的真实资源（冒烟会自动替换）。
body 必须严格按上面标注的请求体字段构造（必填字段齐全、类型正确），
可直接复制「示例body」并按场景微调数值。
外键字段纪律：正例里引用前一步创建资源的 *_id（如 room_id/coupon_id）必须写
占位符 {{room_id}}/{{coupon_id}}，禁止照抄示例里的字面 id；只有负例（期望 4xx）
才写确切的字面非法 id。
每一步都必须标注 expect_status（来自需求契约：正例 200/201，负例按契约写 400/404 等）。
expected 状态码来自需求契约而非猜测。"""


def _created_resources(path: str) -> set[str]:
    """POST 路径的资源名（/rooms → {rooms, room}），供 *_id 外键引用判定。"""
    seg = (path or "").strip("/").split("/")[0]
    if not seg:
        return set()
    return {seg, seg[:-1]} if seg.endswith("s") else {seg}


def inject_body_refs(steps: list[dict]) -> list[dict]:
    """确定性外键引用注入：正例 body 的 room_id/coupon_id 等 → `{字段名}` 占位符。

    背景（corpus-003 实测）：提示词示例 body 的 room_id 是字面 100，模型照抄后
    创建预约 404，整条 L3 链路假失败。规则：仅当（a）该步骤期望 2xx/未标状态码，
    （b）body 字段名以 `_id` 结尾，（c）前面已有 POST 创建设同名资源（单复数），
    （d）值不是占位符——才替换。负例（期望 4xx）保持字面 id，不破坏“不存在 id”意图。
    """
    created: set[str] = set()
    out: list[dict] = []
    for st in steps:
        body = st.get("body")
        want = st.get("expect_status")
        try:
            positive = want is None or 200 <= int(want) < 300
        except (TypeError, ValueError):
            positive = True

        def walk(obj):
            if isinstance(obj, dict):
                fixed = {}
                for k, v in obj.items():
                    if (positive and isinstance(k, str) and k.endswith("_id")
                            and k[:-3] in created
                            and not (isinstance(v, str) and v.startswith("{"))):
                        fixed[k] = f"{{{k}}}"
                    else:
                        fixed[k] = walk(v)
                return fixed
            if isinstance(obj, list):
                return [walk(v) for v in obj]
            return obj

        if positive and body is not None:
            st = {**st, "body": walk(body)}
        out.append(st)
        if str(st.get("method", "")).upper() == "POST":
            created |= _created_resources(st.get("path") or "")
    return out


def parse_scenarios(text: str, endpoints: list[dict]) -> tuple[list[dict], list[str]]:
    """解析 LLM 输出并过静态闸。返回 (通过场景, 剔除理由列表)。"""
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return [], ["LLM 输出不可解析"]
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], ["LLM 输出 JSON 解析失败"]
    allowed = {f"{e['method'].upper()} {e['path']}" for e in endpoints}
    valid, rejected = [], []
    for sc in data.get("scenarios", []):
        if not isinstance(sc, dict):
            continue
        # 模型漂移容错（D8 同款教训）：title/name 双键兼容，格式规约约束不住漂移
        sc = {**sc, "title": (sc.get("title") or sc.get("name") or "").strip()}
        if not sc["title"]:
            continue
        steps = sc.get("steps") or []
        bad = [s for s in steps
               if not isinstance(s, dict)
               or f"{str(s.get('method', '')).upper()} {s.get('path', '')}" not in allowed]
        if not steps:
            rejected.append(f"{sc.get('title')}: 无步骤")
        elif bad:
            b = bad[0]
            rejected.append(f"{sc.get('title')}: 步骤 {b.get('method')} {b.get('path')} "
                            f"不在端点清单（虚构接口）")
        else:
            norm = inject_body_refs(
                [{**s, "method": str(s["method"]).upper()} for s in steps])
            valid.append({"title": sc["title"],
                          "requirement_ref": sc.get("requirement_ref"),
                          "steps": norm, "expected": sc.get("expected")})
    return valid, rejected


def save_scenarios(conn, task_id: int, scenarios: list[dict], level: str = "L3") -> int:
    """落库（重跑幂等：只重置 pending 行，人工确认过的行原样保留——D5 结论不丢）。"""
    conn.execute(
        """DELETE FROM scenario_cases WHERE task_id=? AND level=? AND status NOT IN
           ('confirmed_pass','confirmed_defect','confirmed_scenario')""",
        (task_id, level))
    n = 0
    for sc in scenarios:
        conn.execute(
            """INSERT INTO scenario_cases(task_id, level, title, requirement_ref,
                                          steps, expected)
               VALUES (?,?,?,?,?,?)""",
            (task_id, level, sc["title"], sc.get("requirement_ref"),
             json.dumps(sc["steps"], ensure_ascii=False), sc.get("expected")))
        n += 1
    conn.commit()
    return n


def build_smoke_script(scenarios: list[dict], base_url: str) -> str:
    """冒烟执行脚本：逐场景顺序调用 API，任一步非 2xx/预期码即场景失败。

    引用链：前一步 2xx 的 JSON 响应并入 ctx；路径占位符 {name} 从 ctx 替换，
    {xxx_id} 类兜底取最近一次响应对象的 id 字段——治"字面 {cid} 请求 422"的实锤缺口。
    输出 JSON 到 stdout 供宿主解析（容器内 requests 已由 provision 安装）。"""
    return (
        "import json, re, requests\n"
        f"BASE = {base_url!r}\n"
        "ctx = {}\n"
        "last_obj = {}\n"
        "def sub(path):\n"
        "    def rep(m):\n"
        "        name = m.group(1)\n"
        "        if name in ctx:\n"
        "            return str(ctx[name])\n"
        "        if name.endswith('id') or name.endswith('_id'):\n"
        "            v = last_obj.get('id')\n"
        "            if v is not None:\n"
        "                return str(v)\n"
        "        return m.group(0)\n"
        "    return re.sub(r'\\{([^}]+)\\}', rep, path)\n"
        "def sub_body(b):\n"
        "    if isinstance(b, str):\n"
        "        m = re.fullmatch(r'\\{([^}]+)\\}', b)\n"
        "        if m:\n"
        "            name = m.group(1)\n"
        "            if name in ctx:\n"
        "                return ctx[name]           # 整段占位符：保留原始类型（int id 等）\n"
        "            if name.endswith('id') or name.endswith('_id'):\n"
        "                v = last_obj.get('id')\n"
        "                if v is not None:\n"
        "                    return v\n"
        "        return sub(b)\n"
        "    if isinstance(b, dict):\n"
        "        return {k: sub_body(v) for k, v in b.items()}\n"
        "    if isinstance(b, list):\n"
        "        return [sub_body(v) for v in b]\n"
        "    return b\n"
        f"SCENARIOS = json.loads({json.dumps(json.dumps(scenarios, ensure_ascii=False))})\n"
        "out = []\n"
        "for sc in SCENARIOS:\n"
        "    steps_r = []\n"
        "    ok = True\n"
        "    for st in sc['steps']:\n"
        "        url = BASE + sub(st['path'])\n"
        "        try:\n"
        "            r = requests.request(st['method'], url, json=sub_body(st.get('body')),\n"
        "                                 timeout=15)\n"
        "            want = st.get('expect_status')\n"
        "            good = (r.status_code // 100 == 2) if not want else r.status_code == want\n"
        "            if good and (r.status_code // 100 == 2):\n"
        "                try:\n"
        "                    obj = r.json()\n"
        "                    if isinstance(obj, dict):\n"
        "                        ctx.update(obj)\n"
        "                        last_obj.clear()\n"
        "                        last_obj.update(obj)\n"
        "                except Exception:\n"
        "                    pass\n"
        "            steps_r.append({'step': (sub(f\"{st['method']} {st['path']}\")\n"
        "                                     if '{' in st['path']\n"
        "                                     else f\"{st['method']} {st['path']}\"),\n"
        "                            'status': r.status_code, 'ok': bool(good),\n"
        "                            'body': st.get('body'),\n"
        "                            'resp': r.text[:150]})\n"
        "            if not good:\n"
        "                ok = False\n"
        "                break\n"
        "        except Exception as e:\n"
        "            steps_r.append({'step': f\"{st['method']} {st['path']}\",\n"
        "                            'status': None, 'ok': False, 'error': str(e)[:120]})\n"
        "            ok = False\n"
        "            break\n"
        "    out.append({'title': sc['title'], 'ok': ok, 'steps': steps_r})\n"
        "print('SMOKE_JSON=' + json.dumps(out, ensure_ascii=False))\n"
    )


def apply_smoke_results(conn, task_id: int, results: list[dict], level: str = "L3") -> dict:
    """D5 分流：全步骤通过=auto_pass；任一步失败=pending_confirm（人工介入）。
    已确认行不受冒烟重跑影响（人工结论优先）。
    P3-2（2026-10-31 体检）：冒烟脚本只携带 title——同名场景多行时按 title 匹配会状态
    串写。同名（结果内重复或库内重复）一律不自动套用，全部转人工（D5 语义的保守面）。"""
    auto_pass = pending = 0
    titles_in_results = [r.get("title") for r in results]
    dup_titles = {t for t in titles_in_results if titles_in_results.count(t) > 1}
    if titles_in_results:
        ph = ",".join("?" * len(set(titles_in_results)))
        for t, in conn.execute(
                f"""SELECT title FROM scenario_cases
                    WHERE task_id=? AND level=? AND title IN ({ph})
                    GROUP BY title HAVING COUNT(*) > 1""",
                (task_id, level, *set(titles_in_results))):
            dup_titles.add(t)
    for r in results:
        status = "auto_pass" if r.get("ok") else "pending_confirm"
        if r.get("title") in dup_titles:
            # 同名场景：自动判定不可信 → 全部转人工（已确认行仍由 status 过滤保护）
            status = "pending_confirm"
        conn.execute(
            """UPDATE scenario_cases SET status=?, last_result=? 
               WHERE task_id=? AND level=? AND title=? AND status IN
               ('pending','auto_pass','pending_confirm')""",
            (status, json.dumps(r, ensure_ascii=False), task_id, level, r.get("title")))
        if status == "auto_pass":
            auto_pass += 1
        else:
            pending += 1
    conn.commit()
    return {"auto_pass": auto_pass, "pending_confirm": pending,
            "duplicate_titles_manual": len(dup_titles)}


def confirm_scenario(conn, scenario_id: int, result: str, note: str | None,
                     user_id: int) -> dict:
    """人工确认（D5 唯一人工写入点）：pass/defect/scenario 三选一回库。"""
    mapping = {"pass": "confirmed_pass", "defect": "confirmed_defect",
               "scenario": "confirmed_scenario"}
    if result not in mapping:
        raise ValueError(f"result 必须是 {list(mapping)} 之一")
    row = conn.execute("SELECT status, level FROM scenario_cases WHERE id=?",
                       (scenario_id,)).fetchone()
    if row is None:
        raise LookupError(f"场景 {scenario_id} 不存在")
    if row["status"] not in ("pending_confirm", "auto_pass", "pending",
                             "confirmed_pass", "confirmed_defect", "confirmed_scenario"):
        raise ValueError(f"当前状态 {row['status']} 不可确认")
    from datetime import datetime
    conn.execute(
        """UPDATE scenario_cases SET status=?, actual_note=?, confirmed_by=?,
           confirmed_at=? WHERE id=?""",
        (mapping[result], note, user_id, datetime.now().isoformat(" ", "seconds"),
         scenario_id))
    conn.commit()
    return {"scenario_id": scenario_id, "status": mapping[result]}


def l3_metrics(conn, task_id: int, level: str = "L3") -> dict | None:
    """按级指标：场景通过率 = (auto_pass+confirmed_pass) / (total - pending)。
    无场景行返回 None（整体缺席语义，叙述如实说无数据）。"""
    rows = conn.execute(
        "SELECT status FROM scenario_cases WHERE task_id=? AND level=?",
        (task_id, level)).fetchall()
    if not rows:
        return None
    n = {"total": len(rows), "auto_pass": 0, "pending_confirm": 0,
         "confirmed_pass": 0, "confirmed_defect": 0, "confirmed_scenario": 0,
         "pending": 0}
    for r in rows:
        k = r["status"] if r["status"] in n else "pending"
        n[k] += 1
    denom = n["total"] - n["pending"]
    n["pass_rate"] = round((n["auto_pass"] + n["confirmed_pass"]) / denom, 3) \
        if denom else None
    return n


def gen_scenarios_llm(requirement: str, endpoints: list[dict],
                      llm_fn, task_id: int, max_scenarios: int = 5
                      ) -> tuple[list[dict], list[str], int]:
    """LLM 生成 → 静态闸。返回 (通过场景, 剔除理由, llm_calls)。"""
    if llm_fn is None or not endpoints:
        return [], (["无 LLM 密钥"] if llm_fn is None else ["无可用端点"]), 0
    raw = llm_fn(build_scenario_prompt(requirement, endpoints, max_scenarios))
    valid, rejected = parse_scenarios(raw, endpoints)
    return valid, rejected, 1


# ---------------------------------------------------------------------------
# L4 验收辅助（D5/D6）：验收条件 → GWT 场景清单 → 人工确认 → 确定性转化回归脚本
# ---------------------------------------------------------------------------

ACCEPTANCE_SYSTEM = (
    "你是验收测试设计师（L4 验收辅助）。依据验收条件与可用接口清单，"
    "把每条验收条件转化为 Given/When/Then 场景 + 有序 API 调用序列"
    "（method+path 只能来自可用接口清单，禁止虚构接口）。只输出 JSON。"
)


def build_acceptance_prompt(conditions: list[dict], endpoints: list[dict],
                            max_scenarios: int = 6) -> str:
    cond_text = "\n".join(f"{i}. {c['condition']}（优先级 {c.get('priority') or '中'}）"
                          for i, c in enumerate(conditions, 1))
    eps = "\n".join(
        f"- {e['method']} {e['path']}｜{_schema_digest(e)}" for e in endpoints)
    return f"""## 验收条件（每条至少一个场景，不得遗漏）
{cond_text}

## 可用接口清单（steps 中的 method+path 只能取自这里；body 必须满足请求体字段）
{eps}

## 任务
把每条验收条件转化为 GWT（Given/When/Then）验收场景：when/then 对应有序 API 调用，
每步标注 expect_status（来自验收条件契约）。正例用合法数据，负例在 path 直接写
字面不存在/非法的字面值（如 /coupons/999999），禁止 {{占位符}} 悬空。

## 输出格式
只输出一个 JSON 对象：
{{"scenarios": [{{"title": "验收场景名", "requirement_ref": "对应验收条件编号/原文",
  "gwt": {{"given": "前置状态", "when": "操作", "then": "预期结果"}},
  "steps": [{{"method": "POST", "path": "/coupons", "body": {{}}, "expect_status": 201}}],
  "expected": "验收预期叙述"}}]}}"""


def parse_acceptance(text: str, endpoints: list[dict]) -> tuple[list[dict], list[str]]:
    """解析验收场景（GWT 结构 + 端点静态闸，与 L3 同源闸规则）。"""
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return [], ["LLM 输出不可解析"]
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], ["LLM 输出 JSON 解析失败"]
    allowed = {f"{e['method'].upper()} {e['path']}" for e in endpoints}
    valid, rejected = [], []
    for sc in data.get("scenarios", []):
        if not isinstance(sc, dict):
            continue
        sc = {**sc, "title": (sc.get("title") or sc.get("name") or "").strip()}
        if not sc["title"]:
            continue
        steps = sc.get("steps") or []
        bad = [s for s in steps
               if not isinstance(s, dict)
               or f"{str(s.get('method', '')).upper()} {s.get('path', '')}" not in allowed]
        if not steps:
            rejected.append(f"{sc['title']}: 无步骤")
            continue
        if bad:
            b = bad[0]
            rejected.append(f"{sc['title']}: 步骤 {b.get('method')} {b.get('path')} "
                            f"不在端点清单（虚构接口）")
            continue
        norm = [{**s, "method": str(s["method"]).upper()} for s in steps]
        valid.append({"title": sc["title"], "requirement_ref": sc.get("requirement_ref"),
                      "gwt": sc.get("gwt"), "steps": norm,
                      "expected": sc.get("expected")})
    return valid, rejected


def render_acceptance_module(scenarios: list[dict], base_url: str) -> tuple[str, list[dict]]:
    """D6 确定性转化：confirmed 验收场景（steps 已结构化）→ pytest 回归模块。

    渲染与冒烟执行器同源（引用链 sub_path/sub_body 语义一致），
    差异=产物是 pytest 函数（source='acceptance' 入回归基线）。
    返回 (模块源码, 用例清单)。"""
    cases = []
    for sc in scenarios:
        slug = re.sub(r"\W+", "_", sc["title"]).strip("_") or "acceptance"
        cases.append({"name": f"test_acc_{slug}", "title": sc["title"],
                      "steps": sc["steps"], "expected": sc.get("expected")})
    lines = [
        '"""验收回归脚本（L4 确认场景转化，source=acceptance，D6）。"""',
        "import re",
        "import requests",
        f"BASE = {base_url!r}",
        "ctx = {}",
        "last_obj = {}",
        "",
        "",
        "def sub_path(path):",
        '    return re.sub(r"\\{([^}]+)\\}",',
        "                  lambda m: str(ctx.get(m.group(1), last_obj.get('id', m.group(0)))),",
        "                  path)",
        "",
        "",
        "def sub_body(b):",
        "    if isinstance(b, str):",
        '        m = re.fullmatch(r"\\{([^}]+)\\}", b)',
        "        if m:",
        "            name = m.group(1)",
        "            if name in ctx:",
        "                return ctx[name]",
        "            if name.endswith('id') or name.endswith('_id'):",
        "                v = last_obj.get('id')",
        "                if v is not None:",
        "                    return v",
        "        return m.group(0) if m else b",
        "    if isinstance(b, dict):",
        "        return {k: sub_body(v) for k, v in b.items()}",
        "    if isinstance(b, list):",
        "        return [sub_body(v) for v in b]",
        "    return b",
        "",
        "",
        "def _merge(r):",
        "    try:",
        "        obj = r.json()",
        "        if isinstance(obj, dict):",
        "            ctx.update(obj)",
        "            last_obj.clear()",
        "            last_obj.update(obj)",
        "    except Exception:",
        "        pass",
        "",
        "",
    ]
    for c in cases:
        lines += [f"def {c['name']}():",
                  f'    """验收：{c["expected"] or c["title"]}"""',
                  "    ctx.clear()",
                  "    last_obj.clear()"]
        for st in c["steps"]:
            want = st.get("expect_status")
            chk = f"_r.status_code == {want}" if want is not None \
                else "_r.status_code // 100 == 2"
            lines += [
                f"    _r = requests.request({st['method']!r}, BASE + sub_path({st['path']!r}),",
                f"                           json=sub_body({json.dumps(st.get('body'), ensure_ascii=False)}),",
                "                           timeout=15)",
                f"    assert {chk}, f\"{{_r.status_code}} {{_r.text[:150]}}\"",
                "    _merge(_r)"]
        lines.append("")
    return "\n".join(lines) + "\n", cases


def l4_metrics(conn, task_id: int) -> dict | None:
    """按级指标：验收确认率 = confirmed_* / total。无场景行整体缺席。"""
    rows = conn.execute(
        "SELECT status FROM scenario_cases WHERE task_id=? AND level='L4'",
        (task_id,)).fetchall()
    if not rows:
        return None
    confirmed = sum(1 for r in rows if r["status"].startswith("confirmed"))
    return {"total": len(rows), "confirmed": confirmed,
            "pending": sum(1 for r in rows
                           if not r["status"].startswith("confirmed")),
            "confirm_rate": round(confirmed / len(rows), 3),
            "converted": conn.execute(
                "SELECT COUNT(*) AS n FROM test_scripts WHERE task_id=? AND framework='acceptance'",
                (task_id,)).fetchone()["n"]}
