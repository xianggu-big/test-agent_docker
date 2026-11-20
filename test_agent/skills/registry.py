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
import inspect
import json
import logging
import threading
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


def _validate_args(schema: dict, args: dict) -> str | None:
    """0-B 双模式参数校验器（2026-11-20 设计稿 §2.2；不引第三方依赖）。

    - JSON Schema 形态（schema 的值是 dict）＝**严格模式**：required 缺失/未知键
      （additionalProperties=False）/type/enum/min/max/maxLength/min-maxItems 全校验
      （Agent Loop 新能力用——模型传参不可注入内部字段）；
    - 传统简写形态（值是 str，如 {"cmd": "list[str]"}）＝**类型提示模式**：只校验
      已声明键且值非 None 时的类型，未声明键放行（存量兼容实锤：install_deps 的
      has_requirements 老布尔形态、deps_manifest=None 传参——严格拒未知键会炸现有链路）；
    - 空 schema＝跳过（存量 skill 未声明=不校验）。
    返回 None=通过；str=拒绝原因（进 denied 审计）。"""
    if not schema:
        return None
    if not any(isinstance(v, dict) for v in schema.values()):
        # —— 类型提示模式（存量简写）——
        type_map = {"str": str, "int": int, "float": (int, float), "bool": bool,
                    "list": list, "dict": dict}
        for key, decl in schema.items():
            if key not in args or args[key] is None:
                continue
            want = type_map.get(str(decl).split("[")[0].strip())
            if want is None:
                continue
            v = args[key]
            bad = not isinstance(v, want) or (want in (int, float)
                                              and isinstance(v, bool))
            if bad:
                return f"键 {key!r} 类型应为 {decl}，实得 {type(v).__name__}"
        return None
    # —— 严格模式（JSON Schema 子集）——
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in args or args[key] is None:
            return f"缺少必填键 {key!r}"
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                return f"未知键 {key!r}（additionalProperties=False）"
    for key, v in args.items():
        spec = props.get(key)
        if isinstance(spec, dict):
            err = _check_spec(key, v, spec)
            if err:
                return err
    return None


def _check_spec(key, v, spec) -> str | None:
    t = spec.get("type")
    if t == "string" and not isinstance(v, str):
        return f"键 {key!r} 应为 string，实得 {type(v).__name__}"
    if t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
        return f"键 {key!r} 应为 integer，实得 {type(v).__name__}"
    if t == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
        return f"键 {key!r} 应为 number，实得 {type(v).__name__}"
    if t == "boolean" and not isinstance(v, bool):
        return f"键 {key!r} 应为 boolean，实得 {type(v).__name__}"
    if t == "array" and not isinstance(v, list):
        return f"键 {key!r} 应为 array，实得 {type(v).__name__}"
    if t == "object" and not isinstance(v, dict):
        return f"键 {key!r} 应为 object，实得 {type(v).__name__}"
    if "enum" in spec and v not in spec["enum"]:
        return f"键 {key!r} 取值 {v!r} 不在枚举 {spec['enum']}"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in spec and v < spec["minimum"]:
            return f"键 {key!r}={v} 低于下限 {spec['minimum']}"
        if "maximum" in spec and v > spec["maximum"]:
            return f"键 {key!r}={v} 高于上限 {spec['maximum']}"
    if isinstance(v, list):
        if "minItems" in spec and len(v) < spec["minItems"]:
            return f"键 {key!r} 长度 {len(v)} 低于 minItems {spec['minItems']}"
        if "maxItems" in spec and len(v) > spec["maxItems"]:
            return f"键 {key!r} 长度 {len(v)} 高于 maxItems {spec['maxItems']}"
    if isinstance(v, str) and "maxLength" in spec and len(v) > spec["maxLength"]:
        return f"键 {key!r} 长度超上限 {spec['maxLength']}"
    return None


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
        # 0-B 执行闸（2026-11-20 设计稿 §2.2）：前置闸顺序=拒绝优先级，全部 fail-closed 落审计
        # 1) enabled 闸：enabled=0 ⇒ denied（列存在但历史从不查询——对码实锤；
        #    无行=未注册放行=现状）
        row = self.conn.execute(
            "SELECT enabled FROM skills WHERE name=?", (skill.name,)).fetchone()
        if row is not None and not row["enabled"]:
            return self._log(task_id, skill, args, "denied",
                             error="skill 已停用（enabled=0），拒绝执行")
        # 2) schema 闸：双模式校验（_validate_args；空/简写 schema 存量兼容语义见 docstring）
        schema_err = _validate_args(getattr(skill, "arg_schema", None) or {}, args)
        if schema_err:
            return self._log(task_id, skill, args, "denied",
                             error=f"参数校验拒绝：{schema_err}")
        # 3) 确认门：fail-closed，confirmed=0 直接拒绝，不解释不折衷——
        #    显式 requires_confirmation 与 0-B 风险分层（high 隐含需确认）融合；
        #    现网唯一 high=run_tests 本就显式声明=零行为变化
        if skill.requires_confirmation or skill.risk_level == "high":
            row = self.conn.execute(
                "SELECT confirmed FROM test_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return self._log(task_id, skill, args, "denied", error=f"task {task_id} 不存在")
            if row["confirmed"] == 0:
                return self._log(task_id, skill, args, "denied",
                                 error="人工确认门：任务未确认（confirmed=0），禁止下发")

        # 2) 审计开单 + 带超时执行（0-C：cancel_event 可选协议——inspect 探测 func
        #    签名，支持则注入；FutureTimeout 时 set() 通知尽快退出。存量签名
        #    func(task_id, args) 不注入任何 kwargs=零破坏）
        t0 = time.monotonic()
        inv_id = self._begin(task_id, skill, args)
        cancel_event: threading.Event | None = threading.Event()
        kwargs: dict = {}
        try:
            sig = inspect.signature(skill.func)
            if "cancel_event" in sig.parameters:
                kwargs["cancel_event"] = cancel_event
            else:
                cancel_event = None
        except (TypeError, ValueError):    # noqa: BLE001 —— 内建/不可内省 func=不注入
            cancel_event = None
        future = _pool.submit(skill.func, task_id, args, **kwargs)
        try:
            output = future.result(timeout=skill.timeout_ms / 1000)
            return self._log(task_id, skill, args, "ok", output=str(output),
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             invocation_id=inv_id)
        except FutureTimeout:
            if cancel_event is not None:
                cancel_event.set()   # 0-C：通知支持取消协议的 skill（配合 exec_in 真超时）
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
