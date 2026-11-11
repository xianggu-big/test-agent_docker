"""AI 生成用例（第 1 轮 · 五种方法知识库）"""
import pytest
from fastapi.testclient import TestClient
import app
import store
from booking import validate_duration, quote_total, has_overlap

client = TestClient(app.app)


def _reset():
    store.ROOMS.clear()
    store.ROOMS["1"] = {"id": 1, "name": "A101", "capacity": 6, "hourly_rate": 50}
    store.BOOKINGS.clear()
    store.NEXT_IDS["room"] = 2
    store.NEXT_IDS["booking"] = 1


def _make_room(name="R", capacity=5, hourly_rate=50):
    r = client.post("/rooms", json={"name": name, "capacity": capacity, "hourly_rate": hourly_rate})
    return r.json()["id"]


def _make_booking(room_id, date="2024-01-01", start_hour=9, end_hour=10):
    return client.post("/bookings", json={"room_id": room_id, "date": date, "start_hour": start_hour, "end_hour": end_hour})

def test_create_room_成功响应体_语句分支覆盖():
    _reset()
    r = client.post('/rooms', json={'name': 'B202', 'capacity': 8, 'hourly_rate': 60})
    assert r.status_code == 201
    body = r.json()
    assert set(['id', 'name', 'capacity', 'hourly_rate']).issubset(body.keys())
    assert body['name'] == 'B202'
    assert body['capacity'] == 8
    assert body['hourly_rate'] == 60

def test_read_room_存在_语句分支覆盖():
    _reset()
    rid = _make_room(name='C303', capacity=10, hourly_rate=70)
    r = client.get(f'/rooms/{rid}')
    assert r.status_code == 200
    body = r.json()
    assert body['id'] == rid
    assert body['name'] == 'C303'
    assert body['capacity'] == 10
    assert body['hourly_rate'] == 70

def test_read_room_不存在_等价类划分():
    _reset()
    r = client.get('/rooms/9999')
    assert r.status_code == 404

def test_create_booking_room不存在_等价类划分():
    _reset()
    r = client.post('/bookings', json={'room_id': 9999, 'date': '2024-01-01', 'start_hour': 9, 'end_hour': 10})
    assert r.status_code == 404

import pytest
from booking import validate_duration


@pytest.mark.parametrize("value,expected", [
    (1, 1),
    (2, 2),
    (3, 3),
    (4, 4),
], ids=['min', 'min+1', 'max-1', 'max'])
def test_validate_duration_边界值分析(value, expected):
    assert validate_duration(9, 9 + value) == expected

import pytest
from booking import validate_duration


@pytest.mark.parametrize("value,error", [
    (0, ValueError),
    (5, ValueError),
], ids=['min-1', 'max+1'])
def test_validate_duration_越界_边界值分析(value, error):
    with pytest.raises(error):
        validate_duration(9, 9 + value)

def test_quote_total_折扣_判定表():
    assert quote_total(50, 1) == 50
    assert quote_total(50, 2) == 100
    assert quote_total(50, 3) == 135
    assert quote_total(50, 4) == 180

def test_has_overlap_判定表():
    assert has_overlap(9, 11, 10, 12) is True
    assert has_overlap(9, 12, 10, 11) is True
    assert has_overlap(9, 10, 10, 11) is False
    assert has_overlap(9, 10, 11, 12) is False

def test_create_booking_成功与持久化_语句分支覆盖():
    _reset()
    rid = _make_room(hourly_rate=50)
    r = _make_booking(rid, start_hour=9, end_hour=11)
    assert r.status_code == 201
    body = r.json()
    assert body['status'] == 'active'
    assert body['total'] == 100
    g = client.get(f"/bookings/{body['id']}")
    assert g.status_code == 200
    assert g.json()['id'] == body['id']

def test_create_booking_时长越界_等价类划分():
    _reset()
    rid = _make_room()
    r = _make_booking(rid, start_hour=9, end_hour=14)
    assert r.status_code == 400

def test_create_booking_重叠_判定表():
    _reset()
    rid = _make_room()
    assert _make_booking(rid, start_hour=9, end_hour=11).status_code == 201
    r = _make_booking(rid, start_hour=10, end_hour=12)
    assert r.status_code == 409

def test_create_booking_相邻不重叠_判定表():
    _reset()
    rid = _make_room()
    assert _make_booking(rid, start_hour=9, end_hour=10).status_code == 201
    r = _make_booking(rid, start_hour=10, end_hour=11)
    assert r.status_code == 201

def test_cancel_booking_流程_语句分支覆盖():
    _reset()
    rid = _make_room()
    bid = _make_booking(rid).json()['id']
    r1 = client.post(f'/bookings/{bid}/cancel')
    assert r1.status_code == 200
    assert r1.json()['status'] == 'cancelled'
    r2 = client.post(f'/bookings/{bid}/cancel')
    assert r2.status_code == 409

def test_cancel_booking_不存在_等价类划分():
    _reset()
    r = client.post('/bookings/9999/cancel')
    assert r.status_code == 404

def test_list_bookings_仅active_语句分支覆盖():
    _reset()
    rid = _make_room()
    b1 = _make_booking(rid, start_hour=9, end_hour=10).json()['id']
    _make_booking(rid, start_hour=11, end_hour=12)
    client.post(f'/bookings/{b1}/cancel')
    r = client.get(f'/rooms/{rid}/bookings', params={'date': '2024-01-01'})
    assert r.status_code == 200
    items = r.json()['items']
    assert len(items) == 1
    assert items[0]['start_hour'] == 11

def test_list_bookings_room不存在_等价类划分():
    _reset()
    r = client.get('/rooms/9999/bookings', params={'date': '2024-01-01'})
    assert r.status_code == 404

def test_create_booking_日期格式非法_错误推测法():
    _reset()
    rid = _make_room()
    r1 = client.post('/bookings', json={'room_id': rid, 'date': '2024/01/01', 'start_hour': 9, 'end_hour': 10})
    assert r1.status_code == 422
    r2 = client.post('/bookings', json={'room_id': rid, 'date': '2024-01-01', 'start_hour': 7, 'end_hour': 10})
    assert r2.status_code == 422
    r3 = client.post('/bookings', json={'room_id': rid, 'date': '2024-01-01', 'start_hour': 9, 'end_hour': 22})
    assert r3.status_code == 422

def test_create_room_类型错误_错误推测法():
    _reset()
    r1 = client.post('/rooms', json={'name': 'R', 'capacity': 'abc', 'hourly_rate': 50})
    assert r1.status_code == 422
    r2 = client.post('/rooms', json={'name': 'R', 'capacity': 5, 'hourly_rate': None})
    assert r2.status_code == 422
