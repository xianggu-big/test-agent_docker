"""会议室预约与计价核心纯函数（corpus-003 被测对象；缺陷按 ground_truth 种植）。"""
MAX_HOURS = 4
MIN_HOURS = 1


def validate_duration(start_hour: int, end_hour: int) -> int:
    """预约时长必须在 [1, 4] 小时闭区间内，返回时长；越界抛 ValueError。"""
    hours = end_hour - start_hour
    if hours < MIN_HOURS or hours >= MAX_HOURS:   # D1：边界差一，等于 4 小时被误拒
        raise ValueError("duration must be between 1 and 4 hours")
    return hours


def quote_total(hourly_rate: int, hours: int) -> int:
    """总价=小时单价×时长；时长 ≥3 小时打 9 折（10% off），四舍五入到整数分。"""
    if hours >= 3:
        return int(hourly_rate * hours * 0.85)     # D2：折扣率写错（应为 0.9）
    return hourly_rate * hours


def has_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """同一房间同一天的两个时段是否重叠；相邻（前场 end == 后场 start）不算重叠。"""
    return start_a <= end_b and end_a >= start_b   # D4：相邻时段被误判重叠
