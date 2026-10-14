"""同步记忆分叉判定的四场景契约（K5，2026-09-04）。

背景：旧实现按 chat_log 自增 id join——两库同源、各自新增量相同时 id 恰好对齐，
把两库「各自新增的最后几条」误配成同一批（换机接力每次报假分叉 2 条，生产取证：
台式机群聊 2 条 vs 笔记本私聊 2 条 id 同为 249823/249824）。
修复：并发分叉只认「同一 event_key（QQ 同一条入站消息）内容不同」；
方向守卫用「本机最新记录是否都在快照里」（count_local_missing）。
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "sync_memory_k5", BASE / "tools" / "同步记忆.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

SCHEMA = """CREATE TABLE chat_log (
    id INTEGER PRIMARY KEY, qq_id TEXT, group_id TEXT, is_bot_reply INTEGER DEFAULT 0,
    message TEXT, timestamp TEXT, event_key TEXT)"""


def _make_db(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO chat_log (id, qq_id, group_id, is_bot_reply, message, timestamp, event_key)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def _base_rows(n: int = 10) -> list[tuple]:
    """基线：n 条入站消息（ek 连续）+ 1 条 bot 回复（ek 为空）。"""
    rows = [(i, "u1", "g1", 0, f"msg{i}", f"2026-09-03 23:{i:02d}:00",
             f"v2:group:g1:1000+i:{i}:1") for i in range(1, n + 1)]
    rows.append((n + 1, "u1", "g1", 1, "bot回复", "2026-09-03 23:59:00", ""))
    return rows


def _new_rows(start_id: int, ek_start: int, texts: list[str]) -> list[tuple]:
    return [(start_id + i, "u2", "g1", 0, t, f"2026-09-04 00:{i:02d}:00",
             f"v2:group:g1:2000+{ek_start + i}:{2000 + ek_start + i}:1")
            for i, t in enumerate(texts)]


def _copy_rows(src: Path) -> list[tuple]:
    conn = sqlite3.connect(str(src))
    rows = conn.execute("SELECT id, qq_id, group_id, is_bot_reply, message, timestamp, event_key"
                        " FROM chat_log").fetchall()
    conn.close()
    return [tuple(r) for r in rows]


@pytest.fixture()
def bases(tmp_path):
    """base.db = 公共祖先（id 1-11）；local/snap 都从它长出来。"""
    base = _make_db(tmp_path / "base.db", _base_rows())
    return base, tmp_path


def test_normal_relay_no_fork_even_with_id_collision(bases):
    """接力正常：快照 = 祖先 + 新 2 条（群聊）；本机 = 祖先 + 另 2 条（私聊）。
    两库自增 id 恰好都停在 13——旧实现误报 2 条；新实现按 ek 判定 = 0。"""
    base, tmp = bases
    base_rows = _copy_rows(base)
    # 本机：祖先 + 私聊两条（id 12/13）
    local = _make_db(tmp / "local.db", base_rows + _new_rows(12, 300, ["私聊1", "私聊2"]))
    # 快照：祖先 + 群聊两条（id 也到 12/13——撞车）
    snap = _make_db(tmp / "snap.db", base_rows + _new_rows(12, 400, ["群聊A", "群聊B"]))
    assert sync.count_fork_diff(local, snap) == 0
    # 方向：本机两条私聊不在快照里 → missing=2（本机是旁支，需先打包）
    assert sync.count_local_missing(local, snap) == 2


def test_true_concurrent_fork_detected(bases):
    """真并发：双机收到同一条 QQ 消息（同 ek）各自写入不同内容 → fork=1。"""
    base, tmp = bases
    rows = _copy_rows(base)
    local = _make_db(tmp / "local.db", rows)
    conn = sqlite3.connect(str(tmp / "local.db"))
    conn.execute("INSERT INTO chat_log (id,qq_id,group_id,is_bot_reply,message,timestamp,event_key)"
                 " VALUES (99,'u9','g1',0,'甲写的','2026-09-04 01:00:00','v2:group:g1:9999:9:1')")
    conn.commit()
    conn.close()
    snap = _make_db(tmp / "snap.db", rows)
    conn = sqlite3.connect(str(tmp / "snap.db"))
    conn.execute("INSERT INTO chat_log (id,qq_id,group_id,is_bot_reply,message,timestamp,event_key)"
                 " VALUES (99,'u9','g1',0,'乙写的','2026-09-04 01:00:00','v2:group:g1:9999:9:1')")
    conn.commit()
    conn.close()
    assert sync.count_fork_diff(local, snap) == 1


def test_bot_reply_diff_never_counts_as_fork(bases):
    """bot 自回复天然各机不同且无 ek——两库同 id 内容不同不得计入分叉。"""
    base, tmp = bases
    rows = _copy_rows(base)
    local = _make_db(tmp / "local.db", rows)
    snap = _make_db(tmp / "snap.db", rows)
    for p, msg in ((tmp / "local.db", "本机回复"), (tmp / "snap.db", "快照回复")):
        conn = sqlite3.connect(str(p))
        conn.execute("UPDATE chat_log SET message=? WHERE is_bot_reply=1", (msg,))
        conn.commit()
        conn.close()
    assert sync.count_fork_diff(local, snap) == 0


def test_ancestor_relay_no_missing(bases):
    """正常接力：本机 = 祖先（旧），快照 = 后代（含全部 + 新消息）→ missing=0 放行。"""
    base, tmp = bases
    snap = _make_db(tmp / "snap.db",
                    _copy_rows(base) + _new_rows(12, 500, ["笔记本新消息"]))
    local = base  # 本机就是祖先
    assert sync.count_local_missing(local, snap) == 0
    assert sync.count_fork_diff(local, snap) == 0
