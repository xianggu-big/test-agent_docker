"""db_probe —— 数据库感知测试环境 v1（2026-11-03，用户需求：带库程序测试时库要参与）。

分层原则（与用户共识）：
- **扫描只读源码**：SQL 语句/DB 访问代码在源码里，AI 靠它理解"程序怎么用库"——
  数据文件内容从不进 AI 视野；
- **历史数据永不进考场**：7GB 运行残留不搬（.gitignore 排除），测试要的是
  空库+结构+最小种子，不是历史数据；
- **执行带库**：v1 对 SQLite 家族自动建库（跑迁移/种子 SQL），服务端库
  （MySQL/PG/Mongo）v1 如实降级提示（自动起服务端列 roadmap）。

全部确定性代码，fail-open：任何失败只留提示不炸 provision。
"""
import json
import logging
import re
import sqlite3
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

# DB 客户端特征（按 family 分组的 import/连接串正则）
_DB_PATTERNS = {
    "sqlite": [re.compile(r"require\s*\(\s*['\"]better-sqlite3['\"]"),
               re.compile(r"require\s*\(\s*['\"]sqlite3['\"]"),
               re.compile(r"import\s+sqlite3\b"),
               re.compile(r"sqlite3\.connect\(|better-sqlite3|sqlite:///")],
    "mysql": [re.compile(r"require\s*\(\s*['\"]mysql2?(/promise)?['\"]"),
              re.compile(r"import\s+pymysql|import\s+mysql\.connector"),
              re.compile(r"mysql://|:\d{2,5}/mysql\b")],
    "pg": [re.compile(r"require\s*\(\s*['\"]pg['\"]"),
           re.compile(r"import\s+psycopg"),
           re.compile(r"postgres://|postgresql://")],
    "mongo": [re.compile(r"require\s*\(\s*['\"]mongoose['\"]|require\s*\(\s*['\"]mongodb['\"]"),
              re.compile(r"import\s+pymongo|mongodb://|mongodb\+srv://")],
}

_SKIP = {".git", "node_modules", ".venv", "venv", "dist", "build", "data", "__pycache__",
         ".zcode", ".workbuddy", "coverage"}
_SRC_EXTS = (".js", ".ts", ".mjs", ".cjs", ".py")


def detect_db_usage(ws: Path, max_files: int = 400) -> dict:
    """确定性检测（纯函数）：源码里 DB 客户端特征 → {family, evidence, scripts, sql_files}。
    多家族命中取证据最多者（混用库的项目 v1 只备主力）；无命中 family=None（诚实）。"""
    ws = Path(ws)
    hits: dict[str, list[str]] = {}
    scripts: list[str] = []
    sql_files: list[str] = []
    scanned = 0
    for p in sorted(ws.rglob("*")):
        if not p.is_file() or scanned >= max_files:
            continue
        # 相对 ws 的路径做排除判断（绝对路径的 parts 里含 workspaces 上级的 data/ 会误杀全部文件）
        rel_parts = p.relative_to(ws).parts
        if any(part in _SKIP for part in rel_parts[:-1]):
            continue
        rel = p.relative_to(ws).as_posix()
        if p.suffix.lower() in (".sql",):
            if re.search(r"(migration|schema|init|create|seed)", rel, re.I):
                sql_files.append(rel)
            continue
        if p.name in ("package.json",):
            try:
                pkg = json.loads(p.read_text(encoding="utf-8"))
                for k, v in (pkg.get("scripts") or {}).items():
                    if re.search(r"migrate|seed|db[:_-]?(init|push|setup)", k + " " + str(v), re.I):
                        scripts.append(k)
            except (ValueError, OSError):
                pass
            continue
        if p.suffix.lower() not in _SRC_EXTS:
            continue
        scanned += 1
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:20000]
        except OSError:
            continue
        for fam, pats in _DB_PATTERNS.items():
            for pat in pats:
                m = pat.search(head)
                if m:
                    hits.setdefault(fam, []).append(f"{rel}: {m.group(0)[:50]}")
                    break
    family = max(hits, key=lambda k: len(hits[k])) if hits else None
    return {"family": family, "evidence": hits.get(family, [])[:8],
            "all_families": {k: len(v) for k, v in hits.items()},
            "scripts": scripts[:5], "sql_files": sorted(sql_files)[:10]}


def _run_node_sql(container, runner, ws: Path, db_file: str, sql_files: list[str]) -> tuple[bool, str]:
    """better-sqlite3 在容器内执行 schema SQL（JS+sqlite 家族）。"""
    if not sql_files:
        return False, "无可执行的 SQL 文件"
    script = ("const db=require('better-sqlite3')(process.env.DB_FILE);"
              "const fs=require('fs');"
              + "".join(f"db.exec(fs.readFileSync({f!r},'utf8'));"
                        for f in sql_files) + "console.log('migrated');")
    (ws / ".db_migrate_tmp.js").write_text(script, encoding="utf-8")
    r = runner.exec_in(container, ["node", ".db_migrate_tmp.js"], timeout_s=120)
    (ws / ".db_migrate_tmp.js").unlink(missing_ok=True)
    ok = r.exit_code == 0
    return ok, (f"exit={r.exit_code} {(r.stderr or r.stdout)[-160:]}")


def prepare_database(runner, container, det: dict, ws: Path, lang: str) -> dict:
    """按家族备库（provision 调用，fail-open）：
    - sqlite+python：宿主侧 sqlite3 模块执行迁移/种子 SQL → 库文件随下次快照进容器；
    - sqlite+js(better-sqlite3 已装)：容器内 node 执行 SQL；
    - 有 migrate/seed 类 npm script：容器内 npm run（项目自己的备库逻辑最可信）；
    - mysql/pg/mongo：v1 如实降级（自动起服务端列 roadmap）。"""
    ws = Path(ws)
    family, lang = det["family"], (lang or "").lower()
    notes = []
    # ① 项目自带备库脚本最可信（npm run migrate / seed …）
    for s in det.get("scripts", []):
        if s in ("migrate", "seed", "db:init", "db:push", "db:setup", "db:migrate", "db:seed"):
            r = runner.exec_in(container, ["npm", "run", s], timeout_s=300)
            notes.append(f"npm run {s} exit={r.exit_code}")
            if r.exit_code == 0:
                return {"status": "ok", "note": "项目自带备库脚本执行成功：" + "、".join(notes)}
    # ② sqlite 家族
    if family == "sqlite":
        if lang == "python" and det.get("sql_files"):
            db_file = ws / "test_app.db"
            conn = sqlite3.connect(db_file)
            for f in det["sql_files"]:
                try:
                    conn.executescript((ws / f).read_text(encoding="utf-8"))
                    notes.append(f"{f} ✓")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{f} 失败:{e}")
            conn.commit()
            conn.close()
            return {"status": "ok", "note": "sqlite 宿主建库：" + "、".join(notes) +
                    "（库文件已入工作区，随快照进容器）"}
        if lang == "javascript":
            db_file = "test_app.db"
            ok, note = _run_node_sql(container, runner, ws, db_file, det.get("sql_files", []))
            if ok:
                return {"status": "ok", "note": f"sqlite 容器内建库（{db_file}）：" + note}
            notes.append(note)
    if family in ("mysql", "pg", "mongo"):
        # 卡 6 接线（2026-11-15）：旗标开且 pg/mariadb → 自动拉 sidecar（testcontainers
        # 同 pod 语义：sidecar 与被测容器共享网络栈，项目 DSN 的 127.0.0.1:port 直通，
        # 无需改写 DSN）；mongo 驱动语义复杂 v1 继续如实降级。默认关（MTA_DB_SIDECAR）。
        if config.DB_SIDECAR_ENABLED and family in ("mysql", "pg"):
            return _prepare_server_sidecar(runner, container, det, ws, family)
        return {"status": "skip",
                "note": f"检测到 {family} 服务端依赖：未开启自动 sidecar（MTA_DB_SIDECAR=1 可开）——"
                        f"测试用例若真连库会失败，失败明细会如实归因；建议项目提供 docker-compose 或内存库模式"}
    return {"status": "skip", "note": "检测到 DB 依赖但无自动备库路径（" + family + "）：" + "、".join(notes)}


_DEFAULT_SIDECAR_SPECS = {
    "pg": {"engine": "postgres", "port": 5432,
           "env": {"POSTGRES_USER": "mta", "POSTGRES_PASSWORD": "mta_sidecar",
                   "POSTGRES_DB": "mta_test"}},
    "mysql": {"engine": "mariadb", "port": 3306,
              "env": {"MARIADB_ROOT_PASSWORD": "mta_sidecar", "MARIADB_DATABASE": "mta_test"}},
}

_DSN_RE = re.compile(
    r"(?:postgres(?:ql)?|mysql|mariadb)://([^:\s/]+):([^@\s/]+)@[^/\s]+/([A-Za-z0-9_]+)")


def _sidecar_spec_for(family: str, evidence_text: str) -> dict:
    """确定性默认 spec + evidence DSN 凭据尽力提取（1138 实锤：项目 DSN 的密码/库名
    与默认值不符会 auth fail——源码里的连接串是最可信凭据源，提取不到用默认）。"""
    spec = json.loads(json.dumps(_DEFAULT_SIDECAR_SPECS[family]))   # 深拷贝
    m = _DSN_RE.search(evidence_text or "")
    if m:
        user, password, database = m.group(1), m.group(2), m.group(3)
        if family == "pg":
            spec["env"].update({"POSTGRES_USER": user, "POSTGRES_PASSWORD": password,
                                "POSTGRES_DB": database})
        else:
            spec["env"].update({"MARIADB_USER": user, "MARIADB_PASSWORD": password,
                                "MARIADB_DATABASE": database,
                                "MARIADB_ROOT_PASSWORD": password})
        spec["dsn_source"] = "evidence"
    else:
        spec["dsn_source"] = "default"
    return spec


def _prepare_server_sidecar(runner, container, det: dict, ws: Path, family: str) -> dict:
    """服务端库 sidecar 自动拉起（卡 6，652 postgres:16-alpine 先例的通用化）：
    同 pod 网络栈（sidecar network_mode=container:<被测容器>）→ 被测容器内
    127.0.0.1:port 直通；迁移=det.sql_files 宿主读内容→sidecar 内 CLI 导入；
    fail-open：任何失败如实留痕降级 skip，不炸 provision。生命周期=任务级
    （mta-dbsidecar-<tid>，builder finally 回收+服务启动清扫兜底）。"""
    from .db_sidecar import ensure_db_sidecar, run_migrations
    evidence_text = " ".join(det.get("evidence", []))
    try:
        spec = _sidecar_spec_for(family, evidence_text)
    except Exception as e:      # noqa: BLE001
        return {"status": "skip", "note": f"sidecar spec 构建失败（{e}）——如实降级"}
    tid = "".join(ch for ch in container.name if ch.isdigit()) or "x"
    name = f"mta-dbsidecar-{tid}"
    try:
        sidecar = ensure_db_sidecar(runner, name, spec,
                                    network_mode=f"container:{container.name}")
    except Exception as e:      # noqa: BLE001
        return {"status": "skip",
                "note": f"sidecar 拉起失败（{type(e).__name__}: {str(e)[:150]}）——如实降级，"
                        f"测试用例若真连库会失败并如实归因"}
    notes = [f"sidecar={name} engine={spec['engine']} 凭据来源={spec['dsn_source']}"
             f"（同网络栈，127.0.0.1:{spec['port']} 直通）"]
    if det.get("sql_files"):
        mig = [(f, (ws / f).read_text(encoding="utf-8", errors="ignore"))
               for f in det["sql_files"] if (ws / f).exists()]
        for line in run_migrations(runner, sidecar, spec, mig):
            notes.append(line[:80])
    return {"status": "ok", "note": "；".join(notes)}
