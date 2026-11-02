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
        """入队（幂等：同任务同 kind 已有 queued/running 行则复用，不重复排）。"""
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
        self._thread = threading.Thread(target=self._loop, name="exec-queue", daemon=True)
        self._thread.start()
        log.info("执行队列 worker 已启动（单线程串行）")

    def stop(self) -> None:
        self._stop.set()

    def _recover(self) -> None:
        """启动恢复：running 行（进程已死）标 failed 并写清原因；queued 保留继续消费。"""
        conn = get_conn()
        try:
            n = conn.execute(
                "UPDATE exec_queue SET status='failed', finished_at=datetime('now'),"
                " error='服务重启：该次执行已中断（重新下发即可）' WHERE status='running'").rowcount
            conn.commit()
            if n:
                log.warning("队列恢复：%s 条 running 行标记中断（诚实留痕，不冒充成功）", n)
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
            conn.execute("UPDATE exec_queue SET status='running', started_at=datetime('now')"
                         " WHERE id=?", (row["id"],))
            conn.commit()
        finally:
            conn.close()
        qid, tid, kind = row["id"], row["task_id"], row["kind"]
        self._running_task = tid
        ok, err = True, None
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
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE exec_queue SET status=?, finished_at=datetime('now'), error=? WHERE id=?",
                ("done" if ok else "failed", err, qid))
            conn.commit()
        finally:
            conn.close()
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
