"""千人长期记忆验收：临时 SQLite，绝不读写生产 memory.db。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.memory import MemorySystem
from agent.store import Store


class _EmbedStub:
    ready = True

    @staticmethod
    def encode(_text):
        return np.asarray([1.0, 0.0], dtype=np.float32)

    @staticmethod
    def similarity(left, right):
        return float(np.dot(
            np.asarray(left, dtype=np.float32),
            np.asarray(right, dtype=np.float32),
        ))


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _insert_verified(store: Store, qq_id: str, group_id: str, value: str,
                     *, retention: str = "normal", vector=None) -> tuple[int, int]:
    chat_id = store.insert_chat(
        qq_id, value, group_id=group_id, is_bot=False,
        timestamp="2026-08-20 10:00:00",
    )
    memory_id = store.insert_memory(
        qq_id, "fact", value, importance=6, cognitive="semantic",
        confidence=0.95, origin="extracted", source_group_id=group_id,
        evidence_ids=str(chat_id), retention=retention,
        event_time="2026-08-20 10:00:00",
        evidence_quote=value, claim_type="stated",
    )
    if vector is not None:
        store.set_embedding(memory_id, np.asarray(vector, dtype=np.float32))
    return chat_id, memory_id


def _seed_verified_fixture(store: Store, user_count: int) -> dict[str, dict]:
    """用一个事务装载容量夹具；真实写入路径仍由预检覆盖。

    容量验收关注的是 1,000 用户的持久存储、作用域隔离和召回，而不是
    4,000 次 SQLite 建连/提交的基准。所有夹具字段沿用 ``insert_memory``
    已验证的 extracted + quote/stated 可信契约。
    """
    frozen_messages: dict[str, dict] = {}
    timestamp = "2026-08-20 10:00:00"
    ingested_at = "2026-08-20 10:00:00"
    vectors = {
        "target": np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
        "daily": np.asarray([0.0, 1.0], dtype=np.float32).tobytes(),
    }
    with store._connect() as conn:
        for index in range(user_count):
            qq_id = f"u{index:04d}"
            current_group = f"group-{index % 10}"
            other_group = f"group-{(index + 1) % 10}"
            rows = [
                (current_group, f"{qq_id}只在当前群确认养过星云鹦鹉", "permanent", "target"),
                (current_group, f"{qq_id}当前群普通日常", "normal", "daily"),
                (other_group, f"{qq_id}其他群秘密", "normal", "target"),
                ("", f"{qq_id}私聊秘密", "normal", "target"),
            ]
            for row_index, (group_id, value, retention, vector_kind) in enumerate(rows):
                chat = conn.execute(
                    "INSERT INTO chat_log (qq_id,group_id,is_bot_reply,message,timestamp) "
                    "VALUES (?,?,?,?,?)",
                    (qq_id, group_id, 0, value, timestamp),
                )
                chat_id = int(chat.lastrowid)
                memory = conn.execute(
                    "INSERT INTO memories (qq_id,key,value,timestamp,importance,cognitive,"
                    "confidence,origin,status,target_qq,source_group_id,evidence_ids,"
                    "trust_level,retention,event_time,ingested_at,valid_from,valid_to,"
                    "superseded_by,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qq_id, "fact", value, timestamp, 6, "semantic", 0.95,
                     "extracted", "active", "", group_id, str(chat_id), "verified",
                     retention, timestamp, ingested_at, "", "", None, ""),
                )
                memory_id = int(memory.lastrowid)
                conn.execute(
                    "INSERT INTO memory_evidence (memory_id,chat_id,relation,created_at) "
                    "VALUES (?,?,'supports',?)",
                    (memory_id, chat_id, ingested_at),
                )
                conn.execute(
                    "INSERT INTO memory_embeddings (memory_id,embedding) VALUES (?,?)",
                    (memory_id, vectors[vector_kind]),
                )
                if row_index == 0:
                    frozen_messages[qq_id] = {
                        "id": chat_id, "message": value, "timestamp": timestamp,
                        "is_bot_reply": 0, "group_id": group_id,
                    }
    return frozen_messages


def run_acceptance(user_count: int = 1000, recall_samples: int = 200) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="tangtang-memory-acceptance-") as tmp:
        db_path = Path(tmp) / "acceptance.db"
        store = Store(str(db_path))
        memory = MemorySystem(store=store)
        embed = _EmbedStub()
        setup_started = time.perf_counter()
        # 先走一次生产写入路径，锁住 quote→verified 与 embedding 契约。
        _insert_verified(
            store, "preflight", "group-preflight", "预检用户确认养过星云鹦鹉",
            retention="permanent", vector=[1.0, 0.0],
        )
        frozen_messages = _seed_verified_fixture(store, user_count)
        setup_seconds = time.perf_counter() - setup_started

        isolation_failures = []
        isolation_latencies = []
        for index in range(user_count):
            qq_id = f"u{index:04d}"
            current_group = f"group-{index % 10}"
            check_started = time.perf_counter()
            group_rows = store.query_memories(
                qq_id, trusted_only=True, source_group_id=current_group,
            )
            private_rows = store.query_memories(
                qq_id, trusted_only=True, source_group_id="",
            )
            isolation_latencies.append((time.perf_counter() - check_started) * 1000)
            group_values = "\n".join(row["value"] for row in group_rows)
            private_values = "\n".join(row["value"] for row in private_rows)
            if "其他群秘密" in group_values or "私聊秘密" in group_values:
                isolation_failures.append(f"{qq_id}:group")
            if "其他群秘密" in private_values or "当前群" in private_values:
                isolation_failures.append(f"{qq_id}:private")

        memory.recall(
            "u0000", limit=3, query_text="养过什么宠物", embed_engine=embed,
            source_group_id="group-0",
        )
        recall_latencies = []
        recall_failures = []
        for index in range(min(user_count, max(1, recall_samples))):
            qq_id = f"u{index:04d}"
            group_id = f"group-{index % 10}"
            recall_started = time.perf_counter()
            recalled = memory.recall(
                qq_id, limit=3, query_text="养过什么宠物", embed_engine=embed,
                source_group_id=group_id,
            )
            recall_latencies.append((time.perf_counter() - recall_started) * 1000)
            values = "\n".join(item.value for item in recalled)
            if "星云鹦鹉" not in values or "其他群秘密" in values:
                recall_failures.append(qq_id)

        # 队列状态机由有界样本覆盖；千人目标在于可存储/可隔离召回，
        # 不是人为制造 1,000 次串行 SQLite 提交来测验收工具。
        queue_samples = min(user_count, 100)
        queue_started = time.perf_counter()
        for qq_id, message in list(frozen_messages.items())[:queue_samples]:
            job = store.create_extraction_job(qq_id, [message], direction="forward")
            leased = store.lease_extraction_job(job["id"])
            if not leased:
                raise AssertionError(f"job {job['id']} cannot be leased")
            if not store.mark_extraction_job_ready(
                    job["id"], leased["lease_token"], [],
                    {"protocol_ok": True, "outcome": "success_empty"}):
                raise AssertionError(f"job {job['id']} cannot become ready")
            store.complete_extraction_job(job["id"], origin="extracted")
        queue_seconds = time.perf_counter() - queue_started
        queue_health = store.get_extraction_queue_health()

        permanent_rows = store.query_memories(
            "u0000", trusted_only=True, source_group_id="group-0",
        )
        permanent = next(
            row for row in permanent_rows if row["retention"] == "permanent"
        )
        assertions = {
            "isolation_failures": len(isolation_failures),
            "recall_failures": len(recall_failures),
            "queue_open": int(queue_health.get("total_open", 0) or 0),
            "queue_done": int(queue_health.get("done", 0) or 0),
            "permanent_retained": permanent["retention"] == "permanent",
            "event_time_preserved": permanent["event_time"] == "2026-08-20 10:00:00",
        }
        passed = (
            assertions["isolation_failures"] == 0
            and assertions["recall_failures"] == 0
            and assertions["queue_open"] == 0
            and assertions["queue_done"] == queue_samples
            and assertions["permanent_retained"]
            and assertions["event_time_preserved"]
        )
        return {
            "passed": passed,
            "users": user_count,
            "database_bytes": db_path.stat().st_size,
            "setup_seconds": round(setup_seconds, 3),
            "queue_jobs_per_second": round(queue_samples / max(queue_seconds, 0.001), 2),
            "queue_samples": queue_samples,
            "isolation_ms": {
                "p50": round(statistics.median(isolation_latencies), 3),
                "p95": round(_percentile(isolation_latencies, 0.95), 3),
                "max": round(max(isolation_latencies), 3),
            },
            "recall_ms": {
                "samples": len(recall_latencies),
                "p50": round(statistics.median(recall_latencies), 3),
                "p95": round(_percentile(recall_latencies, 0.95), 3),
                "max": round(max(recall_latencies), 3),
            },
            "assertions": assertions,
            "total_seconds": round(time.perf_counter() - started, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--recall-samples", type=int, default=200)
    parser.add_argument(
        "--output", default="", metavar="PATH",
        help="将可复跑验收结果写入 JSON；不传则仅输出到终端",
    )
    args = parser.parse_args()
    result = run_acceptance(
        user_count=max(1, args.users),
        recall_samples=max(1, args.recall_samples),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        result["report_path"] = str(output)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
