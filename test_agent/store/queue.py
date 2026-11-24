"""执行队列（B1 / 2026-11-02）：单 worker 串行消费，替代"每个下发开一个线程同时跑"。

问题（评估实证）：并发下发多任务时容器互相挤 CPU/内存、SQLite 写竞争（database is locked
历史实锤）；container_jobs 只是台账，不承担排队。

设计：
- 真源=exec_queue 表（第 25 张）。入队=INSERT，消费=pick→running→跑→done/failed。
- 单 worker 线程（daemon）串行：拿最早的 queued 行，跑完才取下一个——并发下发变排队。
- 重启恢复：进程启动时把遗留 status='running' 的行标记 failed（该次执行已随进程消失，
  诚实留痕），queued 行继续消费；调用方重试即可重新入队。
- 老僵尸 queued 任务（本表上线前卡在 queued 的）**不会被自动拉起**——本表是显式入队，
  不扫 test_tasks.status，避免把实验遗留任务批量跑起来（烧 token）。
"""
import logging
import threading
import time
from datetime import datetime

from .. import config
from ..store.db import get_conn

log = logging.getLogger(__name__)

_worker: "QueueWorker | None" = None
_lock = threading.Lock()

_TASK_TERMINAL = ("done", "failed", "cancelled")


def _fail_task_if_pending(task_id: int, err: str | None) -> None:
    """队列失败→任务终态回写（2026-11-15 放行疑云实锤，状态机四断点之二）。

    旧行为：worker 失败只写 exec_queue 行，任务停在放行时原态（ready_for_review/
    queued）——1144/1145 队列已 failed 而任务"仍待审查"，用户以为没点上。
    仅当任务尚未到终态时回写：图自己的 _fail_task 已写的精确错误不覆盖（rowcount=0），
    cancelled 是用户意图不覆盖。锁前异常（SQLite busy/工作区守卫前炸）与图内失败
    兜底两种形态都落到 failed，重新下发即可重试。"""
    conn = get_conn()
    try:
        n = conn.execute(
            "UPDATE test_tasks SET status='failed', error=?, updated_at=datetime('now')"
            " WHERE id=? AND status NOT IN ('done','failed','cancelled')",
            (f"队列执行失败：{err or '原因未知（详见队列行与任务日志）'}（重新下发即可重试）"[:500],
             task_id)).rowcount
        # 容器台账收口（2026-11-16 倒推实锤：异常路径不收口=台账永久滞留 running）
        if n:
            conn.execute(
                "UPDATE container_jobs SET status='failed', finished_at=datetime('now')"
                " WHERE task_id=? AND status='running'", (task_id,))
        conn.commit()
        if n:
            log.warning("队列失败回写：task=%s → failed（原状态非终态，queue 行已留痕）", task_id)
    finally:
        conn.close()


class QueueWorker:
    """单线程串行消费 exec_queue。start() 幂等。"""

    def __init__(self, poll_s: float = 1.5):
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_task: int | None = None

    # —— 入队 ——
    @staticmethod
    def enqueue(task_id: int, kind: str) -> int:
        """入队（幂等：同任务同 kind 已有 queued/running 行则复用，不重复排）。
        2026-11-08 锁循环实锤：INSERT busy 15s 超时抛异常时连接悬挂在开着的写事务
        （in_transaction）→ 全进程写锁死（conn-probe 点名 queue.py:41）——异常路径
        必须 rollback 再 close（悬挂连接的锁随 rollback 释放）。"""
        import sqlite3
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM exec_queue WHERE task_id=? AND kind=? AND status IN ('queued','running')",
                (task_id, kind)).fetchone()
            if row:
                return row["id"]
            qid = conn.execute(
                "INSERT INTO exec_queue(task_id, kind) VALUES (?,?)", (task_id, kind)).lastrowid
            conn.commit()
            log.info("入队 task=%s kind=%s (queue id=%s)", task_id, kind, qid)
            return qid
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def pending_count() -> int:
        conn = get_conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM exec_queue WHERE status IN ('queued','running')"
            ).fetchone()["n"]
        finally:
            conn.close()

    @staticmethod
    def queue_snapshot(limit: int = 20) -> list[dict]:
        conn = get_conn()
        try:
            return [dict(r) for r in conn.execute(
                """SELECT q.id, q.task_id, q.kind, q.status, q.enqueued_at, q.started_at,
                          q.finished_at, q.error, t.status AS task_status
                   FROM exec_queue q LEFT JOIN test_tasks t ON t.id = q.task_id
                   ORDER BY q.id DESC LIMIT ?""", (limit,))]
        finally:
            conn.close()

    # —— 生命周期 ——
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._recover()
        # 运行账本自愈对账（§6.3）：worker 每次启动把残留 running 的 run 收口——
        # 图异常/进程被杀/漏接退出路径都不会留下永久 running。旗标关=零开销。
        try:
            from ..skills.run_ledger import guarded, reconcile_runs
            from .db import get_conn as _gc
            _c = _gc()
            try:
                n = guarded(reconcile_runs, _c)
                if n:
                    log.info("[agent-run] reconcile 收口 %d 条残留 running", n)
            finally:
                _c.close()
        except Exception as e:      # noqa: BLE001 —— 对账失败不拦 worker 启动
            log.warning("[agent-run] reconcile 失败: %s", e)
        self._thread = threading.Thread(target=self._loop, name="exec-queue", daemon=True)
        self._thread.start()
        log.info("执行队列 worker 已启动（单线程串行）")

    def stop(self) -> None:
        self._stop.set()

    def _recover(self) -> None:
        """启动恢复：running 行（进程已死）标 failed 并写清原因；queued 保留继续消费。
        2026-11-15 第八轮（R7）：被中断的 running 行若任务停在 queued（已放行排队中）
        ——queued 不在 ACTIVE 启动清扫集，无人收尸会永久滞留——连带标 failed 留痕。"""
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT task_id FROM exec_queue WHERE status='running'").fetchall()
            n = conn.execute(
                "UPDATE exec_queue SET status='failed', finished_at=datetime('now'),"
                " error='服务重启：该次执行已中断（重新下发即可）' WHERE status='running'").rowcount
            for r in rows:
                # queued 不在 ACTIVE（_sweep_orphan_active_tasks 不覆盖）——必须这里收尸
                conn.execute(
                    "UPDATE test_tasks SET status='failed', error=?, updated_at=datetime('now')"
                    " WHERE id=? AND status='queued'",
                    ("服务重启：排队任务中断（重新下发即可）", r["task_id"]))
            conn.commit()
            if n:
                log.warning("队列恢复：%s 条 running 行标记中断（诚实留痕，不冒充成功）"
                            "；queued 任务已连带收尸", n)
        finally:
            conn.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did = self._step()
            except Exception as e:          # noqa: BLE001 —— worker 永不因单次异常退出
                log.error("队列 worker 异常: %s: %s", type(e).__name__, e)
                did = False
            if not did:
                self._stop.wait(self.poll_s)

    @staticmethod
    def _hybrid_review_after_prep(tid: int) -> None:
        """阶段一跑完停在 ready_for_review 且 ai_reviewer=hybrid → 代审一轮（与旧
        _prep_worker 行为一致；代审失败=无决策落库，人工照常审查，不炸队列）。"""
        conn = get_conn()
        try:
            row = conn.execute("SELECT ai_reviewer, status FROM test_tasks WHERE id=?",
                               (tid,)).fetchone()
        finally:
            conn.close()
        if not (row and row["status"] == "ready_for_review" and row["ai_reviewer"] == "hybrid"):
            return
        try:
            from ..skills.ai_reviewer import reviewer_llm, run_review_once
            conn = get_conn()
            try:
                summary = run_review_once(reviewer_llm(), conn, tid, round_no=1)
            finally:
                conn.close()
            if summary:
                log.info("hybrid 代审完成 task=%s: %s", tid,
                         {k: summary[k] for k in ("approved", "deleted", "regenerate", "inserted")})
        except Exception as e:      # noqa: BLE001
            log.warning("hybrid 代审失败 task=%s: %s: %s", tid, type(e).__name__, e)

    def _step(self) -> bool:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id, task_id, kind FROM exec_queue WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return False
            # A4 取消竞态收敛（2026-11-20 设计方案，Qoder 体检论断 5）：认领与取消
            # 在同一套事务规则下判定谁先成功——三道闸。
            # ①队列行条件更新：stop 先取消（行已 cancelled）则认领失败，不执行。
            # 修复前无条件 UPDATE 会把 stop 刚取消的行改回 running。
            cur = conn.execute("UPDATE exec_queue SET status='running', started_at=datetime('now')"
                               " WHERE id=? AND status='queued'", (row["id"],))
            if cur.rowcount == 0:
                conn.commit()
                return True   # 该行已被取消，下一轮取后续行
            # ②任务状态守卫：任务已 cancelled 则队列行如实回写 cancelled 跳过执行
            # （修复前窗口：队列行 running 而任务 queued，stop 清不到队列行，worker 照跑）
            ts = conn.execute("SELECT status FROM test_tasks WHERE id=?",
                              (row["task_id"],)).fetchone()
            if ts and ts["status"] == "cancelled":
                conn.execute("UPDATE exec_queue SET status='cancelled', finished_at=datetime('now'),"
                             " error='任务已取消（认领守卫拦截）' WHERE id=?", (row["id"],))
                conn.commit()
                return True
            conn.commit()
        finally:
            conn.close()
        qid, tid, kind = row["id"], row["task_id"], row["kind"]
        self._running_task = tid
        ok, err = True, None
        from ..skills.run_ledger import set_current_queue_id   # T3：队列行 id 经 ContextVar
        set_current_queue_id(qid)
        try:
            if kind == "prep":
                from ..graph.builder import run_task_prep, post_prep_review_hook
                run_task_prep(tid)
                # 停等点钩子（off/hybrid/auto 分流）：B1 队列化时曾丢失，晚间实验实锤补回
                post_prep_review_hook(tid)
                self._hybrid_review_after_prep(tid)
            elif kind == "exec":
                # 分流（晚间实验实锤 287/288：单阶段任务也进 exec 队列被 run_task_exec 拒，
                # 卡 queued）：两阶段任务走执行图，单阶段走完整主图
                from ..store.db import get_conn as _get_conn   # 别名：不遮蔽本函数外层 get_conn（UnboundLocalError 实锤）
                _conn = _get_conn()
                _ts = _conn.execute("SELECT two_stage FROM test_tasks WHERE id=?", (tid,)).fetchone()
                _conn.close()
                if _ts and _ts["two_stage"] == 1:
                    from ..graph.builder import run_task_exec
                    run_task_exec(tid)
                else:
                    from ..graph.builder import run_task
                    run_task(tid)
            else:
                from ..graph.builder import run_task_auto
                run_task_auto(tid)
        except Exception as e:              # noqa: BLE001 —— 失败落队列行，不炸 worker
            ok, err = False, f"{type(e).__name__}: {e}"[:500]
            log.error("队列任务失败 task=%s kind=%s: %s", tid, kind, err)
        finally:
            self._running_task = None
            set_current_queue_id(None)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE exec_queue SET status=?, finished_at=datetime('now'), error=? WHERE id=?",
                ("done" if ok else "failed", err, qid))
            conn.commit()
        finally:
            conn.close()
        if not ok:
            _fail_task_if_pending(tid, err)   # 状态机四断点：失败必须落到任务终态
        return True


def get_worker() -> QueueWorker:
    """进程级单例。"""
    global _worker
    with _lock:
        if _worker is None:
            _worker = QueueWorker()
        return _worker


def start_worker() -> QueueWorker:
    w = get_worker()
    w.start()
    return w
