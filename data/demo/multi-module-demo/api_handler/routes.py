"""条目 API 路由（FastAPI）。需求 §2：不存在必须 404。"""
from fastapi import APIRouter, HTTPException

import config
from db_access import store
from utils import text

router = APIRouter()
_ITEMS = {}


@router.get("/items/{iid}")
def read_item(iid: int):
    item = _ITEMS.get(iid)
    if item is None:
        return {"item": None}           # D2：缺失应 404，这里返回 200+null
    return {"item": item}


@router.post("/items")
def create_item(name: str):
    if len(_ITEMS) >= config.MAX_ITEMS:
        raise HTTPException(409, "max items reached")
    iid = len(_ITEMS) + 1
    _ITEMS[iid] = {"id": iid, "name": text.clip(len(name), 1, 20)}
    store.save_item(_ITEMS[iid])
    return _ITEMS[iid]
