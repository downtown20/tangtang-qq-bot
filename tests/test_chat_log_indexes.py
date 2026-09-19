"""chat_log 积压扫描索引契约与查询计划回归。"""

import sqlite3


def test_new_store_creates_covering_extraction_backlog_index(store):
    with sqlite3.connect(store.db_path) as conn:
        indexes = {
            row[1]: [item[2] for item in conn.execute(
                f"PRAGMA index_info({row[1]})"
            )]
            for row in conn.execute("PRAGMA index_list('chat_log')")
        }

    assert indexes["idx_chat_log_extraction_backlog"] == [
        "qq_id", "is_bot_reply", "quarantined_at", "id", "timestamp",
    ]


def test_extraction_backlog_query_uses_covering_index(store):
    query = """
        SELECT c.qq_id, COUNT(*), MIN(c.id), MAX(c.id),
               MIN(NULLIF(c.timestamp,''))
        FROM chat_log c
        LEFT JOIN extraction_cursors e
          ON e.pipeline='memory' AND e.direction='forward' AND e.qq_id=c.qq_id
        WHERE COALESCE(c.is_bot_reply,0)=0
          AND COALESCE(c.quarantined_at,'')=''
          AND c.id>COALESCE(e.cursor_chat_id,0)
        GROUP BY c.qq_id
        ORDER BY COALESCE(MIN(NULLIF(c.timestamp,'')),'9999-12-31'),
                 MIN(c.id), c.qq_id
    """
    with sqlite3.connect(store.db_path) as conn:
        plan = "\n".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + query
        ))

    assert "COVERING INDEX idx_chat_log_extraction_backlog" in plan


def test_new_store_creates_timeline_ordering_indexes(store):
    with sqlite3.connect(store.db_path) as conn:
        indexes = {
            row[1]: [item[2] for item in conn.execute(
                f"PRAGMA index_info({row[1]})"
            )]
            for row in conn.execute("PRAGMA index_list('chat_log')")
        }

    assert indexes["idx_chat_log_group_time"] == [
        "group_id", "timestamp", "id",
    ]
    assert indexes["idx_chat_log_group_user_time"] == [
        "group_id", "qq_id", "timestamp", "id",
    ]


def test_timeline_queries_use_ordering_indexes_without_temp_sort(store):
    group_sql = (
        "SELECT id FROM chat_log WHERE COALESCE(quarantined_at,'')='' "
        "AND group_id=? ORDER BY timestamp DESC,id DESC LIMIT 20"
    )
    speaker_sql = (
        "SELECT id FROM chat_log WHERE COALESCE(quarantined_at,'')='' "
        "AND group_id=? AND qq_id=? ORDER BY timestamp DESC,id DESC LIMIT 50"
    )
    with sqlite3.connect(store.db_path) as conn:
        group_plan = "\n".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + group_sql, ("g1",)
        ))
        speaker_plan = "\n".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + speaker_sql, ("g1", "u1")
        ))

    assert "idx_chat_log_group_time" in group_plan
    assert "USE TEMP B-TREE" not in group_plan
    assert "idx_chat_log_group_user_time" in speaker_plan
    assert "USE TEMP B-TREE" not in speaker_plan
