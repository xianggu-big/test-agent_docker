# -*- coding: utf-8 -*-
"""Go 工具链版本闸 + 零事件失败证据合成（2026-11-08 任务 678/679 实锤）。

病根：gin-master 的 go.mod 声明 `go 1.26.0`，执行镜像 mta-test:go122 只有 go1.22 且
GOTOOLCHAIN=local 拒绝自动下载 → `go test` 秒失败，输出仅一行散行、零 JSON 事件 →
evaluate 读到 0 通过 0 失败/覆盖率 N/A 判 env → repair_env 三轮原地打转熔断
（两次同指纹 8b057ddf）。版本是环境事实，repair 修不动。

修复两件（用户的 A+C 方案）：
A=镜像升级 golang:1.22→1.26（config.IMAGE_GO=mta-test:go126 + recipe 池补 1.24/1.25/1.26）；
C=①provision 开考前确定性版本闸（不符诚实 stop，不烧修复轮次）
  ②go_adapter 零事件失败必须合成 ERROR 行（否则错误详情蒸发、归因链断）。
"""
from pathlib import Path

from test_agent.skills.go_adapter import (go_test_json_to_pytest_text,
                                          go_test_json_to_rows)
from test_agent.skills.stack_detect import (check_go_toolchain, parse_go_bin_version,
                                            parse_go_mod_version)


# ---------- 版本解析 ----------

def test_解析go_mod版本():
    assert parse_go_mod_version("module m\n\ngo 1.26.0\n") == (1, 26)
    assert parse_go_mod_version("go 1.22\n") == (1, 22)
    assert parse_go_mod_version("module m\n") is None          # 无 go 指令
    assert parse_go_mod_version("") is None


def test_解析go_bin版本():
    assert parse_go_bin_version("go version go1.26.1 linux/amd64") == (1, 26)
    assert parse_go_bin_version("go version go1.22.12 linux/amd64") == (1, 22)
    assert parse_go_bin_version("") is None


# ---------- 闸判定（678/679 重放） ----------

def _mk_ws(tmp_path: Path, gomod: str) -> Path:
    (tmp_path / "go.mod").write_text(gomod, encoding="utf-8")
    return tmp_path


def test_678重放_版本不足诚实拦(tmp_path):
    ws = _mk_ws(tmp_path, "module github.com/gin-gonic/gin\n\ngo 1.26.0\n")
    r = check_go_toolchain(ws, "go version go1.22.12 linux/amd64")
    assert r["ok"] is False
    assert r["required"] == (1, 26) and "1.22" in r["actual"]
    # 可行动文案三要素：所需版本 / 镜像实况 / 处理办法
    assert "go>=1.26" in r["reason"] and "go1.22" in r["reason"]
    assert "IMAGE_GO" in r["reason"] and "如实终止" in r["reason"]


def test_版本足够放行(tmp_path):
    ws = _mk_ws(tmp_path, "module m\n\ngo 1.22\n")
    r = check_go_toolchain(ws, "go version go1.26.1 linux/amd64")
    assert r["ok"] is True and r["reason"] == ""


def test_同minor放行(tmp_path):
    # Go 兼容单元是 minor：要求 1.26.0、镜像是 1.26.3 → 放行
    ws = _mk_ws(tmp_path, "go 1.26.0\n")
    assert check_go_toolchain(ws, "go version go1.26.3 linux/amd64")["ok"] is True


def test_无go_mod不拦(tmp_path):
    r = check_go_toolchain(tmp_path, "go version go1.22.0 linux/amd64")
    assert r["ok"] is True and r["required"] is None


def test_版本不可解析fail_open(tmp_path):
    # 解析不出（异常形态）→ 放行交给执行链路，闸自身绝不引入新失败面
    ws = _mk_ws(tmp_path, "module m\n")
    assert check_go_toolchain(ws, "garbage output")["ok"] is True


# ---------- 零事件证据合成（C②） ----------

_TOOLCHAIN_LINE = ("go: go.mod requires go >= 1.26.0 "
                   "(running go 1.22.12; GOTOOLCHAIN=local)")


def test_678重放_零事件合成错误行():
    rows = go_test_json_to_rows(_TOOLCHAIN_LINE)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "error" and r["name"] == "[no test events]"
    assert "1.26.0" in r["message"] and "GOTOOLCHAIN" in r["message"]


def test_零事件进pytest文本且计失败():
    text = go_test_json_to_pytest_text(_TOOLCHAIN_LINE)
    assert "ERROR go::[no test events]" in text
    assert "requires go >= 1.26.0" in text          # 详情不蒸发
    assert "1 failed" in text                        # evaluate 能读到失败而非 0/0


def test_正常事件流不受影响():
    ev = ('{"Action":"pass","Package":"p","Test":"TestA","Elapsed":0.01}\n')
    rows = go_test_json_to_rows(ev)
    assert len(rows) == 1 and rows[0]["status"] == "passed"
    assert "no test events" not in go_test_json_to_pytest_text(ev)


def test_空输出不合成():
    assert go_test_json_to_rows("") == []
    assert "[no test events]" not in go_test_json_to_pytest_text("")


# ---------- A：镜像与 recipe 池一致性 ----------

def test_镜像常量与构建上下文一致():
    from test_agent import config
    assert config.IMAGE_GO == "mta-test:go126"
    assert config.IMAGE_GO in config.IMAGE_BUILD_CTX
    assert config.EXEC_LANGUAGES["go"] == config.IMAGE_GO


def test_recipe池含go126且按数字排序():
    from test_agent import config
    pool = config.RECIPE_IMAGE_POOL["go"]
    assert pool["1.26"] == "golang:1.26"
    # 池内版本键按数字元组比大小（字典序会把 3.9 当比 3.13 大——历史实锤坑）
    newest = max(pool, key=lambda v: tuple(int(x) for x in v.split(".")))
    assert newest == "1.26"


# ---------- 681 实锤：pkg [pkg.test] 包名键不匹配（证据蒸发第二变种） ----------

def test_681重放_测试包后缀键认领():
    # go test 把测试编译错误的 Package 标成 "pkg [pkg.test]"，[build failed] 行是裸 pkg。
    # 用 json.dumps 构造事件（手写 JSON 转义在 heredoc 里被吃过一次，见 658 族教训）
    import json
    js = "\n".join(json.dumps(ev) for ev in [
        {"Action": "output", "Package": "example.com/app",
         "Output": "FAIL example.com/app [build failed]\n"},
        {"Action": "output", "Package": "example.com/app [example.com/app.test]",
         "Output": "./TestGenR1_test.go:21:26: undefined: newFakeRepo\n"},
    ])
    rows = go_test_json_to_rows(js)
    err = [r for r in rows if r["name"] == "[build failed]"][0]
    assert "undefined: newFakeRepo" in err["message"]      # 不再裸 "build failed"
    text = go_test_json_to_pytest_text(js)
    assert "undefined: newFakeRepo" in text
