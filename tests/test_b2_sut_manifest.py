"""B2（任务卡-369 缺陷修复-20261105）：SUT 功能函数候选清单注入（最小版）。

369 实锤：questions.js（0 函数纯数据）被当被测单元，真功能 app.js:724 零执行——
清单把被测对象对准真功能：函数名×文件清单 + 纯数据文件显式禁作 SUT；
对话增补轮不注入（feedback=True）；旗标 SUT_MANIFEST_ENABLED 默认关。
"""
import ast

from test_agent import config
from test_agent.skills.gen_tests import _sut_manifest
from test_agent.skills.prompt_library import build_user_prompt


def test_js_manifest_extracts_functions():
    srcs = [("app.js",
             "function addRecord(q) {}\n"
             "function removeRecord(id) {\n  return true;\n}\n"
             "const helper = (x) => x + 1;\n"
             "const util = function (y) { return y; };\n"
             "tools: function (z) { return z; }\n")]
    out = _sut_manifest(srcs)
    assert out == ["app.js: addRecord, removeRecord, helper, util, tools"]


def test_pure_data_file_marked_not_sut():
    srcs = [("questions.js", "const QUESTIONS = [\n{id: 'r2020-01'},\n];\n"),
            ("app.js", "function removeRecord(id) { return true; }")]
    out = _sut_manifest(srcs)
    assert out[0] == "questions.js（纯数据文件，无可执行函数——禁作被测单元）"
    assert "removeRecord" in out[1]


def test_python_manifest_via_ast():
    srcs = [("calc.py", "def check_length(p):\n    return True\n\n\nX = 1\n")]
    assert _sut_manifest(srcs) == ["calc.py: check_length"]


def test_agent_generated_files_excluded():
    srcs = [("test_gen_r1.py", "def test_x():\n    pass\n"),
            ("app_test.js", "function internalOnly() {}")]
    assert _sut_manifest(srcs) == []


def test_prompt_injection_on_off():
    srcs = [("questions.js", "const QUESTIONS = [1];\n")]
    lst = _sut_manifest(srcs)
    on = build_user_prompt([("questions.js", "const QUESTIONS = [1];")], "需求",
                           sut_functions=lst)
    off = build_user_prompt([("questions.js", "const QUESTIONS = [1];")], "需求")
    assert "被测功能函数候选清单" in on and "禁作被测单元" in on
    assert "被测功能函数候选清单" not in off


def test_flag_default_off():
    assert config.SUT_MANIFEST_ENABLED is False
