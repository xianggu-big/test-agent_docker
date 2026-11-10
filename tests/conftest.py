"""pytest 配置：把项目根目录加进 sys.path，使 test_agent 包可导入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------- API 测试隔离（2026-11-10 429 实锤修复） ----------
# 全量回归实锤：8 个起 TestClient 的测试共享进程级 rate_limit 桶（key=login:127.0.0.1，
# 窗口 300s），累计 login 超 20 次触发 429 → 后续 API 测试 setup 全炸（KeyError: token）。
# 桶与 TOKENS 都是进程内存，tmp 库隔离不了——autouse fixture 在每个测试前清空（只影响
# 测试进程；产品代码零改动，真实服务的限流行为不变）。
import pytest


@pytest.fixture(autouse=True)
def _reset_auth_rate_state():
    try:
        from test_agent.api import auth
        auth._ATTEMPTS.clear()
        auth.TOKENS.clear()
    except Exception:            # noqa: BLE001 —— auth 未加载的纯单测不受影响
        pass
    yield
