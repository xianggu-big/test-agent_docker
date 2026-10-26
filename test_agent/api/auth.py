"""L1 鉴权：pbkdf2 口令哈希 + 内存 token + 首启演示账号注入。

诚实边界（v1）：token 存进程内存，服务重启后需重新登录；只防误用不防
有心的本机攻击者——答辩演示语境可接受，论文可写进"后续工作"。
"""
import hashlib
import secrets

from ..store.db import get_conn

TOKENS: dict[str, dict] = {}  # token -> {user_id, username, role}

_DEFAULT_USERS = [  # (username, password, role) —— 首启注入，演示账号口令公开给答辩现场
    ("admin", "admin123", "admin"),
    ("dev", "dev123", "developer"),
    ("caller", "caller123", "caller"),
]


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return secrets.compare_digest(calc, digest)


def ensure_default_users(conn=None) -> None:
    conn = conn or get_conn()
    for username, password, role in _DEFAULT_USERS:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                (username, hash_password(password), role))
        elif "$" not in (row["password_hash"] or ""):
            # 愈合 CLI 时代写入的占位哈希（'x' 等）：统一升级为 pbkdf2 演示口令
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(password), row["id"]))
    conn.commit()


def login(username: str, password: str) -> dict | None:
    """校验口令，签发内存 token。失败返回 None（不区分账号不存在/口令错，防枚举）。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, role, password_hash FROM users WHERE username=?", (username,)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    user = {"user_id": row["id"], "username": row["username"], "role": row["role"]}
    TOKENS[token] = user
    return {"token": token, **user}
