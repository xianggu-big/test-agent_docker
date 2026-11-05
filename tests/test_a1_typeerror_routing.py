"""A1（任务卡-369 缺陷修复-20261105）：classify_failure 补 TypeError/ReferenceError。

369 实锤：4 条 "TypeError: Assignment to constant variable"（堆栈 evalmachine.<anonymous>）
不在 case 侧错误清单 → 被 "FAILED" 分支吞成 product_defect → 首轮 stop（误诊为产品缺陷，
R2 预算内本可 repair_gen 重建）。修复前本文件 3 条核心断言红、修复后绿（对照证据=git 历史）。

防反向误诊的共现约束：TypeError/ReferenceError 必须与用例侧堆栈标记
（evalmachine / test_gen_r / test_imported / test_gen_fb）共现才判 case_defect；
裸 TypeError 共现被测源文件路径的保持 product_defect（产品真抛类型错的场景不误伤）。
"""
from test_agent.graph.repair import CASE_OWN_DEFECT_ERRORS, classify_failure, decide

# 369 真实失败文本形态（test_case_results.message + jest failureMessage 结构）
MSG_369 = (
    "FAILED test_gen_r1.test.js::test_题库_删除一条记录后不再查询到_等价类划分 - "
    "TypeError: Assignment to constant variable\n"
    "    at evalmachine.<anonymous>:1:2293\n"
    "    at /work/test_gen_r1.test.js:31:22\n"
    "1 failed, 19 passed")


def test_369_typeerror_with_agent_stack_is_case_defect():
    assert classify_failure(1, MSG_369, "") == "case_defect"


def test_369_referenceerror_with_agent_stack_is_case_defect():
    msg = ("FAILED test_gen_r2.test.js::test_注册_非法包被拒绝 - ReferenceError: "
           "registerSubject is not defined\n    at evalmachine.<anonymous>:5:9")
    assert classify_failure(1, msg, "") == "case_defect"


def test_369_decide_repair_gen_within_budget():
    """端到端：369 R2（round_no=2 < MAX_REPAIR_ROUNDS=3）→ repair_gen 而非 stop。"""
    verdict, kind, reason = decide(
        exit_code=1, stdout=MSG_369, stderr="", fingerprints=[], fingerprint="fp369",
        round_no=2, coverage_line=None, coverage_target=0.7, witness_nodeids=None)
    assert verdict == "repair_gen", reason
    assert kind == "case_defect"


def test_bare_typeerror_with_sut_stack_stays_product():
    """反向保护：TypeError 共现被测源文件堆栈（无用例侧标记）→ 保留 product_defect。"""
    sut = ("FAILED test_gen_r1.test.js::test_calc_负数 - TypeError: Cannot read "
           "properties of undefined (reading 'total')\n    at src/calc.js:12:5\n"
           "    at Object.<anonymous> (/work/test_gen_r1.test.js:9:11)")
    assert classify_failure(1, sut, "") == "product_defect"


def test_case_own_defect_errors_extended():
    """剪除/重写口径联动：①gen_tests 剪除 ②repair_strategy 重写都查本集合——
    集合补 TypeError/ReferenceError 后三处消费点自动同口径。"""
    assert "TypeError" in CASE_OWN_DEFECT_ERRORS
    assert "ReferenceError" in CASE_OWN_DEFECT_ERRORS
    # 既有成员不丢（回归保护）
    assert "AssertionError" not in CASE_OWN_DEFECT_ERRORS  # 证人语义不动
    assert "ImportError" in CASE_OWN_DEFECT_ERRORS
