/* L1 前端逻辑：hash 路由 + 角色守卫 + 轮询（主链路真接口） */
"use strict";
const $ = (id) => document.getElementById(id);

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
function route() {
  let page = (location.hash || "#/new").replace("#/", "");
  if (!PAGES.includes(page)) page = "new";
  if (page === "settings" && ME.role !== "admin") page = "new";
  if (page === "workbench" && !(ME.role === "admin" || ME.role === "developer")) page = "new";
  PAGES.forEach(p => $("page-" + p).classList.toggle("hidden", p !== page));
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  if (page === "workbench") refreshWorkbench();
  if (page === "tasks") pollTasks();
  if (page === "reports") refreshReports();
  if (page === "settings") refreshSettings();
  if (page === "new" && NEW_TASK_ID) pollNewCases(NEW_TASK_ID);
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
    const b = await api("/tasks", { method: "POST", body: JSON.stringify({ repo_url: repo, requirement: req }) });
    NEW_TASK_ID = b.task_id;
    $("new-result").innerHTML = `任务 <b>#${b.task_id}</b> 已创建（项目：${b.project_name}）。` +
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
      `<label class="case-item"><input type="checkbox" checked ${c.enabled ? "" : ""}>` +
      `<code>${c.name}</code><span class="m">${c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置"}·${c.method || "-"}</span></label>`).join("");
  } catch (_) { /* 轮询失败静默，下轮再取 */ }
}

/* ---------- 脚本工作台 ---------- */
async function staffTasks() {
  const list = await api("/tasks");
  return list.filter(t => true);
}

async function refreshWorkbench() {
  renderStepper("stepper-wb", 2);
  const sel = $("wb-task");
  const tasks = await api("/tasks");
  sel.innerHTML = `<option value="">— 选择任务 —</option>` +
    tasks.map(t => `<option value="${t.id}">#${t.id} ${t.project_name}（${STATUS_CN[t.status]}）</option>`).join("");
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
    `<label class="case-item"><input type="checkbox" checked><code>${c.name}</code>` +
    `<span class="m">${c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置"}·${c.method || "-"}</span></label>`).join("")
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
      <td><b>#${t.id}</b><div class="hint">${(t.requirement_text || "").slice(0, 24)}…</div></td>
      <td>${t.project_name}<div class="hint">${t.primary_language || "-"}</div></td>
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
  $("rp-task").innerHTML = tasks.map(t => `<option value="${t.id}">#${t.id} ${t.project_name}（${STATUS_CN[t.status]}）</option>`).join("");
  $("rp-task").onchange = loadReport;
  if (tasks.length) { $("rp-task").value = String(tasks[0].id); await loadReport(); }
}

function switchRp(which) {
  $("rp-overview").classList.toggle("hidden", which !== "overview");
  $("rp-logs").classList.toggle("hidden", which !== "logs");
  $("rp-attach").classList.toggle("hidden", which !== "attach");
  document.querySelectorAll("[data-rp]").forEach(b => b.classList.toggle("active", b.dataset.rp === which));
}

async function loadReport() {
  const tid = $("rp-task").value;
  if (!tid) return;
  const [reports, logs] = await Promise.all([api(`/tasks/${tid}/report`).catch(() => []), api(`/tasks/${tid}/logs`).catch(() => [])]);
  const latest = reports[0];
  const rate = latest && latest.pass_rate != null ? Math.round(latest.pass_rate * 100) : null;
  $("rp-rate").textContent = `通过率 ${rate != null ? rate : "--"}%`;
  $("rp-rate").className = "badge " + (rate >= 70 ? "badge-ok" : rate == null ? "badge-queued" : "badge-fail");
  $("rp-metrics").innerHTML = latest ? [
    ["总数", latest.total, ""], ["通过", latest.passed, "ok"],
    ["失败", latest.failed, "fail"], ["跳过", latest.skipped, ""],
  ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v ?? 0}</div></div>`).join("")
    : '<div class="metric"><div class="k">暂无报告</div><div class="v">--</div></div>';
  const lines = logs.map(l => `<div class="log-line"><b>[${l.seq}]</b> ${l.content.replace(/</g, "&lt;")}</div>`);
  $("rp-log-list").innerHTML = lines.join("") || '<div class="hint">暂无日志</div>';
  $("rp-overview").innerHTML = logs.filter(l => l.content.includes("[evaluate") || l.content.includes("[digest") || l.content.includes("[analyze"))
    .map(l => `<div class="log-line">${l.content.replace(/</g, "&lt;")}</div>`).join("") || '<div class="hint">暂无评估摘要</div>';
  $("rp-export").onclick = () => {
    const md = `# 测试报告 · 任务 ${tid}\n\n通过率：${rate ?? "--"}%\n\n## 指标\n\n- 总数 ${latest?.total ?? 0} / 通过 ${latest?.passed ?? 0} / 失败 ${latest?.failed ?? 0} / 跳过 ${latest?.skipped ?? 0}\n\n## 轨迹\n\n` +
      logs.map(l => `- [${l.seq}] ${l.content}`).join("\n");
    $("rp-export-view").textContent = md; $("rp-export-view").classList.remove("hidden");
  };
}

/* ---------- 设置（仅管理员） ---------- */
async function refreshSettings() {
  const cfg = await api("/config");
  $("st-config").innerHTML = Object.entries(cfg).filter(([k]) => k !== "note").map(([k, v]) =>
    `<div class="k">${k}</div><div>${typeof v === "object" ? JSON.stringify(v) : v}</div>`).join("") +
    `<div class="k">说明</div><div class="hint" style="grid-column:1/-1">${cfg.note}</div>`;
  const imgs = await api("/images");
  $("st-images").innerHTML = imgs.map(i =>
    `<div class="img-row"><span>${i.image}</span><span class="badge ${i.ready ? "badge-ok" : "badge-fail"}">${i.ready ? "就绪 " + i.size_mb + "MB" : "缺失"}</span>` +
    (i.ready ? "" : `<button class="btn btn-primary btn-sm" onclick="buildImage('${i.image}')">自动构建</button>`) + `</div>`).join("");
  const users = await api("/users");
  $("st-users").innerHTML = users.map(u =>
    `<div class="img-row"><span>${u.username}</span><span class="badge badge-${u.role === "developer" ? "dev" : u.role}">${ROLE_CN[u.role]}</span><span class="hint">创建于 ${u.created_at}</span></div>`).join("");
}

async function buildImage(image) {
  try { await api(`/images/${encodeURIComponent(image)}/build`, { method: "POST", body: "{}" }); refreshSettings(); }
  catch (e) { alert(e.message); }
}

/* ---------- 绑定与启动 ---------- */
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
  try { ME = await api("/me"); applyRole(); route(); }
  catch (_) { /* 401 已弹出登录 */ }
})();
