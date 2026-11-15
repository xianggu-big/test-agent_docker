"""修复策略族（链路A/B/C）机制层确定性单测 —— fake-LLM 注入，不依赖网络/Docker。"""
import sqlite3

import pytest

from test_agent.skills.repair_strategy import build_rewrite_prompt, run_repair_strategy


def _mem_conn():
    """内存库 + 最小表结构（Row 工厂对齐生产契约）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE test_scripts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER, module_id INTEGER, framework TEXT, version INTEGER,
                    filename TEXT, content TEXT, content_hash TEXT)""")   # filename=产品库列（重跑幂等键）
    conn.execute("""CREATE TABLE test_cases (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    script_id INTEGER, source TEXT, name TEXT, method TEXT, content TEXT,
                    enabled INTEGER DEFAULT 1)""")
    return conn

R1 = '''"""合成第1轮：2好 + 2强信号坏 + 1证人"""
from cart import apply_discount, Cart


def test_apply_discount_ok():
    assert apply_discount(100, 20) == 80.0


def test_cart_total():
    c = Cart()
    c.add("a", 30)
    c.add("b", 70)
    assert c.total() == 100.0


def test_bad_import():
    from cart import discount  # ImportError

    assert discount(100, 20) == 80.0


def test_bad_attr():
    c = Cart()
    c.add("a", 10)
    c.clear()  # AttributeError
    assert c.total() == 0


def test_witness():
    assert apply_discount(100, 50) == 40.0  # 断言失败=证人
'''

BAD_JSON_OK = '{"imports": "from cart import discount", "cases": [{"name": "test_bad_import", "code": "def test_bad_import():\\n    from cart import apply_discount\\n    assert apply_discount(100, 20) == 80.0"}]}'

PAIRS = [("test_gen_r1.py::test_bad_import", "ImportError"),
         ("test_gen_r1.py::test_bad_attr", "AttributeError"),
         ("test_gen_r1.py::test_witness", "AssertionError")]


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def apply_discount(price, percent):\n"
        "    if not 0 <= percent <= 100:\n"
        "        raise ValueError('percent out of range')\n"
        "    return round(price * (1 - percent / 100), 2)\n", encoding="utf-8")
    (tmp_path / "test_gen_r1.py").write_text(R1, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def conn():
    c = _mem_conn()
    sid = c.execute("INSERT INTO test_scripts(task_id, framework, version, content) "
                    "VALUES (1, 'pytest', 1, '')").lastrowid
    for name in ("test_apply_discount_ok", "test_cart_total", "test_bad_import",
                 "test_bad_attr", "test_witness"):
        c.execute("INSERT INTO test_cases(script_id, source, name, content) VALUES (?,?,?,?)",
                  (sid, "ai", name, ""))
    return c


class TestRewrite:
    def test_重写成功保种子_证人不动_跳过全量(self, ws, conn):
        info = run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2,
                                   llm_fn=lambda p: BAD_JSON_OK)
        assert info["strategy"] == "rewrite"
        assert info["rewritten"] == ["test_bad_import"] and info["llm_calls"] == 1
        assert info["skip_full_regen"] is False  # 还有 test_bad_attr 未重写
        text = (ws / "test_rewrite_r2.py").read_text(encoding="utf-8")
        assert "def test_bad_import" in text and "apply_discount" in text  # 种子保留+引用修复
        rows = conn.execute("SELECT name, enabled FROM test_cases ORDER BY id").fetchall()
        orig_bad = [r["enabled"] for r in rows if r["name"] == "test_bad_import"]
        assert orig_bad[0] == 0                        # 原坏案剪除（记录保留）
        assert orig_bad[-1] == 1                       # 重写新案启用（同名两行：旧停+新启）
        assert [r["enabled"] for r in rows if r["name"] == "test_witness"] == [1]  # 证人不动

    def test_全部重写成功才跳过全量(self, ws, conn):
        ok_both = ('{"imports": "", "cases": ['
                   '{"name": "test_bad_import", "code": "def test_bad_import():\\n    assert True"}, '
                   '{"name": "test_bad_attr", "code": "def test_bad_attr():\\n    assert True"}]}')
        info = run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2,
                                   llm_fn=lambda p: ok_both)
        assert info["skip_full_regen"] is True  # 强信号全部重写成功

    def test_预算截断时不跳全量(self, ws, conn):
        info = run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2,
                                   llm_fn=lambda p: BAD_JSON_OK, budget=1)
        assert info["skip_full_regen"] is False       # 只重写了 1/2
        assert len(info["targets"]) == 1

    def test_LLM不可解析_兜底纯剪除(self, ws, conn):
        info = run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2,
                                   llm_fn=lambda p: "抱歉，我无法输出")
        assert info["rewritten"] == [] and info["skip_full_regen"] is False
        assert "兜底" in info.get("fallback", "")
        assert not (ws / "test_rewrite_r2.py").exists()
        flags = dict(conn.execute("SELECT name, enabled FROM test_cases").fetchall())
        assert flags["test_bad_import"] == 0          # 剪除照做

    def test_无Key降级为纯剪除(self, ws, conn):
        info = run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2, llm_fn=None)
        assert info["llm_calls"] == 0 and info["rewritten"] == []

    def test_证人永不被重写(self, ws, conn):
        seen = {}

        def spy(prompt):
            seen["p"] = prompt
            return BAD_JSON_OK

        run_repair_strategy("rewrite", ws, PAIRS, 1, conn, 2, llm_fn=spy)
        assert "test_witness" not in seen["p"]        # 证人不在重写上下文里


class TestDispatch:
    def test_prune基线_无LLM调用_不跳全量(self, ws, conn):
        info = run_repair_strategy("prune", ws, PAIRS, 1, conn, 2)
        assert info["strategy"] == "prune" and info["llm_calls"] == 0
        assert info["skip_full_regen"] is False
        assert len(info["pruned"]) == 2 and len(info["kept"]) == 1

    def test_hybrid_重写照做但全量照跑(self, ws, conn):
        info = run_repair_strategy("hybrid", ws, PAIRS, 1, conn, 2,
                                   llm_fn=lambda p: BAD_JSON_OK)
        assert info["strategy"] == "hybrid"
        assert info["skip_full_regen"] is False       # C 的结合点：重写+全量补测并行
        assert info["rewritten"] == ["test_bad_import"]


class TestPrompt:
    def test_重写提示词含失败上下文与预期纪律(self):
        from test_agent.skills.repair_strategy import REWRITE_SYSTEM
        p = build_rewrite_prompt([("cart.py", "def f(): pass")], "需求",
                                 [{"name": "test_x", "err": "ImportError", "code": "def test_x(): pass"}],
                                 "FAILED test_x - ImportError")
        assert "修复" in p and "ImportError" in p
        assert "不得改为迁就实际输出" in REWRITE_SYSTEM  # 预期先于执行的纪律在 system 段
