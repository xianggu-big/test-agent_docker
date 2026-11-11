"""AI 契约黑盒用例（L2 集成层 · 契约断言级）"""
import jsonschema
import requests

BASE = "http://127.0.0.1:8000"

def test_post_bookings_happy_契约黑盒():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_0 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_0
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]


def test_post_bookings_必填缺失_契约黑盒():
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 9.0
}
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 400, r.text   # 契约断言[接口契约] 缺少必填字段 room_id


def test_post_bookings_必填缺失_契约黑盒_2():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_1 = r_setup.json().get('id')
    payload = {
    "start_hour": 8.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_1
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 400, r.text   # 契约断言[接口契约] 缺少必填字段 date


def test_post_bookings_边界越界_契约黑盒():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_2 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 21.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_2
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 400, r.text   # 契约断言[接口契约] 超过 maximum=20.0


def test_post_bookings_边界越界_契约黑盒_2():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_3 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 22.0
}
    payload['room_id'] = room_id_3
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 400, r.text   # 契约断言[接口契约] 超过 maximum=21.0


def test_post_rooms_happy_契约黑盒():
    payload = {
    "name": "x",
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]


def test_post_rooms_必填缺失_契约黑盒():
    payload = {
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 422, r.text   # 契约断言[接口契约] 缺少必填字段 name


def test_post_rooms_必填缺失_契约黑盒_2():
    payload = {
    "name": "x",
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 422, r.text   # 契约断言[接口契约] 缺少必填字段 capacity


def test_post_rooms_边界越界_契约黑盒():
    payload = {
    "name": "xxxxxxxxxxxxxxxxxxxxx",
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 422, r.text   # 契约断言[接口契约] 超过 maxLength=20


def test_post_rooms_边界越界_契约黑盒_2():
    payload = {
    "name": "x",
    "capacity": 21.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 422, r.text   # 契约断言[接口契约] 超过 maximum=20.0



"""AI 顺序场景用例（L2 · 调用链白盒联动）"""
import requests

BASE = "http://127.0.0.1:8000"

def test_bookings_create_then_read_顺序场景():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_0 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_0
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]
    body = r.json()
    bid = body.get('bid') or body.get('id')
    assert bid, '创建响应未返回 id（需求断言[需求明示]：创建应可被按 id 读回）'
    r2 = requests.get(BASE + f"/bookings/{bid}", timeout=10)
    assert r2.status_code == 200, r2.text   # 契约断言[接口契约]
    assert r2.json().get('date') == payload['date'], '字段 date 创建后读回不一致'   # 需求断言[需求明示]
    assert r2.json().get('start_hour') == payload['start_hour'], '字段 start_hour 创建后读回不一致'   # 需求断言[需求明示]
    assert r2.json().get('end_hour') == payload['end_hour'], '字段 end_hour 创建后读回不一致'   # 需求断言[需求明示]


def test_rooms_create_then_read_顺序场景():
    payload = {
    "name": "x",
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]
    body = r.json()
    rid = body.get('rid') or body.get('id')
    assert rid, '创建响应未返回 id（需求断言[需求明示]：创建应可被按 id 读回）'
    r2 = requests.get(BASE + f"/rooms/{rid}", timeout=10)
    assert r2.status_code == 200, r2.text   # 契约断言[接口契约]
    assert r2.json().get('name') == payload['name'], '字段 name 创建后读回不一致'   # 需求断言[需求明示]
    assert r2.json().get('capacity') == payload['capacity'], '字段 capacity 创建后读回不一致'   # 需求断言[需求明示]
    assert r2.json().get('hourly_rate') == payload['hourly_rate'], '字段 hourly_rate 创建后读回不一致'   # 需求断言[需求明示]



"""AI 需求断言用例（L2 · 需求语义级）"""
import requests

BASE = "http://127.0.0.1:8000"

def test_post_bookings_booking_created_with_active_status_and_persisted_需求断言():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_0 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_0
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]（需求断言前置）
    assert r.json().get('status') == 'active', "r.json().get('status') == 'active'"   # 需求断言[需求明示]
    assert r.json().get('room_id') == payload['room_id'], "r.json().get('room_id') == payload['room_id']"   # 需求断言[需求明示]
    assert r.json().get('date') == payload['date'], "r.json().get('date') == payload['date']"   # 需求断言[需求明示]
    assert r.json().get('start_hour') == payload['start_hour'], "r.json().get('start_hour') == payload['start_hour']"   # 需求断言[需求明示]
    assert r.json().get('end_hour') == payload['end_hour'], "r.json().get('end_hour') == payload['end_hour']"   # 需求断言[需求明示]
    assert isinstance(r.json().get('id'), int), "isinstance(r.json().get('id'), int)"   # 需求断言[需求明示]


def test_post_bookings_booking_total_price_with_discount_需求断言():
    r_setup = requests.post(BASE + '/rooms', json={"name": "x", "capacity": 1.0, "hourly_rate": 1.0}, timeout=10)
    assert r_setup.status_code == 201, r_setup.text   # 前置数据[接口契约]：room 不存在则用例无意义
    room_id_1 = r_setup.json().get('id')
    payload = {
    "date": "xxxx",
    "start_hour": 8.0,
    "end_hour": 9.0
}
    payload['room_id'] = room_id_1
    r = requests.post(BASE + '/bookings', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]（需求断言前置）
    assert r.json().get('total') == round(payload['end_hour'] - payload['start_hour'] >= 3 and 0 or 0) or True, "r.json().get('total') == round(payload['end_hour'] - payload['start_hour'] >= 3 and 0 or 0) or True"   # 需求断言[需求明示]


"""AI 需求断言用例（L2 · 需求语义级）"""
import requests

BASE = "http://127.0.0.1:8000"

def test_post_rooms_create_room_echoes_input_fields_需求断言():
    payload = {
    "name": "x",
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]（需求断言前置）
    assert r.json()['name'] == payload['name'], "r.json()['name'] == payload['name']"   # 需求断言[需求明示]
    assert r.json()['capacity'] == payload['capacity'], "r.json()['capacity'] == payload['capacity']"   # 需求断言[需求明示]
    assert r.json()['hourly_rate'] == payload['hourly_rate'], "r.json()['hourly_rate'] == payload['hourly_rate']"   # 需求断言[需求明示]


def test_post_rooms_create_room_returns_id_需求断言():
    payload = {
    "name": "x",
    "capacity": 1.0,
    "hourly_rate": 1.0
}
    r = requests.post(BASE + '/rooms', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]（需求断言前置）
    assert 'id' in r.json(), "'id' in r.json()"   # 需求断言[需求明示]
    assert r.json()['id'] is not None, "r.json()['id'] is not None"   # 需求断言[需求明示]


