# -*- coding: utf-8 -*-
"""黑盒诚实出口·端点对账升级测试锁（2026-11-19 复盘方案 A+B）。

复盘结论：出口宣称"接口级通过数为准"但没算端点覆盖=半诚实——本轮补齐：
全覆盖→stop 带 "端点 C/T 全覆盖"；有缺口→None 走 repair_gen+缺口消息；
无注册清单→旧行为逐字节不变。detect 读全文件（修 8000 截断边界洞）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.graph.nodes import (_endpoint_calls_in_tests, _norm_ep_path,
                                    _url_to_path, api_blackbox_stop,
                                    detect_api_blackbox)


# ---------------- 归一化 ----------------

def test_norm_path_templates_and_numeric():
    assert _norm_ep_path("/albums/<id>") == "/albums/{p}"
    assert _norm_ep_path("/books/{bid}") == "/books/{p}"
    assert _norm_ep_path("/books/999999") == "/books/{p}"      # 具体数字 id=模板
    assert _norm_ep_path("/ping") == "/ping"
    assert _norm_ep_path("/books/") == "/books"                # 尾斜杠


def test_url_to_path():
    assert _url_to_path("http://127.0.0.1:8080/ping") == "/ping"
    assert _url_to_path("http://127.0.0.1:8080/") == "/"
    assert _url_to_path("http://127.0.0.1:8080/books?x=1") == "/books"


# ---------------- php 提取（task_1981 真机形态重放） ----------------

PHP_REAL = '''<?php
public function test_ping(): void {
    $ch = curl_init('http://127.0.0.1:8080/ping');
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_CUSTOMREQUEST => 'GET', CURLOPT_TIMEOUT => 10]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    curl_close($ch);
    $this->assertSame(200, $code);
}
public function test_post_books(): void {
    $ch = curl_init('http://127.0.0.1:8080/books');
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_CUSTOMREQUEST => 'POST', CURLOPT_POSTFIELDS => '{}']);
    $this->assertSame(201, curl_getinfo($ch, CURLINFO_RESPONSE_CODE));
}
public function test_delete_book(): void {
    $ch = curl_init('http://127.0.0.1:8080/books/999999');
    // 相邻两个 init 之间无 CUSTOMREQUEST → 默认 GET（配对语义）
    $ch2 = curl_init('http://127.0.0.1:8080/books');
    curl_setopt_array($ch2, [CURLOPT_CUSTOMREQUEST => 'DELETE']);
}
'''


def test_php_calls_extraction_real_shape(tmp_path):
    (tmp_path / "test_gen_r1.php").write_text(PHP_REAL, encoding="utf-8")
    calls = _endpoint_calls_in_tests(tmp_path, "php")
    assert "GET /ping" in calls
    assert "POST /books" in calls
    assert "GET /books/{p}" in calls           # 999999 → {p}
    assert "DELETE /books" in calls            # init2 的 method 配对到 init1 后无方法段
    assert len(calls) == 4


# ---------------- 出口三态 ----------------

def _base_kwargs():
    return dict(blackbox=True, verdict="repair_gen", failed=0, exit_code=0,
                passed=23, coverage=0.0)


def test_stop_full_coverage_message():
    out = api_blackbox_stop(endpoint_cov={"covered": 4, "total": 4, "missed": []},
                            **_base_kwargs())
    assert out is not None and out[0] == "stop"
    assert "端点 4/4 全覆盖" in out[2], "对账数字必须进消息（半诚实→真诚实）"


def test_no_stop_when_endpoints_missed():
    out = api_blackbox_stop(
        endpoint_cov={"covered": 2, "total": 4, "missed": ["DELETE /books/{p}", "POST /books"]},
        **_base_kwargs())
    assert out is None, "端点有缺口不得 stop（补测一轮，预算兜底）"


def test_old_behavior_byte_identical_without_cov():
    out = api_blackbox_stop(endpoint_cov=None, **_base_kwargs())
    assert out is not None and "接口级通过数为准" in out[2]
    assert "端点" not in out[2]                # 无清单=旧行为逐字节不变
    # 旧签名兼容（不传 endpoint_cov）
    out2 = api_blackbox_stop(blackbox=True, verdict="repair_gen", failed=0,
                             exit_code=0, passed=23, coverage=0.0)
    assert out2 == out


def test_failed_cases_still_normal_triage():
    assert api_blackbox_stop(endpoint_cov={"covered": 4, "total": 4, "missed": []},
                             blackbox=True, verdict="repair_gen", failed=2,
                             exit_code=1, passed=5, coverage=0.4) is None


# ---------------- detect 读全文件（修 8000 截断） ----------------

def test_detect_http_marker_beyond_8000(tmp_path):
    filler = "// " + "x" * 9000 + "\n"
    (tmp_path / "test_gen_r1.test.js").write_text(
        filler + "const res = await fetch('http://127.0.0.1:8080/ping');\n",
        encoding="utf-8")
    assert detect_api_blackbox(tmp_path, "javascript") is True
