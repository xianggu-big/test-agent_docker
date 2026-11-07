"""黑包阶段4·gRPC 真机验收（docs/黑盒能力补齐任务包-20261106.md 阶段4【验收】）。

验收条目逐条：
1. 含 .proto 的 demo 服务（data/demo/grpc-shop，grpcio 服务器无 reflection）→
   proto 清单提取正确（service/rpc/message）；
2. 合法 RPC 调用通过（grpcurl -proto 模式跨容器真实调用，LLM 按 message 字段构造请求）；
3. 虚构 method 被闸门剔除留痕（rejected_reason 可审计）；
4. grpc_asserts 第 31 表落库（round 分档+rejected 留痕）。

护栏：tmp 库 data/tmp_applogs_accept/grpc_smoke.db；容器/网络 finally 回收；
不碰真实库、不跑真实任务链。产物：data/reports/grpc_assert_accept/smoke_grpc_<ts>.json
"""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "grpc_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "grpc_assert_accept"
DEMO = ROOT / "data" / "demo" / "grpc-shop"
TS = time.strftime("%Y%m%d-%H%M%S")
SUFFIX = uuid.uuid4().hex[:6]
NET = f"mta-grpc-net-{SUFFIX}"
SERVER = f"mta-smoke-grpc-server-{SUFFIX}"
GRPCURL = f"mta-smoke-grpcurl-{SUFFIX}"


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-grpc','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)", (uid, str(DEMO), "grpc-shop-demo", None)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)",
        (pid, uid, (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"), uid)).lastrowid
    conn.commit()

    from test_agent.sandbox.runner import SandboxRunner
    runner = SandboxRunner()
    client = runner.client
    api_key = __import__("os").environ.get("DEEPSEEK_API_KEY", "")
    checks: dict[str, object] = {}
    started = time.time()
    proto_text = (DEMO / "shop.proto").read_text(encoding="utf-8")
    try:
        network = client.networks.create(NET, check_duplicate=True)

        # —— demo 服务器（py312 + grpcio，无 reflection）——
        srv = client.containers.run("mta-test:py312", name=SERVER, detach=True,
                                    network=NET, command=["sleep", "infinity"])
        runner.copy_dir_into(srv, DEMO, "/work")
        runner.exec_in(srv, ["pip", "install", "-q", "-i",
                             "https://pypi.tuna.tsinghua.edu.cn/simple",
                             "grpcio", "grpcio-tools"], timeout_s=300, workdir="/work")
        r = runner.exec_in(srv, ["sh", "-c",
                                 "python3 -m grpc_tools.protoc -I. --python_out=. "
                                 "--grpc_python_out=. shop.proto && "
                                 "nohup python3 server.py > /tmp/srv.log 2>&1 & "
                                 "sleep 3 && cat /tmp/srv.log"],
                           timeout_s=60, workdir="/work")
        checks["demo gRPC 服务器就绪（无 reflection）"] = "serving" in (r.stdout or "")

        # —— grpcurl 执行容器（mta-test:grpcurl=alpine+官方静态二进制；官方极简镜像
        # 无 sh/sleep 长驻 exec 不可用——真机实锤，自建镜像见 docker/grpcurl-test/）——
        gurl = client.containers.run(config.IMAGE_GRPC, name=GRPCURL,
                                     detach=True, network=NET, command=["sleep", "infinity"])
        runner.copy_dir_into(gurl, DEMO, "/work")

        # —— 验收条目 1：proto 清单提取正确（D4 降级链第④级）——
        from test_agent.skills.endpoint_extract import parse_proto, proto_endpoints
        endpoints = proto_endpoints(parse_proto(proto_text))
        checks["proto 清单提取正确（Shop.GetOrder+ListOrders+字段）"] = (
            {(e["service"], e["rpc"]) for e in endpoints} == {("Shop", "GetOrder"),
                                                              ("Shop", "ListOrders")}
            and endpoints[0].get("input_fields") is not None)

        # —— 验收条目 2：LLM 按 message 字段构造请求 → method 闸 → grpcurl 真实调用 ——
        from test_agent.skills.grpc_assert import gen_grpc_calls
        requirement = (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8")
        kept, gen_rejected = gen_grpc_calls(api_key, endpoints, requirement)
        checks["LLM 生成调用过闸（覆盖合法 rpc）"] = len(kept) >= 2 and not gen_rejected

        from test_agent.skills.builtin import build_skills
        from test_agent.skills.registry import SkillRegistry
        skills = build_skills(runner)
        reg = SkillRegistry(conn=conn)
        for sk in skills.values():
            reg.register(sk)
        res = reg.invoke(tid, skills["grpc_assert"], {
            "calls": json.dumps(kept, ensure_ascii=False),
            "proto_text": proto_text, "proto_file": "shop.proto",
            "container_name": GRPCURL, "host": SERVER, "port": 50051, "round_no": 1})
        if res.status != "ok":
            raise RuntimeError(f"grpc_assert skill 执行失败：{res.error[:300]}")
        out = json.loads(res.output)
        checks["合法 RPC 真实调用通过（跨容器 grpcurl）"] = out.get("passed", 0) >= 2
        rows1 = out.get("rows", [])
        checks["不存在 id 的失败例如实落库（NOT_FOUND）"] = any(
            x["verdict"] == "failed" and "NotFound" in (x.get("error") or "")
            for x in rows1)

        # —— 验收条目 3（负例臂）：虚构 method 整条剔除留痕 ——
        neg = [{"service": "Shop", "method": "DeleteOrder", "request": {"id": 1}},
               {"service": "Shop", "method": "GetOrder", "request": {"id": 1}}]
        res2 = reg.invoke(tid, skills["grpc_assert"], {
            "calls": json.dumps(neg, ensure_ascii=False),
            "proto_text": proto_text, "proto_file": "shop.proto",
            "container_name": GRPCURL, "host": SERVER, "port": 50051, "round_no": 2})
        out2 = json.loads(res2.output) if res2.status == "ok" else {}
        checks["虚构 method 整条剔除（rejected=1）且合法放行（passed=1）"] = (
            out2.get("rejected") == 1 and out2.get("passed") == 1)

        # —— 验收条目 4：grpc_asserts 第 31 表落库（round 分档+留痕）——
        n_r1 = conn.execute(
            "SELECT COUNT(*) c FROM grpc_asserts WHERE task_id=? AND round_no=1",
            (tid,)).fetchone()["c"]
        n_rej = conn.execute(
            "SELECT COUNT(*) c FROM grpc_asserts WHERE task_id=? AND round_no=2 "
            "AND verdict='rejected'", (tid,)).fetchone()["c"]
        rej_reason = conn.execute(
            "SELECT rejected_reason FROM grpc_asserts WHERE task_id=? AND round_no=2 "
            "AND verdict='rejected' LIMIT 1", (tid,)).fetchone()
        checks["grpc_asserts 落库（round 分档+rejected 留痕）"] = (
            n_r1 >= 3 and n_rej == 1 and rej_reason is not None
            and "DeleteOrder" in (rej_reason["rejected_reason"] or ""))
    finally:
        for name in (GRPCURL, SERVER):
            try:
                runner.cleanup(name)
            except Exception as e:  # noqa: BLE001
                print("清理失败:", name, e)
        try:
            client.networks.get(NET).remove()
        except Exception as e:  # noqa: BLE001
            print("网络清理失败:", e)

    elapsed = round(time.time() - started, 1)
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid,
              "elapsed_s": elapsed, "server": SERVER, "grpcurl": GRPCURL,
              "network": NET, "checks": checks, "tmp_db": str(TMP_DB)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"smoke_grpc_{TS}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-grpc] {elapsed}s | 证据 → {out}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
