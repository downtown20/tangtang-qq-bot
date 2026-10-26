"""会客厅会话存档 —— 桌面端「会客厅」的会话与消息持久化（2026-10-24）。

为什么是独立 SQLite 而不是塞进 memory.db：
- memory.db 的 schema 归 store.py 统一管理，加表会影响 bot 主库与全部既有查询；
- 会客厅是「观察窗」，它的数据是**记录**不是**人格状态**——分库让两者的
  生命周期解耦：删掉 状态/会客厅.db 只会丢观察记录，不会碰糖糖的记忆。

落盘位置走 `agent/paths.py` 的 `state_path()`（CLAUDE.md 铁律：不许自己拼
根相对路径，闸门 tests/test_runtime_paths_contract.py 会红）。

线程模型：HTTP 服务跑在独立线程，bot 主循环在 asyncio 线程——两边都会调用本
模块，所以连接用 `check_same_thread=False` + 一把模块内锁串行化。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from .paths import state_path

DB_FILE = ".会客厅.db"

#: 角色白名单——前端按它决定气泡样式，也是写库前的防御
ROLES = ("user", "tangtang", "lead", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'observer'
);
CREATE TABLE IF NOT EXISTS console_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    ts          REAL NOT NULL,
    meta_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_console_messages_session
    ON console_messages(session_id, id);
"""


class ConsoleStore:
    """会客厅存档。所有方法都是线程安全的（内部一把锁）。"""

    def __init__(self, path: str | None = None):
        self.path = str(path or state_path(DB_FILE))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL：允许 bot 主循环与 HTTP 线程同时读，写不互相阻塞
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── 会话 ──────────────────────────────────────────────────────
    def create_session(self, title: str = "") -> dict:
        now = time.time()
        sid = uuid.uuid4().hex[:12]
        title = (title or "").strip() or time.strftime(
            "会客厅 · %m-%d %H:%M", time.localtime(now))
        with self._lock:
            self._conn.execute(
                "INSERT INTO console_sessions (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, title, now, now),
            )
            self._conn.commit()
        return {"id": sid, "title": title, "created_at": now}

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "       (SELECT COUNT(*) FROM console_messages m "
                "         WHERE m.session_id = s.id) AS message_count, "
                "       (SELECT m.content FROM console_messages m "
                "         WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1) "
                "         AS last_preview "
                "FROM console_sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "message_count": int(r["message_count"] or 0),
                "last_preview": _preview(r["last_preview"]),
            }
            for r in rows
        ]

    def get_session(self, sid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id, title, created_at, updated_at FROM console_sessions "
                "WHERE id = ?", (sid,)
            ).fetchone()
        if not r:
            return None
        return {"id": r["id"], "title": r["title"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]}

    def ensure_session(self, sid: str = "", title: str = "") -> dict:
        """拿到会话；不存在（或没给 id）就新建一个。前端首次进入也走这里。"""
        if sid:
            got = self.get_session(sid)
            if got:
                return got
        return self.create_session(title)

    def delete_session(self, sid: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM console_sessions WHERE id = ?", (sid,))
            self._conn.execute(
                "DELETE FROM console_messages WHERE session_id = ?", (sid,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── 消息 ──────────────────────────────────────────────────────
    def append_message(self, session_id: str, role: str, content: str,
                       meta: dict[str, Any] | None = None,
                       ts: float | None = None) -> int:
        if role not in ROLES:
            raise ValueError(f"未知消息角色: {role}")
        now = time.time() if ts is None else float(ts)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO console_messages (session_id, role, content, ts, meta_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, now, meta_json),
            )
            self._conn.execute(
                "UPDATE console_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id))
            # 第一条用户消息顺便给会话起个更像样的标题
            self._maybe_retitle(session_id, role, content)
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def _maybe_retitle(self, session_id: str, role: str, content: str) -> None:
        """仍是默认标题时，用首条消息的前 18 字做标题（DSH 的会话命名习惯）。"""
        if role != "user" or not content.strip():
            return
        row = self._conn.execute(
            "SELECT title FROM console_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row or not str(row["title"]).startswith("会客厅 · "):
            return
        text = " ".join(content.split())
        if len(text) > 18:
            text = text[:18] + "…"
        self._conn.execute(
            "UPDATE console_sessions SET title = ? WHERE id = ?",
            (text, session_id))

    def list_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, role, content, ts, meta_json "
                "FROM console_messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        out = []
        for r in reversed(rows):
            meta = None
            if r["meta_json"]:
                try:
                    meta = json.loads(r["meta_json"])
                except (ValueError, TypeError):
                    meta = None
            out.append({
                "id": int(r["id"]),
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"],
                "ts": r["ts"],
                "meta": meta,
            })
        return out

    def message_count(self, session_id: str) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM console_messages WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(r["n"] if r else 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def _preview(text: str | None, limit: int = 40) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")
