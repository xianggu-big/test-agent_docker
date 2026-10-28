"""W4 圈复杂度热点分析（McCabe《A Complexity Measure》TSE 1976 的工程化落点）。

宿主侧对 clone 下来的 workspace 做静态圈复杂度扫描（radon 库 API，确定性代码）：
- 复杂度热点（rank ≥ C）注入 plan 提示词 → 用例设计优先覆盖控制流密集函数
  （风险分级维度：大厂"复杂度/变更频率驱动优先级"的单元层实现）；
- radon 未安装或被测代码语法不可解析时诚实返回空表（不阻断主链路）。
Java 对位工具 PMD 列未来工作（设计文档 §3-W4）。
"""
import logging
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_HOT_RANKS = {"C", "D", "E", "F"}   # radon 六级制：A(1-5) B(6-9) C(10-13) D(14-19) E(20-24) F(25+)


def complexity_hotspots(ws: Path, top: int = 5) -> list[dict]:
    """扫描 workspace 的 Python 源码，返回圈复杂度热点 [{file, name, complexity, rank, lineno}]。

    只统计模块级函数与方法（radon cc_visit 的 Function/Method 块），跳过测试文件与
    噪声目录；按复杂度降序取 top N。radon 缺失/全部低复杂度 → 空表（诚实边界）。
    """
    try:
        from radon.complexity import cc_visit, cc_rank
    except ImportError:
        log.warning("radon 未安装，圈复杂度热点分析跳过（pip install radon）")
        return []
    out: list[dict] = []
    for p in sorted(Path(ws).rglob("*.py")):
        if any(part in config.SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        try:
            code = p.read_text(encoding="utf-8", errors="ignore")
            blocks = cc_visit(code)
        except (OSError, SyntaxError):
            continue
        rel = p.relative_to(ws).as_posix()
        for b in blocks:
            rank = cc_rank(b.complexity)
            if rank in _HOT_RANKS:
                out.append({"file": rel, "name": getattr(b, "name", "?"),
                            "complexity": int(b.complexity), "rank": rank,
                            "lineno": int(getattr(b, "lineno", 0))})
    out.sort(key=lambda x: -x["complexity"])
    return out[:top]
