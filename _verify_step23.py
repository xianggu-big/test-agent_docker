"""临时冒烟脚本：验证 L4 沙箱 + L3 注册表（确认门/审计）。跑完即删。"""
import tempfile
from pathlib import Path

from test_agent import config
from test_agent.store import db
from test_agent.sandbox.runner import SandboxRunner
from test_agent.skills.registry import Skill, SkillRegistry

# 1) 准备数据：user + project + task（confirmed=0）
conn = db.get_conn()
uid = conn.execute("INSERT INTO users(username, password_hash, role) VALUES('smoke','x','developer')").lastrowid
pid = conn.execute("INSERT INTO projects(user_id, repo_url, name) VALUES(?, 'local://smoke', '冒烟项目')", (uid,)).lastrowid
tid = conn.execute("INSERT INTO test_tasks(project_id, user_id, requirement_text) VALUES(?, ?, '冒烟测试')", (pid, uid)).lastrowid
conn.commit()

# 2) 样例被测项目（函数级：直接 import 函数）
tmp = Path(tempfile.mkdtemp(prefix="mta_smoke_"))
(tmp / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
(tmp / "test_sample.py").write_text(
    "from sample import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")

# 3) 沙箱 + run_tests skill（高风险，需人工确认）
runner = SandboxRunner()
container = runner.ensure_container(config.IMAGE_PY, f"mta-run-{tid}")
runner.copy_dir_into(container, tmp)

def run_tests(task_id, args):
    r = runner.exec_in(container, ["pytest", "-q", "--tb=short"], timeout_s=120)
    return f"exit={r.exit_code}\n{r.stdout}{r.stderr}"

skill = Skill(name="run_tests", description="容器内执行 pytest",
              func=run_tests, risk_level="high", timeout_ms=120000,
              requires_confirmation=True)
reg = SkillRegistry(conn)
reg.register(skill)

# 4) 未确认 → 必须 denied（fail-closed 硬约束）
res = reg.invoke(tid, skill, {"cmd": "pytest -q"})
print("未确认调用 →", res.status, "|", res.error)
assert res.status == "denied"

# 5) 人工确认后放行
conn.execute("UPDATE test_tasks SET confirmed=1, confirmed_by=? WHERE id=?", (uid, tid))
conn.commit()
res = reg.invoke(tid, skill, {"cmd": "pytest -q"})
print("已确认调用 →", res.status)
print(res.output.strip())

# 6) 审计台账
print("--- skill_invocations 审计 ---")
for row in conn.execute("SELECT id, status, duration_ms, error FROM skill_invocations WHERE task_id=?", (tid,)):
    print(dict(row))

runner.cleanup(f"mta-run-{tid}")
print("SMOKE OK")
