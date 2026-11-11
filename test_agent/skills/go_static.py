"""Go 静态分析层（任务卡-F2 阶段 1 #4 go vet / #2 gocyclo，2026-11-11）。

W4 复杂度热点与 vet 静态闸的 Go 落地（AGENTS Go 缺口清单 ④⑨）：宿主无 Go
工具链 → 在任务容器内 exec（vet 内建；gocyclo/staticcheck 钉版本进镜像层），
宿主侧确定性解析为结构化结果。零 LLM。

接线：analyze 节点（MTA_GO_STATIC 默认关）对 go 项目 best-effort 起短命容器
采集——gocyclo 热点映射进既有 complexity_hotspots 管线（radon 同尺 rank A-F，
plan 提示词风险倾斜零改动复用）；vet 结构化发现落 agent_messages 留痕。
任何失败 fail-open 返回空表（不阻断主链路，与 radon 缺失同纪律）。

go vet 输出形态：`# 包名` 头行 + `file:line[:col]: message`（-json 未用，
文本形态跨版本稳定）；gocyclo 输出：`复杂度 函数 包 文件:行[:列]` +
末行 Average（剔除）。
"""
import logging
import re

log = logging.getLogger(__name__)

_VET_RE = re.compile(r"^([^:\s]+\.go):(\d+)(?::(\d+))?:\s+(.+)$")
_CYC_RE = re.compile(r"^(\d+)\s+(\S+)\s+(\S+)\s+(.+?):(\d+)(?::\d+)?$")
# radon 同尺（skills/complexity.py _HOT_RANKS 的数值版）：A(1-5) B(6-9) C(10-13)
# D(14-19) E(20-24) F(25+)——C 及以上进热点表（风险倾斜口径与 Python 一致）


def complexity_rank(n: int) -> str:
    if n >= 25:
        return "F"
    if n >= 20:
        return "E"
    if n >= 14:
        return "D"
    if n >= 10:
        return "C"
    if n >= 6:
        return "B"
    return "A"


def parse_go_vet(text: str) -> list[dict]:
    """`file:line[:col]: message` 结构化；# 包头行与编译诊断行自然不匹配=剔除。"""
    rows = []
    for ln in (text or "").splitlines():
        m = _VET_RE.match(ln.strip())
        if m:
            rows.append({"file": m.group(1), "line": int(m.group(2)),
                         "col": int(m.group(3)) if m.group(3) else None,
                         "message": m.group(4).strip()})
    return rows


def parse_gocyclo(text: str) -> list[dict]:
    """`复杂度 包 函数 文件:行[:列]` 结构化（列序实测实锤：16 hot Hot hot.go:3——
    第二列=包、第三列=函数）；Average 尾行自然不匹配=剔除。"""
    rows = []
    for ln in (text or "").splitlines():
        m = _CYC_RE.match(ln.strip())
        if m:
            rows.append({"complexity": int(m.group(1)), "pkg": m.group(2),
                         "func": m.group(3), "file": m.group(4),
                         "line": int(m.group(5))})
    return rows


def collect_go_static(runner, container_name: str, ws_pkg: str = "./...") -> dict:
    """容器内采集 vet+gocyclo，宿主解析。fail-open：任何异常=空表（不抛不阻断）。"""
    try:
        container = runner.client.containers.get(container_name)
        vet = runner.exec_in(container, ["go", "vet", ws_pkg],
                             timeout_s=120, workdir="/work")
        cyc = runner.exec_in(container, ["gocyclo", "."],
                             timeout_s=120, workdir="/work")
        return {"vet": parse_go_vet((vet.stderr or "") + "\n" + (vet.stdout or "")),
                "gocyclo": parse_gocyclo(cyc.stdout or "")}
    except Exception as e:                          # noqa: BLE001 —— fail-open
        log.warning("[go-static] 采集失败（%s: %s）——空表如实",
                    type(e).__name__, str(e)[:80])
        return {"vet": [], "gocyclo": []}
