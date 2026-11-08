"""grpc_assert：gRPC 接口验证 skill（黑盒能力补齐阶段4，docs/黑盒能力补齐任务包-20261106.md）。

完全复用 HTTP 黑盒模式，只换真值清单来源（任务卡明文）：②提取层=.proto 文本解析
（endpoint_extract.parse_proto——service/rpc/message 清单，D4 降级链第④级）；③生成层=
LLM 按 proto message 字段事实构造请求 JSON，**闸门：service+method 必须命中 proto 清单**
（虚构整案剔除留痕，同 scenario_gen 端点铁律）；④执行层=grpcurl 容器内真实调用
（-import-path/-proto/-d JSON/-plaintext，fullstorydev/grpcurl v1.9.3 探针实锤，
无 reflection 依赖——服务器不需要开 reflection）；⑤落库层=grpc_asserts 第 31 表
（请求 JSON/响应摘要/verdict/拒绝理由，幂等口径与 db_asserts/ui_asserts 一致）。

安全模型：method 闸在 skill 内部（不占确认门——闸挡在执行之前，与 db_assert 只读闸
同哲学）；LLM 生成（gen_grpc_calls）在调用方侧完成，skill 只收已生成 calls 复闸（纵深）。
"""
import json
import logging
import re
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def normalize_symbol(service: str, method: str) -> str:
    """grpcurl 符号：Service/Method（同包 proto 无 package 前缀；带点写法归一）。"""
    svc = (service or "").strip().split(".")[-1]
    mth = (method or "").strip().split("/")[-1].split(".")[-1]
    return f"{svc}/{mth}"


def call_gate(calls, endpoints: list[dict]):
    """method 闸：每个 call 的 (service, method) 必须命中 proto 清单；request 必须是对象。

    逐 call 处置（合法放行/虚构剔除留痕同批并存——与 db_assert 整案语义不同：gRPC 调用
    相互独立，单条虚构不应连坐合法调用；任务卡验收=「虚构 method 被闸门剔除留痕」逐条口径）。
    返回 (kept, rejected_rows)。"""
    valid = {(e["service"], e["rpc"]) for e in endpoints or []}
    kept, rejected = [], []
    for c in calls if isinstance(calls, list) else []:
        if not isinstance(c, dict):
            rejected.append({"service": "", "method": "", "request_json": "",
                             "verdict": "rejected", "response_excerpt": "", "error": "",
                             "rejected_reason": "call 必须是对象", "elapsed_ms": 0})
            continue
        svc, mth = str(c.get("service") or ""), str(c.get("method") or "")
        req = c.get("request")
        if (svc, mth) not in valid:
            rejected.append({"service": svc, "method": mth,
                             "request_json": json.dumps(req, ensure_ascii=False)[:400]
                             if req is not None else "",
                             "verdict": "rejected", "response_excerpt": "", "error": "",
                             "rejected_reason": f"service+method ({svc}, {mth}) 未命中 proto "
                                                f"清单（虚构 method）——整条剔除",
                             "elapsed_ms": 0})
            continue
        if not isinstance(req, dict):
            rejected.append({"service": svc, "method": mth, "request_json": "",
                             "verdict": "rejected", "response_excerpt": "", "error": "",
                             "rejected_reason": "request 缺失或不是 JSON 对象——整条剔除",
                             "elapsed_ms": 0})
            continue
        kept.append({"service": svc, "method": mth, "request": req})
    return kept, rejected


def build_grpcurl_cmd(proto_file: str, host: str, port: int,
                      service: str, method: str, request: dict) -> list[str]:
    """grpcurl 参数拼装（单测锁定，不起真服务）：-import-path /work -proto <file> -d <json>。"""
    return ["grpcurl", "-plaintext", "-import-path", "/work", "-proto", proto_file,
            "-d", json.dumps(request, ensure_ascii=False),
            f"{host}:{port}", normalize_symbol(service, method)]


def store_grpc_asserts(conn, task_id: int, round_no: int, rows: list[dict]) -> None:
    """grpc_asserts 第 31 表落库；幂等口径与 db_asserts/ui_asserts 一致
    （round≤1 清全任务、round>1 清同轮）。"""
    if round_no <= 1:
        conn.execute("DELETE FROM grpc_asserts WHERE task_id=?", (task_id,))
    else:
        conn.execute("DELETE FROM grpc_asserts WHERE task_id=? AND round_no=?", (task_id, round_no))
    for r in rows:
        conn.execute(
            """INSERT INTO grpc_asserts(task_id, round_no, service, method, request_json,
                                        verdict, response_excerpt, error, rejected_reason, elapsed_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, round_no, r.get("service", ""), r.get("method", ""),
             (r.get("request_json") or "")[:500], r.get("verdict", "failed"),
             (r.get("response_excerpt") or "")[:500], (r.get("error") or "")[:500],
             (r.get("rejected_reason") or "")[:500], int(r.get("elapsed_ms") or 0)))
    conn.commit()


def _exec_grpcurl(runner, container, cmd: list[str], timeout_s: int = 30):
    return runner.exec_in(container, cmd, timeout_s=timeout_s, workdir="/work")


def grpc_assert(api_key: str, args: dict, task_id: int, runner) -> str:
    """SkillRegistry 入口：args={calls(JSON str), proto_text(str), proto_file(str),
    container_name(grpcurl 执行容器), host(服务器容器名), port, round_no}。"""
    container = runner.client.containers.get(args["container_name"])
    round_no = int(args.get("round_no") or 1)
    proto_file = args.get("proto_file") or "shop.proto"
    host = args.get("host") or "localhost"
    port = int(args.get("port") or 50051)
    proto_text = args.get("proto_text") or ""
    calls = args.get("calls")
    if isinstance(calls, str):
        calls = json.loads(calls)

    from .endpoint_extract import parse_proto, proto_endpoints
    endpoints = proto_endpoints(parse_proto(proto_text))
    kept, rejected = call_gate(calls, endpoints)
    rows: list[dict] = []
    for r in rejected:
        rows.append(r)
    for c in kept:
        t0 = time.time()
        cmd = build_grpcurl_cmd(proto_file, host, port, c["service"], c["method"], c["request"])
        try:
            res = _exec_grpcurl(runner, container, cmd)
            elapsed = int((time.time() - t0) * 1000)
            out = (res.stdout or "") + (res.stderr or "")
            if res.exit_code == 0:
                rows.append({"service": c["service"], "method": c["method"],
                             "request_json": json.dumps(c["request"], ensure_ascii=False)[:400],
                             "verdict": "passed",
                             "response_excerpt": (res.stdout or "").strip()[:400],
                             "error": "", "rejected_reason": "", "elapsed_ms": elapsed})
            else:
                rows.append({"service": c["service"], "method": c["method"],
                             "request_json": json.dumps(c["request"], ensure_ascii=False)[:400],
                             "verdict": "failed", "response_excerpt": "",
                             "error": out.strip()[:400], "rejected_reason": "",
                             "elapsed_ms": elapsed})
        except Exception as e:  # noqa: BLE001 —— 执行失败如实落库，不炸主链路
            rows.append({"service": c["service"], "method": c["method"],
                         "request_json": json.dumps(c["request"], ensure_ascii=False)[:400],
                         "verdict": "failed", "response_excerpt": "",
                         "error": f"{type(e).__name__}: {e}", "rejected_reason": "",
                         "elapsed_ms": int((time.time() - t0) * 1000)})
    from ..store.db import get_conn
    conn = get_conn()
    try:
        store_grpc_asserts(conn, task_id, round_no, rows)
    finally:
        conn.close()
    passed = sum(1 for x in rows if x["verdict"] == "passed")
    failed = sum(1 for x in rows if x["verdict"] == "failed")
    rej = sum(1 for x in rows if x["verdict"] == "rejected")
    return json.dumps({"passed": passed, "failed": failed, "rejected": rej,
                       "endpoints": len(endpoints), "rows": rows[:20]},
                      ensure_ascii=False)


# —— ③生成层：LLM 按 proto message 字段事实构造请求（调用方侧；输出即过闸） ——

from .llm_client import make_chat as _make_chat  # noqa: E402 —— 测试 monkeypatch 此符号


def gen_grpc_calls(api_key: str, endpoints: list[dict], requirement: str,
                   attempts: int = 3):
    """LLM 按 proto 清单构造 RPC 调用（事实注入 message 字段，无经验性 prompt）；
    输出即过闸。返回 (kept, rejected_rows)。"""
    svc_desc = "\n".join(
        f'- service={e["service"]} method={e["rpc"]} 请求体 {e["input"]}：'
        + ("；".join(f"{k}({t})" for k, t in (e.get("input_fields") or {}).items()) or "（无字段）")
        for e in endpoints[:20])
    sysmsg = (
        "你是 gRPC 接口黑盒测试用例生成器。铁律：service+method 只能引用给定清单中真实存在的"
        "rpc，虚构任何 method=整条剔除。request 只能使用该 rpc 请求体 message 里真实存在的字段。"
        "只输出 JSON：{\"calls\":[{\"service\":\"Shop\",\"method\":\"GetOrder\","
        "\"request\":{...}}]}，不要任何解释。覆盖清单中的每个 rpc 至少 1 条调用，"
        "并包含 1 条预期失败/不存在的输入（如不存在的 id）。")
    last = ""
    feedback = ""
    for attempt in range(1, attempts + 1):
        usr = f"需求：\n{requirement}\n\nproto 清单（唯一合法来源）——\n{svc_desc}\n"
        if feedback:
            usr += f"\n上一轮以下内容被闸剔除，必须修正后重新输出全部调用：\n{feedback}"
        chat = _make_chat(api_key=api_key, temperature=0,
                          timeout=120, max_tokens=2048, phase="grpc_gen")
        resp = chat.invoke([("system", sysmsg), ("user", usr)])
        last = getattr(resp, "content", "") or ""
        raw = last.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            feedback = "输出不是完整可解析的 {\"calls\":[...]} JSON"
            continue
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            feedback = "输出 JSON 不可解析（请精简）"
            continue
        calls = data.get("calls") if isinstance(data, dict) else None
        if calls is None:
            feedback = "输出缺少 calls 数组"
            continue
        kept, rejected = call_gate(calls, endpoints)
        if kept:
            return kept, rejected
        feedback = "\n".join(r["rejected_reason"] for r in rejected[:5])
    raise RuntimeError(f"LLM gRPC 调用生成未能在 {attempts} 轮内产出过闸调用"
                       f"（最后问题：{feedback[:200]}）")
