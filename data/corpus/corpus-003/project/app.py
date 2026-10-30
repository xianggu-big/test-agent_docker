"""会议室预约 API（corpus-003 被测服务；FastAPI 自带 OpenAPI 契约）。

需求见 REQUIREMENT.md。已知（对答题人隐藏）：本服务按 ground_truth.json 种了 5 个真实缺陷。
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import store
from booking import has_overlap, quote_total, validate_duration

app = FastAPI(title="meeting-room-api")


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    capacity: int = Field(ge=1, le=20)
    hourly_rate: int = Field(ge=1)


class BookingIn(BaseModel):
    room_id: int
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_hour: int = Field(ge=8, le=20)
    end_hour: int = Field(ge=9, le=21)


@app.post("/rooms", status_code=201)
def create_room(r: RoomIn):
    rid = store.NEXT_IDS["room"]
    store.NEXT_IDS["room"] += 1
    store.ROOMS[str(rid)] = {"id": rid, **r.model_dump()}
    return store.ROOMS[str(rid)]


@app.get("/rooms/{rid}")
def read_room(rid: int):
    room = store.ROOMS.get(str(rid))
    if not room:
        raise HTTPException(404, "room not found")
    return room


@app.post("/bookings", status_code=201, responses={400: {"description": "invalid duration"},
                                                   404: {"description": "room not found"},
                                                   409: {"description": "time overlap"}})
def create_booking(b: BookingIn):
    room = store.ROOMS.get(str(b.room_id))
    if not room:
        raise HTTPException(404, "room not found")
    try:
        hours = validate_duration(b.start_hour, b.end_hour)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for exist in store.BOOKINGS.values():
        if (exist["room_id"] == b.room_id and exist["date"] == b.date
                and exist["status"] == "active"
                and has_overlap(b.start_hour, b.end_hour, exist["start_hour"],
                                exist["end_hour"])):
            raise HTTPException(409, "time overlap")
    bid = store.NEXT_IDS["booking"]
    store.NEXT_IDS["booking"] += 1
    booking = {"id": bid, "room_id": b.room_id, "date": b.date,
               "start_hour": b.start_hour, "end_hour": b.end_hour,
               "total": quote_total(room["hourly_rate"], hours),
               "status": "active"}
    return store.save_booking(booking)


@app.get("/bookings/{bid}")
def read_booking(bid: int):
    booking = store.BOOKINGS.get(str(bid))    # D3：存储用 int 键，读取误用 str 键 → 恒 404
    if not booking:
        raise HTTPException(404, "booking not found")
    return booking


@app.post("/bookings/{bid}/cancel", responses={404: {"description": "booking not found"},
                                                409: {"description": "already cancelled"}})
def cancel_booking(bid: int):
    booking = store.BOOKINGS.get(bid)
    if not booking:
        raise HTTPException(404, "booking not found")
    if booking["status"] == "cancelled":
        return booking              # D5：重复取消应 409，这里幂等返回 200
    booking["status"] = "cancelled"
    return booking


@app.get("/rooms/{rid}/bookings")
def list_bookings(rid: int, date: str):
    if str(rid) not in store.ROOMS:
        raise HTTPException(404, "room not found")
    items = [b for b in store.BOOKINGS.values()
             if b["room_id"] == rid and b["date"] == date and b["status"] == "active"]
    return {"items": items}
