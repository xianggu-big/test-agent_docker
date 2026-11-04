"""多语言基准池 manifest 校验与体检卡（11.4 恢复计划 §4 / Task H 骨架，确定性零 LLM）。

选型原则（计划 §4）：
- A 轨=挂靠公开真值（Defects4J 854 Java bugs / BugsInPy 493 Python bugs），必须给
  buggy_ref/fixed_ref——差分闸的对照来源；
- B 轨=真实项目冒烟无真值（requests/lodash/gson...），失败归因走归因三闸。

诚实边界：本模块只做 schema 校验与体检卡组装；网络拉取与构建/官方测试由
scripts/fetch_baseline.py 与 scripts/env_checkup.py 执行，失败如实 blocked，
不伪造"体检通过"。
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_BASE = ("id", "language", "track", "repo",
                 "official_test_cmd", "truth_source")
SUPPORTED_LANGS = {"python", "javascript", "typescript", "java", "go", "rust", "c", "cpp"}
SUPPORTED_TRACKS = {"A", "B"}


def validate_manifest(m: dict) -> list[str]:
    """返回错误列表；空列表=合法。A 轨必须给 buggy_ref/fixed_ref（差分闸对照）。"""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest 必须是 JSON 对象"]
    for k in REQUIRED_BASE:
        if not str(m.get(k) or "").strip():
            errs.append(f"缺少必填字段: {k}")
    lang = str(m.get("language") or "")
    if lang and lang not in SUPPORTED_LANGS:
        errs.append(f"不支持的语言: {lang}（支持 {sorted(SUPPORTED_LANGS)}）")
    track = str(m.get("track") or "")
    if track and track not in SUPPORTED_TRACKS:
        errs.append(f"track 必须是 A/B，得到: {track}")
    if track == "A":
        for k in ("buggy_ref", "fixed_ref"):
            if not str(m.get(k) or "").strip():
                errs.append(f"A 轨（有真值）必须给 {k}——差分闸的对照来源")
        if (m.get("buggy_ref") and m.get("fixed_ref")
                and m["buggy_ref"] == m["fixed_ref"]):
            errs.append("buggy_ref 与 fixed_ref 不得相同（差分无意义）")
    return errs


def load_manifest(path: Path) -> tuple[dict | None, list[str]]:
    try:
        m = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"manifest 不存在: {path}"]
    except json.JSONDecodeError as e:
        return None, [f"manifest JSON 解析失败: {e}"]
    return m, validate_manifest(m)


def health_card(m: dict, *, checkout: str | None = None,
                build_ok: bool | None = None, tests_ok: bool | None = None,
                duration_s: float | None = None, notes: str = "") -> dict:
    """环境体检卡（计划 §4：体检不过的项目禁止进入 agent 实验）。

    build_ok/tests_ok 全为 None → status=planned（还没体检过，不是通过）。
    """
    executed = build_ok is not None or tests_ok is not None
    if not executed:
        status = "planned"
    elif build_ok and tests_ok:
        status = "ok"
    else:
        status = "failed"
    return {
        "id": m.get("id"), "language": m.get("language"), "track": m.get("track"),
        "version_anchor": m.get("version_anchor"), "checkout": checkout,
        "build_ok": build_ok, "tests_ok": tests_ok,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "status": status, "notes": notes,
    }
