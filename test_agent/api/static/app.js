/* L1 前端逻辑：hash 路由 + 角色守卫 + 轮询（主链路真接口） */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (m) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[m]));

const ROLE_CN = { admin: "管理员", developer: "开发者", caller: "调用者" };
const STATUS_CN = { queued: "待下发", cloning: "克隆中", analyzing: "分析中", provisioning: "准备环境",
  generating: "生成用例", running: "执行中", reporting: "出报告中", done: "已完成",
  failed: "失败", cancelled: "已取消" };
const STEPS = ["输入项目信息", "AI 生成测试任务", "脚本工作台", "人工确认", "下发执行", "查看报告"];

let ME = null;
let TASKS = [];
let TASK_FILTER = "all";
let NEW_TASK_ID = null;
let pollTimer = null;

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const token = localStorage.getItem("token");
  if (token) headers["Authorization"] = "Bearer " + token;
  const r = await fetch("/api" + path, { ...opts, headers });
  if (r.status === 401) { showLogin(); throw new Error("未登录"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || ("HTTP " + r.status));
  return body;
}

/* ---------- 登录 ---------- */
function showLogin() { $("login-mask").classList.remove("hidden"); }
async function doLogin() {
  $("login-err").textContent = "";
  try {
    const b = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value }) });
    const data = await b.json();
    if (!b.ok) throw new Error(data.detail || "登录失败");
    localStorage.setItem("token", data.token);
    ME = data;
    $("login-mask").classList.add("hidden");
    applyRole();
    route();
    loadEnv();
  } catch (e) { $("login-err").textContent = e.message; }
}

function applyRole() {
  $("user-name").textContent = ME.username + "@" + ME.role;
  const badge = $("user-role-badge");
  badge.textContent = ROLE_CN[ME.role] || ME.role;
  badge.className = "badge badge-" + (ME.role === "developer" ? "dev" : ME.role);
  const isStaff = ME.role === "admin" || ME.role === "developer";
  document.querySelector('[data-page="workbench"]').style.display = isStaff ? "" : "none";
  document.querySelector('[data-page="settings"]').style.display = ME.role === "admin" ? "" : "none";
}

/* ---------- 路由 ---------- */
const PAGES = ["new", "workbench", "tasks", "reports", "settings"];
let TASK_TID = 0;                            // F03 详情页当前任务
function route() {
  let page = (location.hash || "#/new").replace("#/", "");
  if (page.startsWith("task/")) {           // F03 任务详情路由 #/task/{tid}
    page = "taskdetail";
    TASK_TID = parseInt(page.split("/") ? (location.hash.split("/")[2] || "0") : "0", 10);
  }
  if (!PAGES.includes(page) && page !== "taskdetail") page = "new";
  if (page === "settings" && ME.role !== "admin") page = "new";
  if (page === "workbench" && !(ME.role === "admin" || ME.role === "developer")) page = "new";
  PAGES.forEach(p => $("page-" + p).classList.toggle("hidden", p !== page));
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  if (page === "workbench") refreshWorkbench();
  if (page === "tasks") pollTasks();
  if (page === "reports") refreshReports();
  if (page === "settings") refreshSettings();
  if (page === "new" && NEW_TASK_ID) pollNewCases(NEW_TASK_ID);
  if (page === "taskdetail" && TASK_TID) loadTaskDetail(TASK_TID);
  if (page !== "tasks" && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function renderStepper(elId, active) {
  $(elId).innerHTML = STEPS.map((s, i) =>
    `<span class="step ${i < active ? "done" : i === active ? "active" : ""}">` +
    `<span class="n">${i < active ? "✓" : i + 1}</span>${i + 1}. ${s}</span>` +
    (i < STEPS.length - 1 ? '<span class="step-arrow">—›</span>' : "")).join("");
}

/* ---------- 新建测试 ---------- */
async function createTask() {
  const repo = $("new-repo").value.trim(), req = $("new-req").value.trim();
  if (!repo || !req) { $("new-result").textContent = "项目地址与需求均必填"; $("new-result").classList.remove("hidden"); return; }
  $("new-submit").disabled = true;
  try {
    const autoPrepare = $("new-auto-prepare") && $("new-auto-prepare").checked ? 1 : 0;
    const b = await api("/tasks", { method: "POST",
      body: JSON.stringify({ repo_url: repo, requirement: req, auto_prepare: autoPrepare }) });
    NEW_TASK_ID = b.task_id;
    $("new-result").innerHTML = `任务 <b>#${b.task_id}</b> 已创建（项目：${esc(b.project_name)}）。` +
      `下一步：<a href="#/workbench">进入脚本工作台</a> 确认后下发执行。`;
    $("new-result").classList.remove("hidden");
    renderStepper("stepper-new", 1);
    pollNewCases(b.task_id);
  } catch (e) { $("new-result").textContent = "创建失败：" + e.message; }
  finally { $("new-submit").disabled = false; }
}

async function pollNewCases(tid) {
  try {
    const b = await api(`/tasks/${tid}/cases`);
    const box = $("new-cases");
    if (!b.script || !b.cases.length) {
      box.innerHTML = `<div class="hint">任务 #${tid} 尚无用例：下发执行后由 AI 按五种方法生成。</div>`;
      return;
    }
    box.innerHTML = b.cases.map(c =>
      `<label class="case-item"><input type="checkbox" ${c.enabled ? "checked" : ""} onchange="toggleCase(${c.id})">` +
      `<code>${esc(c.name)}</code><span class="m">${c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置"}·${esc(c.method) || "-"}</span></label>`).join("");
  } catch (_) { /* 轮询失败静默，下轮再取 */ }
}

/* ---------- 脚本工作台 ---------- */
async function refreshWorkbench() {
  renderStepper("stepper-wb", 2);
  const sel = $("wb-task");
  const tasks = await api("/tasks");
  sel.innerHTML = `<option value="">— 选择任务 —</option>` +
    tasks.map(t => `<option value="${t.id}">#${t.id} ${esc(t.project_name)}（${STATUS_CN[t.status]}）</option>`).join("");
  sel.onchange = loadWorkbench;
}

async function loadWorkbench() {
  const tid = $("wb-task").value;
  $("wb-cases").innerHTML = ""; $("wb-preview").textContent = "（选择任务后加载最新脚本）";
  $("wb-confirm-bar").classList.add("hidden");
  if (!tid) return;
  const [detail, cases] = await Promise.all([api(`/tasks/${tid}`), api(`/tasks/${tid}/cases`)]);
  const active = ["cloning","analyzing","provisioning","generating","running","reporting"].includes(detail.status);
  if (detail.confirmed !== 1 && !active) $("wb-confirm-bar").classList.remove("hidden");
  $("wb-confirm").onclick = async () => {
    try { await api(`/tasks/${tid}/confirm`, { method: "POST", body: "{}" });
      $("wb-confirm-bar").classList.add("hidden"); loadWorkbench(); } catch (e) { alert(e.message); }
  };
  const box = $("wb-cases");
  box.innerHTML = (cases.cases.length ? cases.cases.map(c =>
    `<label class="case-item"><input type="checkbox" ${c.enabled ? "checked" : ""} onchange="toggleCase(${c.id})"><code>${esc(c.name)}</code>` +
    `<span class="m">${c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置"}·${esc(c.method) || "-"}</span></label>`).join("")
    : '<div class="hint">暂无用例：先下发执行触发 AI 生成，或在下方插入用例。</div>');
  if (cases.script && cases.script.content) $("wb-preview").textContent = cases.script.content;
}

async function addCase() {
  const tid = $("wb-task").value;
  if (!tid) { $("ac-result").textContent = "请先选择任务"; return; }
  try {
    const b = await api(`/tasks/${tid}/cases`, { method: "POST", body: JSON.stringify({
      name: $("ac-name").value.trim(), method: $("ac-method").value.trim(), code: $("ac-code").value }) });
    $("ac-result").textContent = `已插入：${b.case_name} → ${b.file}（下次执行生效）`;
    loadWorkbench();
  } catch (e) { $("ac-result").textContent = "插入失败：" + e.message; }
}

async function toggleCase(id) {
  try { await api(`/cases/${id}/toggle`, { method: "POST", body: "{}" }); loadWorkbench(); }
  catch (e) { alert(e.message); loadWorkbench(); }
}

/* ---------- 任务列表 ---------- */
async function pollTasks() {
  try {
    TASKS = await api("/tasks");
    renderTasks();
  } catch (_) {}
  if (!pollTimer) pollTimer = setInterval(pollTasks, 3000);
}

function renderTasks() {
  const groups = {
    all: TASKS, running: TASKS.filter(t => t.progress < 100),
    done: TASKS.filter(t => t.status === "done"), failed: TASKS.filter(t => t.status === "failed"),
  }[TASK_FILTER] || TASKS;
  $("task-rows").innerHTML = groups.map(t => {
    const isStaff = ME.role !== "caller";
    const confirmable = isStaff && t.confirmed !== 1 && t.progress < 100;
    const runnable = isStaff && t.confirmed === 1 && t.progress < 100;
    return `<tr>
      <td><b>#${t.id}</b><div class="hint">${esc((t.requirement_text || "").slice(0, 24))}…</div></td>
      <td>${esc(t.project_name)}<div class="hint">${esc(t.primary_language) || "-"}</div></td>
      <td><span class="badge ${t.status === "done" ? "badge-ok" : t.status === "failed" ? "badge-fail" :
        t.progress < 100 && t.status !== "queued" ? "badge-run" : t.status === "cancelled" ? "badge-cancel" : "badge-queued"}">${STATUS_CN[t.status] || t.status}</span>
        ${t.confirmed !== 1 ? '<div class="hint">未确认 · 禁止下发</div>' : ""}</td>
      <td><span class="progress"><i style="width:${t.progress}%"></i></span>${t.progress}%</td>
      <td>
        <button class="op-link" onclick="viewReport(${t.id})">查看</button>
        <button class="op-link" ${confirmable ? "" : "disabled"} onclick="confirmTask(${t.id})">人工确认</button>
        <button class="op-link" ${runnable ? "" : "disabled"} onclick="runTask(${t.id})">下发执行</button>
        <button class="op-link" onclick="viewLogs(${t.id})">日志</button>
      </td></tr>`;
  }).join("") || '<tr><td colspan="5" class="hint">暂无任务</td></tr>';
}

async function confirmTask(id) { try { await api(`/tasks/${id}/confirm`, { method: "POST", body: "{}" }); pollTasks(); } catch (e) { alert(e.message); } }
async function runTask(id) { try { await api(`/tasks/${id}/run`, { method: "POST", body: "{}" }); pollTasks(); } catch (e) { alert(e.message); } }
function viewReport(id) { location.hash = "#/reports"; setTimeout(() => { $("rp-task").value = String(id); loadReport(); }, 150); }
async function viewLogs(id) { location.hash = "#/reports"; setTimeout(() => { $("rp-task").value = String(id); switchRp("logs"); loadReport(); }, 150); }

/* ---------- 报告 ---------- */
async function refreshReports() {
  const tasks = await api("/tasks");
  $("rp-task").innerHTML = tasks.map(t => `<option value="${t.id}">#${t.id} ${esc(t.project_name)}（${STATUS_CN[t.status]}）</option>`).join("");
  $("rp-task").onchange = loadReport;
  if (tasks.length) { $("rp-task").value = String(tasks[0].id); await loadReport(); }
}

function switchRp(which) {
  $("rp-overview").classList.toggle("hidden", which !== "overview");
  $("rp-suite").classList.toggle("hidden", which !== "suite");
  $("rp-rtm").classList.toggle("hidden", which !== "rtm");
  $("rp-logs").classList.toggle("hidden", which !== "logs");
  $("rp-attach").classList.toggle("hidden", which !== "attach");
  document.querySelectorAll("[data-rp]").forEach(b => b.classList.toggle("active", b.dataset.rp === which));
}

const LV_NAME = { L1: "单元", L2: "集成", L3: "系统", L4: "验收" };

function levelMetric(l, scenarios) {
  const s = l.summary || {};
  // L3/L4 的按级指标在 /levels 的 scenarios 字段里（l3_metrics/l4_metrics），
  // 不能只看 test_levels.summary——L4 summary 只有 scenarios/rejected，
  // 直接读会把「场景/已确认/转化」全显示成 --（真数据在 d.scenarios.L4）。
  if (l.level === "L1") {
    const cov = s.coverage_line != null ? Math.round(s.coverage_line * 100) + "%" : "--";
    return `覆盖 ${cov} · 通过 ${s.passed ?? "--"}`;
  }
  if (l.level === "L2") {
    return `端点 ${s.endpoints_covered ?? "--"}/${s.endpoints_total ?? "--"} · 契约违背 ${s.contract_violations ?? "--"}`;
  }
  const m = (scenarios && scenarios[l.level]) || {};
  if (l.level === "L3") {
    return `场景 ${m.total ?? s.total ?? "--"} · 自动过 ${m.auto_pass ?? s.auto_pass ?? "--"} · 待确认 ${m.pending_confirm ?? s.pending_confirm ?? "--"}`;
  }
  return `场景 ${m.total ?? s.total ?? "--"} · 已确认 ${m.confirmed ?? s.confirmed ?? "--"} · 转化 ${m.converted ?? s.converted ?? "--"}`;
}

// D10 四级卡 + 溯源链（真数据：/levels 端点；无级别行时不出块，L1-only 兼容）
function renderLevels(d) {
  if (!d || !d.levels || !d.levels.length) return "";
  const cards = d.levels.map(l => {
    const bad = l.status === "failed" || l.verdict === "stop";
    return `<div class="level-card ${bad ? "fail" : "ok"}">
      <div class="lv">${l.level} ${LV_NAME[l.level] || ""} <span class="badge ${bad ? "badge-fail" : "badge-ok"}">${esc(l.status)}</span></div>
      <div class="st">判定 ${esc(l.verdict || "--")}</div>
      <div class="st">${esc(levelMetric(l, d.scenarios))}</div>
      <div class="st">${esc((l.stop_reason || "").slice(0, 80))}</div>
    </div>`;
  }).join("");
  const chains = (d.trace_chains || []).slice(0, 2).map(c =>
    `<div class="trace-chain">` + c.nodes.map(n => {
      const cls = ["pending_confirm", "confirmed_defect"].includes(n.status) ? " fail" : "";
      const title = n.title || n.name || n.function || "";
      return `<span class="trace-node${cls}">${n.level} ${esc(title.slice(0, 42))}</span>`;
    }).join("<span class=\"hint\">›</span>") + `</div>`).join("");
  return `<div class="level-grid">${cards}</div>
    <div class="trace-box">跨级溯源 ${d.trace_count || 0} 条${d.trace_count ? "（失败/待定性场景）" : ""}${chains}</div>`;
}

// D9 执行信息卡（教科书"测试执行阶段"要素：何时/谁/怎么执行——外行第一眼看的东西）
function execCard(latest) {
  const ex = latest && latest.exec;
  if (!ex) return "";
  let dur = "";
  if (ex.started_at && latest.created_at) {
    const ms = new Date(latest.created_at.replace(" ", "T")) - new Date(ex.started_at.replace(" ", "T"));
    if (ms > 0 && !isNaN(ms)) dur = ` ｜ 耗时 ${ms >= 60000 ? Math.floor(ms / 60000) + " 分 " : ""}${Math.round(ms % 60000 / 1000)} 秒`;
  }
  return `<div class="trace-box" style="margin-bottom:10px"><b>执行信息</b> ｜ 开始 <b>${esc(ex.started_at || "—")}</b>` +
    ` ｜ 报告生成 <b>${esc(latest.created_at || "—")}</b>${dur}` +
    ` ｜ 执行方式：${esc(ex.mode)}（${ex.rounds} 轮）` +
    ` ｜ 执行者：<b>系统自动</b>（${esc(ex.sandbox)} + LLM ${esc(ex.llm)}，零人工干预）</div>`;
}

// D9 测试用例库（教科书"测试设计阶段 D 基本要素"：编号/标题/级别/输入/预期结果，
// 按测试类型分组排序——外行按"我想看哪类测试"导航，不再是一锅粥）
const METHOD_ORDER = ["等价类划分", "边界值分析", "判定表", "错误推测法", "状态迁移", "蜕变测试", "property-based"];
const PRIO_RANK = { "高": 0, "中": 1, "低": 2 };

function renderSuite(cases) {
  if (!cases || !cases.length) return "";
  const groups = {};
  cases.forEach(c => { const m = (c.method || "").trim() || "未标注方法"; (groups[m] = groups[m] || []).push(c); });
  const names = Object.keys(groups).sort((a, b) => {
    const ia = METHOD_ORDER.findIndex(k => a.includes(k)), ib = METHOD_ORDER.findIndex(k => b.includes(k));
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  const pc = { "高": "badge-fail", "中": "badge-queued", "低": "badge-ok" };
  let html = "<tr><th>#</th><th>测试标题</th><th>级别</th><th>来源</th><th>测试输入</th><th>预期结果〔依据〕</th></tr>";
  for (const m of names) {
    const rows = groups[m].sort((x, y) =>
      ((PRIO_RANK[x.priority] ?? 3) - (PRIO_RANK[y.priority] ?? 3)) || (x.id - y.id));
    html += `<tr><td colspan="6" style="background:var(--bg,#f6f7fa)"><b>${esc(m)}</b>` +
      ` <span class="hint">· ${rows.length} 条</span></td></tr>`;
    html += rows.map(c => `<tr><td>${c.id}</td><td>${esc(c.name)}</td>` +
      `<td><span class="badge ${pc[c.priority] || "badge-queued"}">${esc(c.priority || "—")}</span></td>` +
      `<td>${c.source === "ai" ? "AI 生成" : c.source === "imported" ? "人工插入" : esc(c.source || "—")}</td>` +
      `<td>${esc((c.input || "—").slice(0, 60))}</td>` +
      `<td>${esc((c.expected || "—").slice(0, 80))}${c.expected_basis ? ` <span class="hint">〔${esc(c.expected_basis)}〕</span>` : ""}</td></tr>`).join("");
  }
  return html;
}

async function loadReport() {
  const tid = $("rp-task").value;
  if (!tid) return;
  const [reports, logs, rtm, charts, levels] = await Promise.all([
    api(`/tasks/${tid}/report`).catch(() => []),
    api(`/tasks/${tid}/logs`).catch(() => []),
    api(`/tasks/${tid}/rtm`).catch(() => null),
    api(`/tasks/${tid}/charts`).catch(() => []),
    api(`/tasks/${tid}/levels`).catch(() => null),
  ]);
  const latest = reports[0];
  const rate = latest && latest.pass_rate != null ? Math.round(latest.pass_rate * 100) : null;
  $("rp-rate").textContent = `通过率 ${rate != null ? rate : "--"}%`;
  $("rp-rate").className = "badge " + (rate >= 70 ? "badge-ok" : rate == null ? "badge-queued" : "badge-fail");
  // 指标条：六格 = 总数/通过/失败/跳过 + W4 复杂度热点未覆盖（有热点快照才出）+
  // LLM token 合计（P1-E 成本台账有行才出）
  const cells = latest ? [
    ["总数", latest.total, ""], ["通过", latest.passed, "ok"],
    ["失败", latest.failed, "fail"], ["跳过", latest.skipped, ""],
  ] : [];
  if (latest && latest.hotspots) cells.push(
    ["热点未覆盖", `${latest.hotspots.uncovered}/${latest.hotspots.total}`,
     latest.hotspots.uncovered > 0 ? "fail" : "ok"]);
  if (latest && latest.llm) cells.push(
    ["LLM token", (latest.llm.total ?? 0).toLocaleString("zh-CN"), ""]);
  $("rp-metrics").innerHTML = latest
    ? cells.map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v ?? 0}</div></div>`).join("")
    : '<div class="metric"><div class="k">暂无报告</div><div class="v">--</div></div>';

  // 概览：gen_report 叙述（数字来自 SQL，审查结论徽章）+ 评估摘要兜底
  const rev = latest && latest.review ? (() => { try { return JSON.parse(latest.review); } catch { return null; } })() : null;
  const revBadge = rev
    ? `<div class="log-line"><span class="badge ${rev.status === "pass" ? "badge-ok" : "badge-fail"}">报告二次审查：${rev.status === "pass" ? "通过" : "未过"}（${esc(rev.reviewer || "")}）</span>` +
      (rev.reasons && rev.reasons.length ? ` ${esc(rev.reasons.join("; "))}` : "") + `</div>` : "";
  const narr = latest && latest.narrative
    ? `<pre class="code-view" style="white-space:pre-wrap">${esc(latest.narrative)}</pre>` : "";
  const summaryHtml = revBadge + narr +
    logs.filter(l => l.content.includes("[evaluate") || l.content.includes("[digest") || l.content.includes("[analyze") || l.content.includes("[review"))
      .map(l => `<div class="log-line">${esc(l.content)}</div>`).join("")
    || '<div class="hint">暂无评估摘要</div>';
  // 图表内嵌（gen_charts 产物经 /charts 端点转 base64 data URL）——先拼摘要再加图，
  // 避免 `+` 与 `||` 优先级把图接进"暂无"分支
  const chartsHtml = charts && charts.length
    ? '<div class="charts-grid">' + charts.map(c =>
        `<figure class="chart-fig"><figcaption>${esc(c.title)}</figcaption>` +
        `<img src="${c.data_url}" alt="${esc(c.title)}" loading="lazy"></figure>`).join("") + "</div>"
    : "";
  $("rp-overview").innerHTML = execCard(latest) + renderLevels(levels) + summaryHtml + chartsHtml;

  // RTM 矩阵（测试条件 ↔ 用例映射）——用例六要素移至「测试用例库」tab（D9 分离关注点）
  if (rtm && (rtm.matrix.length || rtm.cases.length)) {
    const covered = rtm.matrix.filter(m => m.status === "covered").length;
    $("rp-rtm-badge").textContent = `条件覆盖 ${covered}/${rtm.conditions_total}` +
      (rtm.map_source ? `（映射方式：${rtm.map_source === "llm" ? "LLM 辅助" : "启发式"}）` : "（任务未跑评估阶段，无映射快照）");
    $("rp-rtm-table").innerHTML =
      "<tr><th>测试条件</th><th>优先级</th><th>关联用例</th><th>状态</th></tr>" +
      rtm.matrix.map(m => `<tr><td>${esc(m.condition)}</td><td>${esc(m.priority || "中")}</td>` +
        `<td>${m.cases.length ? m.cases.map(esc).join("<br>") : '<span class="hint">无用例覆盖</span>'}</td>` +
        `<td><span class="badge ${m.status === "covered" ? "badge-ok" : "badge-fail"}">${m.status === "covered" ? "已覆盖" : "缺口"}</span></td></tr>`).join("");
  } else {
    $("rp-rtm-badge").textContent = "该任务尚无测试条件数据（plan 节点需需求文本与 LLM Key）";
    $("rp-rtm-table").innerHTML = "";
  }

  // D9 测试用例库：按测试类型分组 + 优先级排序 + 编号列（独立 tab，外行导航入口）
  const suite = await api(`/tasks/${tid}/cases`).catch(() => null);
  const suiteCases = suite && suite.cases ? suite.cases : [];
  $("rp-suite-table").innerHTML = renderSuite(suiteCases);
  $("rp-suite-hint").textContent = suiteCases.length
    ? `共 ${suiteCases.length} 条用例，按测试类型分组、组内按优先级（高→低）排序；「〔依据〕」= 该预期结果的出处（需求明示/源码推导/模型推断等）`
    : "该任务尚无用例（需先执行生成阶段）";

  // 用例六要素导出同步用 suite 数据（编号+类型）
  const lines = logs.map(l => `<div class="log-line"><b>[${l.seq}]</b> ${esc(l.content)}</div>`);
  $("rp-log-list").innerHTML = lines.join("") || '<div class="hint">暂无日志</div>';
  $("rp-export").onclick = () => {
    const md = `# 测试报告 · 任务 ${tid}\n\n通过率：${rate ?? "--"}%\n\n## 指标\n\n- 总数 ${latest?.total ?? 0} / 通过 ${latest?.passed ?? 0} / 失败 ${latest?.failed ?? 0} / 跳过 ${latest?.skipped ?? 0}\n\n## 报告叙述\n\n${latest?.narrative ?? "（无）"}\n\n## 需求追踪\n\n` +
      (rtm?.matrix || []).map(m => `- [${m.status === "covered" ? "x" : " "}] ${m.condition} → ${m.cases.join(", ") || "缺口"}`).join("\n") +
      `\n\n## 轨迹\n\n` + logs.map(l => `- [${l.seq}] ${l.content}`).join("\n");
    $("rp-export-view").textContent = md; $("rp-export-view").classList.remove("hidden");
  };
}

/* ---------- 设置（仅管理员） ---------- */
async function refreshSettings() {
  const cfg = await api("/config");
  $("st-config").innerHTML = Object.entries(cfg).filter(([k]) => k !== "note").map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("") +
    `<div class="k">说明</div><div class="hint" style="grid-column:1/-1">${esc(cfg.note)}</div>`;
  const imgs = await api("/images");
  $("st-images").innerHTML = imgs.map(i =>
    `<div class="img-row"><span>${esc(i.image)}</span><span class="badge ${i.ready ? "badge-ok" : "badge-fail"}">${i.ready ? "就绪 " + i.size_mb + "MB" : "缺失"}</span>` +
    (i.ready ? "" : `<button class="btn btn-primary btn-sm" onclick="buildImage('${encodeURIComponent(i.image)}')">自动构建</button>`) + `</div>`).join("");
  const users = await api("/users");
  $("st-users").innerHTML = users.map(u =>
    `<div class="img-row"><span>${esc(u.username)}</span><span class="badge badge-${u.role === "developer" ? "dev" : u.role}">${ROLE_CN[u.role]}</span><span class="hint">创建于 ${esc(u.created_at)}</span></div>`).join("");
}

// ---------- F03 任务详情页（设计图主页面） ----------
// F04 级别卡（设计图规格：顶边色条+LEVEL 标签+四态徽章+指标行+动作链接）
const LV_META = { L1: ["单元测试", "var(--ok2)"], L2: ["集成测试", "var(--info)"],
                  L3: ["系统测试", "var(--warn2)"], L4: ["验收测试", "var(--idle)"] };
function lvBadge(l) {
  if (l.status === "done" && l.verdict === "pass") return '<span class="badge" style="background:#d9f2e5;color:#16a34a">已完成 ✓</span>';
  if (["running", "cloning", "generating", "provisioning", "analyzing", "reporting"].includes(l.status))
    return '<span class="badge" style="background:#dbeafe;color:#2563eb">● 运行中</span>';
  if (l.level === "L3" && (l.summary ? (l.summary.pending_confirm > 0) : false))
    return '<span class="badge" style="background:#fde9c8;color:#b45309">待人工确认</span>';
  if (l.level === "L4" && l.verdict !== "pass") return '<span class="badge" style="background:#f3f4f6;color:#9ca3af">清单待审</span>';
  if (l.status === "failed") return '<span class="badge" style="background:#fee2e2;color:#dc2626">失败</span>';
  return '<span class="badge" style="background:#f3f4f6;color:#6b7280">' + esc(l.status || "—") + '</span>';
}
function lvRows(l) {
  const s = l.summary || {};
  if (l.level === "L1") return [["通过率", `${s.passed ?? "--"} / ${(s.passed ?? 0) + (s.failed ?? 0)}`],
    ["行覆盖率", s.coverage_line != null ? Math.round(s.coverage_line * 100) + "%" : "--"]];
  if (l.level === "L2") return [["端点解析", `${s.endpoints_covered ?? "--"}/${s.endpoints_total ?? "--"}`],
    ["契约违背检出", s.contract_violations ?? "--"], ["需求断言失败", s.requirement_failures ?? "--"]];
  if (l.level === "L3") return [["业务流场景", s.total ?? "--"], ["自动通过", s.auto_pass ?? "--"],
    ["待人工确认", s.pending_confirm ?? "--"]];
  return [["GWT 场景清单", (s.total ?? "--") + " 条"], ["已确认", s.confirmed ?? s.rejected ?? "--"]];
}
function renderLevelCards(levels) {
  if (!levels || !levels.length) return "";
  return '<div class="level-grid">' + levels.map(l => {
    const [cn, color] = LV_META[l.level] || [l.level, "var(--idle)"];
    const bad = l.status === "failed" || l.verdict === "stop";
    const rows = lvRows(l).map(([k, v]) =>
      `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f3f4f6;font-size:13.5px">` +
      `<span style="color:#374151">${esc(k)}</span><b>${esc(String(v))}</b></div>`).join("");
    const link = l.level === "L1" ? `<a href="#/reports" onclick="viewReport(${l.task_id || 0})" style="color:var(--info);font-size:13.5px;font-weight:700">查看报告 →</a>` : "";
    return `<div style="background:#fff;border:1px solid var(--line);border-top:4px solid ${color};` +
      `border-radius:12px;padding:14px 18px;box-shadow:var(--shadow)">` +
      `<div style="display:flex;justify-content:space-between;align-items:center">` +
      `<span style="font-size:11px;font-weight:700;color:var(--idle);letter-spacing:1px">LEVEL ${l.level.slice(1)}</span>${lvBadge(l)}</div>` +
      `<div style="font-size:18px;font-weight:800;margin:4px 0 8px">${cn}</div>${rows}` +
      `<div style="margin-top:10px">${link}</div></div>`;
  }).join("") + "</div>";
}

// F05 流水线（设计图：9 节点胶囊三态 + 调度说明行；/logs 前缀确定性解析，无消息=排队）
const PIPE_NODES = ["clone", "analyze", "plan", "provision", "generate", "run", "evaluate", "report", "review"];
function parsePipeline(logs) {
  const state = {};   // node -> done/running
  (logs || []).forEach(l => {
    const c = (l.content || "");
    PIPE_NODES.forEach(n => {
      if (c.includes(`[${n}]`)) state[n] = "done";
    });
    const gm = c.match(/\[generate 第(\d+)轮\]/);
    if (gm) state.generate = "done";                       // 生成完成标记
    if (/\[run\]|执行（容器内/.test(c)) state.run = c.includes("失败") ? state.run : "running";
    if (/\[evaluate/.test(c) && c.includes("→")) state.evaluate = "running";
  });
  // 节点顺序里第一个非 done 的=running（与 LangGraph 串行语义一致）
  let runningMarked = false;
  const out = {};
  PIPE_NODES.forEach(n => {
    if (state[n] === "done") { out[n] = "done"; return; }
    if (!runningMarked) { out[n] = "running"; runningMarked = true; } else out[n] = "queued";
  });
  return out;
}
function renderPipeline(logs, levels) {
  const st = parsePipeline(logs);
  const pills = PIPE_NODES.map(n => {
    const cls = st[n] === "done" ? 'background:#e7f6ec;color:#16a34a'
      : st[n] === "running" ? 'background:#fff;border:1.5px solid var(--info);color:var(--info)'
      : 'background:#f3f4f6;color:#6b7280';
    return `<span style="display:inline-block;padding:9px 18px;border-radius:10px;font-size:13.5px;` +
      `font-weight:600;${cls}">${n}${st[n] === "done" ? " ✓" : ""}</span>`;
  }).join('<span style="color:#d1d5db;margin:0 6px">→</span>');
  const legs = (levels && levels.levels) ? levels.levels.map(l =>
    `${l.level} ${l.status === "done" ? "✓" : "●"}${l.verdict ? "(" + l.verdict + ")" : ""}`).join(" → ") : "";
  return `<div style="line-height:3">${pills}</div>` +
    `<div class="hint">级别调度：${esc(legs || "（未走 levels 流）")}｜调度说明：R-Loop 失败不回炉，同指纹熔断 ≥2 次或预算耗尽诚实停机</div>`;
}

// F08 跨级溯源面板（/trace chains：触发场景+节点链+gaps 徽章）
function renderTracePanel(trace) {
  const chains = (trace && trace.chains) || [];
  if (!chains.length) return '<div class="hint">该任务无溯源链（失败场景触发建链，链数为 0 如实）</div>';
  return chains.map(c => {
    const trig = c.trigger || {};
    const nodes = (c.nodes || []).map(n =>
      `<span class="trace-node${n.level === "CODE" ? "" : ""}">${esc(n.level)} ${esc(String(n.title || n.name || n.function || "").slice(0, 24))}</span>`)
      .join('<span class="hint">›</span>');
    const gaps = (c.gaps || []).length ? ` <span class="badge badge-fail">缺环 ${c.gaps.length}</span>` : "";
    return `<div style="margin:8px 0"><b style="font-size:13px">${esc(trig.title || "场景 " + c.scenario_id)}</b>` +
      `<span class="badge badge-queued" style="margin-left:8px">${esc(trig.status || "")}</span>${gaps}` +
      `<div class="trace-chain">${nodes}</div></div>`;
  }).join("");
}

async function loadTaskDetail(tid) {
  const [t, reports] = await Promise.all([
    api(`/tasks/${tid}`).catch(() => null),
    api(`/tasks/${tid}/report`).catch(() => []),
  ]);
  const latest = reports && reports[0];
  const ex = latest && latest.exec;
  if (!t) { $("td-hero").innerHTML = '<div class="hint">任务不存在或加载失败</div>'; return; }
  const name = (t.repo_url || "").split("/").pop() || `任务 ${tid}`;
  let dur = "";
  if (ex && ex.started_at && latest.created_at) {
    const ms = new Date(latest.created_at.replace(" ", "T")) - new Date(ex.started_at.replace(" ", "T"));
    if (ms > 0 && !isNaN(ms)) dur = `｜已运行 ${ms >= 60000 ? Math.floor(ms/60000) + "m" : ""}${Math.round(ms%60000/1000)}s`;
  }
  $("td-hero").innerHTML =
    `<div style="font-size:26px;font-weight:800">${esc(name)}</div>` +
    `<span class="badge badge-queued">任务 #${tid}</span>` +
    `<div class="hint" style="margin-top:6px">状态：${esc(STATUS_CN[t.status] || t.status)}${dur}` +
    (ex ? `｜执行方式：${esc(ex.mode)}｜执行者：系统自动（${esc(ex.sandbox)}+LLM ${esc(ex.llm)}）` : "") + `</div>`;
  const req = (latest && latest.narrative) ? "" : "";
  const trow = await api(`/tasks/${tid}`).catch(() => null);
  $("td-req").classList.remove("hidden");
  $("td-req").innerHTML = `<div style="border-left:4px solid var(--ok2);padding-left:14px">` +
    `<b style="color:var(--ok2)">需求</b>　${esc((trow && trow.requirement_text) || "—")}</div>`;
  const lv = await api(`/tasks/${tid}/levels`).catch(() => null);
  const logs = await api(`/tasks/${tid}/logs`).catch(() => []);
  $("td-pipeline").innerHTML = "<div style=\"font-size:16px;font-weight:800;margin-bottom:10px\">" +
    "执行流水线（固定图编排 · 容器沙箱）</div>" + renderPipeline(logs, lv);
  const trace = await api(`/tasks/${tid}/trace`).catch(() => null);
  $("td-trace").innerHTML = "<div style=\"font-size:16px;font-weight:800\">跨级 RTM 溯源链</div>" +
    `<div class="hint">失败自动沿 [验收场景 → 系统场景 → 集成用例 → 单元用例 → 代码行] 反向定位｜共 ${(trace && trace.chains || []).length} 条</div>` +
    renderTracePanel(trace);
  $("td-levels").innerHTML = "<div style=\"font-size:16px;font-weight:800;margin-bottom:10px\">" +
    "测试级别计划（黑盒 + 白盒双轨）</div>" +
    (renderLevelCards(lv && lv.levels) || '<div class="hint">该任务尚无级别台账（未走 levels 流）</div>');
}

async function buildImage(image) {
  try { await api(`/images/${encodeURIComponent(image)}/build`, { method: "POST", body: "{}" }); refreshSettings(); }
  catch (e) { alert(e.message); }
}

/* ---------- 运行环境自检（L1 守卫） ---------- */
let ENV_TIMER = null;
async function loadEnv() {
  try {
    renderEnv(await api("/env/status"));
  } catch (e) {
    $("env-dot").className = "env-dot bad";
    $("env-text").textContent = "环境检测失败：" + e.message;
  }
}
function renderEnv(b) {
  const items = (b.probe && b.probe.items) || [];
  const prep = b.prepare || {};
  const blocking = items.filter(i => i.blocking && i.status !== "ok");
  const warn = items.filter(i => !i.blocking && i.status !== "ok");
  let cls = "ok", text = "环境就绪";
  if (prep.state === "running") { cls = "busy"; text = prep.message || "环境补齐中…"; }
  else if (blocking.length) { cls = "bad"; text = "环境未就绪：" + blocking.map(i => i.label).join("、"); }
  else if (warn.length) { cls = "warn"; text = "环境就绪（" + warn[0].label + "未配置，将诚实降级）"; }
  // R1/F09：三状态胶囊组（设计图规格：Docker/镜像/API Key，浅绿底绿字；异常红、准备中脉冲）
  const PICK = { docker: "Docker", "image:mta-test:py312": "镜像 ✓", llm: "API Key ✓" };
  const pills = ["docker", "image:mta-test:py312", "llm"].map(k => {
    const it = items.find(i => i.key === k) || {};
    const okPill = it.status === "ok";
    const bg = prep.state === "running" ? "#fef3c7" : okPill ? "#d1fae5" : "#fee2e2";
    const fg = okPill ? "#059669" : "#b91c1c";
    return `<span style="display:inline-flex;align-items:center;gap:6px;padding:4px 12px;` +
      `border-radius:999px;background:${bg};font-size:12.5px;font-weight:600;color:${fg}">` +
      `<span style="width:7px;height:7px;border-radius:50%;background:${okPill ? '#22c55e' : '#ef4444'}"></span>` +
      `${esc(PICK[k] || it.label || k)}</span>`;
  }).join("");
  $("env-pills").innerHTML = pills;
  $("env-btn").classList.toggle("hidden", !blocking.length || prep.state === "running");
  $("env-btn").disabled = prep.state === "running";
  $("env-strip").title = items.map(i => `${i.label}：${i.detail}`).join("\n") +
    (prep.finished_at ? `\n最近补齐：${prep.message}` : "");
  $("env-btn").disabled = prep.state === "running";
  clearTimeout(ENV_TIMER);
  if (prep.state === "running") ENV_TIMER = setTimeout(loadEnv, 2500);
}
async function prepareEnv() {
  try {
    $("env-btn").disabled = true;
    $("env-text").textContent = "已开始自动补齐…";
    await api("/env/prepare", { method: "POST" });
    await loadEnv();
  } catch (e) {
    $("env-text").textContent = "补齐启动失败：" + e.message;
    $("env-btn").disabled = false;
  }
}

/* ---------- 绑定与启动 ---------- */
$("env-btn").onclick = prepareEnv;
$("login-btn").onclick = doLogin;
$("login-pass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("new-submit").onclick = createTask;
$("ac-submit").onclick = addCase;
$("wb-confirm").onclick = () => {};
document.querySelectorAll("#task-tabs .tab").forEach(b => b.onclick = () => {
  TASK_FILTER = b.dataset.f;
  document.querySelectorAll("#task-tabs .tab").forEach(x => x.classList.toggle("active", x === b));
  renderTasks();
});
document.querySelectorAll("[data-rp]").forEach(b => b.onclick = () => switchRp(b.dataset.rp));
window.addEventListener("hashchange", route);

(async function boot() {
  renderStepper("stepper-new", 0);
  renderStepper("stepper-wb", 2);
  if (!localStorage.getItem("token")) { showLogin(); return; }
  try { ME = await api("/me"); applyRole(); route(); loadEnv(); }
  catch (_) { /* 401 已弹出登录 */ }
})();
