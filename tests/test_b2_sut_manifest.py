"""B2（任务卡-369 缺陷修复-20261105）：SUT 功能函数候选清单注入（最小版）。

369 实锤：questions.js（0 函数纯数据）被当被测单元，真功能 app.js:724 零执行——
清单把被测对象对准真功能：函数名×文件清单 + 纯数据文件显式禁作 SUT；
对话增补轮不注入（feedback=True）；旗标 SUT_MANIFEST_ENABLED 默认关。
"""
import ast

from test_agent import config
from test_agent.skills import gen_tests as _gen
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


# ---------- B2+（415 实锤）：需求-能力错配确定性核对（2026-11-06） ----------

from test_agent.skills.gen_tests import requirement_capability_mismatch as _mismatch

# 853 项目近似形态：registerSubject 在清单里，但没有任何账户体系函数
_853_LIKE = [("data/subjects/_registry.js",
              "function registerSubject(pack) { return true; }\n"),
             ("js/app.js",
              "function addRecord(q) {}\nfunction vAi() {}\n")]


def test_415_mismatch_login_register_flagged():
    """415 原需求（原文用「登陆」）：登陆/注册在 853 能力清单零命中
    （registerSubject 被「注册」全词匹配挡住不算命中）→ 报警。"""
    out = _mismatch("验证登陆注册系统是否有异常", _853_LIKE)
    assert out is not None
    assert "登陆" in out and "注册" in out and "未找到对应实现" in out


def test_registerSubject_does_not_count_as_register_hit():
    """边界环视：camelCase 的 registerSubject 不被 注册→register 误命中。"""
    srcs = [("data/subjects/_registry.js", "function registerSubject(p) {}\n")]
    assert _mismatch("测试注册功能", srcs) is not None


def test_snake_case_hit_counts():
    """snake_case 的 user_login 应命中 登录→login（下划线不算字母）。"""
    srcs = [("auth.py", "def user_login(u, p):\n    return True\n")]
    assert _mismatch("验证登录功能", srcs) is None


def test_mismatch_none_when_capability_exists():
    """需求关键词在清单命中（添加→addRecord）→ 不报警。"""
    assert _mismatch("测试添加记录的功能", _853_LIKE) is None


def test_mismatch_none_without_keywords_or_sources():
    assert _mismatch("随便测一下", _853_LIKE) is None       # 无关键词
    assert _mismatch("测试登录", []) is None                 # 无源码


def test_mismatch_prompt_injection_on_off():
    warn = "需求中的关键词「登录」未找到对应实现。"
    on = build_user_prompt(_853_LIKE, "测试登录", sut_mismatch=warn)
    off = build_user_prompt(_853_LIKE, "测试登录")
    assert "需求-能力对齐警告" in on and "禁止为它编造任何内联 mock" in on
    assert "需求-能力对齐警告" not in off


def test_mismatch_in_generate_summary_visible():
    """419 教训：错配必须对用户显性可见——generate_tests 的 summary 首键必须是
    capability_mismatch（[generate 第N轮] 消息只展示前 400 字符，首键保证截断下
    用户仍能看到警告）。源码级契约断言。"""
    import inspect
    src = inspect.getsource(_gen.generate_tests)
    marker = 'summary = {"capability_mismatch": sut_mismatch,'
    assert marker in src, "summary 首键必须是 capability_mismatch（419 用户可见性教训）"
