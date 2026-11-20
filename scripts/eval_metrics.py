# -*- coding: utf-8 -*-
"""八指标评测聚合器（2026-11-15 卡 5，用户 8 指标需求，AGENTS"八指标盘点"落地）。

数据源 8/8 全在库内（真实库**只读**——sqlite3 file:ro URI，一行不写）：
  ①识别准确率  = analyze 的 projects.primary_language vs 语料真值 manifest
  ②环境构建成功率 = 进入执行阶段且有逐用例结果/评估轮 = 环境真实跑通
  ③生成成功率   = 有 AI 生成用例（test_cases source=ai）的任务占比 + AI 用例执行覆盖
  ④缺陷检出率   = 有 GT 的任务中检出（verdict=stop 且 failure_kind=product_defect）
  ⑤误报率      = 干净语料（manifest clean=true）上误报 product_defect 的占比
  ⑥覆盖率      = evaluation_rounds.coverage_line 均值（非空轮）
  ⑦耗时        = 任务 created_at → 终态 updated_at 分钟均值
  ⑧Token       = llm_calls.prompt/completion_tokens 求和（台账列）
输出：data/reports/eval_metrics/metrics_<ts>.{json,md}（集合级 + 按语言分列）。

用法：
  python scripts/eval_metrics.py --all                # 全部任务
  python scripts/eval_metrics.py --tasks 1138,1161    # 指定任务
  python scripts/eval_metrics.py --lang go            # 按主语言过滤
真值/干净语料标注：data/reports/eval_metrics/corpus_truth.json（随语料补充）。
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "test_agent.db"
OUT_DIR = ROOT / "data" / "reports" / "eval_metrics"
TRUTH_FILE = OUT_DIR / "corpus_truth.json"

REPORTING = ("done", "failed", "cancelled", "ready_for_review")


def _ro_conn() -> sqlite3.Connection:
    """真实库只读连接（file:ro URI——写入会被 SQLite 拒绝，红线 2 的机械保证）。"""
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_truth() -> dict:
    if TRUTH_FILE.exists():
        return json.loads(TRUTH_FILE.read_text(encoding="utf-8"))
    return {"languages": {}, "clean": [], "gt_defects": {}}


def collect_tasks(conn, args) -> list[dict]:
    q = """SELECT t.id, t.status, t.error, t.created_at, t.updated_at,
                  p.name AS project, p.primary_language
           FROM test_tasks t JOIN projects p ON p.id = t.project_id WHERE 1=1"""
    params: list = []
    if args.tasks:
        ids = [int(x) for x in str(args.tasks).split(",") if x.strip()]
        q += f" AND t.id IN ({','.join('?' * len(ids))})"
        params += ids
    if args.lang:
        q += " AND p.primary_language=?"
        params.append(args.lang)
    q += " AND t.status IN ('done','failed','cancelled') ORDER BY t.id"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def task_metrics(conn, t: dict, truth: dict) -> dict:
    tid = t["id"]
    m: dict = {"task": tid, "project": t["project"], "language": t["primary_language"],
               "status": t["status"], "error": (t["error"] or "")[:120]}
    # ① 识别：真值有标注 → 对照 primary_language
    gt_lang = truth.get("languages", {}).get(t["project"])
    if gt_lang:
        m["id_truth"] = gt_lang
        m["id_hit"] = int(t["primary_language"] == gt_lang)
    # ② 环境：进过执行阶段且有逐用例结果或评估轮
    ran = conn.execute(
        "SELECT (SELECT COUNT(*) FROM test_case_results WHERE task_id=?) +"
        "      (SELECT COUNT(*) FROM evaluation_rounds WHERE task_id=?) AS n",
        (tid, tid)).fetchone()["n"]
    m["env_ok"] = int(ran > 0)
    # ③ 生成：AI 用例数 + 其中拿到执行结果的（origin=generated）
    m["gen_cases"] = conn.execute(
        "SELECT COUNT(*) AS n FROM test_cases c JOIN test_scripts s ON s.id=c.script_id"
        " WHERE s.task_id=? AND c.source='ai'", (tid,)).fetchone()["n"]
    m["gen_executed"] = conn.execute(
        # DISTINCT name：R-Loop 多轮重复跑同一用例（round1 全目录+round2+ 生成物），
        # "执行覆盖"语义=拿过结果的 AI 用例种类数，跨轮不重复计
        "SELECT COUNT(DISTINCT name) AS n FROM test_case_results"
        " WHERE task_id=? AND origin='generated'", (tid,)).fetchone()["n"]
    m["gen_ok"] = int(m["gen_cases"] > 0)
    # ④⑤ 检出/误报：疑似产品缺陷任务 + 干净语料标注
    pk = conn.execute(
        "SELECT COUNT(*) AS n FROM evaluation_rounds WHERE task_id=?"
        " AND failure_kind='product_defect'", (tid,)).fetchone()["n"]
    m["detected"] = int(pk > 0)
    if t["project"] in truth.get("clean", []):
        m["clean_fp"] = m["detected"]
    if t["project"] in truth.get("gt_defects", {}):
        m["gt_defects"] = truth["gt_defects"][t["project"]]
        m["gt_hit"] = m["detected"]
    # ⑥ 覆盖率
    covs = [r["coverage_line"] for r in conn.execute(
        "SELECT coverage_line FROM evaluation_rounds WHERE task_id=?"
        " AND coverage_line IS NOT NULL", (tid,)).fetchall()]
    m["coverage"] = _avg(covs)
    # ⑥b 覆盖率分桶（2026-11-19 复盘方案 E）：API 黑盒用例（真 HTTP）插桩结构性
    # 不可达（存疑 35②），覆盖 0/N/A 是测量边界而非测试质量——与进程内用例的
    # 1.0 混在同一个均值里语义不同质（php 0.0 稀释 js 1.0）。
    #   blackbox=detect_api_blackbox(ws,lang) 真（口径同源：直接复用 evaluate 同款识别）
    #   inproc=非黑盒且有覆盖值；unknown=工作区已清理（无证据，不入桶）
    m["cov_bucket"] = "unknown"
    ws = ROOT / "data" / "workspaces" / f"task_{tid}"
    if ws.is_dir():
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))   # 脚本独立运行时补路径（否则导入恒失败）
            from test_agent.graph.nodes import detect_api_blackbox
            m["cov_bucket"] = ("blackbox" if detect_api_blackbox(ws, t["primary_language"])
                               else "inproc")
        except Exception:                     # noqa: BLE001 —— 导入失败=诚实 unknown
            m["cov_bucket"] = "unknown"
    # ⑦ 耗时（分钟）
    if t["created_at"] and t["updated_at"]:
        import datetime as _dt
        try:
            d = (_dt.datetime.fromisoformat(t["updated_at"])
                 - _dt.datetime.fromisoformat(t["created_at"])).total_seconds() / 60
            m["minutes"] = round(d, 1)
        except ValueError:
            pass
    # ⑧ Token
    tk = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c"
        " FROM llm_calls WHERE task_id=?", (tid,)).fetchone()
    m["tokens"] = tk["p"] + tk["c"]
    return m


def aggregate(rows: list[dict]) -> dict:
    def frac(key):
        # 分母=有该标注的行（识别准确率只对有真值 manifest 的任务有意义）
        marked = [r for r in rows if key in r]
        hit = sum(1 for r in marked if r[key] == 1 or r[key] is True)
        return round(hit / len(marked), 4) if marked else None
    env_rows = rows            # 全部终态任务作分母（未进入执行=环境失败的同义）
    # C3（2026-11-20 设计方案）：误报率输出计数形态——分子/分母明示，分母 <30 禁用
    # 纯比率（Qoder 体检实验-a：overall 0.1176 实为 2/17 仅 4 项目，比率掩盖分母过小；
    # 且 detected=疑似产品缺陷标签 ≠ 缺陷级真阳性，口径注随行输出）
    _fp_num = sum(1 for r in rows if r.get("clean_fp"))
    _fp_den = sum(1 for r in rows if "clean_fp" in r)
    return {
        "n_tasks": len(rows),
        "识别准确率": frac("id_hit") if any("id_hit" in r for r in rows) else None,
        "环境构建成功率": round(sum(1 for r in env_rows if r["env_ok"]) / len(env_rows), 4) if env_rows else None,
        "生成成功率": frac("gen_ok"),
        "AI用例执行覆盖": _avg([(r["gen_executed"] / r["gen_cases"]) if r["gen_cases"] else None
                                for r in rows]),
        "检出率_GT": (sum(1 for r in rows if r.get("gt_hit")) /
                     sum(1 for r in rows if "gt_hit" in r)) if any("gt_hit" in r for r in rows) else None,
        "误报率_干净语料": (round(_fp_num / _fp_den, 4) if _fp_den else None),
        "误报率_计数": (f"{_fp_num}/{_fp_den}"
                       + ("（分母<30：只报计数勿引比率；detected=疑似产品缺陷标签≠缺陷级真阳性）"
                          if 0 < _fp_den < 30 else "")) if _fp_den else None,
        "平均覆盖率": _avg([r["coverage"] for r in rows]),
        # 方案 E 分桶（2026-11-19 复盘）：黑盒 0/N/A 是测量边界，进程内才是真实覆盖——
        # 两桶分开报，混桶均值只保留作历史兼容
        "平均覆盖率_进程内": _avg([r["coverage"] for r in rows
                                    if r.get("cov_bucket") == "inproc"]),
        "平均覆盖率_API黑盒": _avg([r["coverage"] for r in rows
                                     if r.get("cov_bucket") == "blackbox"]),
        "覆盖率分桶计数": {
            "inproc": sum(1 for r in rows if r.get("cov_bucket") == "inproc" and r["coverage"] is not None),
            "blackbox": sum(1 for r in rows if r.get("cov_bucket") == "blackbox" and r["coverage"] is not None),
            "unknown": sum(1 for r in rows if r.get("cov_bucket") == "unknown" and r["coverage"] is not None),
        },
        "平均耗时分钟": _avg([r.get("minutes") for r in rows]),
        "总Token": sum(r["tokens"] for r in rows),
    }


def render_md(overall: dict, by_lang: dict, rows: list[dict]) -> str:
    lines = ["# 八指标评测聚合（自动生成 scripts/eval_metrics.py）", "",
             f"- 任务集：{overall['n_tasks']} 个终态任务；口径注释见脚本 docstring", ""]
    langs = ["识别准确率", "环境构建成功率", "生成成功率", "AI用例执行覆盖", "检出率_GT",
             "误报率_干净语料", "误报率_计数", "平均覆盖率_进程内", "平均覆盖率_API黑盒",
             "平均覆盖率", "平均耗时分钟", "总Token"]
    lines.append("| 语言 | n | " + " | ".join(langs) + " |")
    lines.append("|---|---|" + "---|" * len(langs))
    def _v(x):
        return "—" if x is None else (f"{x:.4f}" if isinstance(x, float) else str(x))
    for lang, agg in [("全部", overall)] + list(by_lang.items()):
        lines.append(f"| {lang} | {agg['n_tasks']} | " +
                     " | ".join(_v(agg[k]) for k in langs) + " |")
    lines += ["", "## 任务明细", "", "| task | project | lang | status | gen | exec_ai | cov | tokens | 检出 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r['project'][:28]} | {r['language']} | {r['status']} "
                     f"| {r['gen_cases']} | {r['gen_executed']} | {r['coverage']} | {r['tokens']} "
                     f"| {r['detected']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="八指标评测聚合器（真实库只读）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tasks", help="逗号分隔任务 id")
    g.add_argument("--lang", help="按主语言过滤")
    g.add_argument("--all", action="store_true", help="全部终态任务")
    args = ap.parse_args()
    if not (args.tasks or args.lang or args.all):
        ap.error("需 --tasks/--lang/--all 之一")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = _ro_conn()
    truth = load_truth()
    tasks = collect_tasks(conn, args)
    rows = [task_metrics(conn, t, truth) for t in tasks]
    overall = aggregate(rows)
    by_lang = {}
    for r in rows:
        by_lang.setdefault(r["language"] or "?", []).append(r)
    by_lang_agg = {k: aggregate(v) for k, v in sorted(by_lang.items())}
    ts = time.strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"metrics_{ts}.json").write_text(
        json.dumps({"overall": overall, "by_language": by_lang_agg, "tasks": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(overall, by_lang_agg, rows)
    (OUT_DIR / f"metrics_{ts}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"输出：{OUT_DIR}/metrics_{ts}.json / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
