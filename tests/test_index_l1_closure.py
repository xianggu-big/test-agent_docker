# -*- coding: utf-8 -*-
"""L1 依赖闭包测试锁（2026-11-18 设计方案-L1L2L3辅助急救层 §1.3，8 例）。

覆盖：出边/入边/2 跳链/None 降级/top5 防爆/_root 排除/AST 声明锁/analyze 集成。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent import config
from test_agent.skills.project_index import build_index, dependency_closure


def _write(tmp_path: Path, rel: str, text: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def ws_out_in(tmp_path):
    """出边+入边双料项目：user→order（user 调 order）与 report→user（report 调 user）。"""
    _write(tmp_path, "user/service.py",
           "from order.repo import create_order\n\n\n"
           "def login(username):\n    return True\n\n\n"
           "def place(username):\n    return create_order(username)\n")
    _write(tmp_path, "order/repo.py",
           "def create_order(item):\n    return {'id': 1}\n")
    _write(tmp_path, "report/api.py",
           "from user.service import login\n\n\n"
           "def show(u):\n    return login(u)\n")
    return tmp_path


def test_l1_1_out_edge_closure(ws_out_in):
    """L1-1 出边：user 调用的 order 进闭包。"""
    idx = build_index(ws_out_in, ["python"])
    cl = dependency_closure(idx, "user", hops=1, callgraph=idx.get("callgraph"))
    assert cl[0] == "user" and "order" in cl, f"出边邻居: {cl}"


def test_l1_2_in_edge_closure(ws_out_in):
    """L1-2 入边：调用 user 的 report 也进闭包。"""
    idx = build_index(ws_out_in, ["python"])
    cl = dependency_closure(idx, "user", hops=1, callgraph=idx.get("callgraph"))
    assert "report" in cl, f"入边邻居也算: {cl}"


def test_l1_3_two_hops(tmp_path):
    """L1-3 hops=2：A→B→C 链上 C 只在 hops=2 出现。"""
    _write(tmp_path, "a/x.py",
           "from b.y import f1\n\n\ndef fa():\n    return f1()\n")
    _write(tmp_path, "b/y.py",
           "from c.z import f2\n\n\ndef f1():\n    return f2()\n")
    _write(tmp_path, "c/z.py", "def f2():\n    return 3\n")
    idx = build_index(tmp_path, ["python"])
    cg = idx.get("callgraph") or {}
    one = dependency_closure(idx, "a", hops=1, callgraph=cg)
    two = dependency_closure(idx, "a", hops=2, callgraph=cg)
    assert one == ["a", "b"], f"hops=1 不含 C: {one}"
    assert "c" in two and "b" in two, f"hops=2 含 C: {two}"


def test_l1_4_none_callgraph_degrades(ws_out_in):
    """L1-4 callgraph=None：只返回 [module]（降级=主方法行为）。"""
    idx = build_index(ws_out_in, ["python"])
    assert dependency_closure(idx, "user", hops=1, callgraph=None) == ["user"]
    # 未知名模块同样降级不炸
    assert dependency_closure(idx, "ghost", hops=1,
                              callgraph=idx.get("callgraph")) == ["ghost"]


def test_l1_5_top5_by_loc(tmp_path):
    """L1-5 邻居 >5：按 LOC 降序取 top5，其余记 detail['skipped']。"""
    _write(tmp_path, "core/lib.py", "def core_fn():\n    return 1\n")
    for i in range(1, 6):   # n1..n5 大文件（LOC 高，进 top5）
        body = "\n".join(f"    x{i} = {i}" for i in range(1, 12))
        _write(tmp_path, f"n{i}/m.py",
               f"from core.lib import core_fn\n\n\ndef f{i}():\n"
               f"{body}\n    return core_fn()\n")
    for j in (6, 7):        # n6/n7 小文件（LOC 低，被挤出）
        _write(tmp_path, f"n{j}/m.py",
               f"from core.lib import core_fn\n\n\ndef g{j}():\n    return core_fn()\n")
    idx = build_index(tmp_path, ["python"])
    detail: dict = {}
    cl = dependency_closure(idx, "core", hops=1, callgraph=idx.get("callgraph"),
                            detail=detail)
    assert len(cl) == 6 and cl[0] == "core", f"top5 封顶: {cl}"
    assert set(detail["skipped"]) == {"n6", "n7"}, f"skipped 留痕: {detail}"


def test_l1_6_root_excluded(ws_out_in):
    """L1-6 _root 不进闭包：根级散文件被调用也不把 _root 收入。"""
    _write(ws_out_in, "main.py",
           "from user.service import login\n\n\ndef boot():\n    return login('x')\n")
    idx = build_index(ws_out_in, ["python"])
    cl = dependency_closure(idx, "user", hops=1, callgraph=idx.get("callgraph"))
    assert "_root" not in cl, f"_root 必须排除: {cl}"


def test_l1_7_ast_declaration_locks():
    """L1-7 AST 声明锁：state 键声明+analyze 接线+gen_tests 透传+目录段匹配。"""
    root = Path(__file__).resolve().parents[1]
    state_src = (root / "test_agent" / "graph" / "state.py").read_text(encoding="utf-8")
    assert "index_closure" in state_src, "state 必须声明 index_closure（366 教训）"
    assert "index_l2_rescued" in state_src
    nodes_src = (root / "test_agent" / "graph" / "nodes.py").read_text(encoding="utf-8")
    assert "dependency_closure" in nodes_src, "analyze 必须接 L1 闭包"
    gen_src = (root / "test_agent" / "skills" / "gen_tests.py").read_text(encoding="utf-8")
    assert 'args.get("index_scope_paths")' in gen_src, "generate 必须透传 scope_paths"
    assert 'rel.startswith(sp + "/")' in gen_src, "scope 匹配必须按目录段边界（防 user 吞 users）"


def test_l1_8_analyze_wires_closure(tmp_path, monkeypatch):
    """L1-8 集成：真 analyze 节点命中后 scope_paths=闭包模块数（tmp 库驱动）。"""
    from test_agent.graph.builder import GraphDeps
    from test_agent.graph.nodes import make_nodes
    from test_agent.store import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = db.get_conn(tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    uid = c.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = c.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                    (uid,)).lastrowid
    tid = c.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                    "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ses = c.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid

    ws = tmp_path / "ws"
    ws.mkdir()
    _write(ws, "user/service.py",
           "from order.repo import create_order\n\n\n"
           "def place(u):\n    return create_order(u)\n")
    _write(ws, "order/repo.py", "def create_order(i):\n    return {'id': 1}\n")
    c.commit()

    nodes = make_nodes(__import__("types").SimpleNamespace(
        conn=c, runner=None, registry=None, skills={}))
    out = nodes["analyze"]({"task_id": tid, "session_id": ses, "workspace": str(ws),
                            "requirement": "测试 order 模块的边界"})
    closure = out.get("index_closure") or []
    assert closure and closure[0] == "order", f"命中 order: {closure}"
    assert (out.get("index_scope_paths") or []) and \
        len(out["index_scope_paths"]) == len(closure), "scope_paths=闭包模块数"
    assert "user" in closure, "L1 闭包应含被调用方 user"
    c.close()
