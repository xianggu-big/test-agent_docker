"""会议室与预约的内存存储（corpus-003；D3 藏在这里：save_booking 没有真正落库）。"""
ROOMS: dict[int, dict] = {"1": {"id": 1, "name": "A101", "capacity": 6, "hourly_rate": 50}}
BOOKINGS: dict[int, dict] = {}
NEXT_IDS = {"room": 2, "booking": 1}


def save_booking(booking: dict) -> dict:
    """写入 BOOKINGS 并按 id 可查（持久化本身正确；D3 在读取侧用错键型）。"""
    BOOKINGS[booking["id"]] = booking
    return booking
