"""②定向修复（prune_failing_cases）与静态闸（_static_filter）单测 —— 不依赖 LLM/Docker。"""
import ast
import sqlite3

from test_agent.skills.gen_tests import _static_filter, prune_failing_cases

GEN_FILE = '''"""AI 生成用例（第 1 轮）"""


def test_ok_one():
    assert True


def test_bad_import():
    from calc import missing_func

    assert missing_func(1) == 2


def test_witness():
    from calc import add

    assert add(1, 2) == 999
'''


def _mem_conn():
    """内存库 + 最小表结构（FK 不开启；Row 工厂对齐生产契约）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE test_scripts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER, module_id INTEGER, framework TEXT, version INTEGER,
                    content TEXT, content_hash TEXT)""")
    conn.execute("""CREATE TABLE test_cases (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    script_id INTEGER, source TEXT, name TEXT, method TEXT, content TEXT,
                    enabled INTEGER DEFAULT 1)""")
    return conn


def _mk_script(conn, tid=1, version=1):
    return conn.execute(
        "INSERT INTO test_scripts(task_id, framework, version, content) VALUES (?, 'pytest', ?, '')",
        (tid, version)).lastrowid


# ---------- prune_failing_cases ----------

class TestPrune:
    def test_坏用例剪除断言证人保留(self, tmp_path):
        (tmp_path / "test_gen_r1.py").write_text(GEN_FILE, encoding="utf-8")
        conn = _mem_conn()
        sid = _mk_script(conn)
        for name in ("test_ok_one", "test_bad_import", "test_witness"):
            conn.execute("INSERT INTO test_cases(script_id, source, name, content) VALUES (?,?,?,?)",
                         (sid, "ai", name, ""))

        info = prune_failing_cases(
            tmp_path,
            [("test_gen_r1.py::test_bad_import", "ImportError"),
             ("test_gen_r1.py::test_witness", "AssertionError")],
            1, conn)

        assert [p["nodeid"] for p in info["pruned"]] == ["test_gen_r1.py::test_bad_import"]
        kept_ids = [k["nodeid"] for k in info["kept"]]
        assert "test_gen_r1.py::test_witness" in kept_ids  # 证人绝不剪
        new_tree = ast.parse((tmp_path / "test_gen_r1.py").read_text(encoding="utf-8"))
        funcs = {n.name for n in new_tree.body if isinstance(n, ast.FunctionDef)}
        assert funcs == {"test_ok_one", "test_witness"}
        flags = dict(conn.execute("SELECT name, enabled FROM test_cases").fetchall())
        assert flags["test_bad_import"] == 0 and flags["test_witness"] == 1  # 记录保留仅停用

    def test_imported文件以库为准重建(self, tmp_path):
        conn = _mem_conn()
        sid = _mk_script(conn)
        conn.execute("INSERT INTO test_cases(script_id, source, name, content) VALUES (?,?,?,?)",
                     (sid, "imported", "test_bad2", "def test_bad2():\n    assert False"))
        conn.execute("INSERT INTO test_cases(script_id, source, name, content) VALUES (?,?,?,?)",
                     (sid, "imported", "test_good", "def test_good():\n    assert True"))

        prune_failing_cases(tmp_path, [("test_imported.py::test_bad2", "ImportError")], 1, conn)

        text = (tmp_path / "test_imported.py").read_text(encoding="utf-8")
        assert "test_bad2" not in text and "test_good" in text

    def test_仓库自带测试永不剪(self, tmp_path):
        f = tmp_path / "test_calc.py"
        f.write_text("def test_repo():\n    assert False\n", encoding="utf-8")
        conn = _mem_conn()
        info = prune_failing_cases(tmp_path, [("test_calc.py::test_repo", "AssertionError")], 1, conn)
        assert info["pruned"] == [] and f.exists()

    def test_未知错误类保守不剪(self, tmp_path):
        (tmp_path / "test_gen_r1.py").write_text(GEN_FILE, encoding="utf-8")
        conn = _mem_conn()
        info = prune_failing_cases(tmp_path, [("test_gen_r1.py::test_ok_one", "")], 1, conn)
        assert info["pruned"] == []


# ---------- _static_filter ----------

class TestStaticFilter:
    def _ws(self, tmp_path):
        (tmp_path / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return tmp_path

    def test_合法用例通过(self, tmp_path):
        ws = self._ws(tmp_path)
        cases = [{"name": "t1", "code": "from calc import add\n\ndef test_t1():\n    assert add(1, 2) == 3\n"}]
        kept, rej = _static_filter(cases, ws)
        assert len(kept) == 1 and rej == []

    def test_引用不存在的符号被剔(self, tmp_path):
        ws = self._ws(tmp_path)
        cases = [{"name": "t1", "code": "from calc import nope\n\ndef test_t1():\n    nope()\n"}]
        kept, rej = _static_filter(cases, ws)
        assert kept == [] and "nope" in rej[0]

    def test_语法错误被剔(self, tmp_path):
        ws = self._ws(tmp_path)
        cases = [{"name": "t1", "code": "def test_t1(:\n    pass"}]
        kept, rej = _static_filter(cases, ws)
        assert kept == [] and "语法错误" in rej[0]


# ---------- _parse_per_file_coverage（B2 缺口回灌数据源） ----------

class TestPerFileCoverage:
    OUT = """Name          Stmts   Miss  Cover   Missing
-------------------------------------------
calc.py           8      2    75%   12-13, 17
test_calc.py     10      0   100%
TOTAL             18      2    89%   12-13, 17
9 passed in 0.05s"""

    def test_只留有缺口的数据行(self):
        from test_agent.graph.nodes import _parse_per_file_coverage
        rows = _parse_per_file_coverage(self.OUT)
        assert rows == [{"file": "calc.py", "stmts": 8, "miss": 2,
                         "cover": 75, "missing": "12-13, 17"}]

    def test_全量覆盖时为空_不算异常(self):
        from test_agent.graph.nodes import _parse_per_file_coverage
        assert _parse_per_file_coverage("TOTAL 8 0 100%\n2 passed") == []


# ---------- parse_conditions（plan/RTM 数据源）与提示词段落 ----------

class TestParseConditions:
    GOOD = '{"conditions": [{"requirement_ref": "VIP 享 8 折", "condition": "VIP 客户折扣后价格为原价 80%", "priority": "高", "method_hint": "等价类划分"}, {}]}'

    def test_解析并剔除空条件(self):
        from test_agent.graph.nodes import parse_conditions
        conds = parse_conditions(self.GOOD)
        assert conds and len(conds) == 1 and conds[0]["priority"] == "高"

    def test_不可解析返回None_诚实降级(self):
        from test_agent.graph.nodes import parse_conditions
        assert parse_conditions("抱歉，我无法输出 JSON") is None
        assert parse_conditions('{"cases": []}') is None


class TestPromptSections:
    def test_条件段落存在(self):
        from test_agent.skills.prompt_library import build_user_prompt
        p = build_user_prompt([("a.py", "def f(): pass")], "需求",
                              conditions=[{"condition": "VIP 打八折", "priority": "高", "method_hint": "等价类划分"}])
        assert "测试条件（RTM" in p and "VIP 打八折" in p

    def test_缺口段落存在(self):
        from test_agent.skills.prompt_library import build_user_prompt
        p = build_user_prompt([("a.py", "def f(): pass")], "需求",
                              coverage_gaps=[{"file": "a.py", "missing": "12-13", "cover": 75}],
                              coverage_target=0.7)
        assert "上一轮覆盖缺口" in p and "12-13" in p and "70%" in p

    def test_无条件无缺口时两段落都不出现(self):
        from test_agent.skills.prompt_library import build_user_prompt
        p = build_user_prompt([("a.py", "def f(): pass")], "需求")
        assert "测试条件" not in p and "上一轮覆盖缺口" not in p

    def test_plan提示词含保守语义纪律(self):
        from test_agent.skills.prompt_library import build_plan_prompt
        assert "不要发明" in build_plan_prompt("VIP 享 8 折")
