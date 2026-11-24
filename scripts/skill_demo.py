# -*- coding: utf-8 -*-
"""代表 Skill 现场调用演示器（学院达标 T6，2026-11-25）——零 LLM、零 Docker。

对总入口 P1-3/Skill 辅方向"(6) 现场演示调用过程"：经 SkillRegistry.invoke 连打
六次（主代表 run_tests 三次 + 辅代表 test.observe.read 三次），逐次打印输入、
输出、skill_invocations 审计行——"输入输出/异常处理/日志"三件套的现场可重复证明。

三次调用形态（设计稿 §10.1）：正常 / 非法参数被拒（schema 闸） / 确认门或停用被拒。
主代表的"正常执行"在无考场容器时走 handler 的诚实环境降级分支（2026-11-15 E9
实锤路径）——异常处理本身就是演示项。

用法：python -I scripts/skill_demo.py（tmp 库，不动生产库）
验收：skill_invocations 产出 ≥6 行审计，预期状态逐一命中。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skill_demo_"))
    print(f"[skill_demo] tmp = {tmp}")

    import test_agent.config as config
    config.DATA_DIR = tmp
    config.DB_PATH = tmp / "t.db"
    from test_agent.store import db
    db.init_db(tmp / "t.db")
    from test_agent.store.db import get_conn
    conn = get_conn(tmp / "t.db")

    from test_agent.skills.registry import SkillRegistry, Skill
    reg = SkillRegistry(conn=conn)

    # ---------- 夹具：两条任务（一条未确认撞确认门）+ 工作区文件 ----------
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('demo','x')"
                       ).lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','demo')",
                       (uid,)).lastrowid
    tid_ok = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
        "VALUES(?,?,0.7,'running',1)", (pid, uid)).lastrowid          # 已确认
    tid_lock = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
        "VALUES(?,?,0.7,'running',0)", (pid, uid)).lastrowid         # 未确认
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    conn.commit()

    results: list[tuple[str, str, str]] = []   # (场景, 预期, 实际)

    def show(title: str, expect: str, res) -> None:
        got = res.status
        results.append((title, expect, got))
        print(f"\n=== {title} ===")
        print(f"  输入: {res_output_input.get(title, '')}")
        print(f"  输出: status={got} duration={res.duration_ms}ms "
              f"error={(res.error or '')[:80]!r}")
        print(f"  输出摘要: {(res.output or '')[:120]!r}")

    res_output_input: dict[str, str] = {}

    # ================= 主代表 run_tests（内置·high·显式确认门） =================
    from test_agent.skills.builtin import build_skills
    fake_runner = types.SimpleNamespace(
        client=types.SimpleNamespace(
            containers=types.SimpleNamespace(
                get=lambda name: (_ for _ in ()).throw(
                    Exception("container not found (demo)")))))
    skills = build_skills(fake_runner)
    reg.register(skills["run_tests"])

    # ① 权限类：确认门（confirmed=0 → fail-closed 拒绝落审计）
    res_output_input["run_tests①确认门"] = json.dumps(
        {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]},
        ensure_ascii=False)
    r1 = reg.invoke(tid_lock, skills["run_tests"],
                    {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]})
    show("run_tests① 确认门（未确认任务）", "denied", r1)

    # ② 异常类：正常下发但考场容器不可达 → handler 诚实环境降级（非裸 404）
    res_output_input["run_tests②容器不可达诚实降级"] = json.dumps(
        {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]},
        ensure_ascii=False)
    r2 = reg.invoke(tid_ok, skills["run_tests"],
                    {"container_name": "mta-run-1", "cmd": ["python", "-m", "pytest"]})
    show("run_tests② 正常下发（容器不可达→诚实环境错误）", "ok", r2)
    honest = "环境错误" in (r2.output or "")
    print(f"  诚实降级检查: {'PASS' if honest else 'FAIL'}（输出含环境错误语义）")

    # ③ 边界/协议类：schema 闸（run_tests=legacy 简写 schema：cmd 需 list[str]，
    #    传字符串→类型不匹配拒；缺键在 legacy 模式放行=存量兼容语义）
    res_output_input["run_tests③schema类型拒"] = json.dumps(
        {"container_name": "mta-run-1", "cmd": "python -m pytest"}, ensure_ascii=False)
    r3 = reg.invoke(tid_ok, skills["run_tests"],
                    {"container_name": "mta-run-1", "cmd": "python -m pytest"})
    show("run_tests③ schema 闸（cmd 类型错：str≠list）", "denied", r3)

    # ================= 辅代表 test.observe.read（Agent 能力型） =================
    from test_agent.skills.capabilities import TaskCtx, capability_menu, \
        find_capability_extended
    ctx = TaskCtx(task_id=tid_ok, session_id=0, workspace=ws, container_name="-",
                  primary_language="python", image="-", state_snapshot={},
                  conn=conn, runner=fake_runner, artifacts_dir=tmp / "artifacts")
    cap = find_capability_extended(ctx, "test.observe.read")
    assert cap is not None, "观察能力应在菜单（python 语言）"

    def cap_as_skill(cap) -> Skill:
        # 与 tool_loop._execute_capability 同款包装（Capability→Skill 经注册表审计）
        from test_agent.skills.capabilities import normalize_result
        import dataclasses as _dc

        def _run(_task_id, _args):
            r = normalize_result(cap.handler(ctx, dict(_args)))
            return json.dumps(_dc.asdict(r), ensure_ascii=False)
        return Skill(name=cap.id, description=cap.description, func=_run,
                     risk_level="low", timeout_ms=5000, arg_schema=cap.inputs_schema)

    skill_read = cap_as_skill(cap)
    reg.register(skill_read)

    # ① 正常类：读文件
    res_output_input["observe①读文件"] = json.dumps(
        {"path": "calc.py", "mode": "read"}, ensure_ascii=False)
    o1 = reg.invoke(tid_ok, skill_read, {"path": "calc.py", "mode": "read"})
    show("observe① 正常读文件", "ok", o1)

    # ② 边界类：limit 超上限（schema maximum=200）
    res_output_input["observe②limit超上限"] = json.dumps(
        {"path": "calc.py", "mode": "read", "limit": 999}, ensure_ascii=False)
    o2 = reg.invoke(tid_ok, skill_read, {"path": "calc.py", "mode": "read", "limit": 999})
    show("observe② schema 闸（limit>200 上限）", "denied", o2)

    # ③ 权限类：停用（enabled=0）
    conn.execute("UPDATE skills SET enabled=0 WHERE name='test.observe.read'")
    conn.commit()
    res_output_input["observe③停用拒"] = json.dumps(
        {"path": "calc.py", "mode": "read"}, ensure_ascii=False)
    o3 = reg.invoke(tid_ok, skill_read, {"path": "calc.py", "mode": "read"})
    show("observe③ 停用（enabled=0）", "denied", o3)
    conn.execute("UPDATE skills SET enabled=1 WHERE name='test.observe.read'")
    conn.commit()

    # ================= 审计台账对账 =================
    rows = conn.execute(
        """SELECT si.id, s.name, si.status, si.duration_ms, substr(COALESCE(si.error,''),1,60) AS err
           FROM skill_invocations si JOIN skills s ON s.id=si.skill_id
           WHERE s.name IN ('run_tests','test.observe.read') ORDER BY si.id""").fetchall()
    print(f"\n=== skill_invocations 审计台账（{len(rows)} 行）===")
    for r in rows:
        print(f"  #{r['id']} {r['name']} → {r['status']} ({r['duration_ms']}ms) "
              f"err={r['err']!r}")

    expect = [("run_tests① 确认门（未确认任务）", "denied"),
              ("run_tests② 正常下发（容器不可达→诚实环境错误）", "ok"),
              ("run_tests③ schema 闸（缺必填 cmd）", "denied"),
              ("observe① 正常读文件", "ok"),
              ("observe② schema 闸（limit>200 上限）", "denied"),
              ("observe③ 停用（enabled=0）", "denied")]
    ok = len(rows) >= 6 and all(got == exp for (_, exp, got) in results) and honest
    print(f"\n[skill_demo] 结果: {'PASS' if ok else 'FAIL'} "
          f"（审计 {len(rows)} 行，六场景状态 {sum(1 for _, e, g in results if e == g)}/6 命中，"
          f"诚实降级 {'PASS' if honest else 'FAIL'}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
