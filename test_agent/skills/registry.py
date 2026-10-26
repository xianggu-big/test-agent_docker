"""L3 Skill 注册表：Schema、风险分级、超时、审计日志、fail-closed 确认门。

借用设计（来自 Multi-tool Agent Runtime MVP 的 ToolRegistry）：
1. 每个 Skill 注册时落库 skills 表（input_schema / risk_level / timeout_ms）
2. 每次调用写 skill_invocations 审计台账（running → ok/failed/timeout/denied），
   论文「工具调用成功率」指标直接查这张表
3. **确认门在工具层，不靠 UI**：requires_confirmation=True 的 Skill 在
   test_tasks.confirmed=0 时直接拒绝执行（返回 denied），fail-closed

本类保持框架无关：LangGraph 节点直接调 invoke()；第 4 步再提供
as_langchain_tools() 把注册表暴露给 LLM。
"""
import dataclasses
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable

from ..store.db import get_conn

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=8)


@dataclass
class SkillResult:
    status: str          # ok / failed / timeout / denied
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    invocation_id: int = 0


@dataclass
class Skill:
    name: str
    description: str
    func: Callable                       # func(task_id: int, args: dict) -> str
    risk_level: str = "low"              # low / medium / high
    timeout_ms: int = 60_000
    requires_confirmation: bool = False  # True ⇒ confirmed=0 时拒绝执行
    arg_schema: dict = field(default_factory=dict)   # 简化 JSON Schema


class SkillRegistry:
    def __init__(self, conn=None):
        self.conn = conn or get_conn()

    # —— 注册 ——

    def register(self, skill: Skill) -> None:
        """注册（幂等 upsert）：元数据落 skills 表。"""
        self.conn.execute(
            """INSERT INTO skills (name, version, input_schema, risk_level, timeout_ms, enabled)
               VALUES (?, '1.0.0', ?, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                 input_schema=excluded.input_schema,
                 risk_level=excluded.risk_level,
                 timeout_ms=excluded.timeout_ms""",
            (skill.name, json.dumps(skill.arg_schema, ensure_ascii=False),
             skill.risk_level, skill.timeout_ms),
        )
        self.conn.commit()

    # —— 调用（审计 + 确认门 + 超时） ——

    def invoke(self, task_id: int, skill: Skill, args: dict) -> SkillResult:
        # 1) 确认门：fail-closed，confirmed=0 直接拒绝，不解释不折衷
        if skill.requires_confirmation:
            row = self.conn.execute(
                "SELECT confirmed FROM test_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return self._log(task_id, skill, args, "denied", error=f"task {task_id} 不存在")
            if row["confirmed"] == 0:
                return self._log(task_id, skill, args, "denied",
                                 error="人工确认门：任务未确认（confirmed=0），禁止下发")

        # 2) 审计开单 + 带超时执行
        t0 = time.monotonic()
        inv_id = self._begin(task_id, skill, args)
        future = _pool.submit(skill.func, task_id, args)
        try:
            output = future.result(timeout=skill.timeout_ms / 1000)
            return self._log(task_id, skill, args, "ok", output=str(output),
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             invocation_id=inv_id)
        except FutureTimeout:
            return self._log(task_id, skill, args, "timeout",
                             duration_ms=skill.timeout_ms,
                             error=f"超时 {skill.timeout_ms}ms", invocation_id=inv_id)
        except Exception as e:  # noqa: BLE001 —— 工具层兜底，异常进审计不进宿主栈
            return self._log(task_id, skill, args, "failed",
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             error=f"{type(e).__name__}: {e}", invocation_id=inv_id)

    # —— 审计台账 ——

    def _begin(self, task_id: int, skill: Skill, args: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO skill_invocations (task_id, skill_id, args_json, status)
               SELECT ?, id, ?, 'running' FROM skills WHERE name = ?""",
            (task_id, json.dumps(args, ensure_ascii=False), skill.name),
        )
        self.conn.commit()
        return cur.lastrowid

    def _log(self, task_id: int, skill: Skill, args: dict, status: str,
             output: str = "", error: str = "", duration_ms: int = 0,
             invocation_id: int = 0) -> SkillResult:
        if invocation_id:
            self.conn.execute(
                "UPDATE skill_invocations SET status=?, duration_ms=?, error=? WHERE id=?",
                (status, duration_ms, error, invocation_id),
            )
            self.conn.commit()
        else:
            # denied 等未开单情形：直接补一条终态记录，保证审计完整
            cur = self.conn.execute(
                """INSERT INTO skill_invocations (task_id, skill_id, args_json, status, duration_ms, error)
                   SELECT ?, id, ?, ?, ?, ? FROM skills WHERE name = ?""",
                (task_id, json.dumps(args, ensure_ascii=False), status, duration_ms, error, skill.name),
            )
            self.conn.commit()
            invocation_id = cur.lastrowid
        log.info("skill[%s] task=%s → %s (%sms) %s", skill.name, task_id, status, duration_ms, error)
        return SkillResult(status=status, output=output, error=error, duration_ms=duration_ms,
                           invocation_id=invocation_id)
