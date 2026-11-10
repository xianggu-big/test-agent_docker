"""会议室与预约的内存存储。"""
ROOMS: dict[int, dict] = {"1": {"id": 1, "name": "A101", "capacity": 6, "hourly_rate": 50}}
BOOKINGS: dict[int, dict] = {}
NEXT_IDS = {"room": 2, "booking": 1}


def save_booking(booking: dict) -> dict:
    """写入 BOOKINGS 并按 id 可查。"""
    BOOKINGS[booking["id"]] = booking
    return booking
