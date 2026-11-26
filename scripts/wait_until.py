# -*- coding: utf-8 -*-
"""wait_until.py——后台等待进程（任务四 AL-7，2026-11-26 测试任务报告 §3）。

用法：python scripts/wait_until.py --hhmm 18:00
内部=算出距下一个 HH:MM 的秒数 N 然后 time.sleep(N)，结束退出码 0。
设计约束：不用轮询空转、不用前台 sleep（>25s 会被拦）——由调用方以后台方式运行，
结束时会收到通知。"""
import argparse
import datetime as dt
import sys
import time


def seconds_until(hhmm: str, now: dt.datetime | None = None) -> int:
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = now or dt.datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return int((target - now).total_seconds())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hhmm", required=True, help="目标时刻（本机时钟，HH:MM）")
    ap.add_argument("--label", default="", help="台账标注（打印用）")
    args = ap.parse_args()
    n = seconds_until(args.hhmm)
    print(f"[wait_until] {args.label or args.hhmm} 还有 {n}s（{n / 3600:.2f}h）——开始等待",
          flush=True)
    time.sleep(n)
    print(f"[wait_until] 到点（本机 {dt.datetime.now():%H:%M:%S}）——退出码 0", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
