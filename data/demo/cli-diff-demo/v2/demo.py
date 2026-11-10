"""CLI 报表演示（任务卡-D+ 差分真机标本）：输出含时间戳/路径/uuid/浮点/时长噪声。
v1: 阈值 >50；v2: 阈值 >=50（边界差一=被测的真实行为变更）。"""
import datetime
import os
import time
import uuid


def main() -> None:
    data = [int(x) for x in open("nums.txt", encoding="utf-8").read().split()]
    threshold = 50
    eligible = [x for x in data if x >= threshold]        # v2: x >= threshold
    print(f"report generated at {datetime.datetime.now().isoformat()}")
    print(f"data file: {os.path.abspath('nums.txt')}")
    print(f"run id: {uuid.uuid4().hex}")
    t0 = time.perf_counter()
    for x in sorted(set(data)):
        print(f"value {x}: ratio={x / 100.0:.3f} status={'in' if x >= threshold else 'out'}")
    print(f"eligible count = {len(eligible)}")
    print(f"elapsed {(time.perf_counter() - t0) * 1000:.1f}ms")


if __name__ == "__main__":
    main()
