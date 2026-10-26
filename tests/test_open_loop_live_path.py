# -*- coding: utf-8 -*-
"""闸门：未闭合事件的落库必须接在**生产真正走的那条路**上（2026-10-25 第 12 轮）。

## 背景（这条闸门是为了防一次已经发生的假接线）

L2（提取→落库）当时把 open_loop 写库写进了 `handler._persist_extracted_items`，
而那个方法**生产零调用**（PRIVATE_METHOD_BASELINE 自己登记为 T 类）。
生产提取走的是 `Store.complete_extraction_job` → `_apply_extraction_item`，
那里只认识 identity/preference/habit/event/relationship/note/alias ——
`open_loop` 落到默认的 `"fact"`，`open_loops` 表 11145 个任务、**0 行**。

零调用闸门查不出这件事：它只问「这个符号有没有调用点」，不问
「那个调用点所在的方法可不可达」。所以这里**用行为测试钉住**：

1. 真跑一次 `complete_extraction_job`，看 `open_loops` 有没有行；
2. 反向对照：非 open_loop 条目不许碰这张表；
3. AST 闸门：`_apply_extraction_item` 必须调用 `_record_open_loop_if_needed`
   ——否则又被搬回某个死方法里，行为测试也救不了（它会一起变绿？不，会红，
   但那时已经晚了：本文件的第 1 条测试就是它变红的现场）。
"""

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _chat(store, qq_id, message, *, group_id="group-a", timestamp="2026-10-25 10:00:00"):
    chat_id = store.insert_chat(qq_id, message, group_id=group_id,
                                is_bot=False, timestamp=timestamp)
    return {"id": chat_id, "message": message, "timestamp": timestamp,
            "is_bot_reply": 0, "group_id": group_id}


def _open_loop_item(messages, value="明天要去面试", *, kind="plan",
                    expected_at="2026-10-26", evidence_ids=None):
    return {
        "type": "open_loop",
        "cognitive": "episodic",
        "value": value,
        "importance": 6,
        "confidence": 0.9,
        "claim_type": "stated",
        "evidence_quote": messages[0]["message"],
        "evidence_ids": (list(evidence_ids) if evidence_ids is not None
                         else [messages[0]["id"]]),
        "source_group_id": messages[0]["group_id"],
        "kind": kind,
        "expected_at": expected_at,
    }


def _run_job(store, qq_id, messages, items):
    """跑一次完整的生产落库路径，返回 (job_id, completed)。"""
    job = store.create_extraction_job(qq_id, messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    assert leased and leased["lease_token"]
    assert store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], items,
        {"protocol_ok": True, "outcome": "success_with_items"},
    ) is True
    return job["id"], store.complete_extraction_job(job["id"], origin="extracted")


def _loops(store, qq_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT id, topic, kind, expected_at, source_memory_id, status, "
            "asked_count FROM open_loops WHERE qq_id=? ORDER BY id",
            (qq_id,),
        ).fetchall()


# ══════════════════════════════════════════════════════════════════
# 1. 生产路径必须真的写进 open_loops
# ══════════════════════════════════════════════════════════════════
def test_open_loop_item_lands_in_open_loops_via_live_path(store):
    messages = [_chat(store, "30001", "我明天要去面试，有点紧张")]
    _job_id, completed = _run_job(store, "30001", messages,
                                  [_open_loop_item(messages)])

    assert completed["status"] == "done"
    rows = _loops(store, "30001")
    assert len(rows) == 1, "生产提取路径没有写 open_loops——L2 又变成假接线了"
    loop_id, topic, kind, expected_at, source_memory_id, status, asked = rows[0]
    assert topic == "明天要去面试"
    assert kind == "plan"
    assert expected_at == "2026-10-26"
    assert status == "open" and asked == 0
    assert source_memory_id > 0, "source_memory_id 必须指回证据记忆（证据链）"

    # 记忆侧：key 应为 event（她什么时候说过这话），不是 fact
    with store._connect() as conn:
        memory_key, memory_value = conn.execute(
            "SELECT key, value FROM memories WHERE id=?", (source_memory_id,)
        ).fetchone()
    assert memory_key == "event", f"open_loop 被记成了 {memory_key}"
    assert memory_value == "明天要去面试"


def test_non_open_loop_item_does_not_create_rows(store):
    """反向对照：普通条目不许往 open_loops 里写——否则整张表都是噪音。"""
    messages = [_chat(store, "30002", "我特别喜欢草莓蛋糕")]
    _run_job(store, "30002", messages, [{
        "type": "preference", "cognitive": "semantic", "value": "喜欢草莓蛋糕",
        "importance": 4, "confidence": 0.9, "claim_type": "stated",
        "evidence_quote": messages[0]["message"],
        "evidence_ids": [messages[0]["id"]],
        "source_group_id": messages[0]["group_id"],
    }])
    assert _loops(store, "30002") == []


def test_recompleting_same_job_does_not_duplicate(store):
    """任务已 done 时重放不许再建行（ready/done 的幂等边界）。"""
    messages = [_chat(store, "30003", "我明天要去体检")]
    item = _open_loop_item(messages, value="明天要去体检",
                           expected_at="2026-10-26")
    job_id, completed = _run_job(store, "30003", messages, [item])
    assert completed["status"] == "done"
    again = store.complete_extraction_job(job_id, origin="extracted")
    assert again["status"] == "done" and again["count"] == 0
    assert len(_loops(store, "30003")) == 1


# ══════════════════════════════════════════════════════════════════
# 2. 去重 / 隔离（薄薄一层，但错了就是重复骚扰或跨人串话）
# ══════════════════════════════════════════════════════════════════
def test_same_topic_again_updates_anchor_not_duplicate(store):
    first = [_chat(store, "30004", "下周三面试", timestamp="2026-10-25 10:00:00")]
    _run_job(store, "30004", first,
             [_open_loop_item(first, value="下周三面试",
                              expected_at="2026-10-29")])
    second = [_chat(store, "30004", "下周三面试，改到周四了",
                    timestamp="2026-10-25 11:00:00")]
    _run_job(store, "30004", second,
             [_open_loop_item(second, value="下周三面试",
                              expected_at="2026-10-30")])
    rows = _loops(store, "30004")
    assert len(rows) == 1, "同一件事被建成了两行（会变成重复追问）"
    assert rows[0][3] == "2026-10-30", "再次提到时应更新时间锚点"


def test_different_people_stay_isolated(store):
    a = [_chat(store, "30005", "明天要去面试")]
    b = [_chat(store, "30006", "明天要去面试")]
    _run_job(store, "30005", a, [_open_loop_item(a)])
    _run_job(store, "30006", b, [_open_loop_item(b)])
    assert len(_loops(store, "30005")) == 1
    assert len(_loops(store, "30006")) == 1


# ══════════════════════════════════════════════════════════════════
# 3. 无证据的悬事必须拒绝（fail-closed，与其它记忆同一条证据门）
# ══════════════════════════════════════════════════════════════════
def test_open_loop_without_evidence_is_rejected(store):
    messages = [_chat(store, "30007", "随便说点什么")]
    _run_job(store, "30007", messages,
             [_open_loop_item(messages, evidence_ids=[])])
    assert _loops(store, "30007") == []
    assert store.query_memories("30007") == []


# ══════════════════════════════════════════════════════════════════
# 4. AST 闸门：接线必须在活路径上
# ══════════════════════════════════════════════════════════════════
def _store_tree():
    source = (PROJECT_ROOT / "agent" / "store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return funcs


def test_live_extraction_path_calls_open_loop_writer():
    funcs = _store_tree()
    assert "_apply_extraction_item" in funcs
    calls = [n for n in ast.walk(funcs["_apply_extraction_item"])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_record_open_loop_if_needed"]
    assert calls, ("_apply_extraction_item 没有调用 _record_open_loop_if_needed"
                   "——open_loop 落库又不在生产路径上了")


def test_complete_extraction_job_uses_apply_item():
    """上游钉一下：complete_extraction_job 必须逐个条目走 _apply_extraction_item。

    否则即便 _apply_extraction_item 里有接线，实际落库换了路径也白搭。
    """
    funcs = _store_tree()
    assert "complete_extraction_job" in funcs
    calls = [n for n in ast.walk(funcs["complete_extraction_job"])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_apply_extraction_item"]
    assert calls, "complete_extraction_job 不再走 _apply_extraction_item"


def test_open_loop_is_not_mapped_to_fact():
    """open_loop 的 type_map 必须映到 event；映到 fact 就是把悬事记成事实。"""
    funcs = _store_tree()
    fn = funcs["_apply_extraction_item"]
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                pairs[key.value] = ast.unparse(value) if hasattr(ast, "unparse") else ""
        if "open_loop" in pairs:
            found = True
            assert pairs["open_loop"] == "'event'", (
                f"open_loop 映成了 {pairs['open_loop']}，应为 'event'")
    assert found, "type_map 里没有 open_loop —— 条目会落到默认 'fact'"
