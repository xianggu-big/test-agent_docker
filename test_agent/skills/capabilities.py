"""Agent Loop 能力层（2026-11-20 设计稿 §3.1；MTA_AGENT_LOOP 默认关=主图行为逐字节不变）。

统一能力描述（Capability）：模型看到的是能力菜单，不是散落的函数。三条铁律
（宏观稿 §6.2）在结构上强制：
1. status 是执行状态不是测试结论——模型永远不写 verdict；
2. metrics 只能来自解析器（normalize_result 校验键白名单+数值类型，模型摘要进
   summary 不进 metrics）；
3. evidence 必须是任务产物目录下真实文件（sha256 落引用），缺失降级 unsupported。

复用映射（实施附录件 1）：十二项能力全部踩在现有函数上（stack_detect/case_results/
coverage_check/attribution/builtin 三断言/gen_tests），无新执行面。
MTA_ROUTER/差分快照等旗标联动：旗标关=能力不入菜单（存在闸拒绝，可审计）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config

log = logging.getLogger(__name__)

# metrics 键白名单（normalize_result 强制——防模型自然语言渗进数字层）
_METRIC_KEYS = {"passed", "failed", "skipped", "errors", "coverage_line",
                "total", "covered", "missed", "exit_code"}
# 0-D（设计稿 §2.4）密钥键名黑名单：args/观察空间出现即拒绝（正则，大小写不敏感）
_SECRET_KEY_RE = re.compile(r"api[_-]?key|key[_-]?env|secret|token|passwd|password",
                            re.IGNORECASE)


@dataclass
class TaskCtx:
    """决策循环的任务上下文（capability handler 的唯一环境入口）。"""
    task_id: int
    session_id: int
    workspace: Path
    container_name: str
    primary_language: str
    image: str
    state_snapshot: dict            # TaskState 只读快照（per_file/index_scope_paths 等）
    conn: object
    runner: object
    artifacts_dir: Path             # 证据落盘根：data/artifacts/task-{tid}/agent-loop/
    agent_mode: str = "strategy"    # strategy / diagnose
    run_id: int | None = None       # agent_runs 行 id（运行级账本 T3；None=账本关/降级）


@dataclass
class CapResult:
    """统一返回结构（宏观稿 §6.2）。"""
    status: str                     # ok / failed / timeout / denied / unsupported
    summary: str = ""               # ≤200 字摘要（进观察空间）
    metrics: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)     # [{"kind","path","sha256"}]
    diagnostics: list = field(default_factory=list)  # [{"code","message"}]
    raw_exit: int | None = None


@dataclass
class Capability:
    id: str
    version: str
    kind: str                       # observe / execute / edit / install
    languages: list
    description: str
    inputs_schema: dict
    evidence_kinds: list
    permissions: dict
    risk: str                       # R0-R4（宏观稿 §6.3）
    timeout_s: int
    compat_images: list = field(default_factory=list)
    handler: Callable | None = None


def _evidence_file(ctx: TaskCtx, kind: str, name: str, text: str) -> dict:
    """证据落盘（真实文件+sha256 引用）——铁律 3 的实现。"""
    d = ctx.artifacts_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{ctx.agent_mode}-step-{name}"
    p.write_text(text, encoding="utf-8")
    return {"kind": kind, "path": str(p),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


def _rel_whitelisted(ctx: TaskCtx, rel: str) -> bool:
    """0-D read 白名单：workspace/artifacts 两目录的相对路径；绝对路径/.. 逃逸/
    data|config 顶层一律拒绝。"""
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return False
    first = Path(rel).parts[0] if Path(rel).parts else ""
    if first in ("data", "config", ".git"):
        return False
    return True


def _cap_observe_read(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：workspace 相对路径读/列/grep（0-D 白名单+SWE-agent 截窗封顶——
    调研修正 2：单文件 ≤200 行窗口、grep ≤50 条，防单次输出吃满观察包）。"""
    rel = args.get("path", "")
    if not _rel_whitelisted(ctx, rel):
        return CapResult(status="denied",
                         summary=f"路径越界（白名单外）：{rel[:80]}",
                         diagnostics=[{"code": "PATH_DENIED", "message": rel[:120]}])
    p = ctx.workspace / rel
    if not p.exists():
        return CapResult(status="failed", summary=f"路径不存在：{rel[:80]}")
    mode = args.get("mode", "read")
    if mode == "list":
        if p.is_file():
            entries = [p.name]
        else:
            entries = sorted(x.name for x in p.iterdir())[:200]
        body = "\n".join(entries)
        ev = _evidence_file(ctx, "file_list", "list.txt", body)
        return CapResult(status="ok", summary=f"{rel}: {len(entries)} 项",
                         evidence=[ev])
    text = p.read_text(encoding="utf-8", errors="replace")
    if mode == "grep":
        pat = args.get("pattern") or ""
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            if pat in line:
                hits.append(f"{i}:{line[:160]}")
                if len(hits) >= 50:
                    break
        body = "\n".join(hits) or f"（无命中：{pat[:60]}）"
        ev = _evidence_file(ctx, "grep", "grep.txt", body)
        return CapResult(status="ok", summary=f"{rel}: {len(hits)} 命中（≤50 封顶）",
                         evidence=[ev])
    lines = text.splitlines()
    off = int(args.get("offset") or 1)
    lim = min(int(args.get("limit") or 200), 200)
    window = lines[off - 1: off - 1 + lim]
    body = "\n".join(f"{off + i}:{ln[:200]}" for i, ln in enumerate(window))
    ev = _evidence_file(ctx, "file_read", "read.txt", body)
    return CapResult(status="ok",
                     summary=f"{rel}: 行 {off}-{off + len(window) - 1}"
                             f"（共 {len(lines)} 行，≤200 行窗口）",
                     evidence=[ev])


def _cap_observe_env(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：环境探测——stack_detect（宿主，零容器）+ 可选容器金丝雀。"""
    from .stack_detect import detect_stack
    probe = args.get("probe", "stack")
    if probe == "stack":
        st = detect_stack(ctx.workspace)
        body = json.dumps(st, ensure_ascii=False, indent=1)[:4000]
        ev = _evidence_file(ctx, "env_stack", "stack.json", body)
        return CapResult(status="ok", summary="stack 探测完成",
                         metrics={"language": 0 if not st.get("language") else 1},
                         evidence=[ev],
                         diagnostics=[{"code": "STACK", "message": body[:400]}])
    if probe == "canary":
        from .attribution import canary_cmd, canary_ok
        cmd = canary_cmd(ctx.primary_language or "python")
        r = ctx.runner.exec_in(ctx.runner.client.containers.get(ctx.container_name),
                               cmd, timeout_s=60)
        ok = canary_ok(r.exit_code, r.stdout or "")
        ev = _evidence_file(ctx, "canary", "canary.txt",
                            f"exit={r.exit_code}\n{r.stdout or ''}\n{r.stderr or ''}")
        return CapResult(status="ok" if ok else "failed",
                         summary=f"金丝雀 {'通过' if ok else '未通过'}（exit={r.exit_code}）",
                         evidence=[ev], raw_exit=r.exit_code)
    return CapResult(status="unsupported", summary=f"未知探测类型：{probe}")


def _cap_run_unit(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：容器内跑指定测试目标子集（与 run 节点同命令形态，targets 参数化）；
    metrics 只能来自 stdout 正则解析（与 evaluate 同源口径）。"""
    targets = args.get("targets") or []
    timeout_s = min(int(args.get("timeout_s") or 300), 600)
    lang = (ctx.primary_language or "python").lower()
    if lang in ("javascript", "typescript"):
        cmd = ["npx", "jest", "--json", *targets]
    else:
        cmd = ["python", "-m", "pytest", "-q", *targets]
    r = ctx.runner.exec_in(ctx.runner.client.containers.get(ctx.container_name),
                           cmd, timeout_s=timeout_s)
    out = r.stdout or ""
    ev = _evidence_file(ctx, "run_stdout", "run_unit.txt",
                        f"exit={r.exit_code}\n{out}\n{r.stderr or ''}")
    # 解析器同源口径（evaluate 同款正则——数字只能来自这里）
    def _n(pat):
        m = re.search(pat, out)
        return int(m.group(1)) if m else 0
    metrics = {"passed": _n(r"(\d+) passed"), "failed": _n(r"(\d+) failed"),
               "exit_code": r.exit_code}
    status = "ok" if r.exit_code == 0 else "failed"
    return CapResult(status=status,
                     summary=f"{len(targets)} 目标：{metrics['passed']} 过 "
                             f"{metrics['failed']} 败（exit={r.exit_code}）",
                     metrics=metrics, evidence=[ev], raw_exit=r.exit_code)


def _cap_observe_coverage(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：覆盖率判读（coverage_check 纯读 state 台账，零容器）。"""
    from .project_index import coverage_check
    snap = ctx.state_snapshot
    r = coverage_check(snap, snap.get("per_file") or [],
                       snap.get("coverage_line"))
    body = json.dumps(r, ensure_ascii=False, indent=1)[:3000]
    ev = _evidence_file(ctx, "coverage", "coverage.json", body)
    return CapResult(status="ok", summary=f"coverage_check 判读：{r.get('status')}",
                     evidence=[ev],
                     diagnostics=[{"code": "COVERAGE", "message": body[:400]}])


def _cap_observe_logs(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：运行日志摘要（last_stdout/stderr 尾部+近 N 条会话消息，≤50 段封顶）。"""
    snap = ctx.state_snapshot
    n = min(int(args.get("tail_n") or 20), 50)
    parts = [f"exit={snap.get('last_exit')}",
             (snap.get("last_stdout") or "")[-n * 200:],
             (snap.get("last_stderr") or "")[-n * 200:]]
    body = "\n---\n".join(p for p in parts if p)[:6000]
    ev = _evidence_file(ctx, "logs", "logs.txt", body)
    return CapResult(status="ok", summary="日志尾部已取证", evidence=[ev])


def _cap_gen_cases(ctx: TaskCtx, args: dict) -> CapResult:
    """R1：定向增补生成（复用 generate_tests 的 feedback 形态——builder.py:558 现成样例）。"""
    from .gen_tests import generate_tests
    from .llm_profile import effective_key
    api_key = effective_key()
    if not api_key:
        return CapResult(status="unsupported", summary="无 Key（诚实降级）")
    ws = ctx.workspace
    fb_no = len(list(ws.glob("test_gen_fb*.py"))) + 1
    existing = [r["name"] for r in ctx.conn.execute(
        """SELECT c.name FROM test_cases c JOIN test_scripts s ON s.id=c.script_id
           WHERE s.task_id=?""", (ctx.task_id,))]
    summary = generate_tests(api_key, {
        "workspace": str(ws),
        "requirement": ctx.state_snapshot.get("requirement") or "",
        "module_hint": ctx.primary_language or "",
        "container_name": ctx.container_name,
        "module_id": ctx.state_snapshot.get("module_id"),
        "round_no": 1,
        "feedback": (args.get("feedback") or "")[:1500],
        "targets": args.get("targets") or [],
        "fb_no": fb_no,
        "version": 900 + fb_no,
        "existing_names": existing,
    }, ctx.task_id, ctx.runner)
    data = json.loads(summary)
    ev = _evidence_file(ctx, "gen_summary", "gen_cases.json",
                        json.dumps(data, ensure_ascii=False, indent=1)[:8000])
    return CapResult(status="ok",
                     summary=f"定向增补 {data.get('generated', 0)} 条",
                     metrics={"passed": 0, "failed": 0,
                              "exit_code": data.get("generated", 0)},
                     evidence=[ev])


# ---------- edit diff 四分类 AST 闸（设计稿 §3.6；宏观稿 §6.5 落地） ----------

def classify_edit_diff(old: str, new: str) -> str:
    """确定性 diff 分类（AST 级，非正则）。返回 layout/fixture/assertion/deletion。
    - deletion：新代码少了 test_ 函数（或新增 skip 装饰）→ 拒绝；
    - assertion：任一 test_ 函数的断言语句变化 → 拒绝（oracle_change_request）；
    - layout：仅 import/模块路径变化 → 允许；
    - fixture：夹具/辅助结构变化但断言与用例集不变 → 允许。
    判据锚：AST 逐函数比对（369 自导自演形态进 negative 金样）。"""
    try:
        t_old = ast.parse(old)
        t_new = ast.parse(new)
    except SyntaxError:
        return "assertion"     # 语法都炸=无法证明语义等价 → 保守拒绝（宁严勿宽）
    f_old = {n.name: n for n in t_old.body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    f_new = {n.name: n for n in t_new.body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    if set(f_old) - set(f_new):
        return "deletion"
    for name, fn in f_old.items():
        nn = f_new.get(name)
        if nn is None:
            return "deletion"
        old_a = [d for d in ast.walk(fn) if isinstance(d, ast.Assert)]
        new_a = [d for d in ast.walk(nn) if isinstance(d, ast.Assert)]
        if [ast.dump(x) for x in old_a] != [ast.dump(x) for x in new_a]:
            return "assertion"
        # skip 装饰器新增=变相删除
        dec_old = {ast.dump(d) for d in fn.decorator_list}
        dec_new = {ast.dump(d) for d in nn.decorator_list}
        if dec_new - dec_old:
            return "deletion"
    imp_old = {ast.dump(n) for n in t_old.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    imp_new = {ast.dump(n) for n in t_new.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    if imp_old != imp_new:
        return "layout"
    body_old = [ast.dump(n) for n in t_old.body
                if not isinstance(n, (ast.Import, ast.ImportFrom))]
    body_new = [ast.dump(n) for n in t_new.body
                if not isinstance(n, (ast.Import, ast.ImportFrom))]
    return "layout" if body_old == body_new else "fixture"


def _cap_edit_scaffold(ctx: TaskCtx, args: dict) -> CapResult:
    """R1：脚手架修复（只允许写测试产物目录；diff 分类闸——断言/删除拒绝→
    oracle_change_request 留痕；旧文件+diff+原因全留档）。"""
    rel = args.get("path", "")
    if not (rel.startswith("test_gen") or rel.startswith("test_imported")):
        return CapResult(status="denied",
                         summary="edit.scaffold 只允许写测试产物文件（test_gen*/test_imported*）",
                         diagnostics=[{"code": "EDIT_SCOPE_DENIED", "message": rel[:120]}])
    p = ctx.workspace / rel
    old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    kind = classify_edit_diff(old, args.get("new_content") or "")
    d = ctx.artifacts_dir / "scaffold"
    d.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^A-Za-z0-9_.-]", "_", rel)
    if kind in ("assertion", "deletion"):
        (d / f"{stamp}.request.json").write_text(json.dumps(
            {"file": rel, "kind": kind,
             "reason": args.get("reason") or "", "old": old, "new": args.get("new_content")},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return CapResult(status="denied",
                         summary=f"{kind} 类变更被拒绝（已生成 oracle_change_request，转人工审查）",
                         diagnostics=[{"code": "ORACLE_CHANGE_REQUEST",
                                       "message": f"{rel}:{kind}"}])
    p.write_text(args.get("new_content") or "", encoding="utf-8")
    (d / f"{stamp}.old.py").write_text(old, encoding="utf-8")
    ev = _evidence_file(ctx, "scaffold_diff", f"scaffold-{stamp}.txt",
                        f"kind={kind}\nreason={args.get('reason')}\n---\n"
                        f"OLD:\n{old[:2000]}\n---\nNEW:\n{(args.get('new_content') or '')[:2000]}")
    return CapResult(status="ok", summary=f"{rel} 脚手架修复（{kind} 类，允许）",
                     evidence=[ev])


def _cap_assert_generic(mod_name: str):
    """db/ui/grpc 断言统一包装（builtin.py:242-252 现成 Skill 闭包的同源形态）。"""
    def _run(ctx: TaskCtx, args: dict) -> CapResult:
        import importlib
        mod = importlib.import_module(f".{mod_name}", package="test_agent.skills")
        fn = getattr(mod, mod_name)
        out = fn(api_key=None, args=args, task_id=ctx.task_id, runner=ctx.runner)
        ev = _evidence_file(ctx, f"{mod_name}_out", f"{mod_name}.txt", str(out)[:6000])
        return CapResult(status="ok", summary=f"{mod_name} 断言执行完成（输出见证据）",
                         evidence=[ev])
    return _run


def _cap_diff_replay(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：差分复跑（attribution.run_diff_gate——no_git/no_ref 如实降级不硬猜）。"""
    from .attribution import run_diff_gate
    r = run_diff_gate(
        lambda cmd, timeout_s=600: ctx.runner.exec_in(
            ctx.runner.client.containers.get(ctx.container_name), cmd, timeout_s=timeout_s),
        ctx.primary_language or "python",
        args.get("failed_files") or [], args.get("v_ref") or "HEAD~1")
    body = json.dumps(r, ensure_ascii=False, indent=1)[:5000]
    ev = _evidence_file(ctx, "diff_gate", "diff.json", body)
    return CapResult(status="ok", summary=f"差分闸：{r.get('outcome')}",
                     evidence=[ev],
                     diagnostics=[{"code": "DIFF", "message": body[:400]}])


def _cap_snapshot(ctx: TaskCtx, args: dict) -> CapResult:
    """R0：快照对比（旗标 SNAPSHOT_ROUTE_ENABLED 联动——关=能力不入菜单，双保险）。"""
    from .snapshot_route import run_snapshot  # 函数内 import（旗标关时不加载）
    out = run_snapshot({}, dict(args), ctx.task_id, ctx.runner)
    ev = _evidence_file(ctx, "snapshot", "snapshot.txt", str(out)[:6000])
    return CapResult(status="ok", summary="快照对比完成", evidence=[ev])


def _build_capabilities(ctx: TaskCtx) -> list[Capability]:
    """十二项 MVP 能力（设计稿 §3.1 表；schema=实施附录件 4 实文）。"""
    schemas = {
        "test.observe.read": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "mode": {"enum": ["read", "list", "grep"]},
                                             "pattern": {"type": "string"},
                                             "offset": {"type": "integer", "minimum": 1},
                                             "limit": {"type": "integer", "minimum": 1,
                                                       "maximum": 200}},
                              "required": ["path", "mode"],
                              "additionalProperties": False},
        "test.observe.env": {"type": "object",
                             "properties": {"probe": {"enum": ["stack", "canary"]}},
                             "required": ["probe"], "additionalProperties": False},
        "test.run.unit": {"type": "object",
                          "properties": {"targets": {"type": "array",
                                                     "items": {"type": "string"},
                                                     "minItems": 1, "maxItems": 20},
                                         "timeout_s": {"type": "integer", "minimum": 10,
                                                       "maximum": 600}},
                          "required": ["targets"], "additionalProperties": False},
        "test.observe.coverage": {"type": "object", "properties": {},
                                  "additionalProperties": False},
        "test.observe.logs": {"type": "object",
                              "properties": {"tail_n": {"type": "integer", "minimum": 1,
                                                        "maximum": 50}},
                              "additionalProperties": False},
        "test.gen.cases": {"type": "object",
                           "properties": {"feedback": {"type": "string", "maxLength": 1500},
                                          "targets": {"type": "array",
                                                      "items": {"type": "string"}}},
                           "required": ["feedback"], "additionalProperties": False},
        "test.edit.scaffold": {"type": "object",
                               "properties": {"path": {"type": "string"},
                                              "new_content": {"type": "string",
                                                              "maxLength": 20000},
                                              "reason": {"type": "string"}},
                               "required": ["path", "new_content", "reason"],
                               "additionalProperties": False},
        "test.assert.db": {"type": "object",
                           "properties": {"assertions": {"type": "array", "minItems": 1}},
                           "required": ["assertions"], "additionalProperties": False},
        "test.assert.ui": {"type": "object",
                           "properties": {"checks": {"type": "array", "minItems": 1}},
                           "required": ["checks"], "additionalProperties": False},
        "test.assert.grpc": {"type": "object",
                             "properties": {"checks": {"type": "array", "minItems": 1}},
                             "required": ["checks"], "additionalProperties": False},
        "test.diff.replay": {"type": "object",
                             "properties": {"failed_files": {"type": "array",
                                                             "items": {"type": "string"},
                                                             "minItems": 1},
                                            "v_ref": {"type": "string"}},
                             "required": ["failed_files", "v_ref"],
                             "additionalProperties": False},
        "test.snapshot": {"type": "object", "properties": {},
                          "additionalProperties": False},
    }
    mk = lambda cid, kind, langs, desc, evk, risk, t, handler: Capability(
        id=cid, version="1.0.0", kind=kind, languages=langs, description=desc,
        inputs_schema=schemas[cid], evidence_kinds=evk,
        permissions={"fs": ["test_artifacts_write"], "network": "none", "secrets": []},
        risk=risk, timeout_s=t, handler=handler)
    lang = [ctx.primary_language or "python"]
    caps = [
        mk("test.observe.read", "observe", ["*"], "读文件/列目录/grep（workspace 相对路径，截窗封顶）",
           ["file_read"], "R0", 30, _cap_observe_read),
        mk("test.observe.env", "observe", ["*"], "环境探测（stack/金丝雀）",
           ["env_stack"], "R0", 90, _cap_observe_env),
        mk("test.run.unit", "execute", ["*"], "容器内运行指定测试目标子集",
           ["run_stdout"], "R0", 600, _cap_run_unit),
        mk("test.observe.coverage", "observe", ["*"], "覆盖率判读（coverage_check）",
           ["coverage"], "R0", 10, _cap_observe_coverage),
        mk("test.observe.logs", "observe", ["*"], "运行日志尾部取证",
           ["logs"], "R0", 10, _cap_observe_logs),
        mk("test.gen.cases", "execute", ["*"], "定向增补生成用例（feedback 形态）",
           ["gen_summary"], "R1", 600, _cap_gen_cases),
        mk("test.edit.scaffold", "edit", ["*"], "脚手架修复（AST diff 分类闸；断言/删除拒绝）",
           ["scaffold_diff"], "R1", 30, _cap_edit_scaffold),
        mk("test.assert.db", "execute", ["*"], "数据库断言（sidecar 只读闸）",
           ["db_assert_out"], "R1", 120, _cap_assert_generic("db_assert")),
        mk("test.assert.ui", "execute", ["*"], "UI 断言（Playwright）",
           ["ui_assert_out"], "R1", 300, _cap_assert_generic("ui_assert")),
        mk("test.assert.grpc", "execute", ["*"], "gRPC 断言（grpcurl）",
           ["grpc_assert_out"], "R1", 120, _cap_assert_generic("grpc_assert")),
        mk("test.diff.replay", "execute", ["*"], "差分复跑（buggy/fixed 对照，需工作区 .git）",
           ["diff_gate"], "R0", 600, _cap_diff_replay),
    ]
    if getattr(config, "SNAPSHOT_ROUTE_ENABLED", False):
        caps.append(mk("test.snapshot", "execute", ["*"], "快照对比（黄金基线 oracle）",
                       ["snapshot"], "R0", 120, _cap_snapshot))
    return caps


def _schema_hint(schema: dict) -> str:
    """菜单参数提示（渐进披露的"简述"层——设计稿 §3.1 菜单含 inputs 简述；
    冒烟实锤：不给参数提示模型盲猜 args 会被 schema 闸拒）。"""
    if not isinstance(schema, dict) or not schema:
        return "无参数"
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    parts = []
    for k, spec in props.items():
        t = spec.get("type") or ("|".join(str(x) for x in spec["enum"])
                                 if "enum" in spec else "any")
        parts.append(f"{k}{'*' if k in req else ''}:{t}")
    return " ".join(parts) or "无参数"


def capability_menu(ctx: TaskCtx) -> list[dict]:
    """能力菜单（渐进披露：菜单给摘要+参数简述，完整 schema 由 authorize 持有）。
    过滤：语言兼容（"*" 恒过）+ diagnose 模式收窄 R0（诊断只读——设计稿 §3.5）
    + 旗标联动已在 _build_capabilities 处理；目录插件动态注册（verified/active
    才可见——包 2，能力菜单不是写死的）。"""
    all_caps = list(_build_capabilities(ctx))
    if ctx.agent_mode != "diagnose":     # 插件=R2，diagnose 只读模式不进菜单
        try:
            from .plugin_runtime import catalog_capabilities
            all_caps.extend(catalog_capabilities(ctx))
        except Exception as e:           # noqa: BLE001 —— 目录异常=仅内置能力
            log.warning("目录插件加载失败（%s: %s）", type(e).__name__, e)
    menu = []
    for c in all_caps:
        if not ("*" in c.languages or (ctx.primary_language or "").lower() in c.languages):
            continue
        if ctx.agent_mode == "diagnose" and c.risk != "R0":
            continue
        menu.append({"id": c.id, "description": c.description, "risk": c.risk,
                     "args_hint": _schema_hint(c.inputs_schema)})
    return menu


def find_capability_extended(ctx: TaskCtx, cap_id: str) -> Capability | None:
    """find_capability 的插件扩展版（含目录条目——executor 用）。"""
    if cap_id.startswith("plugin."):
        try:
            from .plugin_runtime import catalog_capabilities
            for c in catalog_capabilities(ctx):
                if c.id == cap_id:
                    return c
        except Exception:            # noqa: BLE001
            return None
        return None
    return find_capability(ctx, cap_id)


def find_capability(ctx: TaskCtx, cap_id: str) -> Capability | None:
    for c in _build_capabilities(ctx):
        if c.id == cap_id and ("*" in c.languages
                               or (ctx.primary_language or "").lower() in c.languages):
            return c
    return None


def normalize_result(res: CapResult) -> CapResult:
    """结果归一（宏观稿 §6.2 铁律落地）：
    - metrics 键白名单+数值类型强制——非白名单键/非数值一律剔除（模型摘要渗不进数字层）；
    - summary 截 200 字；evidence 必须带 kind/path/sha256 三键。
    无法满足证据要求的 evidence_kinds 契约 → 降级 unsupported（铁律 4）。"""
    cleaned = {}
    for k, v in (res.metrics or {}).items():
        if k in _METRIC_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            cleaned[k] = v
    ev = [e for e in (res.evidence or [])
          if isinstance(e, dict) and {"kind", "path", "sha256"} <= set(e)]
    res.metrics = cleaned
    res.evidence = ev
    res.summary = (res.summary or "")[:200]
    if res.status == "ok" and not ev:
        res.status = "unsupported"
        res.summary = (res.summary + "（证据缺失，降级 unsupported）")[:200]
    return res


def action_fingerprint(capability: str, args: dict) -> str:
    """动作指纹（防死循环）：capability + canonical_json（键排序——防同义重排绕过）。"""
    blob = json.dumps([capability, args], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _authorize_schema(args: dict, schema: dict) -> str | None:
    from .registry import _validate_args
    return _validate_args(schema, args)
