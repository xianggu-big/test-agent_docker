"""State 声明锁（2026-11-15 闲时找bug 第一轮，R4 口径维度）。

LangGraph 会静默过滤 State TypedDict 未声明的键——本仓历史实锤两起
（static_only / two_stage_rebuild 上线以来 0 次触发=静默死代码）。
本轮 R4 扫描又证实五处同族断链（详见 docs/闲时找bug报告-20261115.md 第一轮分诊表）：

  TaskState.job_id              provision return → report 节点收口 container_jobs（id=0 永不命中）
  TaskState.module_id           analyze/init 注入 → container_jobs/test_scripts/test_reports 外键断
  TaskState.complexity_hotspots analyze 产出 → plan 提示词热点倾斜从未生效
  TaskState.auto_prepare        三个入口 init 注入 → 任务级"自动准备"勾选静默失效
  TaskState.last_cmd            run return → MTA_FLAKY_RERUNS flaky 连跑闸恒不触发
  L2State.last_stderr           L2 run return → evaluate raw 尾巴丢 stderr 证据（407 家族）

锁法：AST 收集声明键做成员断言（同 test_c5_wiring_locks.py 的结构锁风格——
图节点闭包深、无法轻量功能测试，结构锁是该粒度下的仓库既定模式）。
"""
import ast
from pathlib import Path

GRAPH = Path(__file__).resolve().parents[1] / "test_agent" / "graph"
STATE_PY = GRAPH / "state.py"
L2_PY = GRAPH / "l2.py"


def _declared_keys(path: Path, cls_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            keys = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    keys.add(stmt.target.id)
            return keys
    raise AssertionError(f"{cls_name} 不存在于 {path.name}")


def test_task_state_declares_provision_job_id():
    """provision return 的 job_id 必须声明，否则 report 收口 UPDATE 恒按 id=0 打空。"""
    assert "job_id" in _declared_keys(STATE_PY, "TaskState")


def test_task_state_declares_module_id_and_hotspots():
    """analyze 产出的 module_id/complexity_hotspots 必须声明（三表外键+plan 热点注入）。"""
    keys = _declared_keys(STATE_PY, "TaskState")
    assert "module_id" in keys
    assert "complexity_hotspots" in keys


def test_task_state_declares_auto_prepare():
    """builder 三入口 init 注入 auto_prepare，provision 读 state——不声明=勾选静默失效。"""
    assert "auto_prepare" in _declared_keys(STATE_PY, "TaskState")


def test_task_state_declares_last_cmd():
    """run return 的 last_cmd 必须声明，否则 flaky 连跑闸（MTA_FLAKY_RERUNS）恒不触发。"""
    assert "last_cmd" in _declared_keys(STATE_PY, "TaskState")


def test_l2_state_declares_last_stderr():
    """L2 run return 的 last_stderr 必须声明，否则 evaluate raw 尾巴 stderr 恒空（407 家族）。"""
    assert "last_stderr" in _declared_keys(L2_PY, "L2State")


def test_nodes_analyze_modules_upsert_reads_id_by_select():
    """modules upsert 走 ON CONFLICT DO UPDATE 时 cur.lastrowid 是同连接上一条成功
    INSERT 的 rowid（实测 sqlite3 语义，见报告），不是模块行 id——重跑时 module_id
    指错行。锁：analyze 闭包在 upsert 后必须用 SELECT 取 id。"""
    src = (GRAPH / "nodes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "make_nodes":
            for fn in ast.walk(node):
                if (isinstance(fn, ast.FunctionDef) and fn.name == "analyze"
                        and "modules(project_id, path" in ast.get_source_segment(src, fn)):
                    seg = ast.get_source_segment(src, fn)
                    assert "ON CONFLICT(project_id, path)" in seg
                    assert 'SELECT id FROM modules WHERE project_id=? AND path=' in seg
                    found = True
    assert found, "未找到 analyze 节点（结构变化请同步本锁）"


def test_gen_tests_insert_is_delete_first_by_filename():
    """gen_tests 落库前必须按 (task_id, filename) delete-first：工作区文件同轮重跑
    是 write_text 覆盖语义，DB 行不覆盖则重跑翻倍；module_id 接通后还会撞
    UNIQUE(task_id, module_id, version)（废案删除模式就在同函数 1941-1943）。"""
    src = (Path(__file__).resolve().parents[1] / "test_agent" / "skills" / "gen_tests.py"
           ).read_text(encoding="utf-8")
    assert 'INSERT INTO test_scripts(task_id, module_id, framework, version, filename' in src
    # delete-first 必须出现在主 INSERT 之前（按源码顺序）
    assert (src.index("DELETE FROM test_cases WHERE script_id IN")
            < src.index("INSERT INTO test_scripts(task_id, module_id, framework, version, filename"))


def test_plan_assigns_llm_before_understand_callback():
    """understand 的记账回调读 llm 元数据（_ctx_budget/_base_url）——llm 必须在
    回调可能触发的 understand_requirement 调用之前赋值。旧顺序：lambda 定义处引用
    尚未赋值的 llm → 回调时 NameError 被 understand.py 的 except 吞 →
    UNDERSTAND_ENABLED 时理解层 LLM 调用零记账（llm_calls 缺行、token 成本低估）。"""
    src = (GRAPH / "nodes.py").read_text(encoding="utf-8")
    assert 'llm = make_chat(phase="plan"' in src, "plan 构造点变了请同步本锁"
    assert (src.index('llm = make_chat(phase="plan"')
            < src.index("understanding = understand_requirement("))
