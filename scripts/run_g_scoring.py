"""G 人机同题对比·统一评分管线（任务卡-G 材料件 3，2026-11-10）。

两臂（人工/系统/混合）共用同一把尺，对比才成立：
- **检出判定**=逐缺陷反事实差分（diff-gate/LIBRO 思想）：套件在被测项目（buggy）
  上跑出失败集 F；对每个植入缺陷 Di 应用唯一修复得变体再跑得 Fi；
  F−Fi 非空 = Di 被检出，差集即"检出了 Di 的测试"。
  确定性、零 LLM、不依赖任务链路（人工套件没有 RTM/场景链）。
- **变异杀死率**=复用 scripts/mutation_kill.py 的变异算子与判定口径
  （对被测项目 3 个源文件做确定性算子变异，killed=产生基线之外的新失败）。
- 基线中与任何缺陷差集都不相关的失败如实列入 unmatched_failures
  （人工臂常见：写错/写崩的测试），不冒充检出也不隐藏。

用法：
  python scripts/run_g_scoring.py <suite_dir> [--label L] [--no-mutation]
suite_dir：只收 test_*.py / *_test.py（人工臂交付物或系统臂工作区）。
产物：data/corpus/corpus-003/g_scores/<label>.json + stdout 摘要。

诚实边界：人工套件可能因环境（fastapi 版本）而非能力原因全军覆没——collect
失败时如实报 error 不出分；等价变异体存活压低 kill_rate 与 mutation_kill
同一口径；检出只对 5 个真缺陷（decoy 不进分母）。
临时目录固定在 data/ 下（D 盘）——Windows C 盘保留 junction 坑，勿改回。
"""
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.graph.repair import failing_nodeids  # noqa: E402

CORPUS = ROOT / "data" / "corpus" / "corpus-003"
PROJECT = CORPUS / "project"
SCORES = CORPUS / "g_scores"

# 逐缺陷唯一修复（唯一依据=ground_truth.json 的缺陷描述；每条必须恰命中一处，
# 否则 ValueError 如实报错——禁止模糊替换）。D6/D7 为 decoy，无修法不进分母。
FIXES: dict[str, list[tuple[str, str, str]]] = {
    "D1": [("booking.py", "hours >= MAX_HOURS", "hours > MAX_HOURS")],
    "D2": [("booking.py", "* 0.85)", "* 0.9)")],
    "D3": [("app.py", ".get(str(bid))", ".get(bid)")],
    "D4": [("booking.py", "start_a <= end_b and end_a >= start_b",
            "start_a < end_b and end_a > start_b")],
    "D5": [("app.py",
            'if booking["status"] == "cancelled":\n        return booking',
            'if booking["status"] == "cancelled":\n        raise HTTPException(409, "already cancelled")')],
}

# 变异目标=被测项目 3 个源文件（收死名单，防人工辅助 .py 被误变异）
MUTATION_FILES = ("app.py", "booking.py", "store.py")

_RUNNER_TIMEOUT = 180


def load_ground_truth(corpus: Path = CORPUS) -> dict:
    return json.loads((corpus / "ground_truth.json").read_text(encoding="utf-8"))


def load_project_sources(project: Path = PROJECT) -> dict[str, str]:
    return {name: (project / name).read_text(encoding="utf-8")
            for name in MUTATION_FILES}


def apply_fix(srcs: dict[str, str], defect_id: str,
              fixes: dict | None = None) -> dict[str, str]:
    """对源码组应用 defect_id 的唯一修复，返回新源码组；命中数≠1 抛 ValueError。"""
    patterns = (fixes or FIXES).get(defect_id)
    if not patterns:
        raise ValueError(f"缺陷 {defect_id} 无注册修法（decoy 或未知 id）")
    out = dict(srcs)
    for fname, old, new in patterns:
        n = out[fname].count(old)
        if n != 1:
            raise ValueError(f"{defect_id} 修复模式在 {fname} 命中 {n} 处（要求恰 1 处）")
        out[fname] = out[fname].replace(old, new)
    return out


def collect_suite_files(suite_dir: Path) -> list[Path]:
    """套件文件=suite_dir 顶层的 test_*.py / *_test.py（人工臂交付口径）。"""
    return sorted(p for p in suite_dir.iterdir()
                  if p.is_file() and (p.name.startswith("test_")
                                      or p.name.endswith("_test.py"))
                  and p.suffix == ".py")


def _real_runner(ws: Path) -> tuple[str, list[str], float]:
    """真 runner：宿主 pytest 重放（与 grade_corpus 同款假设）。返回 (尾行, 失败集, 耗时)。
    encoding 显式 utf-8（2026-11-27 实锤：GBK 控制台下 text=True 默认按 cp936 解
    pytest 的 UTF-8 输出 → UnicodeDecodeError → 误判"套件无法收集"）。"""
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "pytest", str(ws), "-q", "--tb=no",
                        "--no-header", "-rEf", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=_RUNNER_TIMEOUT)
    elapsed = time.monotonic() - t0
    out = r.stdout or ""
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    return tail, sorted(failing_nodeids(out)), elapsed


def _counts_from_tail(tail: str) -> tuple[int, int, int]:
    """从 pytest 尾行解析 (passed, failed, errors)；解析不出全 0（诚实）。"""
    import re
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", tail)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", tail)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", tail)
    if m:
        errors = int(m.group(1))
    return passed, failed, errors


def _kill_rate(ws: Path, project_srcs: dict[str, str], baseline_fail: set[str],
               runner, max_mutants_per_file: int = 12) -> dict:
    """变异杀死率：复用 mutation_kill 的算子与判定口径，目标锁定 3 个项目源文件。"""
    spec = importlib.util.spec_from_file_location(
        "mta_mutation_kill", ROOT / "scripts" / "mutation_kill.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    tmp_root = config.DATA_DIR / "tmp_g_mut"
    tmp_root.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gmut_", dir=str(tmp_root)))
    mutants = []
    try:
        for fname, src in project_srcs.items():
            for i in range(max_mutants_per_file):
                got = mk._mutate(src, i)
                if got is None:
                    break
                mutated, desc = got
                shutil.copytree(ws, tmp, dirs_exist_ok=True)
                (tmp / fname).write_text(mutated, encoding="utf-8")
                try:
                    _tail, fail, _el = runner(tmp)
                    new_fail = set(fail) - baseline_fail
                    killed = bool(new_fail)
                except subprocess.TimeoutExpired:
                    new_fail, killed = set(), True
                mutants.append({"file": fname, "site": i, "mutation": desc,
                                "killed": killed,
                                "new_failures": sorted(n.split(" - ")[0]
                                                       for n in new_fail)[:5]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    killed_n = sum(1 for m in mutants if m["killed"])
    return {"mutants_total": len(mutants), "killed": killed_n,
            "kill_rate": round(killed_n / len(mutants), 3) if mutants else None}


def score_suite(suite_dir: Path, out: Path | None = None, label: str = "",
                project_dir: Path = PROJECT, corpus: Path = CORPUS,
                runner=None, do_mutation: bool = True) -> dict:
    """统一评分入口。runner 可注入（单测假 runner）；默认真 pytest 宿主重放。"""
    runner = runner or _real_runner
    gt = load_ground_truth(corpus)
    real_defects = [d for d in gt["planted_defects"] if not d.get("decoy")]
    suite_files = collect_suite_files(suite_dir)
    if not suite_files:
        return {"error": f"{suite_dir} 无 test_*.py/*_test.py（空交付如实拒收）",
                "label": label}

    tmp_root = config.DATA_DIR / "tmp_g_score"
    tmp_root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(prefix="gscore_", dir=str(tmp_root)))
    try:
        # 工作区=项目源码（原样含 README/requirements）+ 套件文件
        for p in project_dir.iterdir():
            if p.is_file():
                shutil.copy2(p, ws / p.name)
        for p in suite_files:
            shutil.copy2(p, ws / p.name)

        tail, baseline_fail, elapsed = runner(ws)
        baseline_fail = set(baseline_fail)   # 统一集合口径（runner 可返回 list）
        passed, failed, errors = _counts_from_tail(tail)
        if "no tests ran" in tail or (passed == 0 and failed == 0 and errors == 0):
            return {"error": f"套件无法收集或零执行（尾行：{tail[:120]}）——如实不出分",
                    "label": label}
        report: dict = {"label": label, "suite_files": [p.name for p in suite_files],
                        "n_tests": passed + failed + errors, "passed": passed,
                        "failed": failed, "errors": errors,
                        "wall_clock_s": round(elapsed, 2),
                        "baseline_failures": len(baseline_fail)}
        # 检出判定：逐缺陷反事实差分
        srcs = load_project_sources(project_dir)
        detection: dict = {}
        claimed: set[str] = set()
        for d in real_defects:
            did = d["id"]
            fixed = apply_fix(srcs, did)
            for fname, src in fixed.items():
                (ws / fname).write_text(src, encoding="utf-8")
            _tail, fail_fixed, _el = runner(ws)
            detecting = sorted(set(baseline_fail) - set(fail_fixed))
            claimed |= set(detecting)
            detection[did] = {"detected": bool(detecting),
                              "tests": detecting[:6],
                              "location": d.get("location", "")}
            for fname, src in srcs.items():       # 还原 buggy 再试下一缺陷
                (ws / fname).write_text(src, encoding="utf-8")
        report["detection"] = detection
        report["detected_count"] = sum(1 for v in detection.values() if v["detected"])
        report["defects_real"] = len(real_defects)
        report["detection_rate"] = round(report["detected_count"] / len(real_defects), 3)
        unmatched = sorted(set(baseline_fail) - claimed)
        if unmatched:
            report["unmatched_failures"] = unmatched[:10]
            report["unmatched_note"] = ("基线失败中未对应任何植入缺陷的用例（多为写错/"
                                        "写崩的测试）——不冒充检出，如实留档")
        if do_mutation:
            report["mutation"] = _kill_rate(ws, srcs, baseline_fail, runner)
        if out is None:
            SCORES.mkdir(parents=True, exist_ok=True)
            out = SCORES / f"{label or 'suite'}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return report
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite_dir")
    ap.add_argument("--label", default="")
    ap.add_argument("--no-mutation", action="store_true")
    a = ap.parse_args()
    rep = score_suite(Path(a.suite_dir), label=a.label, do_mutation=not a.no_mutation)
    print(json.dumps({k: v for k, v in rep.items() if k != "detection"},
                     ensure_ascii=False, indent=1))
    det = rep.get("detection") or {}
    if det:
        print("检出明细：", {k: v["detected"] for k, v in det.items()})
    return 0 if "error" not in rep else 1


if __name__ == "__main__":
    sys.exit(main())
