"""LLM 模型 profile（任务卡-LLM模型选择-20261108）：设置页三档预设+自定义，切换即时生效。

铁律（不可妥协）：数据库只存 key 所在环境变量的"名字"（key_env），永不存 key 本体
——SQLite 文件会进备份/交接，明文密钥=泄漏面。key 解析=os.environ.get(key_env)。

生效机制：apply() 运行时改写 config.DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 模块属性
（GIL 下赋值原子，调用点逐 invoke 读取，不需重启）；make_chat 对未显式传三件套的
调用点兜底取 profile（显式传参仍优先=逃生口，ai_reviewer 的 per-role 模型因此零改动）。

无 app_settings 行=默认 deepseek（config 现值），行为与历史完全一致，老任务零感知。
"""
import json
import logging
import os
import re
import time

log = logging.getLogger(__name__)

PROFILE_KEY = "llm_profile"
PROVIDERS = ("deepseek", "qwen", "local", "custom")

# 云端预设：base_url 固定；model 预设值可改（用户可覆盖）；key_env=环境变量"名字"
PRESETS: dict[str, dict] = {
    "deepseek": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "provider": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",        # DashScope 兼容模式，预设值可改
        "key_env": "DASHSCOPE_API_KEY",
    },
    "local": {
        "provider": "local",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3-4b-inst-test",
        "key_env": "",               # 本地无需 key
    },
}


def missing_key_message(p: dict) -> str:
    """无 Key 诚实降级的统一文案（§2.1：带 profile 与 key_env 提示）。"""
    return (f"未配置当前模型的 API Key（profile={p.get('provider')}，"
            f"环境变量 {p.get('key_env') or '无'}）")


def _normalize(p: dict) -> dict | None:
    """归一+校验；非法返回 None（caller 转 400）。custom 缺字段/非法 URL 一律拒。"""
    if not isinstance(p, dict):
        return None
    provider = str(p.get("provider") or "").strip()
    if provider not in PROVIDERS:
        return None
    if provider in PRESETS:
        preset = PRESETS[provider]
        out = dict(preset)
        # 预设档允许改 model（预设值可改）；base_url/key_env 仍取预设
        model = str(p.get("model") or "").strip()
        if model:
            out["model"] = model
        return out
    # custom：三件套全部必填，base_url 必须 http/https
    base_url = str(p.get("base_url") or "").strip()
    model = str(p.get("model") or "").strip()
    key_env = str(p.get("key_env") or "").strip()
    if not base_url or not re.match(r"^https?://", base_url):
        return None
    if not model or not key_env:
        return None
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", key_env):
        return None                  # 环境变量名形态校验（防注入面）
    return {"provider": "custom", "base_url": base_url, "model": model,
            "key_env": key_env}


def current(conn=None) -> dict:
    """读当前 profile；无行=默认 deepseek（config 现值，行为与历史一致）。"""
    from .. import config
    own = False
    if conn is None:
        from ..store.db import get_conn
        conn = get_conn()
        own = True
    try:
        row = conn.execute("SELECT value, updated_by, updated_at FROM app_settings "
                           "WHERE key=?", (PROFILE_KEY,)).fetchone()
    finally:
        if own:
            conn.close()
    if row:
        try:
            p = json.loads(row["value"])
            if isinstance(p, dict) and p.get("provider") in PROVIDERS:
                return {**p, "updated_by": row["updated_by"],
                        "updated_at": row["updated_at"]}
        except (ValueError, KeyError):
            pass                     # 脏行=按默认走，诚实 log
    return {**PRESETS["deepseek"], "base_url": config.DEEPSEEK_BASE_URL,
            "model": config.DEEPSEEK_MODEL, "updated_by": "", "updated_at": ""}


def apply(p: dict, conn, updated_by: str = "") -> dict:
    """校验 → 写表 → 运行时改写 config 模块属性（立即全局生效，不需重启）。
    非法 profile 抛 ValueError（server 转 400）。返回应用后的完整 profile。"""
    norm = _normalize(p)
    if norm is None:
        raise ValueError("非法 profile：provider 必须是 deepseek/qwen/local/custom；"
                         "custom 档 base_url（http/https）/model/key_env 全部必填")
    conn.execute(
        "INSERT INTO app_settings(key, value, updated_by) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_by=excluded.updated_by, updated_at=datetime('now')",
        (PROFILE_KEY, json.dumps(norm, ensure_ascii=False), updated_by))
    conn.commit()
    _apply_runtime(norm)
    log.info("[llm-profile] 切换 → %s/%s（key_env=%s，by=%s）",
             norm["provider"], norm["model"], norm["key_env"] or "-", updated_by or "-")
    return {**norm, "updated_by": updated_by,
            "updated_at": conn.execute(
                "SELECT updated_at FROM app_settings WHERE key=?",
                (PROFILE_KEY,)).fetchone()["updated_at"]}


def _apply_runtime(p: dict) -> None:
    """运行时改写 config 模块属性（make_chat 兜底与 /api/config 现值的真源）。"""
    from .. import config
    config.DEEPSEEK_BASE_URL = p["base_url"]
    config.DEEPSEEK_MODEL = p["model"]


def apply_saved_if_any() -> None:
    """init_db 时调用：恢复上次保存的选择（进程启动即生效）。
    函数内延迟导入——db.py 顶层 import skills 会循环依赖（坑预判 #1）。"""
    try:
        from .. import config
        p = current()
        if p.get("provider") != "deepseek":
            _apply_runtime(p)
            log.info("[llm-profile] 启动恢复 profile → %s/%s",
                     p["provider"], p["model"])
        else:
            # deepseek 档也以保存值为准（用户可能改过 model 名），仅当与 config 默认不同才改写
            saved = {k: p[k] for k in ("base_url", "model")}
            cur = {"base_url": config.DEEPSEEK_BASE_URL, "model": config.DEEPSEEK_MODEL}
            if saved != cur:
                _apply_runtime(p)
                log.info("[llm-profile] 启动恢复 profile → %s/%s",
                         p["provider"], p["model"])
    except Exception as e:           # noqa: BLE001 —— 恢复失败不挡启动，config 默认值兜底
        log.warning("[llm-profile] 启动恢复失败（%s: %s），使用 config 默认值",
                    type(e).__name__, e)


def resolve_key(p: dict) -> str | None:
    """按 key_env 读环境变量；local 恒返回 "ollama"（本地不校验 key）。
    未配置返回 None——调用点走既有"无 Key 诚实降级"路径（文案用 missing_key_message）。"""
    if p.get("provider") == "local":
        return "ollama"
    key_env = p.get("key_env") or ""
    if not key_env:
        return None
    return os.environ.get(key_env) or None


def test_connection(p: dict, timeout_s: int = 60) -> dict:
    """连通性测试：60s 超时发一条最小 JSON 指令，返回 {ok, latency_ms, reply|error}。
    接受临时 profile（不落库——POST /test 的 body 直接传本函数）。local 无 ollama
    时给可行动文案"请先启动 ollama serve"。"""
    norm = _normalize(p)
    if norm is None:
        return {"ok": False, "latency_ms": None,
                "error": "非法 profile：provider/base_url/model/key_env 校验未通过"}
    t0 = time.monotonic()
    try:
        chat = make_test_chat(norm, timeout_s)
        resp = chat.invoke([("user", '请只输出 JSON：{"ok": true}')])
        reply = (getattr(resp, "content", "") or "").strip()
        return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000),
                "reply": reply[:200]}
    except Exception as e:           # noqa: BLE001 —— 测试连通性的失败是返回值不是异常
        name, msg = type(e).__name__, str(e)
        if norm["provider"] == "local" and ("connect" in msg.lower()
                                            or "connection" in msg.lower()):
            msg = "本地 Ollama 不可达：请先启动 ollama serve（端点 localhost:11434）"
        return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": f"{name}: {msg}"[:300]}


def make_test_chat(p: dict, timeout_s: int = 60):
    """测试连通性专用的最小客户端（不经 make_chat 的退避/备胎——测试要快速如实失败）。"""
    from langchain_openai import ChatOpenAI
    import httpx
    return ChatOpenAI(
        model=p["model"], api_key=resolve_key(p) or "ollama",
        base_url=p["base_url"], temperature=0, timeout=timeout_s, max_retries=1,
        max_tokens=64, http_client=httpx.Client(default_encoding="utf-8"))
