"""L2 冒烟演示项目：优惠券 API（FastAPI）——四级双轨 P1 端到端可演示的被测对象。

需求（演示用自然语言口径，REQUIREMENT.md 同文）：
1. 创建优惠券：code 长度 2~8、percent 1~100（整数），创建成功返回 201 与完整字段
2. 按 id 查询优惠券：存在返回 200，不存在返回 404
3. 下单使用优惠券：金额满 min_total 才可用，折扣 = amount * percent / 100（向下取整），
   应付 = amount - 折扣；折扣不为负
4. 重复使用同一优惠券下单 → 400

openapi.json 为 FastAPI 导出的静态契约（D4-① 的提取源；重导出：
python -c "from fastapi.testclient import TestClient; import json; from app import app;
print(json.dumps(app.openapi(), ensure_ascii=False))" > openapi.json）
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="coupon-api")

_COUPONS: dict[int, dict] = {}
_USED: set[int] = set()
_NEXT_ID = {"coupon": 1, "order": 1}


class CouponIn(BaseModel):
    code: str = Field(min_length=2, max_length=8)
    percent: int = Field(ge=1, le=100)
    min_total: int = Field(default=0, ge=0)


@app.post("/coupons", status_code=201)
def create_coupon(c: CouponIn):
    cid = _NEXT_ID["coupon"]
    _NEXT_ID["coupon"] += 1
    _COUPONS[cid] = {"id": cid, **c.model_dump()}
    return _COUPONS[cid]


@app.get("/coupons/{cid}")
def read_coupon(cid: int):
    if cid not in _COUPONS:
        raise HTTPException(404, "coupon not found")
    return _COUPONS[cid]


class OrderIn(BaseModel):
    coupon_id: int
    amount: int = Field(ge=0)


@app.post("/orders", status_code=201)
def create_order(o: OrderIn):
    if o.coupon_id not in _COUPONS:
        raise HTTPException(404, "coupon not found")
    if o.coupon_id in _USED:
        raise HTTPException(400, "coupon already used")
    c = _COUPONS[o.coupon_id]
    if o.amount < c["min_total"]:
        raise HTTPException(400, "below min_total")
    _USED.add(o.coupon_id)
    discount = o.amount * c["percent"] // 100
    return {"id": _NEXT_ID["order"], "amount": o.amount,
            "discount": discount, "total": o.amount - discount,
            "coupon_id": o.coupon_id}


@app.get("/orders/{oid}")
def read_order(oid: int):
    # 演示用：订单不持久化——顺序场景「创建→读回」会在此失败，正好演示契约违背的诚实上报
    raise HTTPException(404, "order not found (demo: not persisted)")
