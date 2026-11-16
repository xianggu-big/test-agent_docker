"""multi60（E10 六语言 60 格）健康度矩阵聚合（2026-11-17 卡 1 + 卡 4 口径分账）。

数据源（全部只读）：
- data/reports/e10/multi60_results.json（驱动器格账，每格落盘真源）
- data/test_agent.db：evaluation_rounds（0 收集判定）+ test_tasks.error（终态原因）

判定口径（同 18 格验收）：done / 真检出（证人 stop）/ 诚实闸停（API 型黑盒覆盖率等）/
补测耗尽（用例真执行真失败 3 轮）=可测；同指纹熔断且 0 收集 / 容器不可达 / 链路故障=故障。
402/budget 类 =「预算截断」单列（卡 4：不重跑、不计故障，E10 总聚合按截断点分账）。

用法：python scripts/aggregate_e10_multi60.py
输出 markdown 到 data/reports/e10/multi60_matrix_<时间戳>.md（跑批中途也可运行，
未跑格标 pending）。
"""
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "reports" / "e10"
sys.path.insert(0, str(ROOT))

# 根因标签（2026-11-17 卡 2 五族定性；修复后重跑的格会自然落到新标签）
def _tag_error(err: str, collected: tuple[int, int, int] | None) -> str:
    e = err or ""
    if "402" in e or "budget" in e.lower() or "余额" in e:
        return "预算截断"
    if "409" in e or "NotFound" in e or "容器" in e and "不可达" in e:
        return "容器不可达（宿主 Docker 不稳家族）"
    if "同指纹熔断" in e:
        if collected == (0, 0, 0):
            return "熔断·0 收集（故障）"
        return "熔断·有用例执行（部分可测）"
    if "补测轮次预算耗尽" in e:
        return "补测耗尽（用例真执行真失败）"
    if "疑似产品缺陷" in e:
        return "真检出（证人如实上报）"
    if "API 型黑盒覆盖率" in e or "接口级通过数为准" in e:
        return "诚实闸停（黑盒覆盖率口径）"
    if "未通过" in e or "failed" in e.lower():
        return "用例失败收尾"
    return e[:40] or "未知"


def _short_name(path: str) -> str:
    return Path(path).name.replace("（", "(").split("(")[0]


def main() -> int:
    cells = json.loads((OUT / "multi60_results.json").read_text(encoding="utf-8"))
    conn = sqlite3.connect(ROOT / "data" / "test_agent.db")
    conn.row_factory = sqlite3.Row

    rows = []
    for c in cells:
        tid = c.get("tid")
        collected = None
        if tid:
            r = conn.execute(
                """SELECT passed, failed, skipped FROM evaluation_rounds
                   WHERE task_id=? ORDER BY round_no DESC LIMIT 1""", (tid,)).fetchone()
            if r:
                collected = (r["passed"], r["failed"], r["skipped"])
        status = c.get("status") or "pending"
        tag = "done（全链路完成）" if status == "done" else _tag_error(c.get("error"), collected)
        rows.append({"lang": c.get("lang"), "proj": _short_name(c.get("path") or ""),
                     "sample": c.get("sample"), "tid": tid, "status": status,
                     "tag": tag, "collected": collected})

    langs = ("python", "javascript", "typescript", "java", "go", "php")
    by_key = {(r["lang"], r["proj"], r["sample"]): r for r in rows}
    projects: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["proj"] and r["proj"] not in projects[r["lang"]]:
            projects[r["lang"]].append(r["proj"])

    lines = [f"# multi60 六语言环境健康度矩阵（{time.strftime('%F %T')}，"
             f"留档 {len(rows)}/60 格）", ""]
    # 总分账（卡 4 口径：预算截断单列不计故障）
    counts = defaultdict(int)
    for r in rows:
        counts[r["tag"]] += 1
    lines += ["## 总分账", "",
              "| 判定 | 格数 | 可测口径 |", "|---|---|---|"]
    measurable = ("done（全链路完成）", "真检出（证人如实上报）", "诚实闸停（黑盒覆盖率口径）",
                  "补测耗尽（用例真执行真失败）")
    for tag, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {tag} | {n} | {'✅' if tag in measurable else '❌' if '故障' in tag or '不可达' in tag else '—'} |")
    lines.append("")

    # 矩阵：行=语言×项目，列=采样 1/2
    lines += ["## 六语言 × 项目矩阵（列=采样）", ""]
    for lang in langs:
        projs = projects.get(lang, [])
        if not projs:
            continue
        lines.append(f"### {lang}")
        lines += ["| 项目 | 采样 1 | 采样 2 |", "|---|---|---|"]
        for proj in projs:
            cells_row = []
            for s in (1, 2):
                r = by_key.get((lang, proj, s))
                mark = "—（未跑）" if r is None else {
                    "done": "✅ done"}.get(r["status"], f"❌ {r['tag']}") if r else "—"
                if r and r["status"] != "done":
                    mark = "✅" if r["tag"] in measurable else "❌ " + r["tag"]
                elif r:
                    mark = "✅ done"
                cells_row.append(mark if r else "—（未跑）")
            lines.append(f"| {proj} | {cells_row[0]} | {cells_row[1]} |")
        lines.append("")

    # 逐格明细（失败格全列，成功格计数）
    lines += ["## 失败/存疑格逐格明细", ""]
    for r in rows:
        if r["status"] == "done":
            continue
        col = f"{r['collected'][0]}过/{r['collected'][1]}败/{r['collected'][2]}跳" \
            if r["collected"] else "无评估行"
        lines.append(f"- #{r['tid']} {r['lang']}/{r['proj']} 采样{r['sample']}："
                     f"{r['tag']}（末轮 {col}）")
    lines.append("")

    report = OUT / f"multi60_matrix_{time.strftime('%Y%m%d_%H%M')}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"矩阵已落 {report}")
    print("\n".join(lines[7:20]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
