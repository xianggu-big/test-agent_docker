"""项目归一（B2 / 2026-11-01 评估：本地路径原串作键 → 每次重跑新建"项目"，
280 任务散在几十个单任务组里）。

规则：
- 本地路径：Path.resolve() 规范化（统一分隔符、消 ../、Windows 大小写归一）→ key=`local:<normcase abs>`
- 远程 URL：去尾部斜杠/.git、scheme+host 小写、path 保留大小写 → key=`url:<...>`
- 其它（git@/ssh 无 scheme）：去 .git 尾缀归一 → key=`url:<...>`

兼容：老库 projects 无 repo_key 列时，按 repo_url 现算（映射在内存里）——
不依赖迁移是否已跑过；`backfill` 负责把列写实并按 key 合并重复项目。
"""
import os
import re
from pathlib import Path

_CASE_INSENSITIVE_HOSTS = {"github.com", "www.github.com", "gitlab.com", "bitbucket.org"}


def normalize_repo_key(src: str) -> str:
    """把 repo_url / 本地路径归一成稳定项目键（同源不同写法 → 同一个 key）。
    D3（2026-11-20 设计方案）：纯语法分类先行——UNC（\\\\server\\share 或 //server/share）
    与设备路径在触碰文件系统之前拒绝（Windows UNC 的 isdir/resolve 会发起网络认证
    访问=确认前即可触发宿主网络路径；设备路径 \\\\.\\ 直接拒）。"""
    s = (src or "").strip()
    if not s:
        return "local:"
    if s.startswith(("\\\\", "//")):
        raise ValueError(
            "UNC 网络路径不受支持（D3 本地安全边界：路径解析会触发网络访问）——"
            "请使用本地目录或 http(s) 仓库 URL")
    looks_path = (
        os.path.isdir(s) or s.startswith(("/", "\\")) or s.startswith("~")
        or re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith(".\\") or s.startswith("../")
        or s.startswith("./")
    )
    if looks_path:
        try:
            p = Path(os.path.expanduser(s)).resolve()
        except OSError:
            p = Path(os.path.expanduser(s)).absolute()
        return "local:" + os.path.normcase(str(p))
    if re.match(r"^https?://", s, re.I):
        m = re.match(r"^(https?)://([^/]+)(/.*)?$", s, re.I)
        if not m:
            return "url:" + s.rstrip("/")
        scheme, host, path = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "")
        path = re.sub(r"/+$", "", path)
        path = re.sub(r"\.git$", "", path, flags=re.I)
        # 已知大小写不敏感 host：path 一并小写（GitHub/GitLab/Bitbucket 实际不区分，
        # Owner/Repo 与 owner/repo 是同一仓库）；自建服务保留原样（可能大小写敏感）
        if host in _CASE_INSENSITIVE_HOSTS:
            path = path.lower()
        return f"url:{scheme}://{host}{path}"
    # git@host:owner/repo.git / ssh://...
    t = re.sub(r"/+$", "", s)
    t = re.sub(r"\.git$", "", t, flags=re.I)
    return "url:" + t


def project_display_name(src: str) -> str:
    """项目显示名：URL/路径最后一段（去 .git 尾缀）。"""
    s = (src or "").strip().rstrip("/\\")
    if re.match(r"^https?://|^ssh://|^[^/]+@", s):
        s = re.sub(r"\.git$", "", s, flags=re.I)
    seg = re.split(r"[/\\]", s)
    name = next((x for x in reversed(seg) if x), "")
    return name or "project"


def ensure_project(conn, user_id: int, repo_url: str) -> tuple[int, str, bool]:
    """按归一键复用项目行；不存在才新建。返回 (project_id, name, created)。

    repo_key 列存在则写入（老库列缺失时退化为按 repo_url 现算匹配，不报错）。
    """
    key = normalize_repo_key(repo_url)
    name = project_display_name(repo_url)
    has_col = any(r["name"] == "repo_key"
                  for r in conn.execute("PRAGMA table_info(projects)"))
    if has_col:
        row = conn.execute("SELECT id, name FROM projects WHERE repo_key=?", (key,)).fetchone()
        if row:
            return row["id"], row["name"] or name, False
    # 列缺失/未命中：遍历现算（老库重复行也能命中最早那条）
    for r in conn.execute("SELECT id, name, repo_url FROM projects ORDER BY id"):
        if normalize_repo_key(r["repo_url"] or "") == key:
            return r["id"], r["name"] or name, False
    cols = "user_id, repo_url, name" + (", repo_key" if has_col else "")
    vals = [user_id, repo_url, name] + ([key] if has_col else [])
    pid = conn.execute(
        f"INSERT INTO projects({cols}) VALUES ({','.join('?' * len(vals))})", vals).lastrowid
    conn.commit()
    return pid, name, True


def backfill_project_keys(conn, dry_run: bool = True) -> dict:
    """把 repo_key 写实 + 合并重复项目（modules/test_tasks.project_id 迁移到保留行）。

    保留策略：同 key 取 id 最小者（最早创建，任务历史最全）。
    modules 有 UNIQUE(project_id, path)：合并时若 keep 侧已有同 path 模块，把
    test_scripts.module_id 改指 keep 模块后删除 dup 模块（否则 UNIQUE/FK 双撞）。
    返回 {groups, merged, tasks_moved, modules_moved, detail}；dry_run=True 只统计不写。
    """
    has_col = any(r["name"] == "repo_key"
                  for r in conn.execute("PRAGMA table_info(projects)"))
    rows = [dict(r) for r in conn.execute(
        "SELECT id, repo_url, name, user_id FROM projects ORDER BY id")]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(normalize_repo_key(r["repo_url"] or ""), []).append(r)
    merged = 0
    tasks_moved = 0
    modules_moved = 0
    detail = []

    def _count_tasks(pid):
        return conn.execute("SELECT COUNT(*) AS n FROM test_tasks WHERE project_id=?",
                            (pid,)).fetchone()["n"]

    for key, members in groups.items():
        keep = members[0]
        if not dry_run:
            if has_col and not keep.get("repo_key"):
                conn.execute("UPDATE projects SET repo_key=? WHERE id=?", (key, keep["id"]))
            for dup in members[1:]:
                # ① modules：同 path 合并（改 test_scripts.module_id 指向），否则改归属
                keep_paths = {r["path"]: r["id"] for r in conn.execute(
                    "SELECT id, path FROM modules WHERE project_id=?", (keep["id"],))}
                for m in conn.execute("SELECT id, path FROM modules WHERE project_id=?",
                                      (dup["id"],)).fetchall():
                    if m["path"] in keep_paths:
                        conn.execute("UPDATE test_scripts SET module_id=? WHERE module_id=?",
                                     (keep_paths[m["path"]], m["id"]))
                        conn.execute("DELETE FROM modules WHERE id=?", (m["id"],))
                    else:
                        conn.execute("UPDATE modules SET project_id=? WHERE id=?",
                                     (keep["id"], m["id"]))
                    modules_moved += 1
                # ② 任务归属
                n = _count_tasks(dup["id"])
                conn.execute("UPDATE test_tasks SET project_id=? WHERE project_id=?",
                             (keep["id"], dup["id"]))
                conn.execute("DELETE FROM projects WHERE id=?", (dup["id"],))
                merged += 1
                tasks_moved += n
        else:
            for dup in members[1:]:
                n = _count_tasks(dup["id"])
                m_n = conn.execute("SELECT COUNT(*) AS n FROM modules WHERE project_id=?",
                                   (dup["id"],)).fetchone()["n"]
                merged += 1
                tasks_moved += n
                modules_moved += m_n
        if len(members) > 1:
            detail.append({"key": key, "keep": keep["id"], "dup_ids": [m["id"] for m in members[1:]],
                           "name": keep["name"]})
    if not dry_run:
        conn.commit()
        try:   # 唯一索引防未来再产生重复（历史重复已合并，正常应成功）
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_key ON projects(repo_key)")
            conn.commit()
        except Exception:      # noqa: BLE001 —— 仍有重复（并发插入）时不阻断主流程
            pass
    return {"groups": len(groups), "merged": merged, "tasks_moved": tasks_moved,
            "modules_moved": modules_moved, "detail": detail, "dry_run": dry_run}
