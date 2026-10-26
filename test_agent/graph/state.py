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
    failure_kind: str        # none / env / case_defect / product_defect
    # 最近一轮执行
    last_exit: int
    last_stdout: str
    last_stderr: str
    passed: int
    failed: int
    skipped: int
    coverage_line: float | None
    # 判定
    verdict: str             # pass / repair_env / repair_gen / stop
    stop_reason: str
    done: bool
