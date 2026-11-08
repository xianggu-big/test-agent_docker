"""FP 纪律评测（干净快照 GT，defects=[]）——classify_failure 的 Go 环境信号回归锁定。

背景：go_test_evaluation 三项目 GT 全部 defects=[]（干净原始快照）→ 评测性质=误报纪律。
651 实锤：Go PG 连接拒绝 4 条被误判 product_defect（全部 FP）。本文件锁定修复后口径：
环境失败信号（dial tcp/connection refused 等）必须判 env，绝不落 product_defect。
同时回放 651 真实失败文本（零容器、单测级）——修复前 4 个 FP 在机制上转绿。
"""
from test_agent.graph.repair import classify_failure

# 651 真实失败文本（test_case_results 原文回放）
GO_REST_API_DB_FAIL = (
    "=========================== short test summary info ===========================\n"
    "FAILED github.com/qiangxue/go-rest-api/internal/album::TestRepository - "
    "db.go:30: dial tcp 127.0.0.1:5432: connect: connection refused\n"
    "FAILED github.com/qiangxue/go-rest-api/pkg/dbcontext::TestNew - "
    "db_test.go:113: dial tcp [::1]:5432: connect: connection refused\n"
    "=========================== 68 passed, 4 failed ==========================="
)


def test_go_dial_tcp_refused_is_env():
    assert classify_failure(1, GO_REST_API_DB_FAIL, "") == "env"


def test_go_connection_refused_bare_is_env():
    assert classify_failure(1, "FAILED x::y - getsockopt: connection refused", "") == "env"


def test_go_no_go_files_is_env():
    # 649 实锤形态：嵌套目录/多包根无 Go 文件
    assert classify_failure(1, "no Go files in /work", "") == "env"


def test_python_econnrefused_is_env():
    assert classify_failure(1, "requests.exceptions.ConnectionError: "
                               "HTTPConnectionPool(host='x', port=5432): "
                               "Max retries exceeded (Caused by "
                               "NewConnectionError('<...> ECONNREFUSED'))", "") == "env"


def test_real_defect_still_product():
    # FP 纪律的另一面：真断言失败不受影响（避免把 env 信号写宽误吞产品缺陷）
    assert classify_failure(1, "FAILED x::y - "
                               "TestGenR1_test.go:9: got 'B' want 'A'", "") == "product_defect"


def test_assertion_failure_with_unrelated_log_still_product():
    # 断言失败行存在时（无 env 触发词）维持 product 语义
    text = ("FAILED pkg::T - expected 'A' got 'B'\n"
            "    t.Errorf output\n"
            "=========================== 68 passed, 4 failed ===========================")
    assert classify_failure(1, text, "") == "product_defect"
