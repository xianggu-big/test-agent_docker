"""今日模块（2026-10-28，提交 61e0894 三方案 + 变异仪器 + 对照格）完成度巡检与缺陷探针。

用法：python scripts/module_check.py [--full]
三段式：
  PART1 完成度 = 静态契约检查（开关/函数/提示词段/集成点/仪器锚点逐一核对）；
  PART2 单测回归 = pytest tests/test_design_proposals.py（12 条机制单测）+ test_review_node.py；
  PART3 边界探针 = 单测未覆盖的边界输入逐个喂给方案A/B/C核心函数与变异算子，
       每条探针结论三态：PASS（符合契约）/ FAIL（契约违约，exit 1）/ FINDING（可辩护的
       边界行为，记录不动死刑，留给 v2 决策）。
--full 额外跑全量 pytest 回归（99 绿基线）。

诚实边界：本脚本离线确定性（不触网/不依赖 Docker/不调 LLM）；方案B 的容器侧
hypothesis 安装只做源码契约检查（真容器行为由 run_grid B1 格真机实验覆盖）。
报告落 data/reports/module_check_<ts>.json（运行产物，不入库）。
"""
import ast
import importlib.util
import inspect
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.skills import builtin, gen_tests, prompt_library  # noqa: E402

PASS, FAIL, FINDING = "PASS", "FAIL", "FINDING"


@dataclass
class Item:
    name: str
    status: str          # OK / MISS / PASS / FAIL / FINDING
    detail: str = ""


def part1_completion() -> list[Item]:
    """静态契约：今日该有的构件逐一核对（存在性 + 关键内容）。"""
    out: list[Item] = []

    def ok(name: str, detail: str = ""):
        out.append(Item(name, "OK", detail))

    def miss(name: str, detail: str):
        out.append(Item(name, "MISS", detail))

    # 1) config 三开关，默认 False（保证基线格不受污染）
    for key in ("ASSERTION_GATE_ENABLED", "PBT_ENABLED", "COND_FEEDBACK_ENABLED"):
        v = getattr(config, key, None)
        (ok if v is False else miss)(f"config.{key}", f"默认值={v}（应为 False）")

    # 2) 方案A/B/C 核心函数存在
    for fn in ("assertion_audit", "apply_assertion_gate", "_condition_gaps",
               "refill_condition_gaps"):
        (ok if callable(getattr(gen_tests, fn, None)) else miss)(
            f"gen_tests.{fn}", "缺失" if not callable(getattr(gen_tests, fn, None)) else "")

    # 3) 提示词库：PBT 段 + build_user_prompt 新参数
    has_pbt = getattr(prompt_library, "PBT_SECTION", None)
    (ok if has_pbt else miss)("prompt_library.PBT_SECTION", "缺失")
    sig = inspect.signature(prompt_library.build_user_prompt)
    for p in ("pbt", "cond_forced", "existing_cases"):
        (ok if p in sig.parameters else miss)(
            f"build_user_prompt({p}=...)", "签名缺少该参数")

    # 4) 方案B 容器侧：install_deps 受 PBT_ENABLED 约束装 hypothesis（源码契约）
    src_builtin = inspect.getsource(builtin.build_skills)
    if "hypothesis" in src_builtin and "PBT_ENABLED" in src_builtin:
        ok("builtin.install_deps → hypothesis", "源码契约在位")
    else:
        miss("builtin.install_deps → hypothesis", "源码未引用 hypothesis/PBT_ENABLED")

    # 5) generate_tests 集成三方案（生成主链路挂点）
    src_gen = inspect.getsource(gen_tests.generate_tests)
    for key in ("ASSERTION_GATE_ENABLED", "PBT_ENABLED", "COND_FEEDBACK_ENABLED"):
        (ok if key in src_gen else miss)(
            f"generate_tests 集成 {key}", "主链路未挂该方案")

    # 6) 变异仪器契约：tmp 固定 D 盘（C 盘 Temp junction 坑）+ 核心函数 + 变异上限
    mk = _load("mutation_kill.py")
    if mk is None:
        miss("scripts/mutation_kill.py", "无法加载")
    else:
        mk_src = (ROOT / "scripts" / "mutation_kill.py").read_text(encoding="utf-8")
        (ok if "tmp_mut" in mk_src else miss)("mutation_kill tmp 固定 data/tmp_mut",
                                             "C 盘 junction 修复不在位")
        (ok if mk.MAX_MUTANTS == 12 else miss)("mutation_kill MAX_MUTANTS",
                                               f"={mk.MAX_MUTANTS}（应为 12）")
        for fn in ("_candidates", "_mutate", "source_modules"):
            (ok if callable(getattr(mk, fn, None)) else miss)(f"mutation_kill.{fn}", "缺失")

    # 7) run_grid 三方案格（文本契约，不 import——其顶层 import graph 链路是重依赖）
    rg = (ROOT / "scripts" / "run_grid.py").read_text(encoding="utf-8")
    for cell, key in (("A1", "ASSERTION_GATE_ENABLED"), ("B1", "PBT_ENABLED"),
                      ("C1", "COND_FEEDBACK_ENABLED")):
        (ok if f'"{cell}": {{"{key}": True}}' in rg else miss)(
            f"run_grid.GRID[{cell}]", f"应为 {{{key}: True}}")

    # 8) 今日其他产物在位（阅卷根修/review 终态恢复/聚合脚本的静态锚点）
    for p in ("scripts/grade_corpus.py", "scripts/aggregate_grid.py",
              "scripts/compare_proposals.py", "tests/test_review_node.py"):
        (ok if (ROOT / p).exists() else miss)(p, "文件缺失")

    # 9) 机制单测文件在位
    (ok if (ROOT / "tests/test_design_proposals.py").exists()
     else miss)("tests/test_design_proposals.py", "文件缺失")
    return out


def part2_pytest(extra: list[str] | None = None) -> list[Item]:
    """单测回归：机制 12 条必查；--full 时全量。"""
    out: list[Item] = []
    runs = [("三方案机制单测", ["tests/test_design_proposals.py"], 13),
            ("review 节点单测（今日终态恢复）", ["tests/test_review_node.py"], None)]
    if extra:
        runs.append(("全量回归", extra, None))
    for name, paths, expect in runs:
        r = subprocess.run([sys.executable, "-m", "pytest", *paths, "-q", "--no-header"],
                           cwd=ROOT, capture_output=True, text=True, timeout=600)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(无输出)"
        n_pass, n_fail = _parse_pytest(last)
        good = r.returncode == 0 and n_fail == 0 and (expect is None or n_pass == expect)
        out.append(Item(f"pytest {name}", "OK" if good else "FAIL",
                        f"{last}（期望 {'≥1 过 0 败' if expect is None else f'{expect} 过'}）"
                        if good else f"returncode={r.returncode} tail={last}"))
    return out


def _parse_pytest(tail: str) -> tuple[int, int]:
    import re
    m = re.search(r"(\d+) passed(?:, (\d+) failed)?", tail)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def part3_probes() -> list[Item]:
    """边界探针：单测没覆盖的输入，逐条对契约。"""
    from test_agent.skills.gen_tests import (_condition_gaps, apply_assertion_gate,
                                             assertion_audit, refill_condition_gaps)
    from test_agent.skills.prompt_library import build_user_prompt
    out: list[Item] = []

    def check(name: str, cond: bool, detail: str, finding: bool = False):
        out.append(Item(name, (FINDING if finding else (PASS if cond else FAIL)), detail))

    src = [("m.py", "def check(p):\n    return bool(p)\n")]

    # —— 方案A：断言审计边界 ——
    a = assertion_audit("def t():\n    assert add(1, 2) == 3\n")
    check("A·等值比较=强", a["strong"] == 1 and a["weak"] == 0, f"{a}")

    a = assertion_audit("def t():\n    assert isinstance(v, int)\n")
    check("A·isinstance=弱（docstring 口径）", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert True\n")
    check("A·常量断言=弱", a["weak"] == 1 and "裸真值" in a["reasons"][0], f"{a}")

    a = assertion_audit("def t():\n    assert 0 < x <= 10\n")
    check("A·链式比较=强", a["strong"] == 1 and a["weak"] == 0, f"{a}")

    a = assertion_audit("def t():\n    assert x is None\n")
    check("A·is None=弱（与 is not None 同口径）", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert x in [1, 2]\n")
    check("A·in 成员比较=强", a["strong"] == 1, f"{a}")

    a = assertion_audit("def t():\n    assert not ok_flag\n")
    check("A·not 裸真值=弱", a["weak"] == 1, f"{a}")

    a = assertion_audit("def t():\n    def broken(:\n")
    check("A·语法坏码不炸且计弱", a["weak"] == 1 and "语法" in a["reasons"][0], f"{a}")

    a = assertion_audit("def t():\n    assert a == 1 and b == 2\n")
    check("A·BoolOp 联接逐臂审计（两臂皆强比较=强，e908213 修复后回归守卫）",
          a["strong"] == 1 and a["weak"] == 0, f"{a}——两臂皆具体比较，逐臂审计不再误杀")

    a = assertion_audit("def t():\n    assert a == 1 and flag\n")
    check("A·BoolOp 含弱臂（裸真值）仍判弱", a["weak"] == 1 and "第2臂" in a["reasons"][0],
          f"{a}")

    a = assertion_audit("def t():\n    assert (a, b) == (1, 2)\n")
    check("A·元组整体比较=强（BoolOp 的正确写法对照组）", a["strong"] == 1, f"{a}")

    cases = [{"name": "w1", "code": "def w1():\n    assert f()\n"},
             {"name": "s1", "code": "def s1():\n    assert f() == 1\n"}]
    kept, rejected, stats = apply_assertion_gate(cases, gate=True)
    check("A·闸剔除数与 weak_cases 一致（stats 自洽）",
          stats["gate_rejected"] == stats["weak_cases"] == 1 and len(kept) == 1,
          f"kept={[c['name'] for c in kept]} stats={stats}")

    # —— 方案B：提示词边界 ——
    on = build_user_prompt(src, "需求", pbt=True)
    elems = ["@given", "hypothesis", "settings(max_examples", "属性测试", "不变量"]
    missing = [e for e in elems if e not in on]
    check("B·PBT 段要素齐备（骨架/采样上限/不变量纪律）", not missing,
          f"缺要素：{missing}" if missing else "5/5 要素在位")
    off = build_user_prompt(src, "需求", pbt=False)
    check("B·关时无 PBT 段", "属性测试" not in off and "@given" not in off, "开关隔离正常")
    both = build_user_prompt(src, "需求", include_methods=False, pbt=True)
    check("B·与五方法开关独立（方法关 PBT 开仍注入）", "@given" in both, "独立性成立")

    forced = build_user_prompt(src, "需求", conditions=[{"condition": "空密码应判为无效"}],
                               cond_forced=True, existing_cases=["test_a", "test_b"])
    check("C·强制模式措辞+已有用例名单注入",
          "强制逐条覆盖" in forced and "test_a" in forced, "措辞与防重复名单都在")
    forced_no = build_user_prompt(src, "需求", conditions=[{"condition": "空密码应判为无效"}],
                                  cond_forced=True, existing_cases=None)
    # 注意区分：静态措辞「不得与已有用例重复：」恒在；动态名单行「已有用例（禁止重复，
    # 可直接跳过…）：」才是 existing_cases 注入的产物——None 时不得出现、不得崩
    check("C·强制模式无已有用例时不崩不注入名单",
          "禁止重复，可直接跳过" not in forced_no and "test_a" not in forced_no,
          "None 路径干净（静态措辞『不得与已有用例重复』恒在，属正常）")

    # —— 方案C：缺口映射与回灌边界 ——
    conds = [{"condition": "空密码应判为无效"}, {"condition": "长度恰为上限的密码应有效"}]
    both_covered = [
        {"name": "test_空密码应判为无效_边界值分析", "expected": "无效",
         "code": "def t1():\n    assert check('') is False\n"},
        {"name": "test_长度恰为上限的密码应有效_边界值分析", "expected": "有效",
         "code": "def t2():\n    assert check('x' * 10) is True\n"}]
    check("C·2-gram 全覆盖→零缺口（回灌不触发的正路）",
          _condition_gaps(conds, both_covered) == [], "两条条件均有对准用例")
    check("C·空条件列表不炸", _condition_gaps([], both_covered) == [], "空表安全")

    covered = [both_covered[0]]     # 只盖住条件1 → 条件2 是真实缺口，回灌才会调 LLM
    dup = _FakeLLM(json.dumps({"cases": [
        {"name": "test_空密码应判为无效_边界值分析",
         "code": "def test_空密码应判为无效_边界值分析():\n    assert check('x') is True\n"}]}))
    tmp = _tmp_ws()
    new, _, info = refill_condition_gaps(
        dup, src, "", conds, covered, tmp, {c["name"] for c in covered}, True, False)
    check("C·LLM 回传重名被去重（nodeid 唯一性前提）",
          bool(new) and new[0]["name"] != covered[0]["name"],
          f"原名={covered[0]['name']} 新名={new[0]['name'] if new else '—'}")

    # —— 变异仪器边界 ——
    mk = _load("mutation_kill.py")
    got = mk._mutate("def f(x):\n    return x + 1\n", 0)
    check("M·算术翻转 Add→Sub", got is not None and "x - 1" in got[0], f"{got}")
    n_cand = len(mk._candidates(ast.parse("def f(x):\n    return 0 < x <= 10\n")))
    check("M·链式比较逐 op 计点", n_cand == 2, f"候选点={n_cand}（应 2：Lt/LtE）")
    check("M·越界索引返回 None", mk._mutate("def f(x):\n    return x + 1\n", 99) is None,
          "越界安全")
    check("M·无可变点返回 None", mk._mutate("def f():\n    return 1\n", 0) is None,
          "纯常量安全")
    probe_ws = _tmp_ws()
    (probe_ws / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (probe_ws / "test_mod.py").write_text("def t():\n    assert True\n", encoding="utf-8")
    mods = [p.name for p in mk.source_modules(probe_ws)]
    check("M·source_modules 跳过测试文件", mods == ["mod.py"], f"{mods}")
    return out


@dataclass
class _Resp:
    content: str


class _FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return _Resp(self.payload)


_TMP_DIRS: list[Path] = []


def _tmp_ws() -> Path:
    import tempfile
    root = ROOT / "data" / "tmp_check"
    root.mkdir(parents=True, exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="probe_", dir=str(root)))
    _TMP_DIRS.append(ws)
    return ws


def _cleanup_tmp():
    import shutil
    for ws in _TMP_DIRS:
        shutil.rmtree(ws, ignore_errors=True)


_MK = None


def _load(name: str):
    global _MK
    if name == "mutation_kill.py" and _MK is not None:
        return _MK
    p = ROOT / "scripts" / name
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if name == "mutation_kill.py":
        _MK = mod
    return mod


def main() -> int:
    full = "--full" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")
    print("=" * 72)
    print("PART 1/3 完成度（静态契约）")
    items = part1_completion()
    _print(items)
    print("\n" + "=" * 72)
    print("PART 2/3 单测回归")
    boundary = len(items)
    items += part2_pytest(["-q"] if full else None)
    _print(items, since=boundary)
    print("\n" + "=" * 72)
    print("PART 3/3 边界探针（单测未覆盖输入）")
    boundary = len(items)
    try:
        items += part3_probes()
    finally:
        _cleanup_tmp()
    _print(items, since=boundary)

    n = {s: sum(1 for i in items if i.status == s)
         for s in ("OK", "MISS", "PASS", "FAIL", "FINDING")}
    print("\n" + "=" * 72)
    print(f"汇总：完成度 {n['OK']} OK / {n['MISS']} MISS；探针 {n['PASS']} PASS / "
          f"{n['FAIL']} FAIL / {n['FINDING']} FINDING")
    bad = [i for i in items if i.status in ("MISS", "FAIL")]
    for i in bad:
        print(f"  ✗ {i.name}: {i.detail}")
    for i in items:
        if i.status == FINDING:
            print(f"  ⚠ FINDING {i.name}: {i.detail}")

    rep = ROOT / "data" / "reports" / f"module_check_{ts}.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({"ts": ts, "summary": n,
                               "items": [vars(i) for i in items]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已落 {rep.relative_to(ROOT)}")
    return 1 if bad else 0


def _print(items: list[Item], since: int = 0):
    for i in items[since:]:
        mark = {"OK": "✓", "PASS": "✓", "MISS": "✗", "FAIL": "✗",
                "FINDING": "⚠"}.get(i.status, "?")
        line = f"  [{mark} {i.status:7}] {i.name}"
        if i.detail and i.status in ("MISS", "FAIL", "FINDING"):
            line += f" —— {i.detail}"
        print(line)


if __name__ == "__main__":
    sys.exit(main())
