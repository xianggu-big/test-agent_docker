# -*- coding: utf-8 -*-
"""三角色对比实验驱动（2026-10-31）：tenacity 为被测，3 角色×10 题需求端到端走图。

用法：
  python scripts/run_roles_experiment.py --smoke      # 每角色 1 题（3 轮冒烟）
  python scripts/run_roles_experiment.py              # 全量 30 轮
产物：data/reports/roleexp_<role><idx>.json（题目引用+终态+指标+token 实测）
容错：单轮异常留档继续（不中断批次）；余额停车线=余额<1.5 元时中止批次。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "candidates" / "tenacity"
SOURCE_URL = "https://github.com/jd/tenacity.git"
OUT = ROOT / "data" / "reports"

SMOKE = {"小白": ["x1"], "初级": ["c1"], "高级": ["g1"]}

QUESTIONS = {
    "小白": {
        "x1": ("一个函数总是失败，用了重试之后会不会一直试到成功为止，帮我测测这个重试好不好使",
               "重试确实发生（调用次数>1）"),
        "x2": ("我想让它最多试 3 次就放弃，别一直试下去，帮我看看行不行", "第 3 次后停止"),
        "x3": ("两次重试之间能不能等一会儿再试？比如每次等 1 秒", "间隔≈1s"),
        "x4": ("如果函数成功了就别再重试了吧，测一下成功的情况", "成功即返回只调一次"),
        "x5": ("我想让它只对某种错误重试，其他错误直接放弃，能设置吗", "类型过滤生效"),
        "x6": ("重试很多次还是不行的话，最后应该把那个错误报出来吧，测测最后抛的是不是原来的错", "异常上抛"),
        "x7": ("能不能重试的时候越等越久？别每次都等一样的时间", "递增序列"),
        "x8": ("函数返回值是空的也算失败，这种情况也重试，帮我试试", "None 触发重试"),
        "x9": ("等待时间有点随机才好，不然大家一起重试就挤一块了，有这功能吗", "随机等待生效"),
        "x10": ("统计一下函数到底被调用了几次，重试的时候次数对不对", "计数准确"),
    },
    "初级": {
        "c1": ("对 tenacity 的 stop_after_attempt(N) 做边界值分析：N=1、N=2、N=3 三档，"
               "用一个计数函数验证实际尝试次数恰好等于 N", "次数==N"),
        "c2": ("对 wait_fixed(t) 做等待时间边界验证：t=0 与 t=0.1 两档，记录相邻两次调用的"
               "实际间隔，断言非负且与设定一致（时钟精度容忍 ±20%）", "间隔合规"),
        "c3": ("retry_if_exception_type 的等价类：命中类型（ValueError）应重试，"
               "未命中类型（KeyError）应直接上抛不重试；再验证多类型元组 (ValueError, KeyError)", "分类正确"),
        "c4": ("成功路径等价类：@retry 装饰后的函数返回值与未装饰原函数完全一致（含复杂返回结构），"
               "验证装饰不改变成功语义", "返回一致"),
        "c5": ("stop_after_delay(d)：函数持续失败超过 d 秒后应停止重试，测量总耗时不超过 d+单次执行时间"
               "的合理上界", "超时停止"),
        "c6": ("reraise=True 与默认 False 对比：重试耗尽后最终抛出的异常类型与来源"
               "（RetryError 包装 vs 原始异常）", "reraise 语义"),
        "c7": ("wait_exponential(multiplier=1, max=10)：记录前 4 次重试的实际等待值，"
               "断言按指数增长且不超过 max", "序列正确"),
        "c8": ("retry_if_result：返回 None 与 'error' 触发重试、返回正常值不重试，三类各验证一次", "结果判定"),
        "c9": ("before_sleep 回调：验证每次重试睡眠前被调用、调用次数等于重试次数、"
               "参数包含 retry_state", "回调触发"),
        "c10": ("多参数组合：stop_after_attempt(4)+wait_fixed(0.05)+retry_if_exception_type(ValueError) "
                "联合作用，用同时会抛 ValueError 和 KeyError 的函数验证组合语义", "组合语义"),
    },
    "高级": {
        "g1": ("wait_random(min, max) 参数倒置（min>max，如 wait_random(3, 1)）时的行为："
               "是静默接受、抛错还是产生非法等待值？实测并给出结论", "校验行为明确"),
        "g2": ("stop_after_attempt(0) 与 stop_after_attempt(-1) 的语义：被装饰函数到底被调用几次？"
               "零次尝试是否成立？实测边界", "边界语义明确"),
        "g3": ("wait_exponential(multiplier=1e9) 极大指数：等待值是否溢出、是否被默认/显式 max 钳制，"
               "实测前 3 次等待值", "无溢出/有钳制"),
        "g4": ("在 async def 函数上使用同步 @retry 装饰器：重试是否静默失效（协程一次执行后"
               "异常直接上抛）？实测并给出可诊断性结论", "行为可诊断"),
        "g5": ("被装饰函数含副作用（如列表 append）且中途失败：重试时副作用是否从头重复执行？"
               "验证重试的执行语义（整函数重跑 vs 断点续跑）", "副作用语义"),
        "g6": ("retry_state 的 wait/tries 属性与实际 sleep 的一致性：第 N 次重试声明的等待值"
               "与实测间隔偏差（容忍 ±20% 时钟精度）", "状态与实际一致"),
        "g7": ("wait 策略函数自身抛异常（如自定义 wait=lambda rs: 1/0）：重试框架的降级行为"
               "是崩溃、吞掉还是转为 RetryError？实测", "降级可诊断"),
        "g8": ("嵌套 retry：外层 @retry(stop_after_attempt(2)) 与内层 @retry(stop_after_attempt(3))"
               "叠放，内层持续失败时外层总调用/总尝试次数是乘积、求和还是覆盖？实测并对照文档", "语义一致"),
        "g9": ("多线程并发（10 线程）调用同一 @retry 装饰函数（前 2 次失败）：全局调用计数"
               "与各线程重试次数是否正确（tenacity 状态无锁的实证）", "并发计数正确"),
        "g10": ("RetryError 异常链诊断性：reraise=False 时 RetryError.__cause__/__context__ "
                "是否保留最后一次原始异常的 traceback", "链可诊断"),
    },
}


def main() -> int:
    args = sys.argv[1:]
    smoke = "--smoke" in args
    # 余额停车线
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer " + __import__("os").environ.get("DEEPSEEK_API_KEY", "")})
    bal = float(json.loads(urllib.request.urlopen(req, timeout=15).read())
                ["balance_infos"][0]["total_balance"])
    print(f"[roleexp] 余额 {bal} 元（停车线 1.5）", flush=True)
    if bal < 1.5:
        print("[roleexp] 余额低于停车线，中止")
        return 2

    from test_agent.graph.builder import run_task
    from test_agent.store.db import get_conn

    conn = get_conn()
    plan = []
    for role, qs in QUESTIONS.items():
        keys = SMOKE.get(role, []) if smoke else list(qs)
        for k in keys:
            plan.append((role, k))
    print(f"[roleexp] 批次 {len(plan)} 轮（smoke={smoke}）", flush=True)

    ok = err = 0
    for role, key in plan:
        out = OUT / f"roleexp_{role}{key}.json"
        if out.exists():
            print(f"[roleexp] {role}{key} 已有产物，跳过", flush=True)
            ok += 1
            continue
        text, criteria = QUESTIONS[role][key]
        uid = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        pid = conn.execute("INSERT INTO projects(user_id,repo_url,name) VALUES(?,?,?)",
                           (uid, str(TARGET), f"roleexp-{role}{key}")).lastrowid
        requirement = f"{text}\n（验收判据：{criteria}）"
        tid = conn.execute(
            "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
            " VALUES(?,?,?,?,1)", (pid, uid, requirement, uid)).lastrowid
        conn.commit()
        t0 = time.time()
        try:
            final = run_task(tid)
        except Exception as e:  # 环境类异常也留档继续
            final = None
            print(f"[roleexp] {role}{key} 任务 #{tid} 异常：{type(e).__name__}: {str(e)[:120]}",
                  flush=True)
        rep = conn.execute("SELECT passed, failed, skipped, coverage_line, defects, created_at"
                           " FROM test_reports WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (tid,)).fetchone()
        lc = conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens),0) AS t"
                          " FROM llm_calls WHERE task_id=?", (tid,)).fetchone()
        row = {"experiment": "roleexp", "role": role, "question": key,
               "source_url": SOURCE_URL, "task_id": tid,
               "requirement": text, "criteria": criteria,
               "final": {k: final.get(k) for k in
                         ("verdict", "stop_reason", "passed", "failed", "coverage_line")} if final else None,
               "report": dict(rep) if rep else None,
               "llm": {"calls": lc["c"], "total_tokens": lc["t"]} if lc else None,
               "duration_s": round(time.time() - t0, 1), "manual_review": None,
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        out = OUT / f"roleexp_{role}{key}.json"
        out.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        tag = row["final"]["verdict"] if row["final"] else "EXC"
        print(f"[roleexp] {role}{key} #{tid} → {tag} | token {row['llm']['total_tokens'] if row['llm'] else 0}"
              f" | {out.name}", flush=True)
        ok += 1 if row["final"] else 0
        err += 0 if row["final"] else 1
        # 余额中途复查（每 10 轮）
        if (ok + err) % 10 == 0:
            b = json.loads(urllib.request.urlopen(req, timeout=15).read())
            cur = float(b["balance_infos"][0]["total_balance"])
            if cur < 1.5:
                print(f"[roleexp] 余额 {cur} 低于停车线，中止批次", flush=True)
                break
    print(f"[roleexp] 批次完成：终态可判 {ok} / 异常 {err}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
