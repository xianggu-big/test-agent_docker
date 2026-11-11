"""L2 契约黑盒生成（D7 两级断言）——集成层用例生成的核心，蓝图 §3.2。纯函数层，无 DB 无容器。

层结构：
1. 确定性契约轨（零 LLM）：schema → 合法示例值（example/default/enum/format/边界）、
   负例变体（必填缺失/类型违背/越界，预算封顶）、pytest 代码渲染。
2. 顺序场景轨（白盒联动）：POST 创建 → GET /res/{id} 读回，字段一致性断言——
   顺带解决"路径参数端点没有合法 id 可用"的问题（无 create-parent 的路径参数端点诚实跳过）。
3. 需求断言层（可选 LLM，D7 第二级）：从需求文本生成业务语义断言表达式（变量 r/payload），
   独立成用例，expected_basis='需求明示'——失败=product 强信号；LLM 不可用/不可解析 →
   fail-open 契约轨照跑。表达式过静态闸（ast 编译 + 危险词黑名单）。

预期值出处纪律：契约断言 expected_basis='接口契约'（spec 明文状态码/响应 schema），
spec 未记载的负例状态码 expected_basis='常识'；需求断言 expected_basis='需求明示'。
"""
import ast as _ast
import json
import logging
import re

from .gen_tests import _sanitize_name

log = logging.getLogger(__name__)

MAX_NEG_PER_ENDPOINT = 4          # 预算纪律：每端点负例封顶
MAX_REQUIREMENT_ASSERTS = 6       # 需求断言条数封顶
MAX_SEQ_SCENARIOS = 3             # 顺序场景封顶


# ---------------------------------------------------------------------------
# 1) schema → 确定性示例值
# ---------------------------------------------------------------------------

_FORMAT_EXAMPLES = {"date-time": "2026-01-01T00:00:00Z", "date": "2026-01-01",
                    "uuid": "123e4567-e89b-12d3-a456-426614174000",
                    "email": "user@example.com", "uri": "https://example.com/x"}


def example_from_schema(schema, depth: int = 0):
    """schema → 合法示例值（确定性）：example > default > enum[0] > 按类型/格式/边界构造。"""
    if not isinstance(schema, dict) or depth > 8:
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out = {}
        for k, v in props.items():
            if depth == 0 or k in required:      # 顶层带全字段（富 happy path），嵌套只带必填
                v2 = example_from_schema(v, depth + 1)
                if v2 is not None:
                    out[k] = v2
        return out
    if t == "array":
        if (schema.get("minItems") or 0) == 0 and depth > 0:
            return []
        item = example_from_schema(schema.get("items") or {}, depth + 1)
        return [item] if item is not None else []
    if t == "integer":
        return schema.get("minimum", 1)
    if t == "number":
        return schema.get("minimum", 1.5)
    if t == "boolean":
        return True
    if t == "string":
        fmt = schema.get("format")
        if fmt in _FORMAT_EXAMPLES:
            return _FORMAT_EXAMPLES[fmt]
        lo = int(schema.get("minLength") or 0)
        hi = int(schema.get("maxLength") or 16)
        return "x" * max(1, min(lo or 4, hi, 16))
    return None


def _bound_violation(schema):
    """越界负例值：(violation_value, 理由) 或 None。"""
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if t in ("integer", "number"):
        if "maximum" in schema:
            return (schema["maximum"] + 1, f"超过 maximum={schema['maximum']}")
        if "minimum" in schema:
            return (schema["minimum"] - 1, f"低于 minimum={schema['minimum']}")
    if t == "string":
        if "maxLength" in schema:
            return ("x" * (schema["maxLength"] + 1), f"超过 maxLength={schema['maxLength']}")
        if "minLength" in schema and schema["minLength"] > 0:
            return ("", f"低于 minLength={schema['minLength']}")
    return None


def negative_variants(endpoint: dict) -> list[dict]:
    """负例变体（确定性）：必填缺失（逐字段，封顶2）→ 边界越界（逐有界字段）→ 类型违背×1，
    总预算封顶。边界覆盖所有有界必填字段——只测第一个会漏掉其余字段的越界缺陷
    （mini-E6 实锤：percent 上限失效因 code 先占坑而漏检）。
    返回 [{kind, payload, expected_status, basis, reason}]。"""
    schema = endpoint.get("request_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return []
    props = schema.get("properties") or {}
    required = [k for k in (schema.get("required") or []) if k in props]
    happy = example_from_schema(schema) or {}
    doc_codes = [c for c in endpoint.get("documented_codes", []) if c in ("400", "422")]
    neg_status = int(doc_codes[0]) if doc_codes else 422
    basis = "接口契约" if doc_codes else "常识"
    out: list[dict] = []
    for k in required[:2]:                                   # 必填缺失（逐字段，最多 2 条）
        p = dict(happy); p.pop(k, None)
        out.append({"kind": "必填缺失", "payload": p, "expected_status": neg_status,
                    "basis": basis, "reason": f"缺少必填字段 {k}"})
    for k in required:                                       # 边界越界（逐有界字段）
        if len(out) >= MAX_NEG_PER_ENDPOINT:
            break
        v = _bound_violation(props.get(k) or {})
        if v is not None:
            p = dict(happy); p[k] = v[0]
            out.append({"kind": "边界越界", "payload": p, "expected_status": neg_status,
                        "basis": basis, "reason": v[1]})
    if len(out) < MAX_NEG_PER_ENDPOINT:
        for k in required:                                   # 类型违背（首个非字符串必填）
            t = (props.get(k) or {}).get("type")
            if t and t != "string":
                p = dict(happy); p[k] = {"__wrong__": "type"}
                out.append({"kind": "类型违背", "payload": p, "expected_status": neg_status,
                            "basis": basis, "reason": f"{k} 应为 {t} 却传对象"})
                break
    return out[:MAX_NEG_PER_ENDPOINT]


def _required_path_params(endpoint: dict) -> list[dict]:
    return [p for p in endpoint.get("params", [])
            if p.get("in") == "path" and p.get("required")]


def _fk_setup(ep: dict, payload, endpoints: list[dict], happy: dict,
              used: set) -> tuple[list[str], dict]:
    """外键前置数据（v0.1）：payload 里 *_id 字段 → 找对应资源的 POST 创建端点，
    先创建并从响应取 id。返回 (setup 代码行, {fk字段: 变量名})；找不到创建端点 → 空配置
    （此时用例可能撞 404，被如实报为契约违背——诚实语义不掩盖测试数据缺口）。"""
    if not isinstance(payload, dict):
        return [], {}
    lines: list[str] = []
    assign: dict[str, str] = {}
    for k in payload:
        if not k.endswith("_id") or not isinstance(payload[k], int):
            continue
        res = k[:-3]
        create = next((e for e in endpoints if e["method"] == "POST"
                       and e["path"].rstrip("/") in (f"/{res}", f"/{res}s")), None)
        if create is None:
            continue
        route_key = f"{create['method']} {create['path']}"
        setup_payload = happy.get(route_key)
        if setup_payload is None:
            continue
        var = f"{_py_ident(res)}_id_{len(used)}"
        used.add(var)
        lines.append("    r_setup = requests.post(BASE + " + repr(create["path"])
                     + ", json=" + json.dumps(setup_payload, ensure_ascii=False, default=str)
                     + ", timeout=10)\n")
        lines.append(f"    assert r_setup.status_code == {create.get('success_code', 200)}, "
                     f"r_setup.text   # 前置数据[接口契约]：{res} 不存在则用例无意义\n")
        lines.append(f"    {var} = r_setup.json().get('id')\n")
        assign[k] = var
    return lines, assign


# ---------------------------------------------------------------------------
# 2) 代码渲染（契约轨 / 顺序场景轨）
# ---------------------------------------------------------------------------

def _payload_literal(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=4, default=str)


def _py_ident(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_").lower() or "x"


def _request_expr(ep: dict, url_expr: str, indent: str = "    ") -> str:
    method = ep["method"].lower()
    if ep["method"] in ("POST", "PUT", "PATCH"):
        return f"{indent}r = requests.{method}({url_expr}, json=payload, timeout=10)\n"
    return f"{indent}r = requests.{method}({url_expr}, timeout=10)\n"


def render_contract_module(endpoints: list[dict], base_url: str
                           ) -> tuple[str, list[dict], dict, list[str]]:
    """渲染契约用例 pytest 模块（不含路径参数端点——那由顺序场景轨负责）。
    返回 (代码全文, 用例记录[六要素], happy_payloads{route: payload}, 跳过路由列表)。"""
    lines = ['"""AI 契约黑盒用例（L2 集成层 · 契约断言级）"""',
             "import jsonschema", "import requests", "",
             f'BASE = "{base_url}"', ""]
    resp_schemas = {f"{e['method']} {e['path']}": e["response_schema"]
                    for e in endpoints if e.get("response_schema")}
    if resp_schemas:
        lines += ["_RESP_SCHEMAS = " + json.dumps(resp_schemas, ensure_ascii=False, default=str), ""]
    cases: list[dict] = []
    happy: dict = {}
    skipped: list[str] = []
    used_vars: set = set()
    used_names: set = set()          # 全模块持久去重——同名 def 会静默覆盖（mini-E6 实锤）
    # happy 池预计算：外键前置数据（_fk_setup）需要引用其它端点的合法创建体
    for e in endpoints:
        if e["method"] in ("POST", "PUT", "PATCH") and isinstance(e.get("request_schema"), dict):
            happy[f"{e['method']} {e['path']}"] = example_from_schema(e["request_schema"])

    def _emit(ep: dict, url_expr: str, case_payload, fname_base: str, kind: str,
              method_label: str, expect_line: str, record_extra: dict) -> None:
        """统一用例发射器：外键前置数据 → payload 组装（fk 变量化）→ 请求 → 断言。"""
        setup_lines, assign = _fk_setup(ep, case_payload, endpoints, happy, used_vars)
        fname = _sanitize_name(fname_base, len(cases), used_names)
        body = f"def {fname}():\n" + "".join(setup_lines)
        if case_payload is not None:
            lit = {k: v for k, v in case_payload.items() if k not in assign}
            body += f"    payload = {_payload_literal(lit)}\n"
            for k, var in assign.items():
                body += f"    payload[{k!r}] = {var}\n"
        elif ep["method"] in ("POST", "PUT", "PATCH") and not ep.get("request_schema"):
            # go_route 等无契约体端点（schema 如实 None）：仍发 payload={}——
            # requests json={} 合法，服务端校验决定 400/201，契约断言落在状态码上
            body += "    payload = {}\n"
        body += _request_expr(ep, url_expr)
        body += expect_line
        lines.append(body)
        lines.append("")
        cases.append({"name": fname, "method": method_label, "kind": kind,
                      "input": case_payload, **record_extra})

    for ep in endpoints:
        method, path = ep["method"], ep["path"]
        route_key = f"{method} {path}"
        has_body = method in ("POST", "PUT", "PATCH") and isinstance(ep.get("request_schema"), dict)
        if _required_path_params(ep):
            skipped.append(route_key)            # 路径参数端点：无确定 id，交给顺序场景
            continue
        url_expr = f"BASE + {path!r}"
        payload = happy.get(route_key) if has_body else None
        expect = (f"    assert r.status_code == {ep.get('success_code', 200)}, r.text"
                  f"   # 契约断言[接口契约]\n")
        if ep.get("response_schema"):
            expect += (f"    jsonschema.validate(r.json(), _RESP_SCHEMAS[{json.dumps(route_key)}])"
                       f"   # 契约断言[接口契约]\n")
        _emit(ep, url_expr, payload, f"test_{_py_ident(method + path)}_happy_契约黑盒",
              "happy", "契约黑盒-happy", expect,
              {"expected": f"{ep.get('success_code', 200)} 且响应符合 schema",
               "expected_basis": "接口契约", "route": route_key})

        for neg in negative_variants(ep):
            _emit(ep, url_expr, neg["payload"],
                  f"test_{_py_ident(method + path)}_{_py_ident(neg['kind'])}_契约黑盒",
                  "negative", f"契约黑盒-{neg['kind']}",
                  f"    assert r.status_code == {neg['expected_status']}, r.text"
                  f"   # 契约断言[{neg['basis']}] {neg['reason']}\n",
                  {"expected": f"{neg['expected_status']}（{neg['reason']}）",
                   "expected_basis": neg["basis"], "route": route_key})
    return "\n".join(lines) + "\n", cases, happy, skipped


def render_sequence_module(scenarios: list[dict], happy: dict, base_url: str,
                           endpoints: list[dict] | None = None) -> tuple[str, list[dict]]:
    """渲染顺序场景用例：POST 创建 → GET /res/{id_param} 读回一致（先创建后查询，白盒联动）。
    创建体若含外键（如订单引用优惠券），先走 _fk_setup 前置创建。"""
    lines = ['"""AI 顺序场景用例（L2 · 调用链白盒联动）"""', "import requests", "",
             f'BASE = "{base_url}"', ""]
    cases: list[dict] = []
    used_vars: set = set()
    used_names: set = set()
    for sc in scenarios[:MAX_SEQ_SCENARIOS]:
        create, read = sc["create"], sc["read"]
        route_key = f"{create['method']} {create['path']}"
        payload = happy.get(route_key)
        if payload is None:
            continue                              # 无合法创建体 → 诚实跳过该场景
        idp = _py_ident(sc["id_param"])
        read_path_py = read["path"].replace("{" + sc["id_param"] + "}", "{" + idp + "}")
        fname = _sanitize_name(f"test_{_py_ident(create['path'])}_create_then_read_顺序场景",
                               len(cases), used_names)
        setup_lines, assign = _fk_setup(create, payload, endpoints or [], happy, used_vars)
        lit = {k: v for k, v in payload.items() if k not in assign}
        body = (f"def {fname}():\n" + "".join(setup_lines) +
                f"    payload = {_payload_literal(lit)}\n")
        for k, var in assign.items():
            body += f"    payload[{k!r}] = {var}\n"
        body += (f"    r = requests.post(BASE + {create['path']!r}, json=payload, timeout=10)\n"
                 f"    assert r.status_code == {create.get('success_code', 200)}, r.text"
                 f"   # 契约断言[接口契约]\n"
                 f"    body = r.json()\n"
                 f"    {idp} = body.get({_py_ident(sc['id_param'])!r}) or body.get('id')\n"
                 f"    assert {idp}, '创建响应未返回 id（需求断言[需求明示]：创建应可被按 id 读回）'\n"
                 f"    r2 = requests.get(BASE + f\"{read_path_py}\", timeout=10)\n"
                 f"    assert r2.status_code == {read.get('success_code', 200)}, r2.text"
                 f"   # 契约断言[接口契约]\n")
        fields = [k for k in payload if isinstance(payload[k], (str, int, float, bool))
                  and k not in assign]
        for k in fields[:3]:
            body += (f"    assert r2.json().get({k!r}) == payload[{k!r}], "
                     f"'字段 {k} 创建后读回不一致'   # 需求断言[需求明示]\n")
        lines += [body, ""]
        cases.append({"name": fname, "method": "顺序场景", "kind": "sequence",
                      "input": payload,
                      "expected": f"创建 {route_key} 后按 id 读回 {read['path']} 字段一致",
                      "expected_basis": "需求明示", "route": route_key})
    return "\n".join(lines) + "\n", cases


_REQ_SYSTEM = (
    "你是接口测试工程师。依据用户需求为指定接口生成业务语义断言（需求断言）。"
    '只输出 JSON：{"asserts": [{"name": "英文蛇形名", "exprs": ["单行python布尔表达式"]}] }。'
    "表达式可用变量：r=响应对象（r.json() 等），payload=请求体 dict；"
    "禁止断言 r.status_code（状态码属契约断言层，系统已另行保证）；"
    "只断言需求声明过的业务行为，禁止从实现猜测。"
)


def build_requirement_asserts(requirement: str, endpoints: list[dict], happy: dict,
                              llm, task_id: int | None = None) -> dict[str, list[dict]]:
    """按端点生成需求断言（D7 第二级，LLM 注入可单测）：返回 {route_key: asserts}。
    任何失败 → 该端点缺席（fail-open，契约轨照跑）；LLM 记账 phase='L2.gen_requirement'。"""
    from .llm_usage import log_llm_call
    if llm is None or not requirement or not endpoints:
        return {}
    out: dict[str, list[dict]] = {}
    for ep in endpoints[:3]:                              # 预算：只给前 3 个写端点
        route_key = f"{ep['method']} {ep['path']}"
        summary = {"method": ep["method"], "path": ep["path"],
                   "params": ep.get("params"), "request_schema": ep.get("request_schema"),
                   "response_schema": ep.get("response_schema")}
        prompt = (f"## 用户需求\n{requirement}\n\n## 接口\n"
                  f"{json.dumps(summary, ensure_ascii=False)}\n\n"
                  f"## happy path 请求体\n{json.dumps(happy.get(route_key), ensure_ascii=False)}\n\n"
                  "为该接口生成 1-2 条需求断言用例（业务语义，非 schema 校验）。"
                  "注意：禁止断言 r.status_code（状态码由契约层保证）；"
                  "响应字段名必须严格取自 response_schema 已定义的字段，禁止自拟字段名。")
        try:
            resp = llm.invoke([("system", _REQ_SYSTEM), ("user", prompt)])
            log_llm_call(task_id, "L2.gen_requirement", resp,
                 ctx_budget=getattr(llm, "_ctx_budget", None),
                 base_url=getattr(llm, "_base_url", ""))
            data = json.loads(re.search(r"\{.*\}", resp.content, re.S).group(0))
            got = [a for a in (data.get("asserts") or [])
                   if isinstance(a, dict) and isinstance(a.get("exprs"), list) and a.get("exprs")]
            # 确定性双保险：状态码断言整条剔除（契约层职责，LLM 猜状态码=假阳性源头）
            for a in got:
                a["exprs"] = [e for e in a["exprs"] if "status_code" not in e]
            got = [a for a in got if a["exprs"]]
            if got:
                out[route_key] = got
        except Exception as e:                            # noqa: BLE001 —— fail-open
            log.warning("需求断言 LLM 失败（fail-open，契约轨照跑）: %s", e)
    return out


# ---------------------------------------------------------------------------
# 3) 需求断言（可选 LLM，fail-open）+ 表达式静态闸
# ---------------------------------------------------------------------------

_EXPR_BLOCK = re.compile(r"\b(import|exec|eval|open|lambda|while|for|globals|setattr)\b")


def _safe_expr(e: str) -> bool:
    """LLM 表达式静态闸：能编译成单行表达式 + 无危险词（确定性，LLM 输出不可信）。
    '__' 单独判断（\\b 对纯下划线词不生效，__import__ 会漏网）。"""
    e = (e or "").strip()
    if not e or len(e) > 300 or "__" in e or _EXPR_BLOCK.search(e):
        return False
    try:
        _ast.parse(e, mode="eval")
    except SyntaxError:
        return False
    return True


def render_requirement_module(asserts: list[dict], endpoint: dict, payload,
                              base_url: str, endpoints: list[dict] | None = None,
                              happy: dict | None = None) -> tuple[str, list[dict]]:
    """渲染需求断言用例（D7 第二级）：表达式包进独立用例（变量 r/payload 可用）。
    payload 为该端点 happy path 请求体；无 body 的端点不发 json；外键走 _fk_setup 前置。"""
    method, path = endpoint["method"], endpoint["path"]
    route_key = f"{method} {path}"
    lines = ['"""AI 需求断言用例（L2 · 需求语义级）"""', "import requests", "",
             f'BASE = "{base_url}"', ""]
    cases: list[dict] = []
    used_vars: set = set()
    used_names: set = set()
    for a in asserts[:MAX_REQUIREMENT_ASSERTS]:
        exprs = [e.strip() for e in (a.get("exprs") or []) if _safe_expr(e)]
        if not exprs:
            continue
        fname = _sanitize_name(f"test_{_py_ident(method + path)}_{a.get('name', 'requirement')}_需求断言",
                               len(cases), used_names)
        setup_lines, assign = _fk_setup(endpoint, payload, endpoints or [], happy or {}, used_vars)
        body = f"def {fname}():\n" + "".join(setup_lines)
        if payload is not None:
            lit = {k: v for k, v in payload.items() if k not in assign}
            body += f"    payload = {_payload_literal(lit)}\n"
            for k, var in assign.items():
                body += f"    payload[{k!r}] = {var}\n"
        body += _request_expr(endpoint, f"BASE + {path!r}")
        body += (f"    assert r.status_code == {endpoint.get('success_code', 200)}, r.text"
                 f"   # 契约断言[接口契约]（需求断言前置）\n")
        for e in exprs:
            body += f"    assert {e}, {e!r}   # 需求断言[需求明示]\n"
        lines += [body, ""]
        cases.append({"name": fname, "method": "需求断言", "kind": "requirement",
                      "input": payload, "expected": "；".join(exprs),
                      "expected_basis": "需求明示", "route": route_key})
    return "\n".join(lines) + "\n", cases
