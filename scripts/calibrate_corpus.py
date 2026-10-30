"""语料缺陷可观测性标定（corpus-003 起）：每条真缺陷至少 1 条探针能暴露，decoy 不暴露。

用法：python scripts/calibrate_corpus.py [corpus-003]
纪律：探针只读被测项目自身行为（TestClient 进程内），零 Docker/零 LLM；
真缺陷 present=True 才算标定成功，decoy 必须 present=False（防止占位项污染分母）。
"""
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus"


def _load_project(corpus_id: str):
    proj = CORPUS / corpus_id / "project"
    sys.path.insert(0, str(proj))
    for name in ("app", "booking", "store"):
        sys.modules.pop(name, None)
    app_mod = importlib.import_module("app")
    booking = importlib.import_module("booking")
    store = importlib.import_module("store")
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app), booking, store


def _probe_corpus003():
    client, booking, store = _load_project("corpus-003")
    out: dict[str, dict] = {}

    def room(name="R"):
        r = client.post("/rooms", json={"name": name, "capacity": 4, "hourly_rate": 50})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    # D1：4 小时边界被误拒
    d1_ok = False
    try:
        booking.validate_duration(8, 12)
    except ValueError:
        d1_ok = True
    rid = room("D1")
    r = client.post("/bookings", json={"room_id": rid, "date": "2026-11-01",
                                       "start_hour": 8, "end_hour": 12})
    out["D1"] = {"present": d1_ok and r.status_code == 400,
                 "observed": f"validate_duration(8,12) raises Value={d1_ok}; "
                             f"POST 4h -> {r.status_code}",
                 "expected_correct": "validate_duration(8,12)=4；POST 4h -> 201"}

    # D2：3 小时总价 15% 折扣
    rid = room("D2")
    r = client.post("/bookings", json={"room_id": rid, "date": "2026-11-02",
                                       "start_hour": 9, "end_hour": 12})
    total = r.json().get("total") if r.status_code == 201 else None
    out["D2"] = {"present": booking.quote_total(50, 3) == 127 and total == 127,
                 "observed": f"quote_total(50,3)={booking.quote_total(50, 3)}; "
                             f"POST 3h total={total}",
                 "expected_correct": "quote_total(50,3)=135"}

    # D3：创建后按 id 读回 404（未持久化）
    rid = room("D3")
    r = client.post("/bookings", json={"room_id": rid, "date": "2026-11-03",
                                       "start_hour": 10, "end_hour": 11})
    bid = r.json().get("id") if r.status_code == 201 else None
    g = client.get(f"/bookings/{bid}") if bid else None
    out["D3"] = {"present": bool(bid) and g.status_code == 404,
                 "observed": f"POST -> {r.status_code} id={bid}; GET /bookings/{bid} -> "
                             f"{g.status_code if g else 'N/A'}",
                 "expected_correct": "POST 201 后 GET 200"}

    # D4：相邻时段误判重叠（直接插一条已存在预约，避开 D3 的未持久化干扰）
    rid = room("D4")
    store.BOOKINGS[9001] = {"id": 9001, "room_id": rid, "date": "2026-11-04",
                            "start_hour": 10, "end_hour": 11, "total": 50,
                            "status": "active"}
    r = client.post("/bookings", json={"room_id": rid, "date": "2026-11-04",
                                       "start_hour": 11, "end_hour": 12})
    out["D4"] = {"present": booking.has_overlap(10, 11, 11, 12) and r.status_code == 409,
                 "observed": f"has_overlap(10,11,11,12)={booking.has_overlap(10, 11, 11, 12)}; "
                             f"POST adjacent -> {r.status_code}",
                 "expected_correct": "has_overlap=False；POST adjacent -> 201"}

    # D5：重复取消返回 200（应 409）
    rid = room("D5")
    store.BOOKINGS[9002] = {"id": 9002, "room_id": rid, "date": "2026-11-05",
                            "start_hour": 14, "end_hour": 15, "total": 50,
                            "status": "active"}
    first = client.post("/bookings/9002/cancel")
    second = client.post("/bookings/9002/cancel")
    out["D5"] = {"present": first.status_code == 200 and second.status_code == 200,
                 "observed": f"cancel#1 {first.status_code}; cancel#2 {second.status_code}",
                 "expected_correct": "cancel#1 200; cancel#2 409"}

    # D6 decoy：不存在会议室正确 404
    g = client.get("/rooms/999999")
    out["D6"] = {"present": g.status_code != 404,
                 "observed": f"GET /rooms/999999 -> {g.status_code}",
                 "expected_correct": "404（decoy 应 present=False）"}

    # D7 decoy：列表只回 active
    rid = room("D7")
    store.BOOKINGS[9003] = {"id": 9003, "room_id": rid, "date": "2026-11-06",
                            "start_hour": 16, "end_hour": 17, "total": 50,
                            "status": "cancelled"}
    lst = client.get(f"/rooms/{rid}/bookings", params={"date": "2026-11-06"})
    items = lst.json().get("items", []) if lst.status_code == 200 else []
    out["D7"] = {"present": lst.status_code != 200 or any(i["id"] == 9003 for i in items),
                 "observed": f"list -> {lst.status_code}, items={len(items)}",
                 "expected_correct": "200 且不含 cancelled（decoy 应 present=False）"}
    return out


def main() -> int:
    corpus_id = sys.argv[1] if len(sys.argv) > 1 else "corpus-003"
    gt = json.loads((CORPUS / corpus_id / "ground_truth.json").read_text(encoding="utf-8"))
    probes = _probe_corpus003()
    bad = []
    for d in gt["planted_defects"]:
        p = probes.get(d["id"])
        if not p:
            bad.append(f"{d['id']}: 无探针")
            continue
        ok = (not p["present"]) if d.get("decoy") else p["present"]
        if not ok:
            bad.append(f"{d['id']}: 标定失败（{p['observed']}；应 {p['expected_correct']}）")
        print(f"{d['id']} {'decoy' if d.get('decoy') else 'real':<5} "
              f"present={p['present']} -> {'OK' if ok else 'FAIL'}｜{p['observed']}")
    print("标定结论：", "全部通过" if not bad else "失败：" + "；".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
