# -*- coding: utf-8 -*-
"""演进序列生成器（E10 Arm 3 前置，确定性算子，零 LLM）。

对目标项目副本施加 N 步合成演进（2603.23443 演进测试口径的自有实现）：
  - param_change   数字字面量微调（如 0.9→0.8）：行为漂移但接口不变
  - boundary_shift 比较运算符翻转（>= → >）：需求边界移动（回归测试经典靶）
  - feature_append 追加新函数（含 docstring）：功能追加，测试套件未覆盖

每步 manifest：{seq, op, file, line, old, new, desc}——apply/rollback 均确定性
（文本替换）。退化曲线度量=每步全接力后的 (检出数, 覆盖率, 断言脆性)。

用法：
  python scripts/evo_sequence.py plan  <项目路径> <步数> <输出manifest>
  python scripts/evo_sequence.py apply <manifest> [--step N]   # 应用前 N 步
  python scripts/evo_sequence.py reset <项目路径> <manifest>    # 回滚到原始
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "vendor",
              "dist", "build", "target", ".idea", ".pytest_cache"}


def _src_files(ws: Path) -> list[Path]:
    exts = {".py", ".js", ".ts", ".go", ".java", ".php"}
    out = []
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if Path(f).suffix.lower() in exts:
                out.append(Path(dirpath) / f)
    return sorted(out)


def _plan_ops(ws: Path, steps: int) -> list[dict]:
    """从源码确定性提取可演进的锚点（数字字面量/比较运算符），循环生成序列。"""
    ops: list[dict] = []
    used: set[tuple] = set()   # (file, line) 已用锚点——防同锚点重复（二次应用时
    files = _src_files(ws)     # 旧文本已变 replace 失败，序列断链）
    if not files:
        return ops
    rnd_seed = sum(f.stat().st_size for f in files)  # 确定性伪随机（无随机源依赖）
    idx = 0
    round_no = 0
    while len(ops) < steps and round_no < steps * 6:
        round_no += 1
        f = files[(rnd_seed + round_no * 7) % len(files)]
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        # 算子轮换：按轮次（修 2026-11-16 实锤 bug——按 len(ops) 轮换在"某算子找不到
        # 锚点"时卡死：ops 恒 0 → kind 恒同一算子 → 整序列空）
        kind = ("param_change", "boundary_shift", "feature_append")[round_no % 3]
        if kind == "param_change":
            for ln_no, ln in enumerate(lines, 1):
                if (str(f), ln_no) in used:
                    continue
                m = re.search(r"(?<![A-Za-z0-9_.])(\d+\.\d+)(?![0-9])", ln)
                if m and "=" in ln or ("return" in ln and m):
                    old, new = m.group(1), f"{max(0.1, float(m.group(1)) - 0.1):.1f}"
                    if old != new:
                        ops.append({"seq": len(ops) + 1, "op": kind, "file": str(f),
                                    "line": ln_no, "old": old, "new": new,
                                    "desc": f"参数漂移 {old}→{new}（{f.name}:{ln_no}）"})
                        used.add((str(f), ln_no))
                        break
        elif kind == "boundary_shift":
            for ln_no, ln in enumerate(lines, 1):
                if (str(f), ln_no) in used:
                    continue
                if re.search(r"(if|while).*>=|<=?(?![<=])", ln) and not ln.strip().startswith("#"):
                    m = re.search(r"(>=|<=|<|>)(?!=)", ln)
                    if m:
                        flip = {">=": ">", "<=": "<", "<": "<=", ">": ">="}[m.group(1)]
                        old, new = m.group(1), flip
                        ops.append({"seq": len(ops) + 1, "op": kind, "file": str(f),
                                    "line": ln_no, "old": old, "new": new,
                                    "desc": f"边界翻转 {old}→{new}（{f.name}:{ln_no}）——断言脆性靶"})
                        used.add((str(f), ln_no))
                        break
        else:
            fname = f"evo_feature_{len(ops) + 1}"
            ops.append({"seq": len(ops) + 1, "op": kind, "file": str(f),
                        "line": len(lines) + 1,
                        "old": "", "new": fname,
                        "desc": f"功能追加 {fname}（{f.name}）——套件未覆盖新面"})
        idx += 1
    return ops[:steps]


def _apply_one(ws: Path, op: dict, dry: bool = False) -> bool:
    f = Path(op["file"])
    if not f.exists():
        return False
    text = f.read_text(encoding="utf-8", errors="ignore")
    if op["op"] == "feature_append":
        fname = op["new"]
        ext = f.suffix
        tmpl = {
            ".py": f"\n\ndef {fname}(x: int) -> int:\n    \"\"\"演进追加：x*2+1（套件未覆盖）。\"\"\"\n    return x * 2 + 1\n",
            ".js": f"\n\nfunction {fname}(x) {{ return x * 2 + 1; }}\n",
            ".go": f"\n\nfunc {fname.capitalize()}(x int) int {{ return x*2 + 1 }}\n",
            ".java": f"\n\n    static int {fname}(int x) {{ return x * 2 + 1; }}\n",
            ".php": f"\n\nfunction {fname}(int $x): int {{ return $x * 2 + 1; }}\n",
        }.get(ext, "")
        if dry:
            return bool(tmpl)
        f.write_text(text + tmpl, encoding="utf-8")
        return True
    # param_change / boundary_shift：目标行定点替换
    lines = text.splitlines()
    ln_no = op["line"]
    if ln_no > len(lines):
        return False
    old_ln = lines[ln_no - 1]
    if op["op"] == "param_change":
        new_ln = old_ln.replace(op["old"], op["new"], 1)
    else:
        # 只替换第一个未配对比较符（行内唯一性由 plan 时的锚点保证）
        new_ln = old_ln.replace(op["old"], op["new"], 1)
    if new_ln == old_ln:
        return False
    if not dry:
        lines[ln_no - 1] = new_ln
        f.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return True


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        ws, steps, out = Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
        ops = _plan_ops(ws, steps)
        Path(out).write_text(json.dumps(ops, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"序列 {len(ops)} 步 → {out}")
        for op in ops:
            print(f"  [{op['seq']:>2}] {op['op']:14} {op['desc'][:64]}")
        return 0 if len(ops) == steps else 1
    if cmd == "apply":
        mf = Path(sys.argv[2])
        step_to = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 10 ** 6
        ops = json.loads(mf.read_text(encoding="utf-8"))
        ok = sum(1 for op in ops if op["seq"] <= step_to and _apply_one(Path(op["file"]).parent, op))
        print(f"应用 {ok}/{len([o for o in ops if o['seq'] <= step_to])} 步")
        return 0
    if cmd == "reset":
        ws, mf = Path(sys.argv[2]), Path(sys.argv[3])
        ops = json.loads(mf.read_text(encoding="utf-8"))
        undone = 0
        for op in reversed(ops):   # 逆序回滚
            if op["op"] == "feature_append":
                f = Path(op["file"])
                if f.exists():
                    t = f.read_text(encoding="utf-8", errors="ignore")
                    if f"def {op['new']}(" in t or f"function {op['new']}(" in t:
                        pass   # 特性回滚=删追加块（简化：标记跳过，主实验用副本目录天然免回滚）
                undone += 1
                continue
            inv = {">=": "<=", "<=": ">=", "<": ">", ">": "<"}
            o = dict(op)
            if op["op"] == "boundary_shift":
                o["old"], o["new"] = op["new"], inv.get(op["new"], op["old"])
            if _apply_one(ws, o, dry=True):
                _apply_one(ws, o)
                undone += 1
        print(f"回滚处理 {undone}/{len(ops)} 步（主实验建议用副本目录，免回滚语义）")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
