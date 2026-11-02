/* L1 前端逻辑：hash 路由 + 角色守卫 + 轮询（主链路真接口） */
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (m) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[m]));

const ROLE_CN = { admin: "管理员", developer: "开发者", caller: "调用者" };
const STATUS_CN = { queued: "待下发", cloning: "克隆中", analyzing: "分析中", provisioning: "准备环境",
  generating: "生成用例", ready_for_review: "待审查", running: "执行中", reporting: "出报告中",
  done: "已完成", failed: "失败", cancelled: "已取消" };
const STEPS = ["输入项目信息", "AI 生成测试任务", "脚本工作台", "人工确认", "下发执行", "查看报告"];
// 两阶段两段式 stepper（设计文档 §6：零新发明，节点状态→格子三态）
const STAGE1_STEPS = ["克隆", "摸底", "考纲", "出题"];
const STAGE2_STEPS = ["布考场", "开考", "判卷", "报告", "复审"];

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
let REGISTER_MODE = false;
function toggleRegister() {
  REGISTER_MODE = !REGISTER_MODE;
  $("login-btn").textContent = REGISTER_MODE ? "注 册" : "登 录";
  $("login-err").textContent = REGISTER_MODE ? "注册后默认为「调用者」角色；口令至少 6 位" : "";
}
function showLogin() { $("login-mask").classList.remove("hidden"); }
async function doLogin() {
  $("login-err").textContent = "";
  try {
    const b = await fetch("/api" + (REGISTER_MODE ? "/register" : "/login"), { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value }) });
    let data = await b.json();
    if (REGISTER_MODE) {                                   // 注册成功自动登录
      const lb = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value }) });
      data = await lb.json();
      if (!lb.ok) throw new Error(data.detail || "自动登录失败");
      REGISTER_MODE = false; $("login-btn").textContent = "登 录";
    }
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
  document.querySelector('[data-page="review"]').style.display = isStaff ? "" : "none";
  document.querySelector('[data-page="settings"]').style.display = ME.role === "admin" ? "" : "none";
}

/* ---------- 路由 ---------- */
const PAGES = ["dashboard", "new", "workbench", "review", "tasks", "reports", "profile", "settings"];
let TASK_TID = 0;                            // F03 详情页当前任务
let REVIEW_TID = 0;                          // 评审页当前任务
let REVIEW_POLL = null;                      // 评审页轮询（阶段一跑动时实时刷新）
let TD_POLL = null;                          // 任务详情页轮询（活跃态 3s 刷新流水线）
let EXEC_POLL = null;                        // 实时用例执行面板轮询（活跃态 2s）
function route() {
  let page = (location.hash || "#/dashboard").replace("#/", "");
  if (page.startsWith("task/")) {           // F03 任务详情路由 #/task/{tid}
    page = "taskdetail";
    TASK_TID = parseInt(page.split("/") ? (location.hash.split("/")[2] || "0") : "0", 10);
  }
  if (page.startsWith("review/")) {         // 评审页路由 #/review/{tid}
    page = "review";
    REVIEW_TID = parseInt(location.hash.split("/")[2] || "0", 10) || 0;
  }
  if (!PAGES.includes(page) && page !== "taskdetail") page = "dashboard";
  if (!ME) return;   // /api/me 未返回前先不渲染（whoami 返回后 808 行会重入 route()）
  if (page === "settings" && ME.role !== "admin") page = "dashboard";
  if ((page === "workbench" || page === "review") && !(ME.role === "admin" || ME.role === "developer")) page = "dashboard";
  PAGES.forEach(p => $("page-" + p).classList.toggle("hidden", p !== page));
  // 任务A3：taskdetail 不在 PAGES 里，上面那行永远碰不到它——初始 hidden 就永远摘不掉，
  // #/task/{tid} 变成"点了没反应"（所有「查看执行进度」链接全指这里）。显式 toggle：
  $("page-taskdetail").classList.toggle("hidden", page !== "taskdetail");
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  if (page === "dashboard") loadDashboard();
  if (page === "workbench") refreshWorkbench();
  if (page === "review") { if (REVIEW_TID) loadReview(REVIEW_TID); else pickReviewTask(); }
  if (page === "tasks") pollTasks();
  if (page === "reports") refreshReports();
  if (page === "settings") refreshSettings();
  if (page === "profile") loadProfile();
  if (page === "new" && NEW_TASK_ID) pollNewCases(NEW_TASK_ID);
  if (page === "taskdetail" && TASK_TID) loadTaskDetail(TASK_TID);
  if (page !== "tasks" && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (page !== "review" && REVIEW_POLL) { clearInterval(REVIEW_POLL); REVIEW_POLL = null; }
  if (page !== "taskdetail" && TD_POLL) { clearTimeout(TD_POLL); TD_POLL = null; }
  if (page !== "taskdetail" && EXEC_POLL) { clearTimeout(EXEC_POLL); EXEC_POLL = null; }
  if (page !== "workbench") stopConfirmWatch();         // 离开工作台停阶段一监控（终态监听 watchTaskFinal 跨页保留）
}

/* ---------- 两阶段评审页（阶段一停等点） ---------- */
// 两段式 stepper：格子三态（done/active/queued）由任务状态+日志前缀推导，零新发明
function renderStageStepper(status, logs) {
  const doneNodes = new Set();
  (logs || []).forEach(l => {
    ["[clone]", "[analyze]", "[plan]", "[generate 第", "[provision]", "[run]", "[evaluate", "[report", "[review"]
      .forEach(k => { if ((l.content || "").includes(k)) doneNodes.add(k); });
  });
  const s1Done = k => doneNodes.has(k);
  const s1 = [
    { n: "克隆", d: s1Done("[clone]") },
    { n: "摸底", d: s1Done("[analyze]") },
    { n: "考纲", d: s1Done("[plan]") },
    { n: "出题", d: s1Done("[generate 第") || status === "ready_for_review" },
  ];
  const stage2Active = ["provisioning", "running", "reporting", "done", "failed"].includes(status);
  const s2 = [
    { n: "布考场", d: s1Done("[provision]") },
    { n: "开考", d: s1Done("[run]") },
    { n: "判卷", d: s1Done("[evaluate") },
    { n: "报告", d: s1Done("[report") },
    { n: "复审", d: s1Done("[review") || ["done", "failed"].includes(status) },
  ];
  const seg = (steps, stage) => {
    let activeMarked = false;
    return steps.map(s => {
      let cls = s.d ? "done" : (!activeMarked && stage === 1 ? "active" :
        (!activeMarked && stage2Active ? "active" : ""));
      if (cls === "active") activeMarked = true;
      if (status === "ready_for_review" && stage === 1 && !s.d) cls = "";
      return `<span class="step ${cls}"><span class="n">${s.d ? "✓" : "·"}</span>${s.n}</span>`;
    }).join('<span class="step-arrow">→</span>');
  };
  $("stepper-review").innerHTML =
    `<span class="stage-tag">阶段一</span>` + seg(s1, 1) +
    `<span class="stage-gap"></span>` +
    `<span class="stage-tag">阶段二</span>` + seg(s2, 2);
}

function decisionCard(d) {
  const DCN = { approve: ["通过", "badge-ok"], reject: ["删除", "badge-fail"],
                regenerate: ["再生成", "badge-run"], insert: ["插入", "badge-queued"] };
  const [cn, cls] = DCN[d.decision] || [d.decision, "badge-queued"];
  const role = d.reviewer_role === "ai_proxy" ? "AI 代审" : "人工";
  const ev = d.evidence && d.evidence.condition_ids && d.evidence.condition_ids.length
    ? ` <span class="d-ev">证据：条件 ${d.evidence.condition_ids.join("、")}</span>` : "";
  // 抽检三形态：pending=可点；done=就地变已复核（按钮消失，显示人工结论）；普通=无抽检位
  let spot = "";
  if (d.spotcheck_status === "done") {
    const rv = d.spotcheck_review || {};
    const ok = rv.verdict === "uphold";
    spot = `<span class="badge ${ok ? "badge-ok" : "badge-cancel"}">人工已${ok ? "同意" : "推翻"}</span>` +
      `<span class="d-ev">${esc(rv.note || "")}${rv.at ? " · " + esc(rv.at) : ""}</span>`;
  } else if (d.spotcheck) {
    spot = `<span class="badge badge-cancel">待抽检</span>
       <button class="op-link" onclick="spotcheck(${d.id}, 'uphold', this)">同意</button>
       <button class="op-link" onclick="spotcheck(${d.id}, 'override', this)">推翻</button>`;
  }
  return `<div class="decision">
    <div class="d-head"><span class="badge ${cls}">${cn}</span>
      <code>${esc(d.target === "task" ? "（任务准入）" : d.target)}</code>
      <span class="d-ev">${role} · 第${d.round}轮${d.decision === "reject" && d.reviewer_role === "ai_proxy" ? "（已停用，可勾选恢复）" : ""}</span> ${spot}</div>
    <div class="d-body">${esc(d.reasoning)}${ev}</div></div>`;
}

/* 评审页任务选择器（用户反馈：只显示最新一个、多任务并发切不了）：
   下拉列全部任务（待审查排最前并标★），待审查快捷胶囊一键切换；当前任务高亮。 */
// 任务1：下拉按两阶段流水线排序——阶段二执行中 > 待审查 > 阶段一执行中 > 待下发 > 其他终态，
// 组内按最近活动倒序（对应 stepper 从左到右、从上到下的推进方向）
function orderTasksByPipeline(tasks) {
  const GROUP = { provisioning: 0, running: 0, reporting: 0,          // 阶段二执行中
                  ready_for_review: 1,                                // 待审查
                  cloning: 2, analyzing: 2, generating: 2,            // 阶段一执行中
                  queued: 3 };                                        // 待下发
  const key = t => [GROUP[t.status] ?? 4, String(t.updated_at || t.created_at || "")].slice(0, 1)
    .concat([]); // 占位
  return [...tasks].sort((a, b) => {
    const ga = GROUP[a.status] ?? 4, gb = GROUP[b.status] ?? 4;
    if (ga !== gb) return ga - gb;
    return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
  });
}

async function renderReviewPicker(currentTid) {
  let tasks = [];
  try { tasks = await api("/tasks"); } catch (_) { return []; }
  tasks = orderTasksByPipeline(tasks);
  const pending = tasks.filter(t => t.status === "ready_for_review");
  const others = tasks.filter(t => t.status !== "ready_for_review");
  const sel = $("rv-task");
  const opt = t => `<option value="${t.id}">#${t.id} ${esc(t.project_name)}（${esc(STATUS_CN[t.status] || t.status)}）` +
    `${t.status === "ready_for_review" ? " ★待审查" : ""}${t.ai_reviewer && t.ai_reviewer !== "off" ? " ·代审" + t.ai_reviewer : ""}</option>`;
  sel.innerHTML = `<option value="">— 选择要评审的任务 —</option>` +
    (pending.length ? `<optgroup label="待审查（${pending.length}）">${pending.map(opt).join("")}</optgroup>` : "") +
    `<optgroup label="其他任务（只读查看）">${others.map(opt).join("")}</optgroup>`;
  sel.value = currentTid ? String(currentTid) : "";
  sel.onchange = () => { if (sel.value) location.hash = "#/review/" + sel.value; };
  $("rv-pending-pills").innerHTML = pending.length
    ? `<span class="hint" style="margin:0">待审查 ${pending.length} 个：</span>` + pending.map(t =>
        `<a href="#/review/${t.id}" class="badge ${Number(currentTid) === t.id ? "badge-run" : "badge-queued"}" ` +
        `style="text-decoration:none" title="${esc(t.project_name)}">#${t.id}</a>`).join("")
    : '<span class="hint" style="margin:0">当前没有待审查任务</span>';
  return pending;
}

async function loadReview(tid) {
  renderReviewPicker(tid);                                 // 选择器与正文并行加载
  const [detail, cases, rtm, dec, logs] = await Promise.all([
    api(`/tasks/${tid}`).catch(() => null),
    api(`/tasks/${tid}/cases?all=1`).catch(() => null),
    api(`/tasks/${tid}/rtm`).catch(() => null),
    api(`/tasks/${tid}/review-decisions`).catch(() => null),
    api(`/tasks/${tid}/logs`).catch(() => []),
  ]);
  if (!detail) { $("rv-hero").innerHTML = '<div class="hint">任务不存在</div>'; return; }
  renderStageStepper(detail.status, logs);
  const name = detail.project_name || `任务 ${tid}`;
  const aiCn = { off: "关", hybrid: "AI 先审+抽检", auto: "AI 全权" }[detail.ai_reviewer] || detail.ai_reviewer;
  // 右上角入口按状态给"下一步该去哪"（用户反馈：原"任务详情→"不知道去干嘛）：
  // 执行中→详情页看流水线实时进度；已完成/失败→报告页看判决与分析；待审查→本页就是主场，不另给入口
  const stActive = ["provisioning", "running", "reporting"].includes(detail.status);
  const stFinal = ["done", "failed"].includes(detail.status);
  const nextLink = stActive
    ? `<a href="#/task/${tid}" class="btn btn-ghost btn-sm" style="text-decoration:none" title="执行流水线实时进度、四级接力状态、溯源链">▶ 查看执行进度</a>`
    : stFinal
      ? `<a href="javascript:viewReport(${tid})" class="btn btn-ghost btn-sm" style="text-decoration:none" title="判决卡、失败原因与 AI 分析、覆盖率与图表">📄 查看测试报告</a>`
      : detail.status === "ready_for_review" ? ""
        : `<a href="#/task/${tid}" class="hint" style="margin:0" title="任务头/四级卡/执行流水线/溯源链/归因面板">任务详情 →</a>`;
  $("rv-hero").innerHTML =
    `<div class="rv-hero-top">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span class="rv-title">${esc(name)}</span>
        <span class="badge badge-queued">任务 #${tid}</span>
        <span class="badge ${detail.status === "ready_for_review" ? "badge-run" : "badge-queued"}">${esc(STATUS_CN[detail.status] || detail.status)}</span>
        <span class="hint" style="margin:0">代审：${esc(aiCn)}${detail.two_stage ? " · 两阶段" : ""}</span></div>
      ${nextLink}</div>
    <div class="rv-req">${esc(detail.requirement_text || "—")}</div>`;
  // 方案链（wb-plan 渲染器平移，目标容器参数化指向 rv-plan-body）
  if (rtm && (rtm.conditions_total || (rtm.matrix || []).length)) {
    renderWbPlan(rtm, detail, "rv-plan-body");
  } else {
    $("rv-plan-body").innerHTML = "该任务尚无测试条件数据（准备阶段的考纲在 plan 节点产出后出现）";
  }
  // 用例清单（勾选=启用；待审查态可增删，布考场按勾选结论重建文件）
  // 任务A1：改用 ?all=1 按脚本分组——只看最新脚本会把原始用例整个藏掉（增补写在
  // 独立 test_gen_fbN，version 更高），用户分不清哪些是本轮新增
  const box = $("rv-cases");
  const scriptLabel = (sc) => {
    const fn = sc.filename || "";
    if (/fb\d+/.test(fn)) return `对话增补 ${fn.replace(/^test_gen_/, "").replace(/\..*$/, "")}`;
    if (/r\d+/.test(fn)) return `首轮生成 ${fn.replace(/\..*$/, "")}`;
    if (/imported/.test(fn)) return "人工插入";
    return fn || `脚本 v${sc.version}`;
  };
  const caseRow = (c) => {
    const src = c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置";
    const desc = c.expected ? esc(String(c.expected).slice(0, 80)) : "";
    const basis = c.expected_basis ? `<span class="badge badge-queued" style="font-size:10px;padding:1px 6px">${esc(c.expected_basis)}</span>` : "";
    return `<div class="case-item">
      <label><input type="checkbox" ${c.enabled ? "checked" : ""} onchange="toggleReviewCase(${c.id})">
        <code>${esc(c.name)}</code></label>
      <span class="m">${src}·${esc(c.method) || "-"}</span>
      ${desc ? `<div class="case-desc">${desc} ${basis}</div>` : ""}
    </div>`;
  };
  if (cases && cases.scripts && cases.scripts.length) {
    box.innerHTML = cases.scripts.map(sc =>
      `<div style="margin-bottom:12px">
        <div class="hint" style="margin:0 0 4px"><b>${esc(scriptLabel(sc))}</b>（${sc.cases.length} 条）</div>
        ${sc.cases.map(caseRow).join("")}
      </div>`).join("");
  } else if (cases && cases.cases && cases.cases.length) {   // 老契约兜底（?all 未生效时）
    box.innerHTML = cases.cases.map(caseRow).join("");
  } else {
    box.innerHTML = '<div class="hint">暂无用例（准备阶段出题后出现）</div>';
  }
  // 日志
  $("rv-logs").innerHTML = (logs || []).map(l =>
    `<div class="log-line"><b>[${l.seq}]</b> ${esc(l.content)}</div>`).join("") || '<div class="hint">暂无日志</div>';
  // 决策卡片（卡头解释：抽检是干什么的、记录去哪看）
  const dwrap = $("rv-decisions");
  if (dec && dec.decisions.length) {
    dwrap.innerHTML = `<div class="hint" style="margin:0 0 6px">共 ${dec.decisions.length} 条决策（最新轮抽检 ${dec.pending_spotcheck} 条待人工复核，比例 20%）。` +
      `<b>「同意/推翻」= hybrid 抽检</b>：你的结论作为人机对齐信号永久记录在 review_decisions 台账（论文对比实验的数据源），` +
      `推翻删除会即时恢复用例；复核结论直接显示在对应条目上，不用另找。</div>` +
      `<div id="rv-spot-toast" class="hidden" style="margin-bottom:8px"></div>` +
      dec.decisions.map(decisionCard).join("");
  } else {
    dwrap.innerHTML = '<div class="hint">暂无代审决策（off 模式=人工审查；hybrid/auto 模式的 AI 决策会在这里逐条列出，理由+证据可追责）</div>';
  }
  // 放行按钮：仅待审查态可点；非待审查任务本页只读（增补/插入同禁），避免误操作后端 409
  const reviewable = detail.status === "ready_for_review";
  $("rv-release").disabled = !reviewable;
  $("rv-fb-send").disabled = !reviewable;
  $("rv-ac-submit").disabled = !reviewable;
  if (!reviewable) {
    // 任务A4：按状态分化提示——失败要给 error/下一步，准备中要给阶段日志入口，
    // 不能一律"只读查看"让用户不知道发生了什么
    let hint;
    if (detail.status === "failed") {
      hint = `<div class="hint" style="margin-top:8px;color:#b91c1c"><b>任务已失败</b>（未走完执行链）` +
        (detail.error ? `——失败原因：<code>${esc(String(detail.error).slice(0, 300))}</code>` : "") +
        `<br>下一步：去 <a href="#/task/${tid}" style="color:var(--primary,#0e7c5a)">详情页</a> 看各节点日志定位阶段，` +
        `或修正后在「新建任务」重提；失败任务不会再产生报告/RTM。</div>`;
    } else if (["provisioning", "running", "reporting"].includes(detail.status)) {
      hint = `<div class="hint" style="margin-top:8px;color:#b45309">该任务正在执行阶段二（${esc(STATUS_CN[detail.status] || detail.status)}）——` +
        `用例清单此刻只读；<a href="#/task/${tid}" style="color:var(--primary,#0e7c5a)">▶ 详情页可看实时进度</a>。</div>`;
    } else if (["cloning", "analyzing", "generating"].includes(detail.status)) {
      hint = `<div class="hint" style="margin-top:8px;color:#b45309">该任务正在跑准备阶段（${esc(STATUS_CN[detail.status] || detail.status)}）——` +
        `出题完成后本页自动出现用例与审查控件（5 秒轮询）。</div>`;
    } else {
      hint = `<div class="hint" style="margin-top:8px;color:#b45309">⚠ 该任务当前为「${esc(STATUS_CN[detail.status] || detail.status)}」，` +
        `不在待审查状态——本页只读查看用例与决策；对话增补、手动插入、放行仅对待审查任务开放。</div>`;
    }
    $("rv-hero").insertAdjacentHTML("beforeend", hint);
  }
  $("rv-release").onclick = async () => {
    try {
      await api(`/tasks/${tid}/run`, { method: "POST", body: "{}" });
      $("rv-release-result").innerHTML = `<span style="color:#16a34a">✅ 已放行——阶段二启动（布考场→开考→判卷→报告→复审）。` +
        `<a href="#/task/${tid}" class="btn btn-primary btn-sm" style="text-decoration:none;margin-left:8px">▶ 去看执行进度</a> 跑挂会弹窗告诉你原因，跑完去「报告」看判决。</span>`;
      watchTaskFinal(tid, "阶段二（布考场/开考/判卷/报告/复审）");
      loadReview(tid);
    } catch (e) { $("rv-release-result").textContent = "放行失败：" + e.message; }
  };
  // 阶段一跑动中：5s 轮询刷新（粘性，离开页面即清）
  clearTimeout(REVIEW_POLL);
  if (["cloning", "analyzing", "generating"].includes(detail.status)) {
    REVIEW_POLL = setTimeout(() => loadReview(tid), 5000);
  }
}

async function pickReviewTask() {
  const pending = await renderReviewPicker(0);
  if (pending.length) { location.hash = "#/review/" + pending[0].id; return; }
  $("rv-hero").innerHTML = '<div class="hint">当前没有待审查的任务——两阶段任务在确认后自动跑准备阶段，跑完会出现在这里；' +
    '也可从上方下拉选任意任务只读查看其用例与决策。</div>';
  $("stepper-review").innerHTML = "";
  ["rv-plan-body", "rv-cases", "rv-decisions", "rv-logs"].forEach(id => { $(id).innerHTML = ""; });
}

async function sendFeedback() {
  const tid = REVIEW_TID;
  const text = $("rv-fb-text").value.trim();
  if (!tid || !text) { $("rv-fb-result").textContent = "先写一句你想补充/修改的审查意见"; return; }
  $("rv-fb-send").disabled = true;
  $("rv-fb-result").textContent = "AI 正在按反馈增补生成（走同一三道闸，稍候）…";
  try {
    const b = await api(`/tasks/${tid}/feedback`, { method: "POST",
      body: JSON.stringify({ feedback: text }) });
    // 任务A1：不只报数字——「本轮新增 N 条」面板逐条列名字/方法/作用（expected）/
    // 预期依据/来源文件，默认展开（details open），用户不用再去下面翻清单对差异
    const newCases = b.cases || [];
    const panel = newCases.length
      ? `<details open style="margin-top:8px;background:#f0faf4;border:1px solid #bbe7cb;border-radius:8px;padding:8px 12px">
          <summary style="cursor:pointer;font-weight:700;color:#15803d">本轮新增 ${newCases.length} 条用例（${esc(b.file || "")}）——点开/收起</summary>
          <div style="margin-top:6px;display:grid;gap:6px">
          ${newCases.map(c => `
            <div style="border-left:3px solid #86efac;padding-left:8px">
              <code style="font-weight:700">${esc(c.name)}</code>
              <span class="badge badge-queued" style="font-size:10px;padding:1px 6px;margin-left:6px">${esc(c.method || "-")}</span>
              ${c.priority ? `<span class="hint" style="margin:0 0 0 6px">优先级 ${esc(String(c.priority))}</span>` : ""}
              <div class="case-desc" style="margin-top:2px">作用：${esc(c.expected ? String(c.expected).slice(0, 120) : "（未写预期）")}
                ${c.expected_basis ? `　<span class="badge badge-queued" style="font-size:10px;padding:1px 6px">依据：${esc(c.expected_basis)}</span>` : ""}</div>
            </div>`).join("")}
          </div>
          <div class="hint" style="margin:6px 0 0">这些用例已并入下方用例清单的「对话增补」分组，与其他用例一样可勾选/取消。</div>
        </details>`
      : "";
    $("rv-fb-result").innerHTML = `<span style="color:#16a34a">✅ 增补 ${b.generated} 条 → ${esc(b.file || "")}</span>` +
      (b.static_rejected && b.static_rejected.length ? `；闸门剔除 ${b.static_rejected.length} 条` : "") + panel;
    $("rv-fb-text").value = "";
    loadReview(tid);
  } catch (e) { $("rv-fb-result").textContent = "增补失败：" + e.message; }
  finally { $("rv-fb-send").disabled = false; }
}

async function toggleReviewCase(id) {
  try { await api(`/cases/${id}/toggle`, { method: "POST", body: "{}" }); loadReview(REVIEW_TID); }
  catch (e) { alert(e.message); loadReview(REVIEW_TID); }
}

async function spotcheck(decisionId, verdict, btn) {
  // 原地反馈：按钮即变处理中，成功后整卡刷新为「人工已同意/已推翻」形态（不弹窗）
  if (btn) {
    btn.disabled = true;
    btn.textContent = verdict === "uphold" ? "同意中…" : "推翻中…";
  }
  try {
    const b = await api(`/review-decisions/${decisionId}/spotcheck`, { method: "POST",
      body: JSON.stringify({ verdict }) });
    await loadReview(REVIEW_TID);
    const toast = $("rv-spot-toast");
    if (toast) {
      toast.className = "";
      toast.style.cssText = "background:#e8f7ee;border:1px solid #bbe7cb;color:#15803d;" +
        "border-radius:8px;padding:8px 12px;font-size:12.5px";
      toast.textContent = verdict === "uphold"
        ? "✅ 已同意——结论已记入台账（对齐信号），本条卡片已更新为「人工已同意」"
        : `✅ 已推翻——结论已记入台账${b.restored ? "，用例 " + b.restored + " 已恢复启用" : ""}，本条卡片已更新`;
      setTimeout(() => { toast.classList.add("hidden"); }, 6000);
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = verdict === "uphold" ? "同意" : "推翻"; }
    alert("复核失败：" + e.message);
  }
}

async function batchReviewer(mode) {
  const ids = TASKS.filter(t => t.status !== "cloning" && t.status !== "running").map(t => t.id);
  if (!ids.length) { alert("没有可设置的任务"); return; }
  if (!confirm(`对 ${ids.length} 个任务批量设置代审模式 = ${mode}？`)) return;
  try {
    const b = await api("/tasks/batch/ai-reviewer", { method: "POST",
      body: JSON.stringify({ mode, task_ids: ids }) });
    alert(`已应用 ${b.results.filter(r => r.applied).length}/${ids.length} 个`);
    pollTasks();
  } catch (e) { alert(e.message); }
}

async function batchAutoRun() {
  const ids = TASKS.filter(t => t.ai_reviewer === "auto" && t.confirmed !== 1).map(t => t.id);
  if (!ids.length) { alert("没有「已开 auto 且未确认」的任务——先批量开 auto"); return; }
  if (!confirm(`批量启动 ${ids.length} 个任务的 AI 代审全链（D1 准入→出题→用例集代审→执行）？\n任何任务停在等待点都会如实停下交人工（fail-closed）。`)) return;
  try {
    const b = await api("/tasks/batch/auto-run", { method: "POST",
      body: JSON.stringify({ task_ids: ids }) });
    alert(`已启动 ${b.results.filter(r => r.started).length}/${ids.length} 条代审链`);
    pollTasks();
  } catch (e) { alert(e.message); }
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
    const twoStage = $("new-two-stage") && $("new-two-stage").checked ? 1 : 0;
    const aiReviewer = $("new-ai-reviewer") ? $("new-ai-reviewer").value : "off";
    const b = await api("/tasks", { method: "POST",
      body: JSON.stringify({ repo_url: repo, requirement: req, auto_prepare: autoPrepare,
                             two_stage: twoStage, ai_reviewer: aiReviewer }) });
    NEW_TASK_ID = b.task_id;
    $("new-result").innerHTML = `任务 <b>#${b.task_id}</b> 已创建（项目：${esc(b.project_name)}）。` +
      (twoStage
        ? `两阶段模式：到 <a href="#/workbench">脚本工作台</a> 人工确认后，Agent 自动跑准备阶段（克隆/摸底/考纲/出题），跑完在 <a href="#/review/${b.task_id}">用例评审页</a> 等你审查。`
        : `下一步：<a href="#/workbench">进入脚本工作台</a> 确认后下发执行。`);
    $("new-result").classList.remove("hidden");
    renderStepper("stepper-new", 1);
    pollNewCases(b.task_id);
  } catch (e) { $("new-result").textContent = "创建失败：" + e.message; }
  finally { $("new-submit").disabled = false; }
}

let NEW_POLL_TIMER = null;   // 用户反馈：AI 生成用例"完全看不到"——单次拉取改持续轮询
async function pollNewCases(tid) {
  clearTimeout(NEW_POLL_TIMER);
  try {
    const [b, st] = await Promise.all([
      api(`/tasks/${tid}/cases`),
      api(`/tasks/${tid}`).catch(() => null),
    ]);
    const box = $("new-cases");
    const status = st ? st.status : "unknown";
    if (!b.script || !b.cases.length) {
      box.innerHTML = `<div class="hint">任务 #${tid} ${esc(STATUS_CN[status] || status)}——AI 按五种方法生成后，用例会实时出现在这里（每 8 秒刷新）。</div>`;
      if (!["done", "failed", "stopped", "blocked"].includes(status) && location.hash.includes("new")) {
        NEW_POLL_TIMER = setTimeout(() => pollNewCases(tid), 8000);
      }
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
  const tasks = orderTasksByPipeline(await api("/tasks"));
  sel.innerHTML = `<option value="">— 选择任务 —</option>` +
    tasks.map(t => `<option value="${t.id}">#${t.id} ${esc(t.project_name)}（${STATUS_CN[t.status]}）</option>`).join("");
  sel.onchange = loadWorkbench;
}

function wbTargets(code) {
  // 从用例代码提取被测目标：from <模块> import <函数们> → 模块.函数 列表（确定性，无 LLM）
  const out = [];
  const re = /from\s+([\w\.]+)\s+import\s+([\w\s,]+)/g;
  let m;
  while ((m = re.exec(code || ""))) {
    m[2].split(",").forEach(fn => {
      const name = fn.trim().split(/\s+as\s+/)[0];
      if (name && !name.startsWith("pytest") && !name.startsWith("hypothesis")) out.push(m[1] + "." + name);
    });
  }
  return out.slice(0, 4);
}

function renderWbPlan(rtm, detail, targetId = "wb-plan-body") {
  const box = $(targetId);
  if (!rtm || (!rtm.conditions_total && !(rtm.matrix || []).length)) {
    box.innerHTML = detail.status === "queued"
      ? "该任务尚未下发执行。<b>确认并下发后</b>，Agent 会：①把你的需求拆解成一条条可验证的测试条件（测试分析）；②按条件生成用例或复用已有用例（测试设计）；③在容器里运行（执行）；④出报告并标注哪些条件没覆盖到（评估）。本面板届时展示完整链路。"
      : "该任务无测试条件数据（plan 关闭或运行于旧版本）。";
    return;
  }
  const src = rtm.map_source === "llm" ? "LLM 语义映射" : "关键词映射（2-gram）";
  const nameTargets = {};
  (rtm.cases || []).forEach(c => {
    const tg = wbTargets(c.content || "");
    // L2 契约用例的目标是 API 端点（steps 字段=路由，如 "POST /coupons"），不是函数
    if (c.steps && /^(GET|POST|PUT|DELETE|PATCH)\s/.test(c.steps)) tg.push("API " + c.steps);
    nameTargets[c.name] = tg;
  });
  let rows = "";
  (rtm.matrix || []).forEach(r => {
    const casesHtml = (r.cases || []).length
      ? r.cases.map(n => {
          const tg = (nameTargets[n] || []).map(t => `<code>${esc(t)}</code>`).join(" ");
          return `<div style="margin:3px 0 3px 18px"><span class="badge badge-queued" style="font-size:10px;padding:1px 6px">用例</span> <code>${esc(n)}</code>`
            + (tg ? ` <span class="hint">→ 目标：${tg}</span>` : "") + "</div>";
        }).join("")
      : '<div style="margin:3px 0 3px 18px"><span class="badge badge-fail" style="font-size:10px;padding:1px 6px">缺口</span> <span class="hint">无用例覆盖——下发执行后 Agent 优先为它生成用例</span></div>';
    rows += `<div style="padding:7px 0;border-bottom:1px dashed var(--line)">`
      + `<b style="font-weight:500">${esc(r.condition || "")}</b> `
      + `<span class="badge ${r.status === "covered" ? "badge-ok" : "badge-fail"}" style="font-size:10px;padding:1px 6px">${r.status === "covered" ? "已覆盖" : "缺口"}</span>${casesHtml}</div>`;
  });
  box.innerHTML = `<div class="hint" style="margin-bottom:6px">需求共拆解 <b>${rtm.conditions_total}</b> 条测试条件，已覆盖 <b>${rtm.matrix.filter(x => x.status === "covered").length}</b> 条（映射方式：${src}）。"
    + "每条用例标注了它测的目标函数——这就是"你的需求 ↔ 用例 ↔ 脚本目标"的对应关系。</div>${rows}`;
}

async function loadWorkbench() {
  const tid = $("wb-task").value;
  $("wb-cases").innerHTML = ""; $("wb-preview").textContent = "（选择任务后加载最新脚本）";
  $("wb-confirm-bar").classList.add("hidden");
  $("wb-confirm-ok").classList.add("hidden");
  stopConfirmWatch();                                    // 切换任务即停旧监控
  if (!tid) { $("wb-plan-body").innerHTML = "（选择任务后加载：显示需求拆解出的测试条件、每条条件由哪些用例覆盖、每个用例测哪个目标函数）"; return; }
  const [detail, cases, rtm] = await Promise.all([
    api(`/tasks/${tid}`), api(`/tasks/${tid}/cases`), api(`/tasks/${tid}/rtm`)]);
  const active = ["cloning","analyzing","provisioning","generating","running","reporting"].includes(detail.status);
  if (detail.confirmed !== 1 && !active) $("wb-confirm-bar").classList.remove("hidden");
  renderWbPlan(rtm, detail);
  // 两阶段任务确认后（阶段一跑动中/已停等/已失败）恢复进度面板，切走再切回来不丢
  if (detail.two_stage === 1 && detail.confirmed === 1) {
    if (detail.status === "ready_for_review" || detail.status === "failed") {
      $("wb-confirm-ok").classList.remove("hidden");
      renderConfirmDone(Number(tid), detail.status, detail.error);
    } else if (active || detail.status === "queued") {   // queued=刚确认线程起步中，几百 ms 后变 cloning
      $("wb-confirm-ok").classList.remove("hidden");
      watchStage1(Number(tid), false);
    }
  }
  $("wb-confirm").onclick = async () => {
    try {
      await api(`/tasks/${tid}/confirm`, { method: "POST", body: "{}" });
      $("wb-confirm-bar").classList.add("hidden");
      $("wb-confirm-ok").classList.remove("hidden");
      if (detail.two_stage === 1) {
        CONFIRM_ALERT_FAIL = Number(tid);                 // 阶段一自动启动：跑挂弹窗带原因（用户点名）
        loadWorkbench();                                  // 恢复分支会启动实时监控面板
      } else {
        $("wb-progress").innerHTML = '<div class="confirm-ok">✅ 已确认。下一步：到<b>任务列表</b>点【下发执行】——Agent 将按测试方案拆解需求、生成用例、在容器里运行并出报告。</div>';
        loadWorkbench();
      }
    } catch (e) { alert("确认失败：" + e.message); }
  };
  const box = $("wb-cases");
  box.innerHTML = (cases.cases.length ? cases.cases.map(c => {
    const src = c.source === "ai" ? "AI" : c.source === "imported" ? "人工" : "内置";
    // 用户反馈：每个用例要"大概介绍有什么作用"——expected 摘要+输入+依据标签
    const desc = c.expected ? esc(String(c.expected).slice(0, 52)) : "";
    const inp = c.input ? `<span class="hint" style="margin:0">输入：${esc(String(c.input).slice(0, 36))}</span>` : "";
    const basis = c.expected_basis ? `<span class="badge badge-queued" style="font-size:10px;padding:1px 6px">${esc(c.expected_basis)}</span>` : "";
    const tgt = wbTargets(c.content || "");
    return `<div class="case-item">` +
      `<label><input type="checkbox" ${c.enabled ? "checked" : ""} onchange="toggleCase(${c.id})">` +
      `<code>${esc(c.name)}</code></label>` +
      `<span class="m">${src}·${esc(c.method) || "-"}${c.priority ? "·" + esc(c.priority) : ""}</span>` +
      (desc ? `<div class="case-desc">${desc} ${basis}</div>` : "") +
      (inp ? `<div class="case-desc">${inp}</div>` : "") +
      (tgt ? `<div class="case-desc"><span class="hint" style="margin:0">目标：</span>${tgt.map(t => `<code>${esc(t)}</code>`).join(" ")}</div>` : "") +
      `</div>`;
  }).join("")
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

/* ---------- 阶段一实时监控（用户反馈：确认后"这期间的事情都去哪了"）----------
   确认=授权阶段一自动启动（克隆/摸底/考纲/出题），这里把全过程反馈给用户：
   状态徽章 + 节点日志流实时滚动；ready_for_review→跳评审页入口；failed→弹窗+红条显示原因。 */
let CONFIRM_TIMER = null;
let CONFIRM_ALERT_FAIL = null;   // 该任务阶段一失败要弹窗（confirm 点击时设；弹一次即清——
                                 // loadWorkbench 恢复分支会重启 alertOnFail=false 的监控，直接传参会互相覆盖）
function stopConfirmWatch() {
  if (CONFIRM_TIMER) { clearTimeout(CONFIRM_TIMER); CONFIRM_TIMER = null; }
}

function renderConfirmDone(tid, status, error) {
  const box = $("wb-progress");
  if (status === "failed") {
    box.innerHTML = `<div style="background:#fdecec;border:1px solid #f5c6c6;color:#b91c1c;` +
      `border-radius:8px;padding:10px 12px;font-size:12.5px;line-height:1.7">` +
      `<b>❌ 准备阶段失败</b>｜原因：${esc(error || "（详见下方日志）")}` +
      `<div style="margin-top:4px">可在日志里定位是哪一步出的错；修好后重新确认即可重跑阶段一。</div></div>`;
    return;
  }
  box.innerHTML = `<div style="background:#e8f7ee;border:1px solid #bbe7cb;border-radius:8px;` +
    `padding:10px 12px;font-size:12.5px;line-height:1.8">` +
    `<b style="color:#15803d">✅ 准备阶段完成</b>——Agent 已完成克隆/摸底/考纲/出题，任务停在<b>待审查</b>。` +
    `用例等你逐条审：可勾选启停、手动插入、或用右侧对话框让 AI 增补。` +
    `<div style="margin-top:6px"><a href="#/review/${tid}" class="btn btn-primary btn-sm" ` +
    `style="text-decoration:none;display:inline-block">进入用例评审页 →</a></div></div>`;
}

async function watchStage1(tid, alertOnFail, tries = 0) {
  stopConfirmWatch();
  if (tries > 200) return;                               // 200×3s = 10 分钟封顶
  try {
    const [detail, logs] = await Promise.all([
      api(`/tasks/${tid}`), api(`/tasks/${tid}/logs`).catch(() => [])]);
    const st = detail.status;
    if (st === "queued" && tries >= 10) {                 // 确认 30s 仍未起步：后台线程丢了（服务重启过）
      $("wb-progress").innerHTML = '<div class="confirm-bar" style="margin-top:0">阶段一未启动（服务可能重启过，后台线程已丢）——' +
        '重新点「人工确认」即可重跑准备阶段。</div>';
      return;
    }
    const node = { cloning: "正在克隆仓库…", analyzing: "正在摸底（语言占比/复杂度热点/准出标准）…",
                   provisioning: "正在准备环境…", generating: "正在按考纲出题…",
                   running: "正在容器里执行…", reporting: "正在写报告…" }[st] || st;
    const ok = st !== "failed";
    $("wb-progress").innerHTML = ok
      ? `<div style="background:var(--run-weak);border:1px solid #c3d7fe;border-radius:8px;` +
        `padding:10px 12px;font-size:12.5px"><span class="badge badge-run">${esc(STATUS_CN[st] || st)}</span> ` +
        `<b>${esc(node)}</b> <span class="hint" style="margin:0">——全过程输出见下方日志，跑完自动停在待审查等你审查</span></div>`
      : "";
    $("wb-progress-logs").innerHTML = (logs || []).slice(-40).map(l =>
      `<div class="log-line"><b>[${l.seq}]</b> ${esc(l.content)}</div>`).join("") ||
      '<div class="hint">日志加载中…</div>';
    const box = $("wb-progress-logs");
    box.scrollTop = box.scrollHeight;
    if (st === "ready_for_review") {
      renderConfirmDone(tid, "ready_for_review", null);
      return;                                            // 终态：停轮询
    }
    if (st === "failed") {
      renderConfirmDone(tid, "failed", detail.error);
      if (alertOnFail || Number(tid) === CONFIRM_ALERT_FAIL) {
        CONFIRM_ALERT_FAIL = null;
        alert("阶段一（准备阶段）失败\n\n原因：" + (detail.error || "详见工作台日志") +
              "\n\n日志区已定位到出错步骤；修复后可重新确认重跑。");
      }
      return;
    }
    CONFIRM_TIMER = setTimeout(() => watchStage1(tid, alertOnFail, tries + 1), 3000);
  } catch (_) {
    CONFIRM_TIMER = setTimeout(() => watchStage1(tid, alertOnFail, tries + 1), 5000);   // 网络抖动重试
  }
}

/* ---------- V1 指挥台（2026-11-02：一次请求拿全，服务端聚合） ---------- */
function relTime(ts) {
  if (!ts) return "";
  const d = new Date(String(ts).replace(" ", "T"));
  if (isNaN(d)) return String(ts).slice(0, 16);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  if (s < 86400 * 7) return Math.floor(s / 86400) + " 天前";
  return String(ts).slice(0, 10);
}

const DASH_NODES = ["[clone]", "[analyze]", "[plan]", "[generate 第", "[provision]", "[run]", "[evaluate", "[report", "[review"];
function dashPipeline(logs) {
  const done = new Set();
  (logs || []).forEach(l => DASH_NODES.forEach(k => { if ((l.content || "").includes(k)) done.add(k); }));
  const steps = [["克隆", "[clone]"], ["摸底", "[analyze]"], ["考纲", "[plan]"],
                 ["出题", "[generate 第"], ["布考场", "[provision]"], ["开考", "[run]"],
                 ["判卷", "[evaluate"], ["报告", "[report"], ["复审", "[review"]];
  let nowMarked = false;
  return steps.map(([cn, key]) => {
    const d = done.has(key);
    let cls = d ? "dn" : "";
    if (!d && !nowMarked) { cls = "now"; nowMarked = true; }
    return `<span class="st ${cls}">${d ? "✓ " : (cls === "now" ? "◉ " : "")}${cn}</span>`;
  }).join('<span class="ar">›</span>');
}

async function loadDashboard() {
  if (!ME) return;
  const d = await api("/dashboard").catch(() => null);
  if (!d) { $("dash-kpis").innerHTML = '<div class="hint">指挥台数据加载失败</div>'; return; }
  const k = d.kpi || {};
  $("dash-kpis").innerHTML = [
    [`${k.total ?? 0}`, `任务总数 · 今日 +${k.today ?? 0}`, ""],
    [k.pass_rate != null ? Math.round(k.pass_rate * 100) + "%" : "—", "通过率（近 30 个已评估任务）", "color:var(--ok)"],
    [`${k.defects ?? 0}`, "疑似产品缺陷（报告）", (k.defects ? "color:var(--fail)" : "")],
    [`${((k.tokens_today ?? 0) / 1000).toFixed(1)}k`, "LLM token（今日）", ""],
  ].map(([v, label, st]) => `<div class="dash-kpi"><div class="v" style="${st}">${esc(String(v))}</div><div class="k">${label}</div></div>`).join("");

  // 正在执行（准备阶段 + 执行阶段都算）
  const running = d.running || [];
  if (running.length) {
    $("dash-running").className = "card exec-live";
    $("dash-running").innerHTML = running.map(t => {
      const act = ["provisioning", "running", "reporting"].includes(t.status);
      return `<div style="margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="live-dot"></span><b>#${t.id} ${esc(t.project_name || "")}</b>
          <span class="hint" style="margin:0">${esc((t.requirement_text || "").slice(0, 40))}</span>
          <span class="badge badge-run" style="margin-left:auto">${esc(STATUS_CN[t.status] || t.status)} ${t.progress || 0}%</span>
          <a href="#/task/${t.id}" class="btn btn-sm" style="text-decoration:none">看实时进度 →</a>
        </div>
        <div class="pipe">${dashPipeline([])}</div>
        <div class="hint" style="margin:4px 0 0">${act
          ? "执行中：逐用例结果在详情页「实时用例执行」面板（按轮次更新）"
          : "准备阶段：克隆/摸底/考纲/出题跑完会停在「待审查」，等你审用例"}</div>
      </div>`;
    }).join("");
    // 首个任务补真实流水线（拉该任务日志，避免每张卡一次请求）
    const t0 = running[0];
    api(`/tasks/${t0.id}/logs`).then(logs => {
      const el = $("dash-running").querySelector(".pipe");
      if (el) el.innerHTML = dashPipeline(logs || []);
    }).catch(() => {});
  } else {
    $("dash-running").className = "card";
    $("dash-running").innerHTML = '<div style="display:flex;align-items:center;gap:10px">' +
      '<b>当前没有在跑的任务</b><span class="hint" style="margin:0">新建一个，或去任务列表看历史</span>' +
      '<a href="#/new" class="btn btn-sm btn-primary" style="text-decoration:none;margin-left:auto">＋ 新建测试</a></div>';
  }

  // 项目概览
  const projs = d.projects || [];
  $("dash-projects").innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <b style="font-size:15px">项目概览</b>
      <a href="#/tasks" class="hint" style="margin:0 0 0 auto;text-decoration:none">全部任务 →</a></div>` +
    (projs.length ? `<div class="table-wrap"><table class="dash-tbl">
      <tr><th>项目</th><th>语言</th><th>任务</th><th>通过 / 失败 / 在跑 / 待审</th><th>最近</th><th></th></tr>
      ${projs.map(p => `<tr class="clickable" onclick="location.hash='#/tasks'">
        <td><b>${esc((p.name || "").split(/[\/\\]/).filter(Boolean).pop() || p.name || "")}</b></td>
        <td class="hint" style="margin:0">${esc(p.primary_language || "—")}</td>
        <td class="tabular-nums">${p.n ?? 0}</td>
        <td><span class="badge badge-ok">${p.done ?? 0}</span>
            ${p.failed ? `<span class="badge badge-fail">${p.failed}</span>` : ""}
            ${p.active ? `<span class="badge badge-run">${p.active} 在跑</span>` : ""}
            ${p.review ? `<span class="badge badge-queued">${p.review} 待审</span>` : ""}</td>
        <td class="hint" style="margin:0">${relTime(p.last)}</td>
        <td style="text-align:right"><a href="#/tasks" class="t-ops" style="text-decoration:none;color:var(--primary);font-weight:700">进项目</a></td>
      </tr>`).join("")}</table></div>`
      : '<div class="hint">还没有项目——新建一个测试任务即自动登记项目</div>');

  // 待审查
  const pend = d.pending_review || [];
  $("dash-review").innerHTML = `<b style="font-size:14px">待你审查（${pend.length}）</b>` +
    (pend.length ? pend.map(t => `<div class="rev-item" style="margin-top:8px">
        <b>#${t.id} ${esc((t.requirement_text || "").slice(0, 30))}</b>
        <div class="meta">${esc(t.project_name || "")} · ${t.cases ?? 0} 条用例${t.ai_reviewer && t.ai_reviewer !== "off" ? " · 代审 " + esc(t.ai_reviewer) : ""}</div>
        <div class="acts"><a href="#/review/${t.id}" class="btn btn-sm btn-primary" style="text-decoration:none">去审查</a>
          <a href="#/task/${t.id}" class="btn btn-sm" style="text-decoration:none">看用例</a></div>
      </div>`).join("")
      : '<div class="hint" style="margin-top:6px">没有待审查任务——两阶段任务跑完准备阶段会出现在这里</div>');

  // 动态
  const feed = d.feed || [];
  $("dash-feed").innerHTML = '<b style="font-size:14px">动态</b>' +
    (feed.length ? feed.map(t => `<div class="feed-item">
        <b>#${t.id}</b><span>${esc(STATUS_CN[t.status] || t.status)}${t.cases ? " · " + t.cases + " 条用例" : ""}${t.coverage != null ? " · 覆盖 " + Math.round(t.coverage * 100) + "%" : ""}</span>
        <time>${relTime(t.updated_at || t.created_at)}</time></div>`).join("")
      : '<div class="hint" style="margin-top:6px">暂无活动</div>');
}

/* ---------- 任务列表（V3 精修 + B3 服务端分页筛选） ---------- */
const TASK_FILTER_STATUS = {
  all: "", running: "cloning,analyzing,generating,provisioning,running,reporting,queued",
  review: "ready_for_review", done: "done", failed: "failed",
};
const TASK_PAGE = { limit: 60, offset: 0, total: 0, counts: {}, loading: false };

async function pollTasks() {
  if (TASK_PAGE.loading) return;
  TASK_PAGE.loading = true;
  try {
    const st = TASK_FILTER_STATUS[TASK_FILTER] || "";
    const qs = new URLSearchParams({ limit: TASK_PAGE.limit, offset: TASK_PAGE.offset });
    if (st) qs.set("status", st);
    if (PG_SEARCH.trim()) qs.set("q", PG_SEARCH.trim());
    const d = await api("/tasks/page?" + qs.toString());
    TASKS = d.items || [];
    TASK_PAGE.total = d.total ?? TASKS.length;
    TASK_PAGE.counts = d.counts || {};
    renderTasks();
  } catch (_) {}
  finally { TASK_PAGE.loading = false; }
  if (!pollTimer) pollTimer = setInterval(pollTasks, 3000);
}

const PG_COLLAPSED = new Set();   // 收起的项目名（粘性：3s 轮询重渲染不丢）
let PG_SEARCH = "";

function renderTasks() {
  if (!ME) return;   // /api/me 未返回前的轮询重渲染直接跳过（下一轮 3s 补上）——修 ME.role 竞态
  const stCls = t => t.status === "done" ? "badge-ok" : t.status === "failed" ? "badge-fail" :
    (t.progress < 100 && t.status !== "queued" && t.status !== "cancelled") ? "badge-run" :
    t.status === "cancelled" ? "badge-cancel" : "badge-queued";
  const isStaff = ME.role !== "caller";
  const groups = new Map();
  TASKS.forEach(t => {
    const key = t.project_name || "（未命名项目）";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  });
  let html = "", shown = 0, idx = 0;
  groups.forEach((tasks, pname) => {
    const q = PG_SEARCH.toLowerCase();
    if (PG_SEARCH && pname.toLowerCase().indexOf(q) === -1 &&
        !tasks.some(t => (t.requirement_text || "").toLowerCase().indexOf(q) > -1)) return;
    idx++; shown++;
    const okN = tasks.filter(t => t.status === "done").length;
    const failN = tasks.filter(t => t.status === "failed").length;
    const runN = tasks.filter(t => t.progress < 100 && t.status !== "queued" && t.status !== "cancelled" && t.status !== "done").length;
    const open = !PG_COLLAPSED.has(pname);
    let rows = "";
    tasks.forEach(t => {
      const confirmable = isStaff && t.confirmed !== 1 && t.progress < 100;
      const runnable = isStaff && t.confirmed === 1 && t.progress < 100;
      const reviewable = isStaff && t.status === "ready_for_review";
      // V3 精修：状态=点线态（比徽章更快扫读）、覆盖率=进度条、操作只留主次两条
      const sd = stDot(t);
      const cov = t.coverage != null
        ? `<span class="cov-bar"><span class="tr"><span class="fl" style="width:${Math.round(t.coverage * 100)}%"></span></span><small>${Math.round(t.coverage * 100)}%</small></span>`
        : '<span class="hint" style="margin:0">—</span>';
      const primaryOp = reviewable
        ? `<a href="#/review/${t.id}">去审查 ★</a>`
        : (t.status === "failed" ? `<a href="#/task/${t.id}">失败原因</a>`
          : (t.progress < 100 && t.status !== "queued" && t.status !== "cancelled" && t.status !== "done"
            ? `<a href="#/task/${t.id}">实时进度</a>` : `<a href="javascript:viewReport(${t.id})">报告</a>`));
      rows += `<tr class="pg-task row" data-tid="${t.id}">
        <td class="tabular-nums"><span class="hint" style="margin:0">#${t.id}</span></td>
        <td><b style="font-size:12.5px">${esc((t.requirement_text || "").slice(0, 34))}${(t.requirement_text || "").length > 34 ? "…" : ""}</b>
            <div class="req-line">${t.confirmed !== 1 ? "未确认 · 禁止下发" : (t.ai_reviewer && t.ai_reviewer !== "off" ? "AI 代审 · " + esc(t.ai_reviewer) : "人工审查")}</div></td>
        <td><span class="st-dot ${sd.cls}"><i></i>${sd.txt}</span></td>
        <td class="tabular-nums">${t.cases != null ? t.cases : "—"} 条</td>
        <td>${cov}</td>
        <td class="hint hide-sm" style="margin:0">${relTime(t.updated_at || t.created_at)}</td>
        <td><div class="t-ops">${primaryOp}<a href="javascript:viewLogs(${t.id})" class="mut">日志</a></div></td></tr>`;
    });
    html += `<div class="pg-group ${open ? "open" : ""}">
      <div class="pg-head" role="button" tabindex="0" aria-expanded="${open}" data-p="${esc(pname)}">
        <span class="pg-chev">▾</span><span class="pg-name">${esc(pname)}</span>
        <span class="lang">${esc(tasks[0].primary_language || "-")}</span>
        <span class="pg-sum"><span class="mini ok">通过 ${okN}</span>
          ${failN ? `<span class="mini fail">缺陷 ${failN}</span>` : ""}
          ${runN ? `<span class="mini run-n">运行 ${runN}</span>` : ""}
          <span class="mini mut">${tasks.length} 次运行</span></span>
        <span class="hint pg-last">最近 ${esc(tasks[0].created_at || "")}</span>
      </div>
      <div class="pg-tbl ${open ? "" : "hidden"}"><table class="task-tbl">
        <thead><tr><th style="width:58px">#</th><th>需求</th><th>状态</th><th>用例</th><th>覆盖率</th><th class="hide-sm">最近</th><th style="text-align:right">操作</th></tr></thead>
        <tbody>${rows}</tbody></table></div></div>`;
  });
  const total = TASK_PAGE.total || 0;
  const pages = Math.max(1, Math.ceil(total / TASK_PAGE.limit));
  const curPage = Math.floor(TASK_PAGE.offset / TASK_PAGE.limit) + 1;
  const pager = total > TASK_PAGE.limit
    ? `<div class="t-pager">共 ${total} 个任务 · 第 ${curPage} / ${pages} 页
        <button class="btn btn-sm" ${curPage <= 1 ? "disabled" : ""} onclick="taskPage(${curPage - 1})">‹ 上一页</button>
        <button class="btn btn-sm" ${curPage >= pages ? "disabled" : ""} onclick="taskPage(${curPage + 1})">下一页 ›</button></div>`
    : (total ? `<div class="t-pager">共 ${total} 个任务</div>` : "");
  $("project-groups").innerHTML = (html ||
    '<div class="card hint" style="padding:24px;text-align:center">没有匹配的项目或任务——换个关键词或筛选条件</div>') + pager;
  document.querySelectorAll(".pg-head").forEach(h => {
    const toggle = () => {
      const name = h.dataset.p;
      PG_COLLAPSED.has(name) ? PG_COLLAPSED.delete(name) : PG_COLLAPSED.add(name);
      renderTasks();
    };
    h.onclick = toggle;
    h.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
  });
  document.querySelectorAll(".pg-task").forEach(tr => {
    tr.onclick = e => { if (e.target.closest("button")) return; viewReport(Number(tr.dataset.tid)); };
  });
}

function stDot(t) {
  const running = t.progress < 100 && !["queued", "cancelled", "done", "failed"].includes(t.status);
  if (t.status === "done") return { cls: "ok", txt: "完成" };
  if (t.status === "failed") {
    const product = (t.error || "").includes("疑似产品缺陷");
    return { cls: "er", txt: product ? "疑似缺陷" : "失败" };
  }
  if (t.status === "ready_for_review") return { cls: "wa", txt: "待审查" };
  if (running) return { cls: "blu", txt: (STATUS_CN[t.status] || t.status) + (t.progress ? " " + t.progress + "%" : "") };
  if (t.status === "cancelled") return { cls: "er", txt: "已取消" };
  return { cls: "wa", txt: STATUS_CN[t.status] || t.status };
}

function taskPage(n) {
  TASK_PAGE.offset = Math.max(0, (n - 1) * TASK_PAGE.limit);
  pollTasks();
}

async function confirmTask(id) {
  try {
    const b = await api(`/tasks/${id}/confirm`, { method: "POST", body: "{}" });
    pollTasks();
    if (b.stage1_started) watchTaskFinal(id, "阶段一（克隆/摸底/考纲/出题）");
  } catch (e) { alert("确认失败：" + e.message); }
}
async function runTask(id) {
  try {
    const b = await api(`/tasks/${id}/run`, { method: "POST", body: "{}" });
    pollTasks();
    watchTaskFinal(id, b.stage === 2 ? "阶段二（布考场/开考/判卷/报告/复审）" : "执行");
  } catch (e) { alert("下发失败：" + e.message); }
}

/* 终态监听（用户反馈：运行失败要弹窗说原因）——后台线程跑图，前端只能靠轮询拿终态；
   failed 弹窗带 error 字段；done/待审查静默（列表徽章已显示）；20 分钟封顶防永久轮询。 */
const FINAL_WATCH = {};
async function watchTaskFinal(tid, label, tries = 0) {
  clearTimeout(FINAL_WATCH[tid]);
  if (tries > 300) return;                               // 300×4s = 20 分钟
  try {
    const d = await api(`/tasks/${tid}`);
    if (d.status === "failed") {
      alert(`${label}失败（任务 #${tid}）\n\n原因：${d.error || "详见该任务日志"}\n\n` +
            `可到「报告」页看日志定位出错步骤。`);
      return;
    }
    if (["done", "cancelled", "ready_for_review"].includes(d.status)) return;
  } catch (_) { /* 网络抖动继续 */ }
  FINAL_WATCH[tid] = setTimeout(() => watchTaskFinal(tid, label, tries + 1), 4000);
}
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

// ---------- 报告概览·判决先行（用户反馈：概览太直白，要最快知道结果；失败要原因+分析+怎么修）----------
const FK_META = {
  product_defect: { title: "发现疑似产品缺陷", icon: "🔴", bg: "#fdecec", bd: "#f5c6c6", fg: "#b91c1c",
    advice: ["这些失败是<b>断言失败</b>——用例预期与程序实际行为不符，系统按「宁可误报不可造假」如实上报为证人，<b>未回炉迁就</b>。",
             "先看每条的「预期依据」：标<b>需求明示</b>的，预期可在需求里直接定位——若需求没错，就是产品缺陷，按失败消息定位源码修；",
             "标<b>模型推断/常识</b>的，优先复核用例预期本身是否写错（oracle 问题：失败不一定是程序错）；",
             "点下方「AI 详细分析」拿逐条根因推测与修复建议（基于真实失败消息和源码，标注置信度）。"] },
  case_defect: { title: "用例自身问题导致失败（非产品缺陷）", icon: "⚠️", bg: "#fdf3e1", bd: "#f3ddb0", fg: "#b45309",
    advice: ["失败类型是 import/语法/属性名错误——是<b>生成用例写坏了</b>，不是被测程序的问题。",
             "重新下发执行：R-Loop 会剪掉坏用例并补测；或到评审页手动修正/停用对应用例。"] },
  env: { title: "环境问题：测试未能有效执行", icon: "🔧", bg: "#f3f4f6", bd: "#e2e5ea", fg: "#52525b",
    advice: ["依赖安装、容器或镜像出了问题，用例根本没跑起来。",
             "查日志里 <code>[provision]</code>/<code>[run]</code> 行定位；确认 requirements.txt / package.json 完整；顶栏「一键准备」可补齐 Docker 与镜像。"] },
  static: { title: "静态模式：已产出测试设计，未执行", icon: "📋", bg: "#dbeafe", bd: "#c3d7fe", fg: "#1d4fd8",
    advice: ["该语言暂无容器执行链路（见 README 分级表），系统做了确定性扫描并生成测试设计草稿。",
             "草稿在「测试用例库」tab 可看（六要素齐全，framework=static_draft）；<b>没有覆盖率和执行结果</b>，报告如实标注未验证。"] },
};

function renderVerdict(t, latest, an) {
  if (!latest) {
    // 任务B：失败任务无报告 ≠ "等着就好"——给 error/死在哪一步/为什么没有报告与 RTM，
    // 禁止"不兼容/无数据"含糊话（#281 用户实况：概览和 RTM 生成不出来不知道为什么）
    if (t && t.status === "failed") {
      return `<div class="verdict" style="background:#fef2f2;border-color:#fecaca;color:#b91c1c">
        <div class="v-title">⛔ 任务失败——未执行到评估阶段，故无报告</div>
        <div class="v-sub" style="margin-top:6px">失败原因：<code>${esc(String(t.error || "（未记录，看日志 tab 定位阶段）").slice(0, 400))}</code></div>
        <div class="v-advice" style="margin-top:8px"><b>为什么没有报告和 RTM</b>
        <div class="hint" style="margin:4px 0 0">报告与需求追踪（RTM）快照在评估（判卷）阶段之后才产出；该任务在评估之前就停了，所以这里没有——<b>不是项目不兼容</b>。
        左侧 RTM tab 里已产出的测试条件会标「未映射（未评估）」。</div></div>
        <div class="v-advice" style="margin-top:8px"><b>怎么处理</b><ol>
          <li>看「日志」tab 找最后一个带节点前缀的行（= 死在哪一步）</li>
          <li>按失败原因处理（环境/预算类问题修复后重提任务；${esc(String(t.error || "").includes("预算超限") ? "快照预算可在 config.SNAPSHOT_MAX_TOTAL_BYTES 调整或 .gitignore 排除大目录" : "")}）</li>
          <li>重跑后在「报告」页看判决</li></ol></div></div>`;
    }
    return `<div class="verdict" style="background:#f3f4f6;border-color:#e2e5ea;color:#52525b">
      <div class="v-title">⏳ 暂无报告</div><div class="v-sub">任务 ${esc(STATUS_CN[t?.status] || t?.status || "")}——跑完评估阶段后这里给结论</div></div>`;
  }
  let defects = {};
  try { defects = JSON.parse(latest.defects || "{}"); } catch { defects = {}; }
  const fk = defects.failure_kind || "none";
  const total = latest.total || 0, passed = latest.passed || 0, failed = latest.failed || 0;
  const cov = latest.coverage_line != null ? Math.round(latest.coverage_line * 100) + "%" : "—";
  const rounds = latest.exec?.rounds ?? "—";
  const rev = latest.review ? (() => { try { return JSON.parse(latest.review); } catch { return null; } })() : null;
  const kpis = (items) => `<div class="v-kpis">${items.map(([k, v]) =>
    `<div class="v-kpi"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>`;

  // ✅ 通过
  if (fk === "none" || (!FK_META[fk] && failed === 0 && t?.status === "done")) {
    return `<div class="verdict" style="background:#e8f7ee;border-color:#bbe7cb;color:#15803d">
      <div class="v-title">✅ 测试通过</div>
      <div class="v-sub">${total} 条用例全部通过，覆盖率 ${cov}，R-Loop ${rounds} 轮${rev ? "，报告二次审查" + (rev.status === "pass" ? "通过" : "未过") : ""}——完整数据在下方展开。</div>
      ${kpis([["用例", total], ["通过", passed], ["覆盖率", cov], ["轮次", rounds]])}</div>`;
  }
  const meta = FK_META[fk] || { title: "任务未完成", icon: "⛔", bg: "#f3f4f6", bd: "#e2e5ea", fg: "#52525b",
    advice: ["执行在某一步诚实停下（见原因），修复后重新下发。"] };
  const ev = (an && an.evidence) || {};
  const failedRows = (ev.failed || []);
  const basisCount = {};
  failedRows.forEach(f => { const b = f.expected_basis || "未标注"; basisCount[b] = (basisCount[b] || 0) + 1; });
  const basisLine = Object.keys(basisCount).length
    ? `<div class="hint" style="margin:6px 0 0">失败用例的预期依据分布：${Object.entries(basisCount).map(([b, n]) => `<span class="badge badge-queued" style="font-size:11px">${esc(b)} ×${n}</span>`).join(" ")}</div>` : "";
  const table = failedRows.length ? `
    <div class="table-wrap" style="margin-top:10px"><table class="tbl v-fail-tbl">
      <tr><th>失败用例</th><th>预期（依据）</th><th>实际失败</th><th>归因</th></tr>
      ${failedRows.map(f => `<tr>
        <td><code>${esc(f.name)}</code><div class="hint" style="margin:2px 0 0">${esc(f.method || "")}</div></td>
        <td>${esc((f.expected || "—").slice(0, 90))}${f.expected_basis ? ` <span class="hint">〔${esc(f.expected_basis)}〕</span>` : ""}</td>
        <td><span class="badge ${f.error_class === "AssertionError" ? "badge-fail" : "badge-cancel"}">${esc(f.error_class)}</span>
            <div style="font-size:12px;margin-top:3px;color:var(--muted)">${esc((f.message || "").replace(/^[A-Za-z_]+:?\s*/, "").slice(0, 140))}</div></td>
        <td class="hint">${f.origin === "generated" ? "生成用例" : "自带测试"}</td></tr>`).join("")}
    </table></div>` : (fk === "static" ? "" : '<div class="hint" style="margin-top:8px">（该任务为旧日志格式或未执行到判卷，无逐条失败明细）</div>');
  const adviceHtml = `<div class="v-advice"><b>怎么处理</b><ol>${meta.advice.map(a => `<li>${a}</li>`).join("")}</ol></div>`;
  const aiBlock = fk === "static" ? "" : renderAnalysisBlock(t?.id, an && an.analysis, failedRows.length);
  return `<div class="verdict" style="background:${meta.bg};border-color:${meta.bd};color:${meta.fg}">
    <div class="v-title">${meta.icon} ${meta.title}${fk === "product_defect" ? `：${failed} 处` : ""}</div>
    <div class="v-sub">${esc(defects.stop_reason || latest.error || "")}</div>
    ${kpis([["用例", total], ["通过", passed], ["失败", failed], ["覆盖率", cov], ["轮次", rounds]])}
    <div class="v-box">
      <div class="v-box-h">失败原因</div>
      <div>${esc(defects.stop_reason || "—")}</div>${basisLine}${table}${adviceHtml}${aiBlock}
    </div></div>`;
}

function renderAnalysisBlock(tid, analysis, nFailed) {
  if (!analysis) {
    if (!nFailed) return "";
    const can = ME && ME.role !== "caller";
    return `<div class="v-ai" id="v-ai-block">
      <div class="v-box-h"><span class="ai-chip">AI</span> 详细分析与修复建议</div>
      <div class="hint" style="margin:4px 0 8px">基于真实失败消息、用例预期与被测源码，逐条给根因推测与改法（标注置信度；失败也可能是用例预期错，分析会如实判别）。按需生成，约 3-6k token。</div>
      ${can ? `<button class="btn btn-primary btn-sm" onclick="runAnalysis(${tid}, this)">生成 AI 分析</button>`
            : '<span class="hint">（开发者/管理员可生成）</span>'}</div>`;
  }
  const conf = c => ({ "高": "badge-ok", "中": "badge-cancel", "低": "badge-queued" }[c] || "badge-queued");
  return `<div class="v-ai" id="v-ai-block">
    <div class="v-box-h"><span class="ai-chip">AI</span> 详细分析与修复建议 <span class="hint" style="margin:0">（已缓存 · ${analysis.evidence_count ?? ""} 条证据）</span></div>
    ${analysis.summary ? `<div class="v-ai-sum">${esc(analysis.summary)}</div>` : ""}
    ${(analysis.defects || []).map(d => `<div class="v-ai-item">
      <div class="d-head"><code>${esc(d.case)}</code> <span class="badge ${conf(d.confidence)}">置信度 ${esc(d.confidence || "—")}</span></div>
      <div class="v-ai-row"><b>实际</b>${esc(d.observed)}</div>
      <div class="v-ai-row"><b>预期</b>${esc(d.expected)}</div>
      <div class="v-ai-row"><b>根因推测</b>${esc(d.root_cause)}</div>
      <div class="v-ai-row fix"><b>修复建议</b>${esc(d.fix)}</div></div>`).join("")}
    <div class="hint" style="margin-top:6px">${esc(analysis.note || "")}</div></div>`;
}

async function runAnalysis(tid, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "AI 分析中（约 20-60 秒）…"; }
  try {
    const out = await api(`/tasks/${tid}/analysis`, { method: "POST", body: "{}" });
    if (out.error) { alert("分析不可用：" + out.error); if (btn) { btn.disabled = false; btn.textContent = "重试 AI 分析"; } return; }
    if (out.skipped) { alert(out.skipped); return; }
    await loadReport();
  } catch (e) { alert("分析失败：" + e.message); if (btn) { btn.disabled = false; btn.textContent = "重试 AI 分析"; } }
}

async function loadReport() {
  const tid = $("rp-task").value;
  if (!tid) return;
  const [reports, logs, rtm, charts, levels, tdetail, an] = await Promise.all([
    api(`/tasks/${tid}/report`).catch(() => []),
    api(`/tasks/${tid}/logs`).catch(() => []),
    api(`/tasks/${tid}/rtm`).catch(() => null),
    api(`/tasks/${tid}/charts`).catch(() => []),
    api(`/tasks/${tid}/levels`).catch(() => null),
    api(`/tasks/${tid}`).catch(() => null),
    api(`/tasks/${tid}/analysis`).catch(() => null),
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
  // 成功：判决绿卡 + 全部内容展开（用户："成功就把现在所有的东西都展示出来"）；
  // 失败：判决卡+失败框是主角，执行信息/四级卡/叙述/图表折叠在 details 里可展开
  let fkTop = null;
  try { fkTop = latest ? JSON.parse(latest.defects || "{}").failure_kind : null; } catch { fkTop = null; }
  const openAll = !!latest && (!fkTop || fkTop === "none");   // report 节点 pass 时写的是字符串 "none"
  $("rp-overview").innerHTML = renderVerdict(tdetail, latest, an) +
    `<details class="v-details" ${openAll ? "open" : ""}>
      <summary>执行信息 · 四级卡 · 报告叙述 · 图表（点击展开/收起）</summary>` +
    execCard(latest) + renderLevels(levels) + summaryHtml + chartsHtml + `</details>`;

  // RTM 矩阵（测试条件 ↔ 用例映射）——用例六要素移至「测试用例库」tab（D9 分离关注点）
  if (rtm && (rtm.matrix.length || rtm.cases.length)) {
    const covered = rtm.matrix.filter(m => m.status === "covered").length;
    const noMap = !rtm.map_source;   // 任务B：无映射快照=评估没跑，标「未映射（未评估）」中性态，
                                     // 不给红色「缺口」（缺口=评估跑过发现漏覆盖，两回事）
    $("rp-rtm-badge").textContent = `条件覆盖 ${covered}/${rtm.conditions_total}` +
      (noMap ? "（条件已产出但评估未运行，故无映射快照——条件本身是健康的，跑完评估即有映射）"
             : `（映射方式：${rtm.map_source === "llm" ? "LLM 辅助" : "启发式"}）`);
    $("rp-rtm-table").innerHTML =
      "<tr><th>测试条件</th><th>优先级</th><th>关联用例</th><th>状态</th></tr>" +
      rtm.matrix.map(m => `<tr><td>${esc(m.condition)}</td><td>${esc(m.priority || "中")}</td>` +
        `<td>${m.cases.length ? m.cases.map(esc).join("<br>") : '<span class="hint">无用例覆盖</span>'}</td>` +
        (noMap
          ? '<td><span class="badge badge-queued">未映射（未评估）</span></td>'
          : `<td><span class="badge ${m.status === "covered" ? "badge-ok" : "badge-fail"}">${m.status === "covered" ? "已覆盖" : "缺口"}</span></td>`) +
        `</tr>`).join("");
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

// ---------- 个人中心（任务4：使用记录 = 我的测试任务 + 登录记录） ----------
const EVT_CN = { login: "登录", logout: "退出", register: "注册", failed: "登录失败" };
async function loadProfile() {
  const usage = await api("/me/usage").catch(() => null);
  $("pf-me").innerHTML = `<b>${esc(ME.username)}</b>（${ROLE_CN[ME.role] || ME.role}）· 注册于 ${esc((usage && usage.registered_at) || "—")}` +
    ` <button class="btn btn-ghost btn-sm" style="margin-left:10px" onclick="doLogout()">退出登录</button>`;
  const rows = ((usage && usage.tasks) || []).map(x =>
    `<tr><td class="hint">#${x.id}</td><td>${esc(x.project_name)}</td>` +
    `<td>${esc((x.requirement_text || "").slice(0, 40))}</td>` +
    `<td class="hint">${esc(x.created_at || "")}</td><td class="hint">${esc(x.first_run_at || "未开始")}</td>` +
    `<td><span class="badge ${x.status === "done" ? "badge-ok" : x.status === "failed" ? "badge-fail" : "badge-run"}">${esc(STATUS_CN[x.status] || x.status)}</span></td></tr>`).join("");
  $("pf-tasks").innerHTML = "<tr><th>#</th><th>项目</th><th>需求</th><th>建立时间</th><th>开始执行</th><th>状态</th></tr>" +
    (rows || '<tr><td colspan="6" class="hint">还没有测试任务</td></tr>');
  $("pf-events").innerHTML = "<tr><th>事件</th><th>时间</th></tr>" +
    (((usage && usage.events) || []).map(e =>
      `<tr><td>${EVT_CN[e.event] || e.event}</td><td class="hint">${esc(e.created_at)}</td></tr>`).join("")
    || '<tr><td colspan="2" class="hint">暂无记录</td></tr>');
}
async function doLogout() {
  try { await api("/logout", { method: "POST", body: "{}" }); } catch (_) {}
  localStorage.removeItem("token");
  ME = null;
  showLogin();
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
    return '<span class="badge" style="background:var(--run-weak,#e7f6fa);color:var(--run,#0e7490)">● 运行中</span>';
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
    let link = "";
    if (l.level === "L1") link = `<a href="#/reports" onclick="viewReport(${l.task_id || 0})" style="color:var(--info);font-size:13.5px;font-weight:700">查看报告 →</a>`;
    if (l.level === "L3") link = `<a href="javascript:confirmScenarioPrompt(${l.task_id || 0})" style="color:var(--ok2);font-size:13.5px;font-weight:700">确认场景 →</a>`;
    if (l.level === "L4") link = `<a href="javascript:convertL4Prompt(${l.task_id || 0})" style="color:var(--info);font-size:13.5px;font-weight:700">审阅清单 →</a>`;
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

// R2/F10 缺陷归因清单（摘要态：任务级 defects 三分类+日志 FAILED 行；
// per-case 精确归因=需后端新增 /defects API（F18），前端不编造）
function renderDefectsPanel(latest, defectsApi) {
  if (!latest) return '<div class="hint">无报告</div>';
  const defects = (defectsApi && defectsApi.task_level) || {};
  const fk = defects.failure_kind || "none";
  const cls = { product_defect: "background:#fee2e2;color:#dc2626",
                case_defect: "background:#fef3c7;color:#b45309",
                env: "background:#f3f4f6;color:#6b7280",
                static: "background:var(--run-weak,#e7f6fa);color:var(--run,#0e7490)" }[fk] || "background:#f3f4f6;color:#6b7280";
  const failedLines = ((defectsApi && defectsApi.failed_cases) || [])
    .map(fc => `[${fc.origin === "generated" ? "生成" : "自带"}] ${esc(fc.nodeid.slice(0, 90))}`);  return `<div style="margin-bottom:10px"><span class="badge" style="${cls}">` +
    `${{ product_defect: "疑似产品缺陷", case_defect: "用例自身缺陷", env: "环境问题",
         static: "静态模式（未执行）", none: "无缺陷归因" }[fk]}</span>` +
    ` <span class="hint">${esc(defects.stop_reason || "")}</span></div>` +
    (failedLines.length
      ? `<div style="font-size:12px;color:var(--muted);max-height:180px;overflow:auto">` +
        failedLines.map(x => `<div class="log-line">${x}</div>`).join("") + `</div>`
      : `<div class="hint">日志中无 FAILED 行（全部通过或尚未执行）</div>`) +
    `<div class="hint" style="margin-top:8px">归因三分之一：env / case / product——不确定时宁可误报不可造假；` +
    `per-case 精确归因需后端新增 /defects 接口</div>`;
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
  const name = t.project_name || (t.repo_url || "").split("/").pop() || `任务 ${tid}`;
  const lang = t.primary_language ? ` ${t.primary_language}` : "";
  let dur = "";
  if (ex && ex.started_at && latest.created_at) {
    const ms = new Date(latest.created_at.replace(" ", "T")) - new Date(ex.started_at.replace(" ", "T"));
    if (ms > 0 && !isNaN(ms)) dur = `｜已运行 ${ms >= 60000 ? Math.floor(ms/60000) + "m" : ""}${Math.round(ms%60000/1000)}s`;
  }
  $("td-hero").innerHTML =
    `<div style="font-size:26px;font-weight:800">${esc(name)}</div>` +
    `<span class="badge badge-queued">任务 #${tid}</span>` +
    `<div class="hint" style="margin-top:6px">${esc(lang)}｜状态：${esc(STATUS_CN[t.status] || t.status)}${dur}` +
    (ex ? `｜执行方式：${esc(ex.mode)}｜执行者：系统自动（${esc(ex.sandbox)}+LLM ${esc(ex.llm)}）` : "") + `</div>` +
    // 任务A4：失败/准备中不再只给一个状态词——error + 阶段 + 下一步（用户点进来就是要知道发生了什么）
    (t.status === "failed"
      ? `<div style="margin-top:10px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 14px">
          <b style="color:#b91c1c">任务失败</b>${t.error ? `——<code style="color:#b91c1c">${esc(String(t.error).slice(0, 400))}</code>` : "（未记录错误信息，看下方节点日志定位阶段）"}
          <div class="hint" style="margin:6px 0 0">下一步：对照下方「执行流水线」找到最后一个打勾的节点（= 死在那一步）；
          ${latest ? "已产出报告的可在" : "该任务未执行到评估阶段，因此<b>没有报告与 RTM</b>（不是项目不兼容）；修复后"} 去报告页看判决${latest ? "" : "或重提任务"}。</div>
        </div>`
      : ["provisioning", "running", "reporting"].includes(t.status)
        ? `<div class="hint" style="margin-top:8px;color:#b45309">执行中——下方流水线实时更新（本页打开期间自动轮询）。</div>`
        : "");
  const req = (latest && latest.narrative) ? "" : "";
  const trow = await api(`/tasks/${tid}`).catch(() => null);
  $("td-req").classList.remove("hidden");
  $("td-req").innerHTML = `<div style="border-left:4px solid var(--ok2);padding-left:14px">` +
    `<b style="color:var(--ok2)">需求</b>　${esc((trow && trow.requirement_text) || "—")}</div>`;
  const lv = await api(`/tasks/${tid}/levels`).catch(() => null);
  const logs = await api(`/tasks/${tid}/logs`).catch(() => []);
  $("td-pipeline").innerHTML = "<div style=\"font-size:16px;font-weight:800;margin-bottom:10px\">" +
    "执行流水线（固定图编排 · 容器沙箱）</div>" + renderPipeline(logs, lv);
  const defectsApi = await api(`/tasks/${tid}/defects`).catch(() => null);
  $("td-defects").innerHTML = "<div style=\"font-size:16px;font-weight:800\">缺陷归因清单</div>" +
    "<div class=\"hint\" style=\"margin-bottom:6px\">env / case / product 三分类 · 不确定时宁可误报不可造假</div>" +
    renderDefectsPanel(latest, defectsApi);
  const trace = await api(`/tasks/${tid}/trace`).catch(() => null);
  $("td-trace").innerHTML = "<div style=\"font-size:16px;font-weight:800\">跨级 RTM 溯源链</div>" +
    `<div class="hint">失败自动沿 [验收场景 → 系统场景 → 集成用例 → 单元用例 → 代码行] 反向定位｜共 ${(trace && trace.chains || []).length} 条</div>` +
    renderTracePanel(trace);
  $("td-levels").innerHTML = "<div style=\"font-size:16px;font-weight:800;margin-bottom:10px\">" +
    "测试级别计划（黑盒 + 白盒双轨）</div>" +
    (renderLevelCards(lv && lv.levels) || '<div class="hint">该任务尚无级别台账（未走 levels 流）</div>');
  await renderExecPanel(tid);
  // 任务A4：活跃态自动轮询（3s，终态/离开页面即停——上面提示语写了"自动轮询"，说到做到）
  clearTimeout(TD_POLL);
  if (["provisioning", "running", "reporting", "cloning", "analyzing", "generating"].includes(t.status)) {
    TD_POLL = setTimeout(() => { if (TASK_TID === tid) loadTaskDetail(tid); }, 3000);
  }
}

// 任务C：实时用例执行面板（详情页流水线区域下方）——逐用例状态/耗时/失败证据可展开。
// 粒度诚实标注：按轮次更新（每轮跑完出现该轮明细），不伪装逐用例实时流。
const EXEC_STATUS = {
  passed:  ["✓ 通过", "badge-ok"], failed: ["✗ 失败", "badge-fail"],
  error:   ["⚠ 错误", "badge-fail"], skipped: ["− 跳过", "badge-queued"],
};
async function renderExecPanel(tid) {
  const box = $("td-exec");
  const snap = await api(`/tasks/${tid}/execution`).catch(() => null);
  if (!snap) { box.innerHTML = '<div class="hint">实时用例执行面板加载失败</div>'; return; }
  const active = ["cloning", "analyzing", "generating", "provisioning", "running", "reporting"]
    .includes(snap.status);
  const roundsTxt = (snap.rounds || []).map(r =>
    `第${r.round_no}轮 通过${r.passed}/失败${r.failed}` +
    (r.coverage_line != null ? `·覆盖${Math.round(r.coverage_line * 100)}%` : "")).join("｜");
  let body;
  if ((snap.cases || []).length) {
    // 最新一轮的用例集（历史轮次在轮次轨迹行里）
    const maxRound = Math.max(...snap.cases.map(c => c.round_no));
    const cur = snap.cases.filter(c => c.round_no === maxRound);
    const pass = cur.filter(c => c.status === "passed").length;
    body = `<div class="hint" style="margin:2px 0 8px">当前展示第 ${maxRound} 轮（${cur.length} 条：` +
      `<span style="color:var(--ok2)">${pass} 通过</span> / ` +
      `<span style="color:var(--fail,#dc2626)">${cur.length - pass} 失败·错误·跳过</span>）` +
      `——失败行点击展开失败消息</div>` +
      `<div class="table-wrap"><table class="tbl"><tr><th>用例</th><th>状态</th><th>耗时</th><th>轮次</th><th>来源</th></tr>` +
      cur.map(c => {
        const [cn, cls] = EXEC_STATUS[c.status] || [c.status, "badge-queued"];
        const failRow = c.status !== "passed" && c.status !== "skipped";
        return `<tr${failRow ? ' style="background:#fef2f2"' : ""}>` +
          `<td><code>${esc(c.name)}</code>${failRow && c.message
            ? `<details style="margin-top:4px"><summary class="hint" style="cursor:pointer;margin:0">失败消息</summary>` +
              `<pre class="code-view" style="white-space:pre-wrap;font-size:11px;margin-top:4px">${esc(c.message)}</pre></details>`
            : ""}</td>` +
          `<td><span class="badge ${cls}">${cn}</span></td>` +
          `<td class="hint" style="margin:0">${c.duration_ms != null ? c.duration_ms + "ms" : "—"}</td>` +
          `<td>${c.round_no}</td>` +
          `<td class="hint" style="margin:0">${c.origin === "generated" ? "生成用例" : "自带测试"}</td></tr>`;
      }).join("") + "</table></div>";
  } else if (active) {
    body = `<div class="hint">尚未执行到用例阶段——跑到「开考」节点后这里逐用例出结果（按轮次更新）。</div>`;
  } else {
    body = `<div class="hint">该任务没有逐用例结果（未执行/静态模式/旧任务在本机制上线前运行）。</div>`;
  }
  box.innerHTML =
    `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">` +
    `<div style="font-size:16px;font-weight:800">实时用例执行</div>` +
    (active ? '<span class="badge badge-run">执行中 · 2s 轮询</span>' : "") +
    `<span class="badge badge-queued">粒度：按轮次更新</span></div>` +
    (roundsTxt ? `<div class="hint" style="margin:0 0 6px">轮次轨迹：${esc(roundsTxt)}</div>` : "") +
    `<div class="hint" style="margin:0 0 8px;color:var(--muted)">最近动作：${esc((snap.log_tail || []).slice(-2).join(" ⏐ ") || "—").slice(0, 200)}</div>` +
    body;
  // 活跃态 2s 轮询刷新本面板（终态停；与详情页 3s 整页轮询错开，只刷面板减闪烁）
  clearTimeout(EXEC_POLL);
  if (active && TASK_TID === tid) {
    EXEC_POLL = setTimeout(() => renderExecPanel(tid), 2000);
  }
}

// R4/F15：L3 确认/L4 转化动作（最小可用版：prompt 交互；正式弹层列 F 线后续）
async function confirmScenarioPrompt(tid) {
  const sc = await api(`/tasks/${tid}/scenarios`).catch(() => null);
  const rows = (sc && sc.scenarios) || [];
  const pend = rows.filter(x => x.status === "pending_confirm");
  if (!pend.length) { alert("无待人工确认场景"); return; }
  const idx = prompt("待确认场景（输入序号）：\n" +
    pend.map((x, i) => `${i + 1}. [${x.id}] ${x.title}`).join("\n"));
  const pick = pend[parseInt(idx, 10) - 1];
  if (!pick) return;
  const verdict = prompt("结论（confirmed_pass / confirmed_defect / confirmed_scenario）：", "confirmed_pass");
  if (!verdict) return;
  try {
    await api(`/scenarios/${pick.id}/confirm`, { method: "POST",
      body: JSON.stringify({ result: verdict, note: "" }) });
    alert("已确认：" + pick.title);
    loadTaskDetail(tid);
  } catch (e) { alert(e.message); }
}
async function convertL4Prompt(tid) {
  if (!confirm("将已确认的 L4 场景转化为回归 pytest 脚本？（确定性转化+AST 编译闸）")) return;
  try {
    await api(`/tasks/${tid}/l4/convert`, { method: "POST", body: "{}" });
    alert("转化完成（产物入库，重复转化不重复行）");
  } catch (e) { alert(e.message); }
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
$("rv-fb-send").onclick = sendFeedback;
$("rv-ac-submit").onclick = async () => {
  const tid = REVIEW_TID;
  if (!tid) { $("rv-ac-result").textContent = "请先从任务列表进入某条待审查任务"; return; }
  try {
    const b = await api(`/tasks/${tid}/cases`, { method: "POST", body: JSON.stringify({
      name: $("rv-ac-name").value.trim(), method: $("rv-ac-method").value.trim(), code: $("rv-ac-code").value }) });
    $("rv-ac-result").textContent = `已插入：${b.case_name} → ${b.file}`;
    loadReview(tid);
  } catch (e) { $("rv-ac-result").textContent = "插入失败：" + e.message; }
};
$("wb-confirm").onclick = null;   // 真实 handler 在 loadWorkbench 按任务绑定
document.querySelectorAll("#task-tabs .tab, #task-tabs .fchip").forEach(b => b.onclick = () => {
  TASK_FILTER = b.dataset.f;
  TASK_PAGE.offset = 0;          // 换筛选回第一页（服务端筛选，不再本地过滤）
  document.querySelectorAll("#task-tabs .tab, #task-tabs .fchip").forEach(x => {
    x.classList.toggle("on", x === b); x.classList.toggle("active", x === b);
  });
  pollTasks();
});

const pgSearch = document.getElementById("pg-search");
if (pgSearch) {
  let S_T = null;
  pgSearch.addEventListener("input", e => {     // 防抖 280ms：服务端搜索，避免每键一次请求
    clearTimeout(S_T);
    S_T = setTimeout(() => {
      PG_SEARCH = e.target.value.trim();
      TASK_PAGE.offset = 0;
      pollTasks();
    }, 280);
  });
}
document.querySelectorAll("[data-rp]").forEach(b => b.onclick = () => switchRp(b.dataset.rp));
window.addEventListener("hashchange", route);

(async function boot() {
  renderStepper("stepper-new", 0);
  renderStepper("stepper-wb", 2);
  if (!localStorage.getItem("token")) { showLogin(); return; }
  try { ME = await api("/me"); applyRole(); route(); loadEnv(); }
  catch (_) { /* 401 已弹出登录 */ }
})();
