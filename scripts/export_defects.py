"""缺陷交接导出 CLI：把任务里「测试失败且用例有依据」的行整理成两份交付物。

用法：
    python scripts/export_defects.py --task 366                 # 默认输出 data/reports/handoff/
    python scripts/export_defects.py --task 366 --out D:/tmp    # 自定义输出目录

产物（Markdown，零 LLM，全部确定性导出）：
- task_<tid>_缺陷报告单.md —— 标准缺陷单（元信息/汇总/逐条缺陷/局限免责）
- task_<tid>_AI修复提示词.md —— 整段复制给任意 AI 即可发起修复（带护栏）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_agent.skills.defect_handoff import export_handoff  # noqa: E402
from test_agent.store.db import get_conn  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导出疑似产品缺陷的交接交付物")
    ap.add_argument("--task", type=int, required=True, help="任务 id")
    ap.add_argument("--out", default="data/reports/handoff", help="输出目录")
    args = ap.parse_args()

    conn = get_conn()
    try:
        result = export_handoff(conn, args.task, Path(args.out))
    finally:
        conn.close()
    if result is None:
        print(f"任务 {args.task}：无疑似产品缺陷（generated 用例无失败行），无需交接")
        return 0
    print(f"任务 {args.task}：导出 {result['defects']} 条疑似产品缺陷"
          f"（高置信 {result['high']} 条）")
    print(f"  缺陷报告单：{result['report_path']}")
    print(f"  AI 修复提示词：{result['prompt_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
