"""L3 业务流场景生成与冒烟（D5 半自动：冒烟自动判定 + 失败人工介入）。

纯函数 + LLM 调用薄层，图节点在 graph/l3.py。
静态闸铁律：场景每一步的 method+path 必须命中端点清单（来自 L2 D4 降级链），
LLM 猜的接口一律剔除——端到端场景不允许虚构 API。
人工确认语义：冒烟全步骤通过=auto_pass；任一步失败=pending_confirm（人工介入
判断是缺陷还是场景问题），确认结论三选一（confirmed_pass/confirmed_defect/
confirmed_scenario）回库参与评估；pending 不进通过率分母（未定不算账）。
"""
import json
import logging

from ..skills.llm_usage import log_llm_call

log = logging.getLogger(__name__)

SCENARIO_SYSTEM = (
    "你是资深系统测试工程师（L3 业务流场景设计）。依据需求与可用接口清单，"
    "设计端到端业务流场景：每个场景是一串有序的 API 调用（method+path 只能来自"
    "可用接口清单，禁止虚构接口），覆盖关键业务路径。只输出 JSON。"
)


def build_scenario_prompt(requirement: str, endpoints: list[dict],
                          max_scenarios: int = 5) -> str:
    eps = "\n".join(f"- {e['method']} {e['path']}" for e in endpoints)
    return f"""## 需求
{requirement}

## 可用接口清单（steps 中的 method+path 只能取自这里）
{eps}

## 任务
设计不超过 {max_scenarios} 个端到端业务流场景（关键路径冒烟）：每个场景是 2~4 步
有序 API 调用（前一步的输出可作为后一步的输入），说明场景预期（业务叙述）。
需求含糊处按最保守业务语义设计并在 expected 注明假设。

## 输出格式
只输出一个 JSON 对象：
{{"scenarios": [{{"title": "场景名", "requirement_ref": "需求关键句",
  "steps": [{{"method": "POST", "path": "/orders", "body": {{}}, "expect_status": 200}}],
  "expected": "业务预期叙述"}}]}}"""


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
        if not isinstance(sc, dict) or not sc.get("title"):
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
            norm = [{**s, "method": str(s["method"]).upper()} for s in steps]
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
        f"SCENARIOS = json.loads({json.dumps(json.dumps(scenarios, ensure_ascii=False))})\n"
        "out = []\n"
        "for sc in SCENARIOS:\n"
        "    steps_r = []\n"
        "    ok = True\n"
        "    for st in sc['steps']:\n"
        "        url = BASE + sub(st['path'])\n"
        "        try:\n"
        "            r = requests.request(st['method'], url, json=st.get('body') or None,\n"
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
        "                            'status': r.status_code, 'ok': bool(good)})\n"
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
    已确认行不受冒烟重跑影响（人工结论优先）。"""
    auto_pass = pending = 0
    for r in results:
        status = "auto_pass" if r.get("ok") else "pending_confirm"
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
    return {"auto_pass": auto_pass, "pending_confirm": pending}


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
