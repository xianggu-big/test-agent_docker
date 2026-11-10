# -*- coding: utf-8 -*-
"""上下文预算管理器 H2/H3（任务卡-H 步骤 3/4）：预算降采样+段落裁剪。

纪律锁：ctx_budget=None 时 build_user_prompt 输出与旧版逐字节一致（云端档零感知）；
降采样排序=入口文件优先；截断必须显式声明（禁止静默裁剪）；裁剪顺序按卡定义。
"""
from test_agent.skills.prompt_library import (build_user_prompt, estimate_tokens,
                                              fit_prompt_sections,
                                              fit_sources_to_budget)

_SRC = [("calc.py", "def add(a, b):\n    return a + b\n")]


# ---------- 估算 ----------

def test_估算系数():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 250) == 100        # chars/2.5


# ---------- fit_sources_to_budget ----------

def _mk(n_files: int, chars: int, prefix: str = "mod") -> list[tuple[str, str]]:
    return [(f"{prefix}{i}.py", "x" * chars) for i in range(n_files)]


def test_预算内不动():
    srcs = _mk(3, 500)
    out, decl = fit_sources_to_budget(srcs, 16000)
    assert out == srcs and decl == ""               # 未超预算：零感知


def test_无预算不动():
    srcs = _mk(3, 5000)
    out, decl = fit_sources_to_budget(srcs, None)
    assert out == srcs and decl == ""


def test_超预算_入口文件优先存活():
    srcs = [("utils_deep.py", "u" * 8000), ("main.py", "m" * 6000),
            ("other.py", "o" * 8000)]
    out, decl = fit_sources_to_budget(srcs, 8192)   # 预算≈(8192-2500)*2.5≈14k 字符
    names = [r for r, _ in out]
    assert "main.py" == names[0]                    # 入口文件排首位
    assert len(decl) > 0 and "截断" in decl
    assert "原共" in decl and "你看到的是部分源码" in decl   # 显式声明（禁静默裁剪）


def test_超预算_声明含丢弃清单():
    srcs = [("main.py", "m" * 3000), ("a.py", "a" * 9000), ("b.py", "b" * 9000)]
    out, decl = fit_sources_to_budget(srcs, 4096)
    assert "a.py" in decl and "b.py" in decl        # 丢弃文件留痕
    kept_chars = sum(len(c) for _, c in out)
    assert kept_chars <= int((4096 - 2500) * 2.5) + 400   # 预算红线（含尾段容差）


def test_全部截断仍给最小源码段():
    srcs = [("big.py", "z" * 50000)]
    out, decl = fit_sources_to_budget(srcs, 4096)
    assert len(out) == 1 and len(out[0][1]) > 400   # 尾段容差：至少留 400 字符
    assert "预算截断" in out[0][0]


# ---------- build_user_prompt 集成 ----------

def test_build_user_prompt_预算内逐字节一致():
    base = build_user_prompt(_SRC, "需求")
    with_budget = build_user_prompt(_SRC, "需求", ctx_budget=16384)
    assert base == with_budget                      # 未超预算：输出不变
    assert "预算截断声明" not in base


def test_build_user_prompt_超预算出现截断声明():
    big = [("big.py", "y" * 60000)]
    p = build_user_prompt(big, "需求", ctx_budget=8192)
    assert "上下文预算截断声明" in p
    assert "部分源码" in p
    # 无预算时同输入不出现声明
    assert "上下文预算截断声明" not in build_user_prompt(big, "需求")


# ---------- fit_prompt_sections（H3 段落裁剪） ----------

def test_段落裁剪_按序砍_留痕():
    prompt = ("HEAD\n" + "## 用例设计方法\n" + "M" * 9000 + "\n"
              + "## 上一轮覆盖缺口\n" + "G" * 9000 + "\n" + "TAIL")
    markers = [("## 用例设计方法", "## 上一轮覆盖缺口"),      # 先砍骨架段
               ("## 上一轮覆盖缺口", "TAIL")]                 # 再砍缺口段
    out, trail = fit_prompt_sections(prompt, 2000, markers)
    assert "用例设计方法" not in out                          # 骨架段被砍
    assert "TAIL" in out                                      # 非目标段存活
    assert "裁段落" in trail


def test_段落裁剪_预算内不动():
    prompt = "HEAD\n## 用例设计方法\nM\nTAIL"
    out, trail = fit_prompt_sections(prompt, 16000, [("## 用例设计方法", "TAIL")])
    assert out == prompt and trail == ""


def test_段落裁剪_全砍仍超_尾部兜底():
    prompt = "X" * 30000
    out, trail = fit_prompt_sections(prompt, 2000, [("NOMARK", "NOMARK")])
    assert len(out) <= 2000 * 2.5 + 1
    assert "头部兜底截断" in trail


def test_段落裁剪_无预算零感知():
    prompt = "X" * 30000
    out, trail = fit_prompt_sections(prompt, None, [("A", "B")])
    assert out == prompt and trail == ""
