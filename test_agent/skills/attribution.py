"""失败归因三闸 v1（docs/11.4恢复计划.md §3）：环境闸 / 差分闸 / flaky 连跑。

研究先行（2026-11-04 检索核验，计划 §2 引用表）：
- 差分执行 buggy vs fixed 判定生成测试有效性：LIBRO（Kang et al.，ASE'23/ICSE'24，
  "validated by execution on both buggy and fixed program versions"）
- 生成用例连跑 k 次防"运气通过"：TestGen-LLM（Meta, FSE 2024, arXiv:2402.09171，
  跑 5 次丢弃任一失败；衍生实践默认 k=3）
- fail-to-pass / pass-to-pass 判定口径：SWT-Bench（arXiv:2406.12952）

诚实边界：
- 三个开关默认全关（config.ENV_GATE_ENABLED / DIFF_GATE_ENABLED / FLAKY_RERUNS=0），
  与 RECIPE_ENABLED 等 V 变量同一惯例——触发与否都写进台账消息，不静默。
- 差分闸要求工作区是 git 仓库且能解析对照 ref；不满足→如实返回 no_git/no_ref，
  调用方维持原归因（宁可 uncertain 不可硬猜）。
- bug 版失败 + 对照版同样失败只能判"非产品缺陷"（用例错/环境错不可分），置信=低；
  最终定性仍靠人工标注与变异标定（E4 口径不变）。
- 与既有"防造假三道闸"（语义/证人/报告）职责不同：本模块管失败归因，防的是
  "把用例/环境错误记成产品缺陷"（task_366 误诊形态）。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

CANARY_OK = "CANARY_OK"

# 2×2 判定标签（复用既有 taxonomy：env/case_defect/product_defect，见 graph/repair.py）
LABEL_PRODUCT = "product_defect"
LABEL_CASE = "case_defect"
LABEL_NONE = "none"


def canary_cmd(lang: str) -> list[str]:
    """环境闸金丝雀命令：框架自检 + 临时文件 IO 自检（同一容器内联执行，不落测试文件）。

    不 import 项目依赖——依赖缺失由 provision/db_probe 负责；金丝雀只回答
    "这个容器还能不能可靠地跑测试"这一个问题的最小集合。
    """
    if lang == "python":
        code = (
            "import pytest, tempfile, os;"
            "f = tempfile.NamedTemporaryFile(delete=False);"
            "f.write(b'ok'); f.close(); os.unlink(f.name);"
            f"print('{CANARY_OK}')"
        )
        return ["python", "-c", code]
    if lang == "javascript":
        code = (
            "const fs=require('fs'),os=require('os'),p=os.tmpdir()+'/_canary.tmp';"
            "fs.writeFileSync(p,'ok');fs.unlinkSync(p);"
            f"console.log('{CANARY_OK}')"
        )
        return ["node", "-e", code]
    raise ValueError(f"canary 不支持语言: {lang}")


def canary_ok(exit_code: int, stdout: str) -> bool:
    """金丝雀通过判定：退出码 0 且输出含约定标记（防"exit 0 但啥没跑"假绿）。"""
    return exit_code == 0 and CANARY_OK in (stdout or "")


def classify_2x2(bug_failed: bool, ref_failed: bool) -> tuple[str, str]:
    """差分闸 2×2 判定矩阵（LIBRO 口径）。返回 (label, confidence)。

    - V_bug fail + V_ref pass  → product_defect（fail-to-pass，高置信缺陷暴露）
    - V_bug fail + V_ref fail  → case_defect（低置信：用例错/环境错不可分，但
      **排除产品缺陷**——对照版同样失败说明断言与正确行为不符）
    - V_bug pass + V_ref fail  → case_defect（高置信：断言反演/迁就 bug 行为写成预期）
    - V_bug pass + V_ref pass  → none（正常，不适用归因）
    """
    if bug_failed and not ref_failed:
        return LABEL_PRODUCT, "高"
    if bug_failed and ref_failed:
        return LABEL_CASE, "低"       # 排除产品缺陷，但用例/环境二选一不可分
    if not bug_failed and ref_failed:
        return LABEL_CASE, "高"       # overfit：把 bug 行为写成了断言
    return LABEL_NONE, "—"


def is_flaky(outcomes: Sequence[bool]) -> bool:
    """连跑结果不一致（pass/fail 混合）→ flaky（TestGen-LLM 口径：任一不一致即弃）。"""
    return len(set(outcomes)) > 1


def subset_cmd(lang: str, files: Sequence[str]) -> list[str]:
    """差分闸只在失败用例子集上复跑（计划 §3 闸 2 成本控制：不双跑全量）。"""
    files = list(files)
    if not files:
        raise ValueError("差分子集为空")
    if lang == "python":
        return ["python", "-m", "pytest", "-q", *files]
    if lang == "javascript":
        return ["npx", "jest", *files]
    raise ValueError(f"差分闸不支持语言: {lang}")


def run_diff_gate(exec_fn: Callable[..., object], lang: str,
                  failed_files: Sequence[str], v_ref: str,
                  timeout_s: int = 600, bug_stderr: str = "") -> dict:
    """差分闸执行器：在容器 /work 内用 git 切到对照 ref 复跑失败子集，再切回。

    exec_fn(cmd, timeout_s=...) → 任意带 exit_code/stdout/stderr 属性的对象
    （sandbox.runner.ExecResult 即满足；测试可注入 fake）。
    未跟踪文件（生成用例 test_gen_*）在 git checkout 下天然保留——用例不动，
    只切被测源码，正是 LIBRO 语义。

    双败格细分（2026-11-05，ReportPortal 同 taxonomy 的确定性版）：V_bug/V_ref 双败
    只能"排除产品缺陷"，用例错与环境错不可分（2×2 理论边界）——现用两个确定性证据
    收窄：①金丝雀复跑（框架/IO 健康？）②bug/对照两版 stderr 的错误特征表
    （ECONNREFUSED 系→环境特征；Cannot find module/ImportError 系→用例特征）。
    零 LLM，只收窄证据不改标签（标签仍 case_defect，细分写进 detail 供人工/统计）。

    返回 {"status", "ref_failed", "label", "confidence", "detail"}：
    - status=ok           → label/confidence 有效（classify_2x2 结果）
    - status=no_git/no_ref → 差分不可用，label=None（调用方维持原归因）
    - status=restore_failed → 结论有效但工作区停留在 ref，调用方必须显著告警
    """
    failed_files = list(failed_files)
    r = exec_fn(["git", "-C", "/work", "rev-parse", "--is-inside-work-tree"], timeout_s=30)
    if getattr(r, "exit_code", 1) != 0:
        return {"status": "no_git", "ref_failed": None, "label": None,
                "confidence": None, "detail": "工作区不是 git 仓库，差分闸不可用"}
    r = exec_fn(["git", "-C", "/work", "rev-parse", "--verify", f"{v_ref}^{{commit}}"],
                timeout_s=30)
    if getattr(r, "exit_code", 1) != 0:
        return {"status": "no_ref", "ref_failed": None, "label": None,
                "confidence": None, "detail": f"对照 ref {v_ref} 无法解析"}
    r = exec_fn(["git", "-C", "/work", "rev-parse", "HEAD"], timeout_s=30)
    if getattr(r, "exit_code", 1) != 0:
        return {"status": "no_git", "ref_failed": None, "label": None,
                "confidence": None, "detail": "无法读取当前 HEAD"}
    orig_head = (getattr(r, "stdout", "") or "").strip()

    r = exec_fn(["git", "-C", "/work", "checkout", "-q", "--detach", v_ref], timeout_s=60)
    if getattr(r, "exit_code", 1) != 0:
        return {"status": "no_ref", "ref_failed": None, "label": None,
                "confidence": None, "detail": f"切到 {v_ref} 失败：{(getattr(r, 'stderr', '') or '')[-160]}"}

    r = exec_fn(subset_cmd(lang, failed_files), timeout_s=timeout_s)
    ref_exit = getattr(r, "exit_code", 1)
    ref_stdout = getattr(r, "stdout", "") or ""
    ref_failed = not outcome_passed(lang, ref_exit, ref_stdout)
    evidence = ("；" + _double_fail_evidence(exec_fn, lang, bug_stderr,
                                            getattr(r, "stderr", "") or "")
                if ref_failed else "")

    r_back = exec_fn(["git", "-C", "/work", "checkout", "-q", orig_head], timeout_s=60)
    label, confidence = classify_2x2(True, ref_failed)
    if getattr(r_back, "exit_code", 1) != 0:
        return {"status": "restore_failed", "ref_failed": ref_failed,
                "label": label, "confidence": confidence,
                "detail": f"差分完成但工作区未能切回 {orig_head[:8]}——必须人工检查 git 状态"}
    return {"status": "ok", "ref_failed": ref_failed, "label": label,
            "confidence": confidence,
            "detail": f"对照 {v_ref} 上{'同样失败' if ref_failed else '通过'}" + evidence}


def outcome_passed(lang: str, exit_code: int, stdout: str) -> bool:
    """flaky 连跑的单次结果判定：只看用例通过/失败，不吃覆盖率退出码干扰。"""
    if lang == "python":
        import re
        failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", stdout)) else 0
        passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", stdout)) else 0
        return failed == 0 and passed > 0
    # js：jest --json 原始输出（rerun 直接 exec 同一命令，未经 js_adapter 转换）
    import json as _json
    import re
    if m := re.search(r'"numFailedTests":\s*(\d+)', stdout):
        failed = int(m.group(1))
        passed = int(m.group(1)) if (m := re.search(r'"numPassedTests":\s*(\d+)', stdout)) else 0
        return failed == 0 and passed > 0
    _ = _json
    return exit_code == 0


# —— 双败格细分证据表（task_366 教训 + ReportPortal 同 taxonomy 的确定性版）——
# ENOENT/EACCES 语义两可（缺文件可能是用例引用错也可能是环境），不入表只留原话
_ENV_FAIL_HINTS = (
    "ECONNREFUSED", "ETIMEDOUT", "ECONNRESET", "EHOSTUNREACH", "EADDRINUSE",
    "connection refused", "no route to host", "ConnectionRefusedError",
    "ConnectionResetError", "socket.timeout", "No space left on device",
    "out of memory", "OOMKilled",
)
_CASE_FAIL_HINTS = (
    "Cannot find module", "MODULE_NOT_FOUND", "ModuleNotFoundError",
    "ImportError", "SyntaxError", "ReferenceError", "NameError",
)


def _scan_hints(text: str, table: tuple[str, ...]) -> list[str]:
    text = text or ""
    return [h for h in table if h in text]


def _double_fail_evidence(exec_fn: Callable[..., object], lang: str,
                          bug_stderr: str, ref_stderr: str) -> str:
    """双败格收窄证据（零 LLM，确定性）：金丝雀复跑 + 两版 stderr 特征扫描。
    只收窄证据不改标签——标签仍是 case_defect（用例错/环境错理论不可分），
    细分结论供人工裁决与 A 轨统计。"""
    hints: list[str] = []
    try:
        c = exec_fn(canary_cmd(lang), timeout_s=120)
        hints.append("金丝雀复跑通过（框架/IO 健康）" if canary_ok(c.exit_code, c.stdout)
                     else f"金丝雀复跑未通过（exit={c.exit_code}）→ 环境健康存疑")
    except Exception as e:  # noqa: BLE001 —— 探测失败不推翻差分结论，如实留痕
        hints.append(f"金丝雀探测异常（{type(e).__name__}）")
    for src, text in (("bug版", bug_stderr), ("对照版", ref_stderr)):
        if env_h := _scan_hints(text, _ENV_FAIL_HINTS):
            hints.append(f"{src} stderr 含环境特征错：{env_h[0]}")
        elif case_h := _scan_hints(text, _CASE_FAIL_HINTS):
            hints.append(f"{src} stderr 含用例特征错：{case_h[0]}")
    return "；".join(hints[:4])
