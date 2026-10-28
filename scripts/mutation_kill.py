"""用例有效性仪器：变异杀死率（方案B 独有指标，MutGen/Test-vs-Mutant 思想的度量侧）。

用法：python scripts/mutation_kill.py <task_id> <corpus_id>
对任务工作区的被测模块做确定性算子变异（比较符翻转/布尔联接词翻转/算术加减翻转，
每变异体恰一处），宿主重放生成套件（与 grade_corpus 同款"纯标准库语料宿主重放"假设）：
killed = 变异体产生了基线之外的新失败（基线含证人失败时按差集计，避免证人污染）；
变异体导致收集错误同样计杀死（-rEf 让 ERROR 进摘要）。
明细落 data/corpus/<corpus_id>/mutation_task<task_id>.json。

诚实边界：等价变异体（变异后语义不变）会存活压低 kill_rate；每变异体只跑一轮；
变异算子集有限（不做语句删除/返回值替换）——数字用于方案间横向对比，不与文献 kill_rate 直接比。
临时目录固定在 data/ 下（D 盘）：C 盘 Temp 会撞 Windows 保留 junction（Documents and Settings）
导致 pytest 扫描 PermissionError——2026-10-28 实锤，勿改回。
"""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config  # noqa: E402
from test_agent.graph.repair import failing_nodeids  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_MUTANTS = 12
_CMP_FLIP = {ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
             ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Add: ast.Sub, ast.Sub: ast.Add}
_BOOL_FLIP = {ast.And: ast.Or, ast.Or: ast.And}


def _candidates(tree: ast.AST) -> list[tuple[ast.AST, str, int | None]]:
    """可翻转变异点 [(节点, 描述, op下标或None)]：链式比较的每个 op 单独算一个点。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP_FLIP:
                    out.append((node, f"cmp {type(op).__name__}", i))
        elif isinstance(node, ast.BinOp) and type(node.op) in _CMP_FLIP:
            out.append((node, f"binop {type(node.op).__name__}", None))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_FLIP:
            out.append((node, f"boolop {type(node.op).__name__}", None))
    return out


def _mutate(src: str, index: int) -> tuple[str, str] | None:
    """对第 index 个变异点产生变异源码，返回 (变异源码, 描述)；越界或等价返回 None。"""
    tree = ast.parse(src)
    cands = _candidates(tree)
    if index >= len(cands):
        return None
    target, desc, op_i = cands[index]
    if op_i is not None:
        target.ops[op_i] = _CMP_FLIP[type(target.ops[op_i])]()
    elif isinstance(target, ast.BinOp):
        target.op = _CMP_FLIP[type(target.op)]()
    else:
        target.op = _BOOL_FLIP[type(target.op)]()
    out = ast.unparse(tree)
    return (out, desc) if out != src else None


def source_modules(ws: Path) -> list[Path]:
    """被测模块 = 工作区非测试 .py（与 gen_tests._collect_sources 同口径）。"""
    return [p for p in sorted(ws.rglob("*.py"))
            if not any(part in config.SKIP_DIRS for part in p.parts)
            and not p.name.startswith("test_") and not p.name.endswith("_test.py")]


def _suite_run(ws: Path) -> tuple[set[str], bool]:
    """跑一次套件，返回 (失败集, 是否干净收集)。-rEf：ERROR 也进短摘要。"""
    r = subprocess.run([sys.executable, "-m", "pytest", str(ws), "-q", "--tb=no",
                        "--no-header", "-rEf", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, timeout=180)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    collect_ok = (" passed" in tail or " no tests ran" in tail
                  or " failed" in tail or "error" not in tail)
    return set(failing_nodeids(r.stdout)), collect_ok


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    task_id, corpus_id = int(sys.argv[1]), sys.argv[2]
    ws = config.DATA_DIR / "workspaces" / f"task_{task_id}"
    if not ws.exists():
        print(json.dumps({"error": f"工作区缺失 {ws}（任务未跑或已被清理）"}, ensure_ascii=False))
        return 1
    baseline_fail, baseline_ok = _suite_run(ws)
    if not baseline_ok:
        print(json.dumps({"error": "基线（未变异）套件收集失败，仪器数字不可信，先修工作区"},
                         ensure_ascii=False))
        return 1
    mutants: list[dict] = []
    tmp_root = config.DATA_DIR / "tmp_mut"
    tmp_root.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"task{task_id}_", dir=str(tmp_root)))
    try:
        for mod in source_modules(ws):
            src = mod.read_text(encoding="utf-8")
            for i in range(MAX_MUTANTS):
                got = _mutate(src, i)
                if got is None:
                    break
                mutated, desc = got
                shutil.copytree(ws, tmp, dirs_exist_ok=True)
                (tmp / mod.relative_to(ws)).write_text(mutated, encoding="utf-8")
                try:
                    fail, _ok = _suite_run(tmp)
                    new_fail = fail - baseline_fail
                    killed = bool(new_fail)
                except subprocess.TimeoutExpired:
                    new_fail, killed = set(), True   # 变异体挂死也算杀死
                mutants.append({"module": mod.name, "site": i, "mutation": desc,
                                "killed": killed,
                                "new_failures": sorted(n.split(" - ")[0] for n in new_fail)[:5]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    killed_n = sum(1 for m in mutants if m["killed"])
    report = {"task_id": task_id, "corpus_id": corpus_id,
              "baseline_failures": len(baseline_fail),
              "mutants_total": len(mutants), "killed": killed_n,
              "kill_rate": round(killed_n / len(mutants), 3) if mutants else None,
              "detail": mutants}
    out = ROOT / "data" / "corpus" / corpus_id / f"mutation_task{task_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("task_id", "mutants_total", "killed", "kill_rate", "baseline_failures")},
                     ensure_ascii=False))
    print(f"明细已落 {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
