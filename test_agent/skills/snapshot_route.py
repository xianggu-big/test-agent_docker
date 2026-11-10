"""快照路线（任务卡-D+ 路线二，2026-11-10）：observation=dom/process_output, oracle=golden_file。

oracle 谱系补白：判据来自"历史输出"——首次运行录基线，之后结构化逐字段比对；
适用"重构不改行为/序列化输出稳定/页面结构回归"。

**防造假安全边界（本路线的灵魂）**：基线更新必须显式 confirm=True——
禁止自动更新。否则"改基线掩盖回归"就是新一代造假（与证人冻结/禁改断言
同一纪律族）。首跑无基线=只录不判（capture 语义），compare 在录制后进行。

DOM 骨架：html.parser 确定性解析（零浏览器、零 LLM）——静态页面的 DOM 即解析树；
动态页面（运行时派生元素）需 playwright 路径，列为扩展。骨架只取
tag/id/class/文本头，忽略空白与内联样式噪声。

旗标：MTA_SNAPSHOT_ROUTE 默认关——route_plan 按旗标剔除该路线；写函数
（record/update）有执行闸（关=RuntimeError 拒绝），compare/load 只读不设闸
（单测可离线锁口径）。
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from .. import config

_TEXT_HEAD = 80
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col",
              "embed", "source", "track", "wbr"}


# —————————————————————————— 基线存储（data/snapshots/<key>/<name>.json） ——————————————————————————

def _baseline_path(key: str, name: str) -> Path:
    safe = re.sub(r"[^\w.-]", "_", f"{key}/{name}")
    p = Path(config.SNAPSHOT_DIR) / f"{safe}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _require_flag() -> None:
    if not config.SNAPSHOT_ROUTE_ENABLED:
        raise RuntimeError("快照路线未启用（MTA_SNAPSHOT_ROUTE 默认关）——"
                           "基线写入属有副作用操作，须显式开启")


def record_baseline(key: str, name: str, payload) -> Path:
    """首跑录基线；已存在则如实拒绝（录基线是显式动作，静默覆盖=变相改基线）。"""
    _require_flag()
    p = _baseline_path(key, name)
    if p.exists():
        raise RuntimeError(f"基线已存在 {p}——如需更新请走 update_baseline（须显式确认）")
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
                 encoding="utf-8")
    return p


def load_baseline(key: str, name: str):
    p = _baseline_path(key, name)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def update_baseline(key: str, name: str, payload, confirm: bool = False) -> Path:
    """更新基线：必须显式 confirm=True。拒绝自动更新=防"改基线掩盖回归"。"""
    _require_flag()
    if not confirm:
        raise RuntimeError("更新基线必须显式确认（confirm=True）——基线即 oracle，"
                           "静默改基线=改判据掩盖回归（防造假红线）")
    p = _baseline_path(key, name)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
                 encoding="utf-8")
    return p


# —————————————————————————— 结构化 diff（新增/删除/变更 三态 + 字段路径） ——————————————————————————

def compare(baseline, current, path: str = "$",
            diffs: list | None = None) -> dict:
    """递归结构 diff：walk 两棵 JSON 树，产出 {path, kind: added/removed/changed,
    baseline?, current?}。list 按索引对齐（结构性快照的约定：顺序本身是契约）。"""
    diffs = [] if diffs is None else diffs
    if isinstance(baseline, dict) and isinstance(current, dict):
        for k in sorted(set(baseline) - set(current)):
            diffs.append({"path": f"{path}.{k}", "kind": "removed",
                          "baseline": baseline[k]})
        for k in sorted(set(current) - set(baseline)):
            diffs.append({"path": f"{path}.{k}", "kind": "added",
                          "current": current[k]})
        for k in sorted(set(baseline) & set(current)):
            compare(baseline[k], current[k], f"{path}.{k}", diffs)
    elif isinstance(baseline, list) and isinstance(current, list):
        for i in range(max(len(baseline), len(current))):
            if i >= len(baseline):
                diffs.append({"path": f"{path}[{i}]", "kind": "added",
                              "current": current[i]})
            elif i >= len(current):
                diffs.append({"path": f"{path}[{i}]", "kind": "removed",
                              "baseline": baseline[i]})
            else:
                compare(baseline[i], current[i], f"{path}[{i}]", diffs)
    elif baseline != current:
        diffs.append({"path": path, "kind": "changed",
                      "baseline": baseline, "current": current})
    return {"same": not diffs, "diffs": diffs}


# —————————————————————————— DOM 骨架（html.parser 确定性，零浏览器） ——————————————————————————

class _DomWalker(HTMLParser):
    """把 HTML 解析为骨架树：{tag, id, class, text, children}。只留结构与关键属性，
    文本截头 80 字符并折叠空白（快照的契约是结构，不是逐字内容）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: dict = {"tag": "#document", "id": "", "class": [],
                           "text": "", "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        amap = dict(attrs)
        node = {"tag": tag,
                "id": amap.get("id", ""),
                # class 是无序集合（CSS 匹配按集合语义）：存排序串防"重排=假阳性"，
                # 变更在 ...class 单点定位
                "class": " ".join(sorted((amap.get("class") or "").split())),
                "text": "", "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        t = re.sub(r"\s+", " ", data).strip()
        if t and self.stack[-1]["children"] == []:
            cur = self.stack[-1]["text"]
            if len(cur) < _TEXT_HEAD:
                self.stack[-1]["text"] = (cur + " " + t).strip()[:_TEXT_HEAD]


def capture_dom_structure(html: str) -> dict:
    w = _DomWalker()
    w.feed(html)
    w.close()
    return w.root


def compare_dom(baseline_tree: dict, current_tree: dict) -> dict:
    return compare(baseline_tree, current_tree)
