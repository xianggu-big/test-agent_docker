"""CLI 差分路线（任务卡-D+ 路线一，2026-11-10）：observation=cli, oracle=reference_diff。

oracle 谱系补白：判据来自"参照物"（git 历史版本/参照实现）而非人工声明——
"无规格"场景的标准答案。同一命令分别跑当前版与参照版，归一化后比对
stdout/退出码；不一致 = 改动引入的行为差异（强 product_defect 信号，
是否缺陷还是有意变更需需求判断——诚实边界，见 summarize 注记）。

**归一化层（防假阳性洪水）**：ISO 时间戳→<TS>、文件路径→<PATH>、hex 随机 id
→<ID>、时长→<DUR>、浮点→<F>；**裸整数保留**——它们是 CLI 输出的真实载荷
（"processed=7" vs "processed=8" 正是要抓的差异，吞掉整数=吞掉检出能力）。

**双向判定的工程化（比卡面"ref 也复现"更可测）**：参照物双跑确定性校验——
参照版同输入跑两次，自身输出就不一致 → 差异可能为噪声，如实降级 ref_unstable
不冒充缺陷信号；差分检出后再对当前版复核一次，复核与参照一致 → cur_unstable
（当前版抖动）。两道闸共同兑现"0 假阳性"验收。

旗标：MTA_DIFF_ROUTE 默认关——route_plan 按旗标剔除该路线；本函数另有执行闸
（关=RuntimeError 拒绝），双保险。宿主侧执行（卡 §1.2 明示先做宿主侧最简方案）。
"""
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

_RE_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_RE_PATH = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"']+)|(?:/(?![\s<])[^\s\"<>]+/[\w.\-]+)")
# 含空格的 Windows 路径主正则只吃到首个空格（"D:\test agent_docker\x" 的空格段），
# 残余的「分隔符相对路径」再补一刀——否则跨版本目录名残留在差异行=路径伪差异
_RE_PATH_RES = re.compile(r"(?:[\w.\-]+[\\/])+[\w.\-]+")
_RE_HEXID = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_RE_DUR = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|us|µs|ns|sec|s)\b")
_RE_FLOAT = re.compile(r"\b\d+\.\d+\b")


def normalize_output(text: str) -> str:
    """行级归一化（顺序敏感：时间戳先于浮点，路径先于 hex id）。已知取舍：
    分数形态 "1/2" 会被残余路径正则吞为 <PATH>——CLI 差分场景分数罕见、
    路径伪差异常见，取舍偏向后者。"""
    s = text or ""
    s = _RE_TS.sub("<TS>", s)
    s = _RE_PATH.sub("<PATH>", s)
    s = _RE_PATH_RES.sub("<PATH>", s)
    s = _RE_HEXID.sub("<ID>", s)
    s = _RE_DUR.sub("<DUR>", s)
    s = _RE_FLOAT.sub("<F>", s)
    return s


def _first_diffs(cur_norm: str, ref_norm: str, limit: int = 5) -> list[dict]:
    """归一化文本的首批差异行（哪一行/参照 vs 当前）。"""
    cl, rl = cur_norm.splitlines(), ref_norm.splitlines()
    out = []
    for i in range(max(len(cl), len(rl))):
        a = cl[i] if i < len(cl) else "<缺行>"
        b = rl[i] if i < len(rl) else "<缺行>"
        if a != b:
            out.append({"line_no": i + 1, "current": a[:200], "reference": b[:200]})
            if len(out) >= limit:
                break
    return out


def classify_pair(cur_exit: int, cur_out: str, ref_exit: int, ref_out: str) -> str:
    """单输入的差分判定：same / stdout_diff / exit_diff / both_diff。"""
    same_out = normalize_output(cur_out) == normalize_output(ref_out)
    same_exit = cur_exit == ref_exit
    if same_out and same_exit:
        return "same"
    if not same_out and not same_exit:
        return "both_diff"
    return "exit_diff" if not same_exit else "stdout_diff"


@dataclass
class DiffCase:
    args: list
    kind: str                      # same/stdout_diff/exit_diff/both_diff/ref_unstable/cur_unstable
    cur_exit: int = 0
    ref_exit: int = 0
    cur_norm: str = ""
    ref_norm: str = ""
    line_diffs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"args": self.args, "kind": self.kind, "cur_exit": self.cur_exit,
                "ref_exit": self.ref_exit, "cur_norm": self.cur_norm,
                "ref_norm": self.ref_norm, "line_diffs": self.line_diffs}


def _default_runner(ws, args) -> tuple[int, str]:
    """宿主侧默认执行：python <args>，cwd=工作区。"""
    r = subprocess.run([sys.executable, *args], cwd=str(ws), capture_output=True,
                       text=True, timeout=120, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or ""


def run_cli_diff(ws_cur, ws_ref, inputs: list, runner=None, enabled: bool | None = None,
                 confirm_diff: bool = True) -> dict:
    """差分主入口：每组输入 cur 跑 1 次、ref 跑 2 次（确定性校验）；检出后再复核
    cur 一次（confirm_diff）。runner 可注入（单测）；默认宿主 python 执行。"""
    if enabled is None:
        enabled = config.DIFF_ROUTE_ENABLED
    if not enabled:
        raise RuntimeError("差分路线未启用（MTA_DIFF_ROUTE 默认关）——如需使用请显式开启")
    runner = runner or _default_runner
    cases: list[DiffCase] = []
    for args in inputs:
        cur_exit, cur_out = runner(ws_cur, args)
        ref_exit1, ref_out1 = runner(ws_ref, args)
        ref_exit2, ref_out2 = runner(ws_ref, args)
        c = DiffCase(args=list(args), kind="", cur_exit=cur_exit, ref_exit=ref_exit1,
                     cur_norm=normalize_output(cur_out),
                     ref_norm=normalize_output(ref_out1))
        if normalize_output(ref_out1) != normalize_output(ref_out2) or ref_exit1 != ref_exit2:
            c.kind = "ref_unstable"          # 参照自身不确定：降级不判，防假阳性
        else:
            c.kind = classify_pair(cur_exit, cur_out, ref_exit1, ref_out1)
            if c.kind != "same" and confirm_diff:
                cur_exit2, cur_out2 = runner(ws_cur, args)
                if classify_pair(cur_exit2, cur_out2, ref_exit1, ref_out1) == "same":
                    c.kind = "cur_unstable"  # 当前版抖动：复核与参照一致，降级
        if c.kind in ("stdout_diff", "both_diff"):
            c.line_diffs = _first_diffs(c.cur_norm, c.ref_norm)
        cases.append(c)
    summary = {"total": len(cases),
               "diff_introduced": sum(1 for c in cases
                                      if c.kind in ("stdout_diff", "exit_diff", "both_diff")),
               "same": sum(1 for c in cases if c.kind == "same"),
               "ref_unstable": sum(1 for c in cases if c.kind == "ref_unstable"),
               "cur_unstable": sum(1 for c in cases if c.kind == "cur_unstable")}
    note = ("diff_introduced=改动引入的行为差异（强 product_defect 信号）；"
            "是否缺陷还是有意变更需需求判断——差分只能定位差异，不能定性意图（诚实边界）"
            if summary["diff_introduced"] else "")
    return {"cases": [c.to_dict() for c in cases], "summary": summary, "note": note}
