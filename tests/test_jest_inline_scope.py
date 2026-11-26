"""jest 内联配置注入判定（2026-11-17 multi60 卡 2 补充，1291 nestjs testMatch 盲区）。

三档收口为纯函数 _js_needs_inline：①无配置 → TS/散装注入（853 先例）②有配置但
testMatch/roots/rootDir 限定扫描范围 → 注入覆盖（nestjs 根目录生成物永远 No tests
found 的根治）③有配置不限定 → 项目自己的配置（零行为变化）。
"""
import json
from pathlib import Path

from test_agent.graph.nodes import _jest_config_restricts_scan, _js_needs_inline


def _mk(ws: Path, rel: str, content: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_nestjs_testmatch_src_restricts(tmp_path):
    ws = tmp_path
    _mk(ws, "jest.config.js",
        "module.exports = { rootDir: 'src', testMatch: ['**/*.spec.ts'] };")
    assert _jest_config_restricts_scan(ws) is True
    assert _js_needs_inline(ws, "typescript") is True


def test_package_json_jest_testmatch_restricts(tmp_path):
    ws = tmp_path
    _mk(ws, "package.json", json.dumps({
        "name": "app", "jest": {"roots": ["<rootDir>/src"], "testMatch": ["**/?(*.)spec.ts"]}}))
    assert _jest_config_restricts_scan(ws) is True


def test_rootdir_dot_does_not_restrict(tmp_path):
    ws = tmp_path
    _mk(ws, "jest.config.js", "module.exports = { rootDir: '.', preset: 'ts-jest' };")
    assert _jest_config_restricts_scan(ws) is False


def test_transform_only_config_no_restrict(tmp_path):
    ws = tmp_path
    _mk(ws, "jest.config.js",
        "module.exports = { preset: 'ts-jest', testEnvironment: 'node' };")
    assert _jest_config_restricts_scan(ws) is False
    assert _js_needs_inline(ws, "typescript") is False, "有配置不限定 → 走项目配置"


def test_word_boundary_guard(tmp_path):
    """scripts 值含 'roots' 子串不得误判（_pkg_declares_jest 的子串教训同源）。"""
    ws = tmp_path
    _mk(ws, "package.json", json.dumps({
        "name": "app", "scripts": {"build": "node tools/carrots.js"}}))
    assert _jest_config_restricts_scan(ws) is False
    assert _js_needs_inline(ws, "javascript") is False, "有 package.json 无 jest 配置的 JS 项目走默认"


def test_no_config_defaults(tmp_path):
    ws = tmp_path
    _mk(ws, "index.ts", "export {}")
    assert _js_needs_inline(ws, "typescript") is True      # 853/白包阶段1 先例
    _mk(ws, "package.json", json.dumps({"name": "x"}))
    assert _js_needs_inline(ws, "javascript") is False     # 有 package.json 的散装 JS 走默认


def test_rootdir_subdir_value_restricts(tmp_path):
    ws = tmp_path
    _mk(ws, "jest.config.js", "module.exports = { rootDir: 'app', };")
    assert _jest_config_restricts_scan(ws) is True


def test_ts_config_nonrestrict_needs_inline(tmp_path):
    """1968 实锤（2026-11-26 容器复现）：jest.config.ts（Nx 异步工厂形态）裸 jest
    必崩 readConf（index.js:147）——容器全局 jest 无 ts-node，任何 .ts 配置文件都
    加载不了；项目自带 ts-node 也救不了（Nx 工厂在裸 jest 上下文跑不起来）。
    修复=非限定 .ts 配置也注入内联（"宁可误注入"既有纪律；1664 实锤内联形态对
    TS 有效）。.js/.mjs/.cjs/.json 的非限定配置不受影响（可被全局 jest 加载）。"""
    ws = tmp_path
    _mk(ws, "jest.config.ts",
        "export default async (): Promise<Config> => ({ projects: await getJestProjectsAsync() });")
    _mk(ws, "package.json", json.dumps({"name": "nx-app", "devDependencies": {"@nx/jest": "*"}}))
    assert _js_needs_inline(ws, "typescript") is True


def test_mjs_config_nonrestrict_still_project(tmp_path):
    """可加载形态（.mjs）的非限定配置保持走项目配置（零行为变化档不收窄）。"""
    ws = tmp_path
    _mk(ws, "jest.config.mjs", "export default { testEnvironment: 'node' };")
    _mk(ws, "package.json", json.dumps({"name": "x"}))
    assert _js_needs_inline(ws, "javascript") is False
