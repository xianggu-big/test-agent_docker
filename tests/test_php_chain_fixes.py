"""PHP 链三修测试锁（2026-11-15 任务卡-错误清扫与找bug收尾 卡 2，1148-1150 实锤）。

真机取证（本卡附带，Docker 复现 1148 demo-main）：
1. composer install 实际能装完 vendor（132 包），但 post-install auto-scripts
   （php-script 不存在）exit 127 → composer 非零退出——失败被 install_deps 只记
   数字、provision 消息只打"ok"吞掉；
2. vendor 装完后 phpunit 仍在 platform_check.php 即 Fatal（Composer detected
   issues: require PHP >= 8.4.1，镜像 8.3.36）→ exit 255 → junit 缺失 → 适配器
   统一文本 "0 passed" 假象 → repair_env 三轮同指纹熔断，真实死因蒸发；
3. 1150（wordpress）同时有 package.json+composer.json → probe 优先级 package_json
   先中 → composer install 从未执行 → vendor 缺失 → phpunit 缺失；
4. "pytest 格式输出"之谜取证结论：=php_adapter 统一文本自身（S5 设计使然，
   "N passed" 摘要行），php83 镜像烘焙的 python3+pytest 是 L2 契约轨用，
   L1 从未执行 pytest——无需删改。
"""
from pathlib import Path

import pytest

from test_agent.graph.nodes import (php_exit_for_decide, php_platform_gate_result,
                                    php_unified_with_raw)
from test_agent.skills.builtin import probe_deps_manifest


# ---------- 1. composer install 强制（1150 根因：清单优先级） ----------

def _mk_ws(tmp_path: Path, files: list[str]) -> Path:
    for f in files:
        (tmp_path / f).write_text("{}", encoding="utf-8")
    return tmp_path


def test_php_prefers_composer_over_package_json(tmp_path):
    """php 项目同时有 package.json + composer.json → composer_json 必须胜出
    （phpunit 只能来自 composer；npm 只管前端工具链）。"""
    ws = _mk_ws(tmp_path, ["package.json", "composer.json"])
    assert probe_deps_manifest(ws, "php") == "composer_json"


def test_non_php_keeps_package_json_priority(tmp_path):
    """非 php 项目（或未传语言）行为逐字节不变——package.json 先中。"""
    ws = _mk_ws(tmp_path, ["package.json", "composer.json"])
    assert probe_deps_manifest(ws, "python") == "package_json"
    assert probe_deps_manifest(ws) == "package_json"


def test_php_without_composer_falls_through(tmp_path):
    """php 项目无 composer.json → 走既有链（package.json 或 None），不硬猜。"""
    ws = _mk_ws(tmp_path, ["package.json"])
    assert probe_deps_manifest(ws, "php") == "package_json"
    assert probe_deps_manifest(_mk_ws(tmp_path / "e2", []), "php") is None


# ---------- 2. phpunit 缺失明确报错（builtin.run_tests 前置探测） ----------

class _FakeResult:
    def __init__(self, exit_code, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr
        self.ok = exit_code == 0


class _FakeContainer:
    def __init__(self, responses):
        self._responses = list(responses)

    def exec_run(self, cmd, workdir="/work", demux=True):
        r = self._responses.pop(0)
        return r.exit_code, (r.stdout.encode(), r.stderr.encode())


class _FakeContainers:
    def __init__(self, container):
        self._c = container

    def get(self, name):
        return self._c


class _FakeClient:
    def __init__(self, container):
        self.containers = _FakeContainers(container)


class _FakeRunner:
    def __init__(self, container):
        self.client = _FakeClient(container)

    def exec_in(self, container, cmd, workdir="/work", timeout_s=30):
        # 复用容器预置的响应队列（与 exec_run 同源），保持调用序语义
        return container._responses.pop(0)


def test_run_tests_phpunit_missing_explicit_error(tmp_path):
    """vendor/bin/phpunit 不存在 → 结构化"环境错误：vendor 未安装"，禁止静默 0 收集。"""
    from test_agent.skills.builtin import build_skills
    import json as _json

    container = _FakeContainer([_FakeResult(1)])   # test -f vendor/bin/phpunit → 缺失
    runner = _FakeRunner(container)
    skills = build_skills(runner)
    out = _json.loads(skills["run_tests"].func(1, {
        "container_name": "c", "cmd": ["vendor/bin/phpunit", "TestGenR1.php"]}))
    assert out["exit"] == -1
    assert "vendor 未安装" in out["stderr"] and "composer" in out["stderr"]
    assert "环境错误" in out["stderr"], "必须走环境错误结构化口径（归因层可识别）"


def test_run_tests_phpunit_present_runs_normally(tmp_path):
    """phpunit 存在 → 照常执行主命令（前置探测不误伤）。"""
    from test_agent.skills.builtin import build_skills
    import json as _json

    container = _FakeContainer([
        _FakeResult(0),                    # test -f vendor/bin/phpunit → 在
        _FakeResult(0, "OK 3 passed", ""),
    ])
    runner = _FakeRunner(container)
    skills = build_skills(runner)
    out = _json.loads(skills["run_tests"].func(1, {
        "container_name": "c", "cmd": ["vendor/bin/phpunit", "TestGenR1.php"]}))
    assert out["exit"] == 0 and "3 passed" in out["stdout"]


# ---------- 3. exit 语义（1149：17 过 0 败 exit≠0 不得走 repair_env） ----------

def test_php_exit_for_decide_clears_zero_fail():
    assert php_exit_for_decide("php", 2, 17, 0) == 0, "phpunit warnings 非零退出 0 败=通过"
    assert php_exit_for_decide("php", 255, 17, 0) == 0
    assert php_exit_for_decide("php", 1, 17, 3) == 1, "有失败照旧非零"
    assert php_exit_for_decide("php", 1, 0, 0) == 1, "0/0 不清零（没跑成的假通过防线）"
    assert php_exit_for_decide("python", 1, 17, 0) == 1, "仅 PHP 生效"


def test_decide_php_zero_fail_exit2_is_not_env():
    """端到端判词：evaluate 先经 php_exit_for_decide 归一（17 过 0 败 exit=2 → 0），
    再进 decide——必须不再落 repair_env（1149 熔断链根）。"""
    from test_agent.graph import repair
    text = ("=========================== short test summary info ===========\n"
            "=========================== 17 passed, 0 failed, 0 skipped ===")
    exit_code = php_exit_for_decide("php", 2, 17, 0)
    assert exit_code == 0
    verdict, kind, _ = repair.decide(exit_code, text, "", [], "fp1", 1, 0.5, 0.7)
    assert verdict != "repair_env", f"0 败 exit≠0 归一后不得归 env（got {verdict}/{kind}）"
    assert verdict == "repair_gen", "通过但覆盖率未达标 → 补测（不是环境修复）"


# ---------- 4. PHP 平台闸（1148：PHP>=8.4.1 vs 镜像 8.3.36） ----------

def test_platform_gate_blocks_version_mismatch():
    text = ("php                  8.3.36   doctrine/collections requires php ([>= 8.4]) failed\n"
            "ext-ctype            8.3.36   success")
    reason = php_platform_gate_result(1, text)
    assert reason and "PHP 平台要求不满足" in reason and "failed" in reason


def test_platform_gate_passes_on_success():
    assert php_platform_gate_result(0, "all success") is None


# ---------- 5. run 节点证据保留（1148：Fatal 只在原始 stdout） ----------

def test_unified_text_keeps_raw_when_junit_missing():
    unified = "==== 0 passed, 0 failed, 0 skipped ====\n"
    raw_out = "PHP Fatal error: Uncaught RuntimeException: Composer detected issues " \
              "in your platform: Your Composer dependencies require a PHP version \">= 8.4.1\""
    out = php_unified_with_raw(unified, raw_out, "", has_junit=False)
    assert "phpunit·raw" in out and "Fatal error" in out, "junit 缺失时原始死因必须并入"


def test_unified_text_untouched_when_junit_present():
    unified = "==== 3 passed, 0 failed ====\n"
    assert php_unified_with_raw(unified, "old raw", "", has_junit=True) == unified
    assert php_unified_with_raw(unified, "", "", has_junit=False) == unified, "无原始输出不附加"


# ---------- 6. clover 分母净化（1181 实锤：vendor 稀释 TOTAL→0% 熔断） ----------

def test_clover_denominator_excludes_vendor_and_generated():
    from test_agent.skills.php_adapter import clover_to_rows
    xml = ('<report><project>'
           '<file name="/work/vendor/composer/ClassLoader.php">'
           '<metrics statements="147" coveredstatements="0"/></file>'
           '<file name="/work/TestGenR1.php"><metrics statements="30" coveredstatements="30"/></file>'
           '<file name="/work/tests/ShopTest.php"><metrics statements="3" coveredstatements="0"/></file>'
           '<file name="/work/src/Shop.php"><metrics statements="6" coveredstatements="6"/></file>'
           '<file name="/work/src/Miss.php"><metrics statements="10" coveredstatements="5"/></file>'
           '</project></report>')
    rows, total = clover_to_rows(xml)
    assert total == 69, f"(6+5)/(6+10)=68.75→69，got {total}"
    assert len(rows) == 1 and rows[0]["file"].endswith("Miss.php"),         "vendor/测试文件/生成物不得进分母与缺口行"
