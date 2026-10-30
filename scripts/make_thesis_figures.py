# -*- coding: utf-8 -*-
"""论文插图生成器：图 3-1 / 4-1 / 4-2（图 6-1 用 gen_charts 真机产物拷贝）。
用法：python scripts/make_thesis_figures.py [输出目录]
默认输出到 C:/Users/xianggu/Desktop/毕设/论文/图/
"""
import sys, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

OUT = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/xianggu/Desktop/毕设/论文/图"
os.makedirs(OUT, exist_ok=True)

EDGE = "#37474F"; FACE = "#FFFFFF"; HEAD = "#ECEFF1"; ACC = "#90A4AE"; DASH = "#546E7A"

def box(ax, x, y, w, h, text, fc=FACE, fs=10, lw=1.2, style="round,pad=0.02,rounding_size=0.06"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=style, fc=fc, ec=EDGE, lw=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, color="#212121", linespacing=1.5)

def arrow(ax, x1, y1, x2, y2, style="-", color=EDGE, lw=1.4, rad=0.0, label=None, lfs=8.5, lox=0, loy=0, lc=None):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=13,
                                 lw=lw, color=color, linestyle=style,
                                 connectionstyle=f"arc3,rad={rad}"))
    if label:
        ax.text((x1 + x2) / 2 + lox, (y1 + y2) / 2 + loy, label, ha="center", va="center",
                fontsize=lfs, color=lc or color)

# ---------------- 图 3-1 四级接力业务流转 ----------------
def fig3_1():
    fig, ax = plt.subplots(figsize=(12.4, 4.6))
    ax.set_xlim(0, 12.4); ax.set_ylim(0, 4.6); ax.axis("off")
    box(ax, 0.15, 1.7, 1.5, 1.3, "用户输入\n仓库＋\n自然语言需求", fc=HEAD, fs=9.5)
    levels = [
        (2.1, "L1 单元级\n（深度自动化）", "九节点闭环：分析→计划(RTM)\n→双轨生成→执行→归因补测\n→报告→审查", "全自动闭环"),
        (5.0, "L2 集成级\n（深度自动化）", "端点契约＋调用链\n契约/顺序/需求三类用例\n两级断言", "全自动接力"),
        (7.9, "L3 系统级\n（半自动）", "业务流场景生成\n关键路径冒烟\n（端点静态闸＋引用链）", "冒烟自动判定\n失败人工介入"),
        (10.8, "L4 验收级\n（辅助）", "验收场景清单\nGiven/When/Then", "人工确认\n→可执行回归资产"),
    ]
    for x, title, body, note in levels:
        box(ax, x, 1.45, 1.5, 1.9, "", fc=FACE)
        ax.text(x + 0.75, 3.0, title, ha="center", va="center", fontsize=9.5, weight="bold", color="#263238")
        ax.text(x + 0.75, 2.15, body, ha="center", va="center", fontsize=7.6, color="#37474F", linespacing=1.55)
        ax.text(x + 0.75, 1.12, note, ha="center", va="center", fontsize=7.6, color=DASH, linespacing=1.4)
    arrow(ax, 1.65, 2.4, 2.08, 2.4)
    for x in (3.62, 6.52, 9.42):
        arrow(ax, x, 2.4, x + 0.42, 2.4)
        ax.text(x + 0.21, 2.62, "级别编排器\n串行接力", ha="center", fontsize=7.2, color=DASH)
    box(ax, 4.3, 3.85, 4.6, 0.55, "跨级评估汇聚 → 总指标包 → 总报告 → 总审查（证据链落库）", fc=HEAD, fs=9)
    for x in (2.85, 5.75, 8.65, 11.55):
        arrow(ax, x, 3.37, x, 3.83, style="--", color=ACC, lw=1.0)
    ax.text(0.18, 0.35, "注：L1—L4 为测试级别编号；各级台账以跨级追溯键（trace_from）链接，支持高层失败向低层定位。",
            fontsize=8, color="#607D8B")
    fig.tight_layout(); fig.savefig(f"{OUT}/图3-1_四级接力业务流转.png", dpi=220); plt.close(fig)

# ---------------- 图 4-1 五层总体架构 ----------------
def fig4_1():
    # Loop-5（TODO-11）：原 ylim 5.6 时 caption(y=5.32) 撞界面层框顶(y=5.47)——
    # 画布扩到 6.05，caption 移到框顶之上的独立行
    fig, ax = plt.subplots(figsize=(11.6, 6.05))
    ax.set_xlim(0, 11.6); ax.set_ylim(0, 6.05); ax.axis("off")
    layers = [
        (4.55, "界面层", "任务管理 ｜ 脚本工作台（人工确认门） ｜ 报告页（RTM 矩阵·图表·审查徽章） ｜ 环境状态", "人机协作"),
        (3.45, "编排层", "LangGraph 固定图九节点 ｜ 级别编排器（L1→L2→L3 串行接力） ｜ 条件边（归因分流·三停）", "测试四阶段编排"),
        (2.35, "技能层", "技能注册表（输入模式/风险分级/超时） ｜ fail-closed 确认门 ｜ 调用审计台账", "工具调用可追溯"),
        (1.25, "沙箱层", "Docker 常驻容器＋exec 驱动 ｜ 镜像注册表与按需构建 ｜ 资源限额·超时重建", "隔离·可复现"),
        (0.15, "数据层", "SQLite 22 表五域：任务域 ｜ 用例域 ｜ 执行域 ｜ 评估域 ｜ 成本域", "过程证据可追溯"),
    ]
    for y, name, body, note in layers:
        box(ax, 2.3, y, 7.6, 0.92, "", fc=FACE)
        box(ax, 0.35, y, 1.75, 0.92, name, fc=HEAD, fs=11)
        ax.text(6.1, y + 0.46, body, ha="center", va="center", fontsize=8.8, color="#37474F")
        ax.text(10.9, y + 0.46, note, ha="center", va="center", fontsize=8.6, color=DASH)
    for y in (3.45, 2.35, 1.25, 0.15):
        for x in (3.6, 6.1, 8.6):
            arrow(ax, x, y + 0.92, x, y + 1.32, style="-", color=ACC, lw=0.9)
    ax.text(0.18, 5.82, "自上而下：请求经界面层进入编排层，动作经技能层治理后在沙箱层执行，全过程证据落数据层。",
            fontsize=8.4, color="#607D8B")
    fig.tight_layout(); fig.savefig(f"{OUT}/图4-1_五层总体架构.png", dpi=220); plt.close(fig)

# ---------------- 图 4-2 单元级九节点编排与条件边 ----------------
def fig4_2():
    fig, ax = plt.subplots(figsize=(12.6, 4.9))
    ax.set_xlim(0, 12.6); ax.set_ylim(0, 4.9); ax.axis("off")
    nodes = ["克隆", "分析", "计划", "备环境", "生成", "执行", "评估", "报告", "审查"]
    w, h, gap, y0 = 1.06, 0.62, 0.28, 2.9
    xs = [0.25 + i * (w + gap) for i in range(9)]
    for x, n in zip(xs, nodes):
        box(ax, x, y0, w, h, n, fc=HEAD if n in ("评估", "审查") else FACE, fs=10)
    for i in range(8):
        arrow(ax, xs[i] + w, y0 + h / 2, xs[i + 1], y0 + h / 2)
    ax.text(6.1, 4.55, "主链（固定图）", fontsize=9, color="#455A64", ha="center")
    ex, gx, px = xs[6] + w / 2, xs[4] + w / 2, xs[3] + w / 2
    ylow = 1.72
    # 回炉走线：评估底部→下方水平线→目标框底部（折线+端头箭头，避免穿越主链）
    for tx, lab, lx, dy in ((gx, "repair_gen（用例缺陷：剪除/重写）", 7.9, -0.26),
                            (px, "repair_env（环境失败：修环境）", 2.9, 0.16)):
        ax.plot([ex, ex], [y0 - 0.04, ylow], ls="--", color=DASH, lw=1.3)
        ax.plot([ex, tx], [ylow, ylow], ls="--", color=DASH, lw=1.3)
        ax.add_patch(FancyArrowPatch((tx, ylow), (tx, y0 - 0.06), arrowstyle="-|>",
                                     mutation_scale=12, lw=1.3, color=DASH, linestyle="--"))
        ax.text(lx, ylow + dy, lab, ha="center", fontsize=8.3, color=DASH)
    ax.text(6.15, 0.62, "三停：准出达标 ｜ 同指纹熔断（≥2） ｜ 轮次预算耗尽（3）　——　证人失败不计指纹；仅剩证人按覆盖率分流",
            ha="center", fontsize=8.2, color="#37474F",
            bbox=dict(boxstyle="round,pad=0.3", fc="#ECEFF1", ec=ACC, lw=0.8))
    rx, rp = xs[8] + w / 2, xs[7] + w / 2
    arrow(ax, rx, y0 + h + 0.04, rp, y0 + h + 0.04, style="--", rad=-0.35, color="#78909C",
          label="reject→受控重写一次（必收敛）", lfs=8, loy=0.42, lox=0.1)
    arrow(ax, xs[1] + w / 2, y0 + h + 0.04, rp - 0.2, y0 + h + 0.04, style=":", rad=-0.16, color="#90A4AE",
          label="主语言不支持→诚实短路", lfs=8, loy=0.62, lox=-1.5)
    ax.text(0.15, 0.22, "注：实线为主链；虚线为条件边；评估节点为判定中枢，其判定与轮次轨迹逐轮落库。",
            fontsize=8, color="#607D8B")
    fig.tight_layout(); fig.savefig(f"{OUT}/图4-2_单元级九节点编排与条件边.png", dpi=220); plt.close(fig)

if __name__ == "__main__":
    fig3_1(); fig4_1(); fig4_2()
    print("figures written to", OUT)
