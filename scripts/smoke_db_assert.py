"""黑包阶段2·DB sidecar + db_assert 真机验收（docs/黑盒能力补齐任务包-20261106.md 阶段2【验收】）。

验收条目逐条：
1. 含 MySQL 的 demo 项目（data/demo/db-shop，orders 表迁移+种子）→ sidecar 建库成功；
2. 被测容器经 docker network 连接 sidecar（第二容器 client 形态跨容器 SELECT）；
3. db_assert 只读 SQL 验证通过/失败各一例（经 SkillRegistry 正式通道）；
4. 越权写 SQL（UPDATE）与注入形态（SELECT 1; DROP TABLE）被闸门剔除且留痕。

护栏：tmp 库 data/tmp_applogs_accept/db_smoke.db；容器/网络 finally 回收；
不碰真实库、不跑真实任务链。产物：data/reports/db_assert_accept/smoke_db_<ts>.json
"""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.sandbox.runner import SandboxRunner  # noqa: E402

TMP_DB = config.DATA_DIR / "tmp_applogs_accept" / "db_smoke.db"
REPORT_DIR = config.DATA_DIR / "reports" / "db_assert_accept"
DEMO = ROOT / "data" / "demo" / "db-shop"
TS = time.strftime("%Y%m%d-%H%M%S")
SUFFIX = uuid.uuid4().hex[:6]
NET = f"mta-dba-net-{SUFFIX}"
SIDECAR = f"mta-smoke-db-{SUFFIX}"
CLIENT = f"mta-smoke-dbclient-{SUFFIX}"

MIGRATIONS = [
    ("001_init.sql",
     "CREATE TABLE IF NOT EXISTS orders (\n"
     "  id INT AUTO_INCREMENT PRIMARY KEY,\n"
     "  customer VARCHAR(64) NOT NULL,\n"
     "  amount DECIMAL(10,2) NOT NULL,\n"
     "  status VARCHAR(16) NOT NULL DEFAULT 'created'\n"
     ") ENGINE=InnoDB;\n"),
    ("002_seed.sql",
     "INSERT INTO orders(customer, amount, status) VALUES\n"
     " ('alice', 120.50, 'created'),\n"
     " ('bob', 80.00, 'paid'),\n"
     " ('carol', 45.25, 'created'),\n"
     " ('dave', 200.00, 'paid'),\n"
     " ('erin', 9.99, 'cancelled');\n"),
]


def main() -> int:
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    if TMP_DB.exists():
        TMP_DB.unlink()
    config.DB_PATH = TMP_DB
    from test_agent.store.db import get_conn, init_db
    init_db()
    conn = get_conn()
    uid = conn.execute("INSERT INTO users(username,password_hash) VALUES('smoke-db','x')").lastrowid
    pid = conn.execute("INSERT INTO projects(user_id,repo_url,name,primary_language) "
                       "VALUES(?,?,?,?)", (uid, str(DEMO), "db-shop-demo", None)).lastrowid
    tid = conn.execute(
        "INSERT INTO test_tasks(project_id,user_id,requirement_text,confirmed,confirmed_by)"
        " VALUES(?,?,?,?,1)", (pid, uid, (DEMO / "REQUIREMENT.md").read_text(encoding="utf-8"),
                               uid)).lastrowid
    conn.commit()

    runner = SandboxRunner()
    client = runner.client
    checks: dict[str, object] = {}
    started = time.time()
    try:
        # —— sidecar 起库 + 迁移种子（验收条目 1）——
        network = client.networks.create(NET, check_duplicate=True)
        from test_agent.skills.db_sidecar import ensure_db_sidecar, run_migrations
        spec = {"engine": "mariadb", "port": 3306,
                "env": {"MARIADB_ROOT_PASSWORD": "smoke-root-pw",
                        "MARIADB_DATABASE": "shop"}}
        sidecar = ensure_db_sidecar(runner, SIDECAR, spec, network_name=NET)
        checks["sidecar 建库成功（mariadb:11 就绪）"] = sidecar.status == "running"
        mig_out = run_migrations(runner, sidecar, spec, MIGRATIONS)
        checks["迁移+种子 SQL 执行 exit=0"] = all("exit=0" in o for o in mig_out) and len(mig_out) == 2

        # —— 跨容器网络互通（验收条目 2：被测容器形态经 docker network 连 sidecar 容器名）——
        client.containers.run("mariadb:11", name=CLIENT, detach=True, network=NET,
                              command=["sleep", "infinity"])
        runner.exec_in(client.containers.get(CLIENT),
                       ["sh", "-c", "sleep 3"], timeout_s=10)
        r = runner.exec_in(
            client.containers.get(CLIENT),
            ["sh", "-c", "mariadb -h " + SIDECAR + " -uroot -psmoke-root-pw shop "
                         "-e 'SELECT COUNT(*) FROM orders' 2>&1"],
            timeout_s=30, workdir="/")
        checks["跨容器经 network 连 sidecar 容器名 SELECT"] = r.exit_code == 0 and "5" in r.stdout

        # —— db_assert 经 SkillRegistry 正式通道（验收条目 3+4）——
        from test_agent.skills.builtin import build_skills
        from test_agent.skills.registry import SkillRegistry
        skills = build_skills(runner)
        reg = SkillRegistry(conn=conn)
        for sk in skills.values():
            reg.register(sk)
        def invoke(sql: str, round_no: int) -> dict:
            res = reg.invoke(tid, skills["db_assert"],
                             {"sql": sql, "container_name": SIDECAR,
                              "engine": "mariadb", "database": "shop", "round_no": round_no})
            return json.loads(res.output) if res.status == "ok" else {"status": res.status,
                                                                      "error": res.error}
        # 批1（round 1）= 一轮 db_assert 的正例批：通过例 + 失败例同批（gate 拆分逐条执行）
        b1 = invoke("SELECT COUNT(*) FROM orders WHERE status='paid';\n"
                    "SELECT * FROM nonexistent_table_xyz", 1)
        rows1 = b1.get("rows", [])
        checks["只读验证通过例（SELECT 计数 paid=2）"] = any(
            x["verdict"] == "passed" and x.get("row_count") == 2 for x in rows1)
        checks["只读验证失败例（表不存在 → failed 如实）"] = any(
            x["verdict"] == "failed" for x in rows1)
        # 批2（round 2）= 越权写负例批；批3（round 3）= 注入负例批（独立调用，真实链路一轮一批）
        b2 = invoke("UPDATE orders SET status='hacked'", 2)
        checks["越权 UPDATE 被闸剔除且留痕"] = (
            b2.get("rows") and b2["rows"][0]["verdict"] == "rejected"
            and "UPDATE" in b2["rows"][0].get("rejected_reason", "").upper())
        b3 = invoke("SELECT 1; DROP TABLE orders", 3)
        checks["注入形态（SELECT 1; DROP）整案拒绝"] = (
            b3.get("rows") and all(x["verdict"] == "rejected" for x in b3["rows"]))
        # 注入形态后 orders 表必须仍在（真没执行 DROP 的机器实证）
        r2 = runner.exec_in(sidecar, ["sh", "-c",
                                      "mariadb -uroot -psmoke-root-pw shop -e 'SHOW TABLES' 2>&1"],
                            timeout_s=30, workdir="/")
        checks["注入后 orders 表完好（DROP 确未执行）"] = r2.exit_code == 0 and "orders" in r2.stdout
        n_rows = conn.execute("SELECT COUNT(*) c FROM db_asserts WHERE task_id=?", (tid,)).fetchone()["c"]
        n_rej = conn.execute("SELECT COUNT(*) c FROM db_asserts WHERE task_id=? AND verdict='rejected'",
                             (tid,)).fetchone()["c"]
        n_r1 = conn.execute("SELECT COUNT(*) c FROM db_asserts WHERE task_id=? AND round_no=1",
                            (tid,)).fetchone()["c"]
        checks["db_asserts 落库（3 轮分档+rejected 留痕）"] = n_rows == 4 and n_rej >= 2 and n_r1 == 2
    finally:
        for name in (CLIENT, SIDECAR):
            try:
                runner.cleanup(name)
            except Exception as e:  # noqa: BLE001
                print("清理失败:", name, e)
        try:
            client.networks.get(NET).remove()
        except Exception as e:  # noqa: BLE001
            print("网络清理失败:", e)

    elapsed = round(time.time() - started, 1)
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "task_id": tid, "elapsed_s": elapsed,
              "sidecar": SIDECAR, "network": NET, "checks": checks,
              "tmp_db": str(TMP_DB)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"smoke_db_{TS}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print(f"[smoke-db] {elapsed}s | 证据 → {out}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
