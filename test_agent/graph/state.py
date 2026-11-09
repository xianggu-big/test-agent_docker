"""LangGraph 任务状态 —— 字段与 test_tasks 状态机、第 5 层数据表一一对应。

状态只放"跨节点需要"的数据；一次性中间值留在节点内部，不污染状态。
"""
from typing import TypedDict


class TaskState(TypedDict, total=False):
    # 任务身份（builder 从库中加载）
    task_id: int
    session_id: int          # agent_sessions 行 id
    repo_url: str
    requirement: str
    # 链路产物
    workspace: str           # 宿主侧工作区（clone / 快照源）
    primary_language: str
    image: str
    container_name: str
    # R-Loop 账本
    round_no: int
    coverage_target: float   # 准出标准（分析阶段冻结进任务行，A1）
    fingerprints: list[str]  # 历史轮失败指纹（含当前轮之前的）
    fingerprint: str         # 当前轮指纹
    witness_nodeids: list[str]  # 冻结证人（V8）：断言失败用例累积，指纹熔断豁免
    failure_kind: str        # none / env / case_defect / product_defect
    # 最近一轮执行
    last_exit: int
    last_stdout: str
    last_stderr: str
    passed: int
    failed: int
    skipped: int
    coverage_line: float | None
    per_file: list[dict]     # 逐文件覆盖明细（B2：缺口回灌与趋势图数据源）
    # 判定
    verdict: str             # pass / repair_env / repair_gen / stop
    stop_reason: str
    static_only: bool        # 分级（Tier3）：主语言无容器执行链路 → plan 后进 static_draft 而非 provision
    two_stage_rebuild: bool  # 阶段二布考场：provision 按 DB 审查结论重写生成用例文件（408 实锤：未声明
                             # 被 LangGraph schema 过滤，rebuild 上线以来 0 次触发=静默死代码）
    review_rejects: int      # 报告审查拒绝次数（硬停止计数器，与轮次预算解耦，§3.4）
    review_status: str       # pass / rejected（终态如实呈现报告审查结论）
    gen_reason: str | None   # 最近一轮 gen_tests 的 reason 透传（出题 0 条归因如实——2026-11-09）
    done: bool
