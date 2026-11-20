"""Agent Loop 统一授权层（2026-11-20 设计稿 §3.2；宏观稿 §6.3 落地）。

authorize 是决策循环里唯一执行入口前的硬闸。核心区分（宏观稿最重要的设计）：
- **硬约束**（本模块直接拒绝）：能力不存在/schema 不符/密钥键/模式矩阵/预算/重复——
  模型可申请但不能绕过；
- **软启发式**（record_soft_override 只留痕不生效）：粗糙正则/固定优先级——为阶段 2
  "模型凭探测证据申请覆盖"预埋数据面。

模式矩阵（§6.3）：normal=R0/R1 自动；verify_mode=1=仅 R0（冻结只读）；
static_only=仅 R0。R3/R4 永不自动（MVP 菜单也不含）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from . import capabilities as caps_mod
from .capabilities import TaskCtx, action_fingerprint

log = logging.getLogger(__name__)

_RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    risk: str = ""
    stop: bool = False          # True=停止循环（预算/重复），非单次拒绝
    stop_reason: str = ""


def authorize(action: dict, ctx: TaskCtx, *, menu: list[dict] | None = None,
              seen_fps: dict | None = None, budget_used: dict | None = None) -> Decision:
    """七道硬闸（顺序=拒绝优先级）。menu/seen_fps/budget_used 由 tool_loop 传入——
    本函数无状态，可独立单测。"""
    if menu is None:
        menu = caps_mod.capability_menu(ctx)
    if seen_fps is None:
        seen_fps = {}
    if budget_used is None:
        budget_used = {}

    # 1) 协议闸：action 必须是 call/finish 两形态之一
    if not isinstance(action, dict) or action.get("action") not in ("call", "finish"):
        return Decision(False, reason="协议违规：action 必须是 call 或 finish")
    if action["action"] == "finish":
        return Decision(True, reason="finish 透传")
    cap_id = action.get("capability")
    args = action.get("args")
    if not cap_id or not isinstance(args, dict):
        return Decision(False, reason="call 缺 capability 或 args（协议违规）")

    # 2) 存在闸：能力在菜单内（未注册/语言不兼容/旗标关=不可见即不可用）
    cap = caps_mod.find_capability(ctx, cap_id)
    if cap is None or not any(m["id"] == cap_id for m in menu):
        return Decision(False, reason=f"能力不在菜单：{cap_id}")

    # 3) schema 闸：复用 0-B 校验器（严格模式——未知键拒）
    from .registry import _validate_args
    err = _validate_args(cap.inputs_schema, args)
    if err:
        return Decision(False, reason=f"参数校验拒绝：{err}")

    # 4) 密钥闸（0-D）：args 任何键命中黑名单正则 → 拒绝；permissions.secrets 恒空校验
    for k in args:
        if caps_mod._SECRET_KEY_RE.search(str(k)):
            return Decision(False, reason=f"密钥键名越界：{k}")
    if cap.permissions.get("secrets"):
        return Decision(False, reason="能力声明了 secrets 权限（MVP 恒空，配置异常）")

    # 5) 模式矩阵闸（§6.3）：verify 冻结=仅 R0；static=仅 R0；normal=R0/R1
    mode = _task_mode(ctx)
    if mode == "verify" and _RISK_ORDER.get(cap.risk, 9) > _RISK_ORDER["R0"]:
        return Decision(False, reason=f"验证冻结任务仅允许 R0（{cap_id}={cap.risk}）")
    if mode == "static" and _RISK_ORDER.get(cap.risk, 9) > _RISK_ORDER["R0"]:
        return Decision(False, reason=f"静态模式仅允许 R0（{cap_id}={cap.risk}）")
    if _RISK_ORDER.get(cap.risk, 9) >= _RISK_ORDER["R2"]:
        return Decision(False, reason=f"{cap.risk} 不在 MVP 自动范围（R3/R4 需人工，R2 归包 2）")

    # 6) 预算闸：任一触顶=stop（不是单次拒绝——是循环停止条件）；
    #    上限缺省（<=0）=不启用该维度（authorize 无 budget_used 调用形态=纯闸单测）
    for dim, label in (("steps", "步数"), ("tokens", "token"), ("seconds", "时间")):
        mx = budget_used.get(f"max_{dim}") or 0
        if mx and budget_used.get(dim, 0) >= mx:
            return Decision(True, reason=f"{label}预算耗尽", stop=True, stop_reason="budget")

    # 7) 重复闸：同指纹已出现 ≥2 次 → stop("repeated_action")（防死循环）
    fp = action_fingerprint(cap_id, args)
    if seen_fps.get(fp, 0) >= 2:
        return Decision(True, reason="同一动作重复超过阈值", stop=True,
                        stop_reason="repeated_action")

    return Decision(True, reason="authorized", risk=cap.risk)


def _task_mode(ctx: TaskCtx) -> str:
    snap = ctx.state_snapshot or {}
    if snap.get("verify_mode") == 1:
        return "verify"
    if snap.get("static_only"):
        return "static"
    return "normal"


def record_soft_override(ctx: TaskCtx, heuristic: str, evidence: str) -> None:
    """软启发式通道（MVP 只留痕不生效）：模型凭探测证据申请覆盖粗糙启发式——
    写 agent_messages 供人工审阅与阶段 2 数据面；绝不直接改任何闸行为。"""
    try:
        row = ctx.conn.execute(
            "SELECT id FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (ctx.task_id,)).fetchone()
        if row is None:
            return
        seq = ctx.conn.execute("SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?",
                               (row["id"],)).fetchone()["n"]
        ctx.conn.execute(
            "INSERT INTO agent_messages(session_id, seq, role, content) VALUES (?,?,?,?)",
            (row["id"], seq, "assistant",
             f"[agent-loop·soft-override] {heuristic}：{evidence[:300]}（留痕，未生效）"))
        ctx.conn.commit()
    except Exception as e:      # noqa: BLE001 —— 留痕失败不炸主链路
        log.warning("soft-override 留痕失败 task=%s: %s", ctx.task_id, e)
