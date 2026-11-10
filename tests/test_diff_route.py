# -*- coding: utf-8 -*-
"""CLI 差分路线（任务卡-D+ 路线一）单元测试。

口径锁：归一化层不误报（时间戳/路径/浮点/时长/hex id 占位化，裸整数保留=
CLI 输出的真实载荷）；双向判定工程化=参照物双跑确定性校验（参照自身不稳→
ref_unstable 如实降级，不冒充缺陷信号）；旗标关=执行闸拒绝+路线剔除。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.diff_route import (classify_pair, normalize_output,
                                          run_cli_diff)


# ---------- 归一化层 ----------

def test_归一化_时间戳路径浮点时长hexid():
    a = "report at 2026-11-10T22:33:01 from D:\\work\\nums.txt ratio=0.850 took 132ms id=a1b2c3d4e5f6"
    b = "report at 2027-01-02T08:10:59 from /tmp/x/other.txt ratio=0.777 took 98ms id=ffff00001111"
    assert normalize_output(a) == normalize_output(b)


def test_归一化_裸整数保留_真实差异不被吞():
    a = "processed=7"
    b = "processed=8"
    assert normalize_output(a) != normalize_output(b)   # 整数是 CLI 输出的真实载荷


def test_归一化_无差异文本原样():
    assert normalize_output("plain line\nanother") == "plain line\nanother"


# ---------- 双向判定（工程化：参照物确定性双跑） ----------

def test_classify_pair_同():
    assert classify_pair(0, "out", 0, "out") == "same"
    assert classify_pair(0, "out@2026-11-10T22:00:00", 0, "out@2027-01-01T00:00:00") == "same"


def test_classify_pair_stdout差异():
    assert classify_pair(0, "processed=7", 0, "processed=8") == "stdout_diff"


def test_classify_pair_退出码差异():
    assert classify_pair(1, "same out", 0, "same out") == "exit_diff"


def test_classify_pair_全差异():
    assert classify_pair(2, "a", 1, "b") == "both_diff"


# ---------- run_cli_diff（注入假 runner，不跑真进程） ----------

def _fake_runner(script: dict):
    """构造 runner：ws 路径名 → 固定输出（含噪声字段验证归一化）。"""
    def run(ws, args):
        key = Path(ws).name
        out = script[key]
        return 0, out
    return run


def test_run_cli_diff_检出真实差异_零假阳性(tmp_path):
    cur = tmp_path / "cur"; cur.mkdir()
    ref = tmp_path / "ref"; ref.mkdir()
    noise = ("generated at 2026-11-10T22:00:00 workspace={ws} id=abc123def456\n"
             "processed={n} ratio=0.500 took 120ms\n")
    script = {
        "cur": (noise.format(ws="/d/work/cur", n=7) + "processed=7\n"),
        "ref": (noise.format(ws="/d/work/ref", n=8) + "processed=8\n"),
    }
    rep = run_cli_diff(cur, ref, [["run"]], runner=_fake_runner(script),
                       enabled=True)
    assert rep["cases"][0]["kind"] == "stdout_diff"
    # 归一化后 cur/ref 的噪声行逐字相同——差异只在裸整数行
    c = rep["cases"][0]
    assert c["cur_norm"] != c["ref_norm"]
    assert "<TS>" in c["cur_norm"] and "<PATH>" in c["cur_norm"] and "<ID>" in c["cur_norm"]


def test_run_cli_diff_参照不稳_如实降级不冒充缺陷(tmp_path):
    cur = tmp_path / "cur"; cur.mkdir()
    ref = tmp_path / "ref"; ref.mkdir()
    calls = {"ref": 0}

    def unstable_runner(ws, args):
        if Path(ws).name == "ref":
            calls["ref"] += 1
            return 0, f"tick={calls['ref']}"       # 参照两次跑输出不同=自身不确定
        return 0, "stable out"

    rep = run_cli_diff(cur, ref, [["run"]], runner=unstable_runner, enabled=True)
    assert rep["cases"][0]["kind"] == "ref_unstable"   # 不判定为缺陷信号
    assert rep["summary"]["ref_unstable"] == 1


def test_run_cli_diff_旗标关_拒绝执行(tmp_path):
    cur = tmp_path / "a"; cur.mkdir()
    ref = tmp_path / "b"; ref.mkdir()
    with pytest.raises(RuntimeError, match="MTA_DIFF_ROUTE"):
        run_cli_diff(cur, ref, [["run"]], runner=lambda w, a: (0, "x"), enabled=False)


def test_run_cli_diff_全同_零假阳性(tmp_path):
    cur = tmp_path / "a"; cur.mkdir()
    ref = tmp_path / "b"; ref.mkdir()
    out = "same line\nratio=0.123 at 2026-11-10T01:02:03 path=C:\\x\\y id=001122334455"
    script = {"a": out, "b": "same line\nratio=0.987 at 2027-12-12T12:12:12 path=/z/w id=ffaabbccddee"}
    rep = run_cli_diff(cur, ref, [["r"]], runner=_fake_runner(script), enabled=True)
    assert rep["cases"][0]["kind"] == "same"
    assert rep["summary"]["diff_introduced"] == 0
