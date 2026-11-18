# -*- coding: utf-8 -*-
"""L2 覆盖率急救·真机触发格（2026-11-18 晚，用户令"设计真机触发证据链两格"）。

口径=项目既定"真机验证"标准（G 三闸真机验证同款）：tmp 库（不污染生产）+
真 Docker 容器+真图节点（analyze/run/evaluate 全真调）+真 pytest-cov 执行。
唯一预置=生成用例文件（幻觉 import 的确定性重放——A9 失败家族典型形态），
gen 环节不调 LLM（零 API 成本）。

触发链设计：
  analyze 真路由命中 payment（模块名直中 high）→闭包 hops1=[payment, service]
  → run 真容器执行预置炸用例（import payment_v2 → ModuleNotFoundError →
    collection error → exit 2 → 被测代码 0 行执行）
  → evaluate：classify=case_defect → repair_gen → coverage_check 读真覆盖表
    （范围内全 0%）→ zero_coverage → 急救扩圈 hops=2（+billing）→ 消息留痕
  → 第二轮 evaluate（rescued=True，同输出）→ 仍零覆盖 → 放弃路由回退全库
总判据：[index·L2] 扩圈消息+回退消息双留痕，state 三键（scope/closure/rescued）
变化与设计一致。
"""
import json
import shutil
import sqlite3
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from test_agent import config
from test_agent.graph.nodes import make_nodes
from test_agent.store import db

DEMO = ROOT / "data/demo/l2-rescue-demo"
OUT = ROOT / "data/reports/l1l2l3_real/l2_rescue_real_results.json"
CONTAINER = "mta-l2rescue-real"
WS_DIR = ROOT / "data/reports/l1l2l3_real/ws_l2rescue"

# 幻觉 import 炸用例（A9 家族确定性重放：模块名幻觉 → payment 链 0 行执行）
BOMB_TEST = '''"""幻觉 import 重放用例（真机触发格预置）。"""
import payment_v2


def test_charge():
    assert True
'''


def main() -> int:
    # ---------- 0. 环境准备：tmp 库 + tmp workspace + 真容器 ----------
    base = ROOT / "data/reports/l1l2l3_real/tmp_l2rescue"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    monkey_db = base / "t.db"
    monkeypatch_data_dir(base)

    conn = db.get_conn(monkey_db)
    db.init_db(monkey_db)
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('u','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,'r','l2rescue')",
                       (uid,)).lastrowid
    tid = conn.execute("INSERT INTO test_tasks(project_id,user_id,coverage_target,status,confirmed) "
                       "VALUES(?,?,0.7,'queued',1)", (pid, uid)).lastrowid
    ses = conn.execute("INSERT INTO agent_sessions(task_id) VALUES(?)", (tid,)).lastrowid
    conn.commit()

    ws = WS_DIR
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(DEMO, ws)
    (ws / "test_gen_r1.py").write_text(BOMB_TEST, encoding="utf-8")

    from test_agent.sandbox.runner import SandboxRunner
    runner = SandboxRunner()
    runner.cleanup(CONTAINER)
    container = runner.ensure_container(config.IMAGE_PY, CONTAINER, fresh=True)
    runner.copy_dir_into(container, ws)

    # 真注册表（tmp 库审计台账同真）——真容器真执行
    from test_agent.skills.builtin import build_skills
    from test_agent.skills.registry import SkillRegistry
    skills = build_skills(runner)
    registry = SkillRegistry(conn)

    from test_agent.graph.builder import GraphDeps
    nodes = make_nodes(GraphDeps(conn=conn, runner=runner, registry=registry, skills=skills))

    state = {"task_id": tid, "session_id": ses, "workspace": str(ws),
             "requirement": "测试 payment 模块的收费边界",
             "primary_language": "python", "round_no": 0,
             "container_name": CONTAINER, "fingerprints": [], "witness_nodeids": []}

    evidence = {"case": "L2-R 真机急救触发", "steps": []}

    def messages() -> list:
        return [r["content"] for r in conn.execute(
            "SELECT content FROM agent_messages WHERE session_id=? ORDER BY seq", (ses,))]

    # ---------- 1. analyze：真路由+真闭包 ----------
    out_a = nodes["analyze"](dict(state))
    state.update({k: out_a[k] for k in ("index_scope_paths", "index_closure")
                  if k in out_a})
    m_analyze = [m for m in messages() if "[analyze·index]" in m]
    step1 = {"step": "analyze 真路由", "closure": out_a.get("index_closure"),
             "scope": out_a.get("index_scope_paths"), "msg": m_analyze[-1][:120] if m_analyze else ""}
    evidence["steps"].append(step1)
    assert out_a.get("index_closure") == ["payment", "service"], \
        f"闭包 hops1 应为 [payment, service]: {out_a.get('index_closure')}"
    print("[1] analyze: closure =", out_a.get("index_closure"), flush=True)

    # ---------- 2. run：真容器执行（collection error） ----------
    out_r = nodes["run"](dict(state))
    state.update({k: out_r[k] for k in ("last_stdout", "last_stderr", "last_exit", "last_cmd")
                  if k in out_r})
    print(f"[2] run: exit={state.get('last_exit')} stdout_tail="
          f"{state.get('last_stdout', '')[-120:].strip()!r}", flush=True)
    evidence["steps"].append({"step": "run 真容器", "exit": state.get("last_exit"),
                              "stdout_tail": state.get("last_stdout", "")[-200:].strip()})

    # ---------- 3. evaluate 第 1 轮：急救触发 ----------
    state["round_no"] = 1
    out_e1 = nodes["evaluate"](dict(state))
    msgs1 = messages()
    expand = [m for m in msgs1 if "[index·L2]" in m]
    step3 = {"step": "evaluate R1 急救", "verdict": out_e1.get("verdict"),
             "rescued": out_e1.get("index_l2_rescued"),
             "scope_after": out_e1.get("index_scope_paths"),
             "l2_msgs": [m[:150] for m in expand]}
    evidence["steps"].append(step3)
    print(f"[3] evaluate R1: verdict={out_e1.get('verdict')} rescued={out_e1.get('index_l2_rescued')} "
          f"scope_after={out_e1.get('index_scope_paths')}", flush=True)
    for m in expand:
        print("    ", m[:130], flush=True)
    assert out_e1.get("index_l2_rescued") is True, "R1 急救必须置 rescued"
    assert any("扩圈" in m for m in expand), "必须有扩圈消息"

    # ---------- 4. evaluate 第 2 轮：仍零覆盖 → 回退全库 ----------
    state2 = dict(state)
    state2.update({"round_no": 2,
                   "index_scope_paths": out_e1["index_scope_paths"],
                   "index_closure": out_e1["index_closure"],
                   "index_l2_rescued": True,
                   "fingerprints": out_e1.get("fingerprints", [])})
    out_e2 = nodes["evaluate"](state2)
    msgs2 = messages()
    fallback = [m for m in msgs2 if "回退全库" in m and "[index·L2]" in m]
    step4 = {"step": "evaluate R2 回退", "verdict": out_e2.get("verdict"),
             "scope_after": out_e2.get("index_scope_paths"),
             "fallback_msgs": [m[:150] for m in fallback]}
    evidence["steps"].append(step4)
    print(f"[4] evaluate R2: verdict={out_e2.get('verdict')} scope_after="
          f"{out_e2.get('index_scope_paths')}", flush=True)
    for m in fallback:
        print("    ", m[:130], flush=True)
    assert out_e2.get("index_scope_paths") == [], "R2 必须放弃路由（scope 清空）"
    assert fallback, "必须有回退全库消息"

    # ---------- 总判据 ----------
    evidence["verdict"] = ("PASS" if (out_e1.get("index_l2_rescued") is True
                                      and fallback and not out_e2["index_scope_paths"])
                           else "FAIL")
    evidence["mechanism"] = ("真机急救链走通：路由锁定→真容器零覆盖→扩圈→仍零覆盖→回退全库"
                             if evidence["verdict"] == "PASS" else "链路未走通，见 steps")
    evidence["generated_at"] = time.strftime("%F %T")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[判定] {evidence['verdict']} → {OUT}", flush=True)

    runner.cleanup(CONTAINER)
    conn.close()
    return 0 if evidence["verdict"] == "PASS" else 1


def monkeypatch_data_dir(base: Path) -> None:
    """索引落盘跟随 tmp DATA_DIR（脚本级 monkeypatch，进程内有效）。"""
    config.DATA_DIR = str(base)


if __name__ == "__main__":
    sys.exit(main())
