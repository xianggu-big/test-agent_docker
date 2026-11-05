"""缺陷交接层：product_defect 证据 → 标准缺陷报告单 + AI 修复提示词（确定性，零 LLM）。

定位（用户 2026-11-04 提出"测试→开发/AI 迭代闭环"）：
- 归因三分类判出 product_defect 后，把「测试失败且用例自身有预期依据」的行整理成
  两份交付物：①标准缺陷报告单（Markdown，六要素血统：编号/标题/优先级/输入/
  步骤/预期）②可直接复制给任意 AI 的修复提示词（带护栏）。
- 纪律一（数字确定性）：缺陷清单全部由 SQL 确定性导出（test_case_results /
  test_cases / rtm_maps / test_reports），与评估链同源，LLM 不参与生成。
- 纪律二（依据分层）：expected_basis → 置信度。需求明示/docstring=高，源码推导=中，
  常识/未标注=低——治预言误差（arXiv 2412.14137）：高置信可直接开修，中低置信
  建议人工先复核预期是否成立。
- 纪律三（oracle 诚实）：修复提示词必须带护栏——禁止改断言/证人；AI 若判断预期
  本身错误，出口是"反驳证据交人工裁决"，不是改代码迁就。
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .. import config

# expected_basis → 置信度（§7 证据分层；子串匹配兜底防 LLM 措辞漂移）
_CONFIDENCE = [("需求明示", "高"), ("docstring", "高"), ("源码推导", "中"), ("常识", "低")]
_PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2, "未标注": 3}

_RUN_CMD = {
    "python": "pytest <生成用例文件> -q（容器 mta 镜像内）",
    "javascript": "npx jest <生成用例文件>（容器 mta 镜像内）",
}


def confidence_of(basis: str | None) -> str:
    """预期依据 → 置信度（高/中/低）。未标注按低处理并如实显示原值。"""
    b = basis or ""
    for key, conf in _CONFIDENCE:
        if key in b:
            return conf
    return "低"


def _load_meta(conn, task_id: int) -> dict | None:
    """任务级元信息（需求/项目/镜像/终态/覆盖率/AI 分析缓存）。不存在返回 None。"""
    row = conn.execute(
        """SELECT t.id, t.requirement_text, t.status, p.name, p.repo_url,
                  p.primary_language, r.coverage_line, r.defects, r.analysis
           FROM test_tasks t
           JOIN projects p ON p.id = t.project_id
           LEFT JOIN test_reports r ON r.id = (
               SELECT id FROM test_reports WHERE task_id = t.id ORDER BY id DESC LIMIT 1)
           WHERE t.id = ?""", (task_id,)).fetchone()
    if row is None:
        return None
    img = conn.execute(
        "SELECT image FROM container_jobs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    defects_tag = {}
    try:
        defects_tag = json.loads(row["defects"]) if row["defects"] else {}
    except (json.JSONDecodeError, TypeError):
        pass
    analysis = None
    try:
        analysis = json.loads(row["analysis"]) if row["analysis"] else None
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "task_id": task_id, "requirement": (row["requirement_text"] or "").strip(),
        "status": row["status"], "project": row["name"] or f"project#{row['id']}",
        "repo_url": row["repo_url"] or "", "language": row["primary_language"] or "unknown",
        "image": img["image"] if img else "（无台账记录）",
        "coverage_line": row["coverage_line"],
        "failure_kind": defects_tag.get("failure_kind"), "stop_reason": defects_tag.get("stop_reason"),
        "analysis": analysis,
    }


def _load_case_index(conn, task_id: int) -> dict[str, dict]:
    """用例名 → 最新版本的六要素字段（A5 保证 name 与 nodeid 对齐）。"""
    rows = conn.execute(
        """SELECT c.name, c.expected, c.expected_basis, c.priority, c.method, c.content,
                  s.version
           FROM test_cases c JOIN test_scripts s ON s.id = c.script_id
           WHERE s.task_id = ? ORDER BY s.version ASC, c.id ASC""", (task_id,)).fetchall()
    index: dict[str, dict] = {}
    for r in rows:  # 后写入覆盖先写入 = 最新版本胜出
        index[r["name"]] = {"expected": r["expected"], "basis": r["expected_basis"],
                            "priority": r["priority"], "method": r["method"],
                            "content": r["content"]}
    return index


def _load_condition_index(conn, task_id: int) -> dict[str, dict]:
    """用例名 → RTM 条件（matrix 快照：[{condition_id, condition, cases, ...}]）。"""
    row = conn.execute(
        "SELECT matrix FROM rtm_maps WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    if row is None:
        return {}
    try:
        matrix = json.loads(row["matrix"])
    except (json.JSONDecodeError, TypeError):
        return {}
    index: dict[str, dict] = {}
    for m in matrix if isinstance(matrix, list) else []:
        for case in (m.get("cases") or []):
            index.setdefault(str(case), m)
    return index


def load_defects(conn, task_id: int) -> list[dict]:
    """疑似产品缺陷清单：generated 用例的 failed/error 行 × 六要素 × RTM 条件。

    - 只收 origin='generated'（带 expected_basis 证据链）；repo 自带测试失败
      不上单（无依据，列存量问题，由调用方另行汇总）。
    - 同一用例多轮失败 → 合并一条（记录失败轮次序列，消息取最新轮）。
    - 排序：优先级 高→中→低，再按置信度 高→中→低。
    """
    case_index = _load_case_index(conn, task_id)
    cond_index = _load_condition_index(conn, task_id)
    rows = conn.execute(
        """SELECT round_no, level, nodeid, name, status, message
           FROM test_case_results
           WHERE task_id=? AND origin='generated' AND status IN ('failed','error')
           ORDER BY round_no ASC, name ASC""", (task_id,)).fetchall()
    merged: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["level"], r["name"])
        d = merged.get(key)
        if d is None:
            info = case_index.get(r["name"], {})
            cond = cond_index.get(r["name"], {})
            basis = info.get("basis")
            d = {
                "level": r["level"], "name": r["name"], "nodeid": r["nodeid"],
                "status": r["status"], "message": (r["message"] or "").strip(),
                "rounds": [r["round_no"]],
                "expected": info.get("expected") or "（用例库缺失，见测试代码断言）",
                "basis": basis, "confidence": confidence_of(basis),
                "priority": info.get("priority") or "未标注",
                "method": info.get("method") or "未标注",
                "content": info.get("content") or "（用例代码未落库，nodeid 见上）",
                "condition": cond.get("condition"),
                "requirement_ref": cond.get("requirement_ref") if "requirement_ref" in cond.keys() else None,
            }
            merged[key] = d
        else:
            d["rounds"].append(r["round_no"])
            d["message"] = (r["message"] or "").strip() or d["message"]
            d["status"] = r["status"]
    defects = sorted(merged.values(),
                     key=lambda d: (_PRIORITY_ORDER.get(d["priority"], 3),
                                    ["高", "中", "低"].index(d["confidence"])))
    for i, d in enumerate(defects, 1):
        d["bug_id"] = f"BUG-{i:02d}"
    return defects


def _fold(text: str, limit: int = 400) -> str:
    """多行消息折成单行（⏎ 分隔）防 Markdown 断版式，超限截断。"""
    return (text or "").strip().replace("\r", "").replace("\n", " ⏎ ")[:limit]


def _analysis_lookup(meta: dict, name: str) -> dict:
    """AI 归因缓存中该用例的 root_cause/fix（推测性内容，仅作参考）。"""
    a = meta.get("analysis") or {}
    for item in a.get("defects", []) if isinstance(a, dict) else []:
        if isinstance(item, dict) and name in str(item.get("case") or item.get("name") or ""):
            return item
    return {}


# —— E（任务卡-369 20261105）缺陷去重/同根因聚类：确定性签名，零 LLM 零新依赖 ——
# 调研锚点：Sentry Issue Grouping（只取 in-app frames）/ ReBucket（ICSE 2012）桶思想；
# DeepLSH（arXiv 2310.06703）为学习型方案，本项目单任务失败 N≈4-40 条不需要（诚实取舍）。

def _norm_message(msg: str) -> str:
    """消息归一化：数字→<N>、引号内字符串→<S>、路径→<PATH>——369 的 4 条
    TypeError 消息归一化后同构（仅变量名/id 字面差异）。"""
    s = msg or ""
    s = re.sub(r"\d+(?:\.\d+)?", "<N>", s)
    s = re.sub(r"\"[^\"]*\"|'[^']*'", "<S>", s)
    s = re.sub(r"(?:[A-Za-z]:)?(?:/|\\)[\w./\\-]+", "<PATH>", s)
    return s


def _error_class_of(msg: str) -> str:
    """错误类提取：jest/pytest 失败行的首错误类名（TypeError/SyntaxError/AssertionError…）。"""
    m = re.search(r"\b([A-Z]\w*(?:Error|Exception))\b", msg or "")
    return m.group(1) if m else "unknown"


_FRAME_EXCLUDE_RE = re.compile(
    r"evalmachine|test_gen_r\d|test_gen_fb|test_imported|node_modules|jest|pytest")


def _sut_frame(msg: str) -> str:
    """SUT 首帧（Sentry in-app frames 思想）：堆栈中第一个指向被测项目源文件的帧；
    错误全在用例侧（369 形态）→ 空串 → 自然聚为「用例侧缺陷」组。"""
    for m in re.finditer(r"^\s*at\s+(.+)$", msg or "", re.M):
        frame = m.group(1).strip()
        if frame and not _FRAME_EXCLUDE_RE.search(frame):
            return frame[:160]
    return ""


def defect_signature(d: dict, failure_kind: str = "") -> str:
    """同根因签名 = sha1(failure_kind | error_class | norm_message | sut_frame)[:12]"""
    raw = " | ".join([failure_kind or "", _error_class_of(d.get("message")),
                      _norm_message(d.get("message")), _sut_frame(d.get("message"))])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def cluster_defects(defects: list[dict], failure_kind: str = "") -> list[dict]:
    """按签名分桶。返回组清单（每组含签名/成员 bug_id/组内最高置信），并给每个
    defect 写 cluster/cluster_size 字段（渲染按组展示「缺陷组 N，影响 K 用例」）。"""
    buckets: dict[str, list[dict]] = {}
    for d in defects:
        sig = defect_signature(d, failure_kind)
        d["cluster"] = sig
        buckets.setdefault(sig, []).append(d)
    groups = []
    for sig, members in buckets.items():
        groups.append({
            "signature": sig, "members": [m["bug_id"] for m in members],
            "size": len(members),
            "top_confidence": min((m["confidence"] for m in members),
                                  key=lambda c: ["高", "中", "低"].index(c) if c in ("高", "中", "低") else 3),
        })
    for g in groups:
        for m in buckets[g["signature"]]:
            m["cluster_size"] = g["size"]
    return groups


def render_defect_report(meta: dict, defects: list[dict]) -> str:
    """标准缺陷报告单（Markdown）。数字与引用全部来自加载层数据。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cov = f"{meta['coverage_line']:.0%}" if meta.get("coverage_line") is not None else "N/A"
    clusters = (cluster_defects(defects, (meta.get("defects_tag") or {}).get("failure_kind", ""))
                if config.DEFECT_CLUSTERING_ENABLED else [])
    basis_dist: dict[str, int] = {}
    for d in defects:
        key = d["basis"] or "未标注"
        basis_dist[key] = basis_dist.get(key, 0) + 1
    conf_dist: dict[str, int] = {}
    for d in defects:
        conf_dist[d["confidence"]] = conf_dist.get(d["confidence"], 0) + 1
    lines = [
        f"# 缺陷报告单（任务 #{meta['task_id']}）",
        "",
        f"> 生成：{now}｜口径：仅收「测试失败且用例有预期依据」（origin=generated）的疑似产品缺陷；"
        "用例缺陷与环境失败已由平台归因另行处理，自带测试（origin=repo）失败不计入本单。",
        "",
        "## 1. 元信息",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 项目 | {meta['project']}（{meta['language']}） |",
        f"| 仓库 | {meta['repo_url']} |",
        f"| 执行环境 | {meta['image']} |",
        f"| 需求原文 | {(meta['requirement'] or '（未填写）')[:200]} |",
        f"| 任务终态 | {meta['status']}｜行覆盖率 {cov} |",
        f"| 停止原因 | {meta.get('stop_reason') or '—'} |",
        "",
        "## 2. 汇总",
        "",
        f"- 疑似产品缺陷 **{len(defects)}** 条（置信度分布："
        + "、".join(f"{k} {v}" for k, v in sorted(conf_dist.items())) + "）",
        "- 依据分布：" + ("、".join(f"{k} {v}" for k, v in sorted(basis_dist.items())) or "—"),
        ("- 同根因分组 **{}** 组（确定性签名聚类；".format(len(clusters))
         + "；".join(f"组{i+1} 影响 {g['size']} 用例" for i, g in enumerate(clusters)) + "）")
        if clusters else "",
        "- 使用方式：高置信（需求明示/docstring）可直接开修；中低置信建议人工先复核预期"
        "是否成立（预言误差风险），再进修复。",
        "",
        "## 3. 缺陷清单",
    ]
    last_cluster = None
    for d in defects:
        if clusters and d.get("cluster") != last_cluster:
            last_cluster = d.get("cluster")
            g = next((g for g in clusters if g["signature"] == last_cluster), None)
            if g:
                lines += ["", f"### 缺陷组（签名 `{g['signature']}`，同根因 {g['size']} 条："
                          f"{'、'.join(g['members'])}——组内最高置信 {g['top_confidence']}）", ""]
        ana = _analysis_lookup(meta, d["name"])
        cond = f"{d['condition']}（需求关键句：{d['requirement_ref']}）" if d["condition"] else "（无 RTM 映射）"
        rounds = "、".join(f"第{r}轮" for r in d["rounds"])
        lines += [
            "",
            f"### {d['bug_id']}　{d['name']}",
            "",
            f"- 优先级：{d['priority']}｜置信度：**{d['confidence']}**（依据：{d['basis'] or '未标注'}）"
            f"｜设计方法：{d['method']}｜级别：{d['level']}",
            f"- 关联测试条件：{cond}",
            f"- 失败轮次：{rounds}（终态 {d['status']}）",
            f"- 复现命令：{_RUN_CMD.get(meta['language'], '见任务执行日志')}",
            f"- **预期结果**：{d['expected']}（依据：{d['basis'] or '未标注'}）",
            f"- **实际结果**：`{_fold(d['message'])}`",
            "",
            "失败用例代码（复现即跑）：",
            "",
            f"```{meta['language'] if meta['language'] != 'unknown' else ''}",
            d["content"].rstrip(),
            "```",
        ]
        if ana:
            lines += [
                "",
                f"AI 归因参考（推测，供定位）：root_cause={ana.get('root_cause', '—')}；"
                f"fix={ana.get('fix', '—')}；confidence={ana.get('confidence', '—')}",
            ]
    lines += [
        "",
        "## 4. 局限与免责",
        "",
        "- 本单由确定性代码从数据库导出，不含任何 LLM 生成数字；AI 归因参考为推测性内容，已逐条标注。",
        "- 「疑似产品缺陷」非终判：依据为「源码推导/常识」的条目存在预言误差可能（预期写错而代码正确），"
        "定性权在人工/开发。",
    ]
    return "\n".join(lines) + "\n"


def render_fix_prompt(meta: dict, defects: list[dict]) -> str:
    """可直接复制给任意 AI 的修复提示词（含护栏：禁改断言 + 反驳出口 + 验证要求）。"""
    lang = meta["language"]
    cov = f"{meta['coverage_line']:.0%}" if meta.get("coverage_line") is not None else "N/A"
    head = [
        "你是资深开发工程师。以下是自动化测试平台判定的「疑似产品缺陷」清单："
        "测试用例有明确预期依据，被测代码行为与预期不符。请逐条定位并修复被测代码。",
        "",
        "# 项目上下文",
        f"- 项目：{meta['project']}（仓库：{meta['repo_url']}，{lang}）",
        f"- 需求原文：{meta['requirement'] or '（未填写，以下列各条的预期与依据为准）'}",
        f"- 测试环境：{meta['image']}｜任务终态 {meta['status']}｜行覆盖率 {cov}",
        f"- 复现方式：{_RUN_CMD.get(lang, '按测试文件直接运行')}",
        "",
        "# 硬性约束（必须遵守）",
        "1. 只修改被测产品代码；**禁止修改、删除、跳过下列测试用例与断言**——每条预期值都有依据（已标注）。",
        "2. 若你判断某条预期本身错误（需求不存在该行为/与官方语义矛盾）：不要改代码迁就测试，"
        "输出反驳证据（引用需求原文或官方文档原文），把该条标记为「预期待人工裁决」后跳过。",
        "3. 每条修复后必须运行复现命令验证全绿，才算完成该条。",
        "4. 输出格式：逐条缺陷一节——根因（一句话）/ 修改文件清单 / 关键 diff / 验证输出。",
        "",
        f"# 缺陷清单（共 {len(defects)} 条，按优先级与置信度排序）",
    ]
    clusters = (cluster_defects(defects, (meta.get("defects_tag") or {}).get("failure_kind", ""))
                if config.DEFECT_CLUSTERING_ENABLED else [])
    body: list[str] = []
    last_cluster = None
    for d in defects:
        if clusters and d.get("cluster") != last_cluster:
            last_cluster = d.get("cluster")
            g = next((g for g in clusters if g["signature"] == last_cluster), None)
            if g:
                lines += ["", f"### 缺陷组（签名 `{g['signature']}`，同根因 {g['size']} 条："
                          f"{'、'.join(g['members'])}——组内最高置信 {g['top_confidence']}）", ""]
        ana = _analysis_lookup(meta, d["name"])
        ref = f"（需求关键句：{d['requirement_ref']}）" if d.get("requirement_ref") else ""
        body += [
            "",
            f"## {d['bug_id']}　{d['name']}",
            f"- 优先级：{d['priority']}｜预期依据：{d['basis'] or '未标注'}（置信度 {d['confidence']}）"
            f"｜失败轮次：{'、'.join(f'第{r}轮' for r in d['rounds'])}",
            f"- 关联测试条件：{d['condition'] or '（无 RTM 映射）'}{ref}",
            f"- **预期结果**：{d['expected']}",
            f"- **实际结果**：`{_fold(d['message'])}`",
            "",
            "失败用例（不得修改）：",
            "",
            f"```{lang if lang != 'unknown' else ''}",
            d["content"].rstrip(),
            "```",
        ]
        if ana.get("root_cause") or ana.get("fix"):
            body += [
                "",
                f"平台 AI 归因参考（推测，仅供定位）：{ana.get('root_cause', '—')} → "
                f"建议 {ana.get('fix', '—')}",
            ]
    tail = [
        "",
        "# 开始",
        "按顺序逐条处理；全部完成后给出总结表（缺陷号 / 处置（已修复|待人工裁决）/ 验证结果）。",
    ]
    return "\n".join(head + body + tail) + "\n"


def auto_export_handoff(conn, task_id: int, out_dir: Path | None = None,
                        notify=None) -> dict | None:
    """任务最后一步（review 节点）自动落盘两份交付物；无疑似缺陷返回 None。

    用户 2026-11-04 硬要求：每个任务执行到最后一步都必须自动生成。
    out_dir 缺省 = config.DATA_DIR/reports/handoff/（运行产物不入库）；
    notify(callable[str]) 接收人读摘要供写进 agent_messages 留痕。
    """
    out = Path(out_dir) if out_dir else config.DATA_DIR / "reports" / "handoff"
    built = export_handoff(conn, task_id, out)
    if built and notify:
        notify(f"[handoff] 已自动生成缺陷交接：疑似缺陷 {built['defects']} 条"
               f"（高置信 {built['high']}）→ {built['report_path']} 与 {built['prompt_path']}")
    return built


def build_handoff(conn, task_id: int) -> dict | None:
    """渲染两份交付物（不落盘）——API 与文件导出共用的单一真源。

    无疑似缺陷返回 None（诚实空手，不造文档）；任务不存在抛 ValueError。
    """
    meta = _load_meta(conn, task_id)
    if meta is None:
        raise ValueError(f"任务 {task_id} 不存在")
    defects = load_defects(conn, task_id)
    if not defects:
        return None
    return {"report": render_defect_report(meta, defects),
            "prompt": render_fix_prompt(meta, defects),
            "defects": len(defects),
            "high": sum(1 for d in defects if d["confidence"] == "高")}


def export_handoff(conn, task_id: int, out_dir: Path) -> dict | None:
    """导出两份交付物到 out_dir；无疑似缺陷时返回 None（诚实空手，不造文档）。"""
    built = build_handoff(conn, task_id)
    if built is None:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"task_{task_id}_缺陷报告单.md"
    prompt = out_dir / f"task_{task_id}_AI修复提示词.md"
    report.write_text(built["report"], encoding="utf-8")
    prompt.write_text(built["prompt"], encoding="utf-8")
    return {**built, "report_path": str(report), "prompt_path": str(prompt)}
