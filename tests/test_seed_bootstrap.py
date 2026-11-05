"""种子自举模板单测（fake-LLM/fake-runner，无真容器）：结构锚点注入与降级语义。"""
from pathlib import Path

from test_agent.skills.gen_tests import _generate_seed
from test_agent.skills.prompt_library import build_user_prompt

# JSON 内换行必须写成 \\n 转义（与真实 LLM 响应一致——裸换行是非法 JSON）


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """第 1 次返回种子 JSON（可配置 N 次坏输出后再返回）。"""

    def __init__(self, seed_json, bad_before=0):
        self.seed_json = seed_json
        self.bad_before = bad_before
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.bad_before:
            return _Resp("完全不是 JSON")
        return _Resp(self.seed_json)


class _FakeRunner:
    def __init__(self, collect_ok=True):
        self.collect_ok = collect_ok

    def exec_in(self, container, cmd, timeout_s=120, workdir="/work"):
        ok = self.collect_ok

        class R:
            exit_code = 0 if ok else 1
            stdout = "" if ok else "no module named pytest"
            stderr = ""
        return R()


def _ws(tmp_path):
    (tmp_path / "checker.py").write_text("def check_length(p):\n    return False\n",
                                         encoding="utf-8")
    return tmp_path


def test_seed_success_returns_content_and_cleans_file(tmp_path):
    ws = _ws(tmp_path)
    seed_json = ('{"imports": "from checker import check_length", '
                 '"cases": [{"name": "t", "code": '
                 '"def test_simple():\\n    assert check_length(\'x\') is False"}]}')
    got = _generate_seed(_FakeLLM(seed_json), ws, "需求", _FakeRunner(True),
                         object(), "test_seed_probe.py")
    assert got is not None and "def test_simple" in got and "checker" in got
    assert not (ws / "test_seed_probe.py").exists()      # 验证后删除防执行干扰


def test_seed_collect_fail_retries_then_degrades(tmp_path):
    ws = _ws(tmp_path)
    seed_json = ('{"imports": "", '
                 '"cases": [{"name": "t", "code": "def test_t():\\n    assert 1"}]}')
    got = _generate_seed(_FakeLLM(seed_json), ws, "需求", _FakeRunner(False),
                         object(), "test_seed_probe.py", tries=3)
    assert got is None                                   # 收集失败→诚实降级
    assert not (ws / "test_seed_probe.py").exists()


def test_seed_unparsable_retries(tmp_path):
    ws = _ws(tmp_path)
    llm = _FakeLLM('{"cases": [{"name": "t", "code": "def test_t(): assert 1"}]}',
                   bad_before=2)
    got = _generate_seed(llm, ws, "需求", _FakeRunner(True), object(),
                         "test_seed_probe.py", tries=3)
    assert got is not None and llm.calls == 3            # 前两次坏输出第三次成功


def test_prompt_injects_seed_example_with_discipline():
    src = [("m.py", "def f(x):\n    return x\n")]
    on = build_user_prompt(src, "需求", seed_example="def test_s():\n    assert 1")
    assert "结构样板" in on and "严格模仿" in on and "def test_s" in on
    assert "结构样板" not in build_user_prompt(src, "需求")   # 关=不注入
