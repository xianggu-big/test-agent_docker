// 前端 4 定时器守卫结构断言（2026-11-16 存疑 32 验收；第七轮修复的回归锁）
// 用法：node scripts/verify_frontend_guards.js（退出码 0=全过）
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'test_agent', 'api', 'static', 'app.js'), 'utf8');
const checks = [
  ['⑨ loadReview 续挂前校验 REVIEW_TID+评审页 hash', /REVIEW_POLL = setTimeout\(\(\) => \{\s*\n\s*if \(REVIEW_TID === tid && location\.hash\.startsWith\("#\/review\/"\)\) loadReview\(tid\);/],
  ['⑩ watchStage1 复活前 hash 校验', /CONFIRM_TIMER = setTimeout\(\(\) => \{\s*\n\s*if \(location\.hash\.includes\("workbench"\)/],
  ['⑪ pollTasks 只在任务页建 interval', /if \(!pollTimer && location\.hash\.startsWith\("#\/tasks"\)\)/],
  ['⑪b interval 回调离页自清', /else \{ clearInterval\(pollTimer\); pollTimer = null; \}/],
  ['⑫ doLogout 清全部定时器', /for \(const t of \[typeof REVIEW_POLL/],
  ['⑫b FINAL_WATCH 链清理', /for \(const k in FINAL_WATCH\)/],
];
let fail = 0;
for (const [label, re] of checks) {
  const ok = re.test(src);
  if (!ok) fail++;
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${label}`);
}
process.exit(fail ? 1 : 0);
