# -*- coding: utf-8 -*-
"""三语言变异报告解析统一 schema（任务卡-F 能力补齐，2026-11-12）。

统一口径与 go_gremlins.py 同构（{killed, survived, timeout, total, kill_rate,
score, survivors}）；判据=变异报告（工具权威判定），attribution 三分类不新造。
钉版本：StrykerJS@8.5.1 / pitest-maven（demo pom 钉）/ Infection 0.29.14。
"""
import json

from test_agent.skills.mutation_parse import (parse_infection_json,
                                              parse_pit_xml,
                                              parse_stryker_json)


def _w(tmp_path, name, text) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_stryker_全击杀(tmp_path):
    p = _w(tmp_path, "s.json", json.dumps({
        "files": {"calc.js": {"mutants": [
            {"mutatorName": "ArithmeticOperator", "status": "Killed"},
            {"mutatorName": "StringLiteral", "status": "Killed"}]}}}))
    r = parse_stryker_json(p)
    assert r["killed"] == 2 and r["survived"] == 0
    assert r["kill_rate"] == 1.0 and r["score"] == 100.0


def test_stryker_存活变异留痕(tmp_path):
    p = _w(tmp_path, "s2.json", json.dumps({
        "files": {"calc.js": {"mutants": [
            {"mutatorName": "ArithmeticOperator", "status": "Survived",
             "location": {"start": {"line": 12}}},
            {"mutatorName": "ConditionalOperator", "status": "Killed"},
            {"mutatorName": "Block", "status": "Timeout"}]}}}))
    r = parse_stryker_json(p)
    assert r["survived"] == 1 and r["timeout"] == 1 and r["killed"] == 1
    assert r["survivors"][0]["mutator"] == "ArithmeticOperator"
    assert ":12" in r["survivors"][0]["location"]


def test_pit_xml状态解析(tmp_path):
    xml = """<mutations>
  <mutation status='KILLED' mutator='Math'><sourceFile>Calc.java</sourceFile><lineNumber>10</lineNumber></mutation>
  <mutation status='SURVIVED' mutator='Math'><sourceFile>Calc.java</sourceFile><lineNumber>22</lineNumber></mutation>
  <mutation status='TIMED_OUT' mutator='Math'><sourceFile>Calc.java</sourceFile><lineNumber>30</lineNumber></mutation>
</mutations>"""
    r = parse_pit_xml(_w(tmp_path, "pit.xml", xml))
    assert r["killed"] == 1 and r["survived"] == 1 and r["timeout"] == 1
    assert r["survivors"][0]["location"] == "Calc.java:22"


def test_pit_xml_部分属性缺省容错(tmp_path):
    # 717 真机实锤：mutations.xml 里部分 mutation 无 sourceFile/lineNumber 子元素
    xml = """<mutations>
  <mutation status='KILLED' mutator='Math'><sourceFile>Calc.java</sourceFile><lineNumber>10</lineNumber></mutation>
  <mutation status='SURVIVED' mutator='Math'><description>no source children</description></mutation>
  <mutation status='TIMED_OUT' mutator='Math'><description>no source children</description></mutation>
</mutations>"""
    r = parse_pit_xml(_w(tmp_path, "pit2.xml", xml))
    assert r["total"] == 3 and r["killed"] == 1
    assert "None" not in r["survivors"][0]["location"]


def test_infection_escaped同义survived(tmp_path):
    data = {"stats": {"killed": 5, "escaped": 2, "errors": 1,
                      "total": 8, "mutationScoreIndicator": 75.0},
            "escaped_mutants": [{"mutator": "Integer-Addition",
                                 "originalFilePath": "/work/src/Shop.php",
                                 "originalStartingLine": 33}]}
    r = parse_infection_json(_w(tmp_path, "inf.json", json.dumps(data)))
    assert r["killed"] == 5 and r["survived"] == 2 and r["timeout"] == 1
    assert r["score"] == 75.0
    assert r["survivors"][0]["mutator"] == "Integer-Addition"


def test_文件缺失诚实降级(tmp_path):
    for fn in (parse_stryker_json, parse_pit_xml, parse_infection_json):
        r = fn(str(tmp_path / "不存在的变异报告_xyz"))
        assert r["total"] == 0 and r["kill_rate"] is None


def test_损坏文件诚实降级(tmp_path):
    b1 = _w(tmp_path, "bad1.json", "{broken")
    assert parse_stryker_json(b1)["total"] == 0
    b2 = _w(tmp_path, "bad2.xml", "<mutations><unclosed>")
    assert parse_pit_xml(b2)["total"] == 0


# ---------- 段 1.1：jest coverageMap → 统一层桥接（c8/istanbul 通道） ----------

def test_js_adapter_统一层桥接(tmp_path):
    # jest --coverage 的 coverageMap（istanbul 形态）经 from_istanbul 出统一 schema；
    # js_adapter 文本渲染主链路不变（双解析互证）
    from test_agent.skills import js_adapter
    cm = {"src/calc.js": {
        "path": "src/calc.js",
        "statementMap": {"0": {"start": {"line": 1}, "end": {"line": 1}}},
        "s": {"0": 1},
        "fnMap": {}, "f": {}, "branchMap": {}, "b": {}}}
    rows, tot, miss = js_adapter._coverage_rows(cm)
    assert tot >= 1
    unified = getattr(js_adapter, "LAST_UNIFIED_COVERAGE", None)
    assert unified and any(
        f["file"].endswith("src/calc.js") for f in unified["files"])
    # 真机 schema 字段（f2_stage0/js 覆盖统一层实测）：statements/missing_lines
    assert all("statements" in f and "missing_lines" in f for f in unified["files"])


def test_infection_空报告kill_rate为None非零(tmp_path):
    """统一 schema 口径：_unified 在 total=0 时 kill_rate/score=None（诚实"没跑"）；
    infection 分支旧返回 0.0/0（=冒充"零杀伤"），跨工具聚合时系统性拉低均值。"""
    data = {"stats": {}}
    r = parse_infection_json(_w(tmp_path, "inf.json", json.dumps(data)))
    assert r["kill_rate"] is None and r["score"] is None, "total=0 必须 None（与 _unified 同口径）"


def test_stryker_根为数组的报告诚实降级(tmp_path):
    """工具版本差异可能输出根节点非 dict 的 JSON——旧实现 data.get 抛 AttributeError
    逃出 except（except 只包 read/loads）。"""
    p = tmp_path / "stryker_list.json"
    p.write_text(json.dumps(["mutant1", "mutant2"]), encoding="utf-8")
    r = parse_stryker_json(p)
    assert r["total"] == 0 and r["kill_rate"] is None


def test_js_adapter_istanbul键集错位不出负覆盖率():
    """istanbul 版本差异 s 与 statementMap 键集不齐时，miss（无 smap 过滤）可大于
    n（=len(smap)）→ 渲染出 -100% 这类负覆盖率。miss 必须与 miss_lines 同口径。"""
    from test_agent.skills.js_adapter import _coverage_rows
    rows, n, miss = _coverage_rows({'/work/a.js': {
        'statementMap': {'1': {'start': {'line': 3}}}, 's': {'0': 0, '1': 0}}})
    text = "\n".join(rows)
    assert "-" not in text.split("%")[0].split()[-1], f"负覆盖率渲染: {text}"
    assert miss <= n
