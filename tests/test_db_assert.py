"""黑包阶段2·db_assert 只读闸（sqlglot）+ 结果落库幂等（docs/黑盒能力补齐任务包-20261106.md 测试矩阵）。

负例先行（护栏明文：闸门类负例优先级最高——防造假卖点的直接证据）。
sqlglot 只读闸：白名单仅 SELECT/SHOW；出现 UPDATE/DELETE/DROP 整案剔除留痕。
"""
import json

import pytest


# ---------- 只读闸（_readonly_gate 纯函数） ----------

class TestReadonlyGate:
    def test_select_allowed(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("SELECT COUNT(*) FROM orders")
        assert kept == ["SELECT COUNT(*) FROM orders"] and rejected == []

    def test_show_allowed(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("SHOW TABLES")
        assert len(kept) == 1 and rejected == []

    def test_union_of_selects_allowed(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("SELECT id FROM a UNION SELECT id FROM b")
        assert len(kept) == 1 and rejected == []

    def test_update_rejected(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("UPDATE orders SET status='x'")
        assert kept == [] and len(rejected) == 1
        assert "UPDATE" in rejected[0].upper() or "update" in rejected[0]

    def test_delete_rejected(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("DELETE FROM orders")
        assert kept == [] and len(rejected) == 1

    def test_drop_rejected(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("DROP TABLE orders")
        assert kept == [] and len(rejected) == 1

    def test_insert_rejected(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("INSERT INTO orders VALUES (1)")
        assert kept == [] and len(rejected) == 1

    def test_multi_statement_injection_rejected_whole_case(self):
        """SELECT 1; DROP TABLE orders —— 注入形态：任一语句违规 → 整案剔除（一条都不执行）。"""
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("SELECT 1; DROP TABLE orders")
        assert kept == [] and len(rejected) >= 1
        assert any("DROP" in r.upper() for r in rejected)

    def test_syntax_error_rejected(self):
        from test_agent.skills.db_assert import _readonly_gate
        kept, rejected = _readonly_gate("SELEC * FORM orders")
        assert kept == [] and len(rejected) == 1

    def test_rejection_reason_carries_statement_kind(self):
        """留痕要求：拒绝理由指明语句类型（可审计）。"""
        from test_agent.skills.db_assert import _readonly_gate
        _, rejected = _readonly_gate("ALTER TABLE orders ADD COLUMN c INT")
        assert "ALTER" in rejected[0].upper()


# ---------- db_asserts 落库幂等 ----------

def _mk_rows():
    return [{"sql_text": "SELECT 1", "verdict": "passed", "row_count": 1,
             "error": "", "elapsed_ms": 12}]


def _seed_task(conn):
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','n')",
                       (uid,)).lastrowid
    return conn.execute("INSERT INTO test_tasks(project_id,user_id,confirmed) VALUES(?,?,1)",
                        (pid, uid)).lastrowid


def test_store_db_asserts_idempotent_same_round(tmp_path):
    from test_agent.store import db
    from test_agent.skills.db_assert import store_db_asserts
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    _seed_task(conn)
    store_db_asserts(conn, 1, 1, _mk_rows(), sidecar="mta-db-1", engine="mariadb")
    store_db_asserts(conn, 1, 1, _mk_rows(), sidecar="mta-db-1", engine="mariadb")
    n = conn.execute("SELECT COUNT(*) c FROM db_asserts WHERE task_id=1").fetchone()["c"]
    assert n == 1                       # 同轮重存不重复计行（对齐 app_logs 幂等口径）


def test_store_db_asserts_rejected_rows_kept_for_audit(tmp_path):
    """越权 SQL 剔除必须留痕进表（审计列 rejected_reason 非空）。"""
    from test_agent.store import db
    from test_agent.skills.db_assert import store_db_asserts
    db.init_db(tmp_path / "t.db")
    conn = db.get_conn(tmp_path / "t.db")
    rows = [{"sql_text": "DROP TABLE orders", "verdict": "rejected",
             "row_count": None, "error": "", "elapsed_ms": 0,
             "rejected_reason": "语句类型 DROP 不在只读白名单"}]
    tid = _seed_task(conn)
    store_db_asserts(conn, tid, 1, rows, sidecar="mta-db-1", engine="mariadb")
    row = conn.execute("SELECT verdict, rejected_reason FROM db_asserts WHERE task_id=?",
                       (tid,)).fetchone()
    assert row["verdict"] == "rejected" and "DROP" in row["rejected_reason"]


# ---------- db_assert skill 注册契约 ----------

def test_db_assert_registered_in_build_skills():
    from test_agent.skills.builtin import build_skills
    skills = build_skills(runner=None)
    sk = skills.get("db_assert")
    assert sk is not None and sk.risk_level == "medium"       # 读 DB ≥ medium
    assert sk.requires_confirmation is False                  # 只读闸在 skill 内部，不占确认门
