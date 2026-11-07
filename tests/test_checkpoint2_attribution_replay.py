"""◆检查点二·归因准确率复验（黑包执行序：黑2 DB 后的中期检查点，2026-11-07 收口轮）。

两部分：
1. 归因三闸真机重放（E1/E2/E3）——data/reports/gate_verify/verify_gates_real.py，
   真机证据落该目录（零 LLM、tmp 库、容器用完即删），不在 pytest 里跑（需真 Docker）。
2. 新语言堆栈帧归属抽查（本文件，零 LLM 确定性重放）：
   - Java 产品缺陷（java_adapter 统一文本，顶帧=SUT 源）→ product_defect 保留；
   - JS/TS 用例侧 TypeError（顶帧=evalmachine/生成文件）→ case_defect（A1 语义）；
   - JS/TS 产品缺陷（顶帧=被测源）→ product_defect 保留。
   Java 用例侧异常（NPE 等 Java 异常类不在 TypeError|ReferenceError 白名单）按既有
   诚实边界落 product_defect 启发式 → 人工标注纠正（classify_failure docstring 明文）。
"""
from test_agent.graph.repair import classify_failure


JAVA_PRODUCT = """============================= test session starts ==============================
FAILED src/test/java/TestGenR1.java::test_边界_90 [failure]
----------------------------------------------------
at com.example.OrderService.getTier(OrderService.java:12)
at com.example.TestGenR1.testBoundary90(TestGenR1.java:23)
AssertionError: expected: <silver> but was: <gold>"""


JAVA_CASE_STYLE = """FAILED src/test/java/TestGenR1.java::test_引用错 [error]
----------------------------------------------------
at com.example.TestGenR1.testRef(TestGenR1.java:8)
java.lang.NullPointerException: cannot invoke order.tier()"""


JS_CASE = """FAIL src/calc.test.js
● 边界 90 › throws

ReferenceError: buyMilkHelper is not defined
    at evalmachineWithContext.js:12:19
    at Object.<anonymous> (test_gen_r1.test.js:5:3)"""


JS_PRODUCT = """FAIL src/calc.test.js
● 边界 90 › clamp

AssertionError: expected 100 to be 90
    at Object.<anonymous> (src/calc.js:14:11)
    at Object.<anonymous> (test_gen_r1.test.js:9:5)"""


def test_java_product_defect_top_frame_sut_kept():
    # 顶帧=OrderService.java（SUT）→ 不进用例侧分支 → product_defect 保留
    assert classify_failure(1, JAVA_PRODUCT, "") == "product_defect"


def test_js_case_defect_top_frame_evalmachine():
    # A1 语义：顶帧=vm 沙箱/生成文件 → case_defect
    assert classify_failure(1, JS_CASE, "") == "case_defect"


def test_js_product_defect_top_frame_sut_kept():
    # 顶帧=src/calc.js（被测源）→ product_defect 保留（369 A1 修复核心语义）
    assert classify_failure(1, JS_PRODUCT, "") == "product_defect"


def test_java_case_style_falls_to_product_heuristic_honest_boundary():
    # Java 异常类（NPE）不在 A1 白名单（TypeError|ReferenceError）→ 落 product_defect
    # 启发式——既有诚实边界（自动归因=启发式，人工标注纠正），此处锁定现行为防静默漂移
    assert classify_failure(1, JAVA_CASE_STYLE, "") == "product_defect"
