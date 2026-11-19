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
    module_id: int | None     # analyze upsert 产物（重跑幂等取同一行）——container_jobs/
                              # test_scripts/test_reports 外键链。必须声明：LangGraph 过滤
                              # 未声明键=外键全断（2026-11-15 闲时找bug 第一轮实锤）
    job_id: int               # provision 落 container_jobs 的行 id——report 收口 UPDATE 的键；
                              # 不声明则 state.get("job_id", 0) 恒 0，容器台账行永远停在 running
    complexity_hotspots: list[dict]  # analyze 产出 → plan 提示词风险分级倾斜（W4）；
                              # 不声明=热点只落库、plan 注入是静默死代码
    auto_prepare: bool        # 任务级"自动准备环境"（builder 三入口 init 注入、provision 消费）；
                              # 不声明=勾选静默失效，只剩全局开关
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
    last_cmd: list[str]      # run 节点回传的执行命令——flaky 连跑闸（MTA_FLAKY_RERUNS）
                             # 复跑用；不声明=闸条件恒 falsy，连跑功能静默死代码
    passed: int
    failed: int
    skipped: int
    coverage_line: float | None
    per_file: list[dict]     # 逐文件覆盖明细（B2：缺口回灌与趋势图数据源）
    # 判定
    verdict: str             # pass / repair_env / repair_gen / stop
    index_scope_paths: list[str]   # 索引路由锁定的模块路径（空=全库）
    index_closure: list[str]       # L1 依赖闭包模块清单（[命中模块]+邻居；空=未启用）
    index_l2_rescued: bool         # L2 覆盖率急救已扩圈过一次（防死循环护栏）
    index_endpoints: list[dict]    # 索引命中的接口清单（L1 对账用）
    stop_reason: str
    static_only: bool        # 分级（Tier3）：主语言无容器执行链路 → plan 后进 static_draft 而非 provision
    two_stage_rebuild: bool  # 阶段二布考场：provision 按 DB 审查结论重写生成用例文件（408 实锤：未声明
                             # 被 LangGraph schema 过滤，rebuild 上线以来 0 次触发=静默死代码）
    review_rejects: int      # 报告审查拒绝次数（硬停止计数器，与轮次预算解耦，§3.4）
    review_status: str       # pass / rejected（终态如实呈现报告审查结论）
    gen_reason: str | None   # 最近一轮 gen_tests 的 reason 透传（出题 0 条归因如实——2026-11-09）
    ui_routes: list[str]     # MTA_ROUTER=1 时通过前置条件的可用路线（analyze 产出——
                             # 必须声明：LangGraph 会过滤未声明键，rebuild 静默死代码教训）
    verify_mode: int         # A2 验证任务冻结（2026-11-20）：1=执行图禁生成/剪除/重写/回炉
                             # （必须声明：未声明键被 LangGraph schema 过滤=366 教训）
    done: bool
