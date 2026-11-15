"""第五轮修复锁（2026-11-15 闲时找bug 第五轮，R18 sandbox 层 + R19 db 迁移层）。

实锤五处：
1. JS 服务启动命令注入：package.json main 文件名未过 P2-6 白名单直接拼进 sh -c
  （Python 版 _start_service 有同款校验，JS 版漏）——被测仓库可注入命令；
2. copy_dir_into 的 tar 只打包文件条目——空目录（logs/、fixture 目录）进容器后
   静默丢失，应用/测试因目录不存在失败且无留痕；
3. ui_assert 容器回传 tar 裸 extractall 无 filter（同仓 _extract_tarball_strip1
   有手写穿越拒绝——两套口径并存，Python<3.14 无默认 data filter）；
4. db._migrate 的 12 步法重建（DROP TABLE）连带删掉该表全部索引且同轮不补——
   老库首启触发重建的整个服务会话无索引（:memory: 实验实锤）；
5. llm_profile.current() 只认旧单 profile 格式——apply()（设置页）写的是
   {default,profiles,routing} 文档格式：重启后 apply_saved_if_any 静默失效、
   effective_key 解析出 deepseek key 劫持 local/qwen 档（:memory: 实验实锤）。
"""
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from test_agent import config


# ---------- 1. JS 入口注入 ----------

def test_detect_js_entry_rejects_shell_metachar_main(tmp_path):
    from test_agent.graph.l2 import _detect_js_entry
    (tmp_path / "package.json").write_text(
        json.dumps({"main": "a;touch pwn;.js"}), encoding="utf-8")
    (tmp_path / "a;touch pwn;.js").write_text("console.log(1)", encoding="utf-8")
    assert _detect_js_entry(tmp_path) is None, "main 含 shell 元字符必须拒绝（P2-6 同款）"


def test_detect_js_entry_accepts_normal_main(tmp_path):
    from test_agent.graph.l2 import _detect_js_entry
    (tmp_path / "package.json").write_text(
        json.dumps({"main": "src/server.js"}), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text("require('http')", encoding="utf-8")
    assert _detect_js_entry(tmp_path) == "src/server.js"


def test_start_service_js_fail_closed_on_bad_entry():
    from test_agent.graph.l2 import _start_service_js
    calls = []

    class _R:
        def exec_in(self, *a, **kw):
            calls.append(a)

    ok, why = _start_service_js(_R(), object(), "a;rm -rf /tmp/x;.js", 8080)
    assert ok is False and "非法" in why
    assert calls == [], "fail-closed：非法入口不得有任何 exec"


# ---------- 2. 快照保留空目录 ----------

def test_copy_dir_into_preserves_empty_dirs(tmp_path):
    from test_agent.sandbox.runner import SandboxRunner
    src = tmp_path / "proj"
    (src / "logs").mkdir(parents=True)
    (src / "data" / "nested").mkdir(parents=True)          # 空目录树
    (src / "app.py").write_text("print('hi')", encoding="utf-8")
    captured = {}

    class _C:
        def put_archive(self, dest, fh):
            captured["dest"] = dest
            captured["tar"] = fh.read()

        def exec_run(self, *a, **kw):
            return type("R", (), {"exit_code": 0})()

    runner = object.__new__(SandboxRunner)                  # 不连 Docker
    SandboxRunner.copy_dir_into(runner, _C(), src, "/work")
    with tarfile.open(fileobj=io.BytesIO(captured["tar"])) as tf:
        names = {m.name for m in tf.getmembers()}
        mtypes = {m.name: m.type for m in tf.getmembers()}
    assert "logs" in names and "data/nested" in names, "空目录必须保留为目录条目"
    assert tarfile.DIRTYPE in mtypes.values()
    assert "app.py" in names


# ---------- 3. ui_assert extractall filter（结构锁） ----------

def test_ui_assert_extractall_uses_data_filter():
    src = (Path(config.__file__).parent / "skills" / "ui_assert.py").read_text("utf-8")
    assert '.extractall(' in src
    assert 'filter="data"' in src, "容器回传 tar 必须用 data filter（穿越拒绝）"


# ---------- 4. 重建后索引补回 ----------

def test_migrate_rebuild_keeps_indexes(tmp_path, monkeypatch):
    """12 步法重建（DROP TABLE）连带删索引且同轮 executescript 已跑过——同会话
    无索引。锁：重建后 _migrate 内必须补回（老库首启场景）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    dbp = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", dbp)
    from test_agent.store import db
    db.init_db(dbp)
    c = db.get_conn(dbp)
    monkeypatch.setattr(db, "_tasks_status_check_outdated", lambda conn: True)
    db._migrate(c)                     # 强制走重建路径（老库首启等价形态）
    names = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='test_tasks'")}
    c.close()
    assert {"idx_tasks_status", "idx_tasks_project"} <= names,         "重建删除的索引必须在 _migrate 内补回（DROP TABLE 连带删索引）"


# ---------- 5. current() 读得懂文档格式 ----------

def test_current_resolves_doc_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from test_agent.store import db
    from test_agent.skills import llm_profile
    db.init_db()
    c = db.get_conn()
    llm_profile.apply({"provider": "local"}, c, "tester")
    cur = llm_profile.current(c)
    assert cur.get("provider") == "local", "设置页保存的 local 档重启后必须能解析回"


def test_effective_key_uses_default_profile_key_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key")
    from test_agent.store import db
    from test_agent.skills import llm_profile
    db.init_db()
    c = db.get_conn()
    llm_profile.apply({"provider": "qwen", "api_key": "sk-qwen-key",
                       "model": "qwen-max", "base_url": "https://b"}, c, "tester")
    from test_agent.skills.llm_profile import effective_key
    assert effective_key() == "sk-qwen-key", "key 解析必须跟 default 档的 key_env（旧实现恒 deepseek）"
