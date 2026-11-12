"""槽位填充式用例模板库（T1/T2/T5）—— 基础方法的"结构先验"通道。

动机与出处（docs/黑盒白盒方法组合与沙箱自适应设计.md §5）：
- 基础黑盒方法（等价类/边界值/错误推测）的用例形态高度固定——参数化表格驱动；
  让 LLM 自由写代码是浪费且易错（2412.14137 实证生成器断言易"为通过而设计"，
  Tanaka 2510.25297 实证 LLM 不主动边界攻击）。模板强制注入结构（如边界六点），
  LLM 只填槽位（函数调用表达式、字面量、预期值），确定性代码渲染。
- 预期值仍由 LLM 先于执行给出（expected_basis 纪律不变）；渲染物走同一套
  静态闸/collect-only 闸/落库，与自由生成通道可比（消融时关 TEMPLATES_ENABLED）。

A5 对齐纪律：一次展开只产出一个 def test_ 函数（nodeid ↔ test_cases.name 一一对应），
无效/异常路径由 LLM 以 mode="invalid" 另发一条模板用例（T4 的非法迁移是函数内 raises 段）。
"""
import builtins
import re

# 内置异常白名单（invalid 模式的 error 槽位只允许内置异常名，防渲染出未知符号）
_ALLOWED_ERRORS = {name for name, obj in vars(builtins).items()
                   if isinstance(obj, type) and issubclass(obj, BaseException)}

KIND_METHOD = {"T1": "等价类划分", "T2": "边界值分析", "T5": "错误推测法", "T4": "状态迁移法"}
KIND_SUFFIX = {"T1": "等价类", "T2": "边界值", "T5": "错误推测", "T4": "状态迁移"}


class SlotError(ValueError):
    """槽位校验失败（调用方据此剔除该案并在台账留理由，不渲染半成品）。"""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SlotError(msg)


def _check_rows(rows, mode: str, kind: str) -> None:
    _require(isinstance(rows, list) and rows, f"{kind} {mode} 槽位 cases 必须是非空数组")
    for r in rows:
        _require(isinstance(r, dict), "cases 元素必须是对象")
        _require("value" in r, "cases 元素缺少 value")
        _require(len(repr(r["value"])) <= 200, "value 字面量超长（>200 字符）")
        if mode == "valid":
            _require("expected" in r, "valid 元素缺少 expected（预期先于执行确定）")
            _require(len(repr(r["expected"])) <= 200, "expected 字面量超长")
        else:
            _require(r.get("error") in _ALLOWED_ERRORS,
                     f"error 必须是内置异常名，收到 {r.get('error')!r}")


def _render_transition(slots: dict, fname: str) -> str:
    """T4 状态迁移（Chow TSE 1978 迁移覆盖思想的模板化）：

    setup 建初始状态 → 逐迁移 step{call, assert} 走合法路径（迁移全覆盖）→
    可选 invalid[{call, error}] 在终态上拒绝非法迁移（0-switch 语义）。
    """
    setup = slots.get("setup")
    steps = slots.get("steps")
    invalid = slots.get("invalid") or []
    _require(isinstance(setup, str) and setup.strip(), "setup 必须是非空初始化表达式")
    _require(isinstance(steps, list) and len(steps) >= 2,
             f"状态迁移至少 2 条合法迁移（迁移全覆盖语义），收到 {len(steps) if isinstance(steps, list) else 0}")
    lines = [setup.strip()]
    for s in steps:
        _require(isinstance(s, dict) and isinstance(s.get("call"), str) and s["call"].strip(),
                 "steps 元素缺少 call（迁移调用表达式）")
        _require(isinstance(s.get("assert"), str) and s["assert"].strip(),
                 "steps 元素缺少 assert（迁移后的状态断言）")
        lines.append(s["call"].strip())
        lines.append(f"assert {s['assert'].strip()}")
    for s in invalid:
        _require(isinstance(s, dict) and isinstance(s.get("call"), str) and s["call"].strip()
                 and s.get("error") in _ALLOWED_ERRORS,
                 f"invalid 元素需 call + 内置异常名，收到 {s!r}")
        lines.append("with pytest.raises(" + s["error"] + "):")
        lines.append("    " + s["call"].strip())
    imp_lines = slots.get("imports")
    _require(isinstance(imp_lines, str) and imp_lines.strip(), "imports 必须是非空字符串")
    if not re.search(r"^\s*import pytest\s*$", imp_lines, re.M):
        imp_lines = "import pytest\n" + imp_lines
    body = "\n    ".join(lines)
    return f"{imp_lines}\n\n\ndef {fname}():\n    {body}\n"


def render_slot_template(kind: str, slots: dict, base_name: str = "case") -> str:
    """把模板槽位渲染成单个 pytest 参数化测试函数（A5：恰一个 def）。

    T1 等价类：valid/invalid ≥2 行；T2 边界值：valid ≥4 点（六点语义强制）；
    T5 错误推测：合计 ≥2 行攻击用例。校验失败抛 SlotError（诚实剔除，不渲染半成品）。
    """
    _require(kind in KIND_METHOD, f"未知模板 {kind!r}")
    _require(isinstance(slots, dict), "slots 必须是对象")
    if kind == "T4":
        base = re.sub(r"\W+", "_", (base_name or "case").strip("_")) or "case"
        return _render_transition(slots, f"test_{base}_{KIND_SUFFIX[kind]}")
    imports = slots.get("imports")
    target = slots.get("target")
    mode = slots.get("mode")
    rows = slots.get("cases")
    _require(isinstance(imports, str) and imports.strip(), "imports 必须是非空字符串")
    _require(isinstance(target, str) and "{v}" in target,
             "target 必须是含 {v} 占位符的调用表达式")
    _require(mode in ("valid", "invalid"), "mode 必须是 valid 或 invalid")
    _check_rows(rows, mode, kind)
    if kind == "T2" and mode == "valid" and len(rows) < 4:
        raise SlotError(f"T2 边界点不足（{len(rows)} < 4，六点语义要求 min-1/min/min+1/max-1/max/max+1）")
    if kind == "T1" and len(rows) < 2:
        raise SlotError(f"T1 等价类至少 2 行（有效/无效类各取代表），收到 {len(rows)}")
    if kind == "T5" and len(rows) < 2:
        raise SlotError(f"T5 错误推测至少 2 条攻击用例，收到 {len(rows)}")

    call = target.replace("{v}", "value")
    base = re.sub(r"\W+", "_", (base_name or "case").strip("_")) or "case"
    suffix = KIND_SUFFIX[kind]
    ids = ", ".join(repr(str(r.get("label") or repr(r["value"]))) for r in rows)
    body_rows = ",\n    ".join(
        f"({r['value']!r}, {r['expected']!r})" if mode == "valid"
        else f"({r['value']!r}, {r['error']})" for r in rows)

    if mode == "valid":
        header = '@pytest.mark.parametrize("value,expected", [\n    ' + body_rows + ",\n"
        params, tail = "value, expected", f"assert {call} == expected"
    else:
        header = '@pytest.mark.parametrize("value,error", [\n    ' + body_rows + ",\n"
        params, tail = "value, error", f"with pytest.raises(error):\n        {call}"
    header += f"], ids=[{ids}])\n"

    imp_lines = "\n".join(line for line in imports.splitlines() if line.strip())
    if not re.search(r"^\s*import pytest\s*$", imp_lines, re.M):
        imp_lines = "import pytest\n" + imp_lines
    fname = f"test_{base}_{suffix}" + ("" if mode == "valid" else "_无效输入")
    return (f"{imp_lines}\n\n\n{header}"
            f"def {fname}({params}):\n    {tail}\n")


# 模板方法 → 默认 expected_basis（治理一致性：T1 四语言 basis 字段缺口补齐）
_DEFAULT_BASIS = {
    "T1": "常识", "T2": "常识", "T5": "常识",
    "T4": "需求明示",   # 状态迁移=需求声明语义
    "MR": "需求明示",   # 蜕变关系=需求声明语义
}


def expand_template_cases(cases: list[dict]) -> tuple[list[dict], list[str]]:
    """双通道展开（gen_tests 调用）：含 template 槽位的案渲染成代码，其余原样通过。

    返回 (展开后的用例清单, 渲染失败理由列表)——失败案诚实剔除并留台账理由，
    绝不渲染半成品；展开产物与自由生成走同一套静态闸/collect-only 闸/落库。
    """
    out, rejected = [], []
    for c in cases:
        kind = c.get("template")
        if not kind:
            out.append(c)
            continue
        try:
            code = render_slot_template(str(kind), c.get("slots") or {},
                                        base_name=str(c.get("name") or "case"))
        except SlotError as e:
            rejected.append(f"{c.get('name')}: 模板 {kind} 槽位无效（{e}）")
            continue
        c = dict(c)
        c["code"] = code
        c.setdefault("method", KIND_METHOD.get(str(kind)))
        # T1 basis 字段补法（治理一致性核对表 T1 ⚠️→✅，2026-11-12）：模板展开的
        # 案若缺 expected_basis，按方法类型给确定性默认（等价类/边界值/错误推测=
        # 「常识」；状态迁移/蜕变=「需求明示」语义）——LLM 已填的不覆盖
        c.setdefault("expected_basis", _DEFAULT_BASIS.get(str(kind), "常识"))
        out.append(c)
    return out, rejected
