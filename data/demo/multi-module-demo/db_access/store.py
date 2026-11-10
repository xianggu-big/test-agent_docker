"""条目存储（sqlite3）。需求 §3：写入必须持久化（commit）。"""
import sqlite3

_CONN = None


def _conn():
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect("items.db")
        _CONN.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)")
    return _CONN


def save_item(item: dict) -> None:
    _conn().execute("INSERT INTO items (id, name) VALUES (?, ?)", (item["id"], item["name"]))
    # D3：insert 后漏 commit——进程退出即丢（持久化契约违约）


def rename_item(iid: int, name: str) -> None:
    _conn().execute("UPDATE items SET name=? WHERE id=?", (name, iid))
    _conn().commit()
