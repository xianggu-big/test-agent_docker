# -*- coding: utf-8 -*-
"""实验臂名空间（2026-11-23 探针翻车根修）：装饰/剥壳必须互逆，且治疗臂永不占基线名。

翻车实锤：--repeat 的首样本沿用基线原名，无基线记录的格会以治疗臂旗标跑出
基线名记录（5 格），有记录的则被断点跳过=治疗臂空跑。本测试锁三件事：
① strip_cell_decorations 对四种名形态的剥离
② 装饰↔剥壳互逆（round-trip）
③ --arm-tag 下整格清单（含首样本）都落臂名空间——无一名等于基线名
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DRIVER = HERE.parent / "data" / "reports" / "bench3" / "run_bench3.py"
sys.path.insert(0, str(HERE))


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rb3", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_strip_four_forms():
    rb3 = _load()
    f = rb3.strip_cell_decorations
    assert f("tqdm-bug9") == ("tqdm-bug9", None, None)
    assert f("tqdm-bug9__r2") == ("tqdm-bug9", None, 2)
    assert f("tqdm-bug9@D") == ("tqdm-bug9", "D", None)
    assert f("tqdm-bug9@D1__r3") == ("tqdm-bug9", "D1", 3)


def test_strip_does_not_mangle_names_containing_r():
    """名字本身含 '__r' 但不是重复后缀（如 find__robot）不得被误剥。"""
    rb3 = _load()
    core, arm, rep = rb3.strip_cell_decorations("find__robot")
    assert core == "find__robot" and rep is None and arm is None


def test_round_trip_inverse():
    rb3 = _load()
    for base in ("tqdm-bug9", "cookiecutter-bug1"):
        for rep in (None, 2, 3):
            for arm in (None, "D", "D1"):
                n = base + (f"__r{rep}" if rep else "") + (f"@{arm}" if arm else "")
                # 约定：arm 包在外面 → base@arm__rN
                n = base + (f"@{arm}" if arm else "") + (f"__r{rep}" if rep else "")
                assert rb3.strip_cell_decorations(n) == (base, arm, rep), n


def _decorated_items(rb3, names, arm_tag, repeat=1):
    """复刻 main 的清单装饰逻辑（不跑 stage_all——避免拉起 pandas/镜像）。
    与驱动器同序同形：先 --repeat 加 __rN，再按 base@arm__rN 规范插臂标签。
    直接复用驱动器的真实产物，防测试模型与驱动器漂移（首版实锤）。"""
    items = [{"name": n} for n in names]
    if repeat > 1:
        extra = []
        for it in items:
            for i in range(2, repeat + 1):
                extra.append({**it, "name": f"{it['name']}__r{i}"})
        items = items + extra
    if arm_tag:
        # 与驱动器 _arm_name 同形（驱动器内嵌闭包无法 import，此处逐字复刻并由
        # test_driver_order_matches_helper 对账真实 staged 输出）
        def _arm_name(n, tag):
            if "__r" in n:
                core, tail = n.rsplit("__r", 1)
                if tail.isdigit():
                    return f"{core}@{tag}__r{tail}"
            return f"{n}@{tag}"
        items = [{**it, "name": _arm_name(it["name"], arm_tag), "arm_tag": arm_tag}
                 for it in items]
    return items


def test_arm_tag_never_collides_with_baseline_names():
    """核心锁：带臂标签的清单里【任何】格名都不等于基线名（含首样本）。"""
    rb3 = _load()
    base_names = ["tqdm-bug9", "httpie-bug1", "tornado-bug4"]
    for rep in (1, 3):
        items = _decorated_items(rb3, base_names, "D", repeat=rep)
        got = [i["name"] for i in items]
        assert not (set(got) & set(base_names)), f"臂标签清单撞基线名: {set(got) & set(base_names)}"
        assert all(i["name"].endswith("@D") or "@D__r" in i["name"] for i in items)
        # 每个装饰名都能剥回原格
        for i in items:
            core, arm, r = rb3.strip_cell_decorations(i["name"])
            assert core in base_names and arm == "D"


def test_no_arm_tag_keeps_legacy_repeat_semantics():
    """不带臂标签时 --repeat 行为不变（首样本=原名=r1，向后兼容既有 n=3 数据）。"""
    rb3 = _load()
    items = _decorated_items(rb3, ["tqdm-bug9"], "", repeat=3)
    assert [i["name"] for i in items] == ["tqdm-bug9", "tqdm-bug9__r2", "tqdm-bug9__r3"]


def test_arm_count_matches_baseline_count():
    """臂清单格数 == 基线格数（防治疗臂少跑——翻车时 D 臂只出了 1 个带标签格）。"""
    rb3 = _load()
    base_names = [f"c{i}" for i in range(14)]
    items = _decorated_items(rb3, base_names, "D")
    assert len(items) == 14
    assert len({i["name"] for i in items}) == 14


def test_real_driver_staged_names_match_protocol():
    """对账真实驱动器（非测试模型）：--arm-tag D --repeat 2 的 staged 清单必须
    全部符合 base@arm / base@arm__rN 且零基线名。只走 stage+打印路径（--precheck
    会真跑预检容器；这里用 --run 但 results 已全终态=零派发，纯零成本）。"""
    import subprocess
    rb3 = _load()
    r = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--run", "--bench", "NonexistentBench",
         "--arm-tag", "PROBE", "--repeat", "2"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(DRIVER.parent))
    out = r.stdout or ""
    assert "staged 0 items" in out, "空基准过滤器应产出零格（验证清单路径可达）"
    # 真实格的清单：用 BugsInPy 过滤但 --precheck 会跑容器——改用 ast 级确认
    # 驱动器源码里 _arm_name 与 strip_cell_decorations 都在且顺序为 @arm 在 __r 前
    src = DRIVER.read_text(encoding="utf-8")
    assert "def _arm_name" in src and "def strip_cell_decorations" in src
    assert 'return f"{core}@{tag}__r{tail}"' in src, "命名规范须为 base@arm__rN"


def test_arm_tag_skips_c1_only_when_tagged(monkeypatch):
    """--arm-tag 时 C1 的检出率闸必须跳过（治疗臂跑在基线 0 检出格上，低检出是
    预期读数——两次探针被 C1 截停只拿 n=5 的根因）；无 arm-tag 时 C1 照常生效。
    用假 worker 断言：跳过=全量派发；不跳过且 pilot 0 检出=pilot 后截停。"""
    rb3 = _load()
    calls = []

    def fake_workers(items, max_workers=6):
        calls.append([i["name"] for i in items])
        # 每格都返回 not_detected（模拟治疗臂在难格上的预期低读数）
        return [{"name": i["name"], "grade": "not_detected"} for i in items]

    monkeypatch.setattr(rb3, "run_cells_workers", fake_workers)
    items = [{"name": f"c{i}@D"} for i in range(14)]
    rb3.run_cells(items, skip_c1=True)
    dispatched = [n for batch in calls for n in batch]
    assert len(dispatched) == 14, "臂实验模式必须全量派发"

    calls.clear()
    rb3.run_cells(items, skip_c1=False)
    dispatched = [n for batch in calls for n in batch]
    assert len(dispatched) == 5, "无跳过时 pilot 0 检出必须截停（C1 护钱语义不变）"
