"""评估计数口径锁（2026-11-15 闲时找bug 第一轮，R5 数据正确性维度）。

js_adapter.jest_json_to_pytest_text 与 graph/nodes.py evaluate 的计数契约：
evaluate 从摘要行解析 `(\d+) failed` 与 `(\d+) errors?` 后执行 `failed += errors`
（errors 计入失败——套件崩溃=用例侧问题，853 实锤语义）。适配器若先把 errors
折进 failed 又保留 "N errors" 行，同一批错误被加两次（failed=2N）——
evaluation_rounds.failed / pass_rate 分母全部虚高。

历史：fold（errors and failed==0 → failed=errors）先于 evaluate 的 errors 正则
引入，两者叠加后成为双计。
"""
import json
import re

from test_agent.skills.js_adapter import jest_json_to_pytest_text


def _jest_text(failed: int, errors: int, passed: int = 2) -> str:
    return json.dumps({
        "numPassedTests": passed,
        "numFailedTests": failed,
        "numRuntimeErrorTestSuites": errors,
        "numPendingTests": 0,
        "testResults": [],
        "coverageMap": None,
    })


def _evaluate_counts(stdout: str) -> int:
    """复刻 evaluate 节点的计数正则（nodes.py evaluate：failed += errors）。"""
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", stdout)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) errors?", stdout)) else 0
    return failed + errors


def test_suite_crash_errors_counted_once():
    """套件崩溃（failed=0、errors=2）：evaluate 最终 failed 必须是 2，不是 4。"""
    text = jest_json_to_pytest_text(_jest_text(failed=0, errors=2), stderr="")
    assert _evaluate_counts(text) == 2


def test_assertion_failures_not_folded_with_errors():
    """断言失败与套件崩溃并存（failed=1、errors=1）：合计 2，各自只计一次。"""
    text = jest_json_to_pytest_text(_jest_text(failed=1, errors=1), stderr="")
    assert _evaluate_counts(text) == 2


def test_zero_errors_summary_has_no_errors_line():
    """无 errors 时不输出 errors 行（避免 evaluate 把 0 当噪声）。"""
    text = jest_json_to_pytest_text(_jest_text(failed=1, errors=0), stderr="")
    assert "errors" not in text.split("====")[-1] or " 0 errors" not in text
    assert _evaluate_counts(text) == 1
