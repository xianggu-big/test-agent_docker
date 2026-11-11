"""AI 契约黑盒用例（L2 集成层 · 契约断言级）"""
import jsonschema
import requests

BASE = "http://127.0.0.1:8080"

def test_get_books_happy_契约黑盒():
    r = requests.get(BASE + '/books', timeout=10)
    assert r.status_code == 200, r.text   # 契约断言[接口契约]


def test_post_books_happy_契约黑盒():
    payload = {}
    r = requests.post(BASE + '/books', json=payload, timeout=10)
    assert r.status_code == 201, r.text   # 契约断言[接口契约]


def test_get_ping_happy_契约黑盒():
    r = requests.get(BASE + '/ping', timeout=10)
    assert r.status_code == 200, r.text   # 契约断言[接口契约]


