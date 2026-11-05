"""E（任务卡-369 缺陷修复-20261105）：缺陷去重/同根因聚类。

签名 = sha1(failure_kind | error_class | 归一化消息 | SUT首帧)[:12]；369 的 4 条
TypeError（Assignment to constant variable，仅变量/id 字面差异）归一化后同构 →
4 条聚 1 组（废案单从 4 条变 1 条+"影响 4 用例"）；不同 error_class 不合并；
旗标关=渲染逐字节不变。调研锚点：Sentry in-app frames / ReBucket（ICSE 2012）。
"""
import pytest

from test_agent import config
from test_agent.skills.defect_handoff import (
    _norm_message, _sut_frame, cluster_defects, defect_signature, render_defect_report)


def _mk(bug_id, msg, confidence="中"):
    return {"bug_id": bug_id, "message": msg, "confidence": confidence,
            "name": f"case_{bug_id}", "basis": "源码推导", "priority": "中",
            "condition": None, "requirement_ref": None, "method": "未标注",
            "expected": "—", "content": "—", "rounds": [1], "status": "failed", "nodeid": f"f::{bug_id}", "level": "L1"}


# 369 四条真实失败消息（test_case_results 原文形态：仅变量/id 字面差异）
M1 = ("Error: TypeError: Assignment to constant variable\n"
      "    at evalmachine.<anonymous>:1:2293\n    at /work/test_gen_r1.test.js:31:22")
M2 = ("Error: TypeError: Assignment to constant variable\n"
      "    at evalmachine.<anonymous>:1:2293\n    at /work/test_gen_r2.test.js:40:22")
M3 = ("Error: TypeError: Assignment to constant variable\n"
      "    at evalmachine.<anonymous>:1:2301\n    at /work/test_gen_fb1.test.js:49:22")


def test_norm_message_collapses_literals():
    assert _norm_message("id 'r2020-01' at /work/x.js line 31") == \
        "id <S> at <PATH> line <N>"


def test_sut_frame_empty_when_all_agent_frames():
    assert _sut_frame(M1) == ""                    # evalmachine/test_gen 全被排除


def test_sut_frame_picks_project_frame():
    m = M1 + "\n    at src/app.js:724:5"
    assert "app.js" in _sut_frame(m)               # Sentry in-app：被测源帧入选


def test_369_four_messages_cluster_into_one():
    ds = [_mk(f"BUG-{i:02d}", m) for i, m in enumerate((M1, M2, M3, M1), 1)]
    groups = cluster_defects(ds, failure_kind="product_defect")
    assert len(groups) == 1                        # 4→1 组
    assert groups[0]["size"] == 4
    sigs = {d["cluster"] for d in ds}
    assert len(sigs) == 1


def test_different_error_class_not_merged():
    ds = [_mk("BUG-01", M1),
          _mk("BUG-02", M1.replace("TypeError", "SyntaxError"))]
    groups = cluster_defects(ds)
    assert len(groups) == 2                        # error_class 不同不合并


def test_signature_deterministic():
    d = _mk("B1", M1)
    assert defect_signature(d, "product_defect") == defect_signature(d, "product_defect")


def _meta():
    return {"task_id": 369, "project": "853", "language": "javascript", "repo_url": "-",
            "image": "mta-test:node20-jest", "requirement": "测试添加删除", "status": "failed",
            "coverage_line": None, "stop_reason": "疑似产品缺陷", "analysis": None,
            "defects_tag": {"failure_kind": "product_defect"}, "conditions": []}


def test_render_clustered_vs_plain(tmp_path, monkeypatch):
    ds = [_mk(f"BUG-{i:02d}", m) for i, m in enumerate((M1, M2, M3), 1)]
    meta = _meta()
    monkeypatch.setattr(config, "DEFECT_CLUSTERING_ENABLED", True)
    clustered = render_defect_report(meta, ds)
    monkeypatch.setattr(config, "DEFECT_CLUSTERING_ENABLED", False)
    plain = render_defect_report(meta, ds)
    assert "缺陷组" in clustered and "同根因 3 条" in clustered
    assert "缺陷组（签名" not in plain              # 旗标关=逐字节不变（无组头）
