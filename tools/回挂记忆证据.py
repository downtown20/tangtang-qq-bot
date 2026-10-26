"""严格子串回挂：把 legacy 记忆挂回它的原话（P2 立项研究 · 第一期）。

背景
────
全库 74% 的记忆是 `legacy_unverified`：`evidence_ids` 是历史批处理留下的、没有
校验过的 CSV，于是它们被信任门挡在可信召回之外。原始聊天记录 `chat_log` 还在，
但这批记忆和原话之间**没有关系行**（`memory_evidence`），所以"数据在、原文在、
两者之间没有桥"。

本工具建第一座桥，且**只建最窄的那座**：
    仅当记忆的 `value`（规范化后）**原样出现在**同一个人某条非 bot 消息里，
    才写 `memory_evidence` 并走 `update_memory_evidence` 的受控提升。

为什么这么保守
──────────────
方案 B（LLM 辅助语义回挂）能覆盖改写/归纳类记忆，但它引入一个核心风险：
引文被定位到 ≠ 语义真的支持这条记忆。第一期先做零语义判断的这一半，
用一个**误判率理论为 0** 的判据建立工具链和基线数字——基线跑出来才知道
主体有多大、改写幅度有多大，二期才有对照。

六条不可让步的边界
──────────────────
1. **verdict 由既有校验器说了算**：本工具只提出候选，最终由
   `store.update_memory_evidence` → `_validate_memory_evidence` 校验
   （归属/作用域/引文支持）。工具里**不允许**有第二套放行判断。
2. 只在 `--apply` 时写；**默认 dry-run**，且 dry-run 的预测与 apply 结果用
   同一段判定代码，两边必须一致（`--report` 里两个数字可直接比对）。
3. 只处理 `origin='extracted'` 且 `status='active'` 的 legacy 行——受控提升
   本来就只对这类开放（`update_memory_evidence` 的前置条件）。
4. **同一句话在多个群出现过就不挂**（status=ambiguous）：那说明无法确定这条
   记忆来自哪个场景，而 `source_group_id` 会被这次回挂一并钉死，猜错就是
   给记忆安错来源。
5. 每条落库都留痕：`memory_revisions` 记 `actor=backfill:evidence:<批次>`，
   `memory_reverification_events` 记 `reviewer=backfill:substring:<批次>`。
   回滚按批次走，不需要人工翻日志。
6. **幂等**：候选查询排除已可信的行；`memory_evidence` 主键天然去重；
   回滚只看"当前仍是该批次提升的那一档"。

用法
────
    # 1) 先看基线（不写库）
    python tools/回挂记忆证据.py --db memory.db --report 回挂基线.json --sample 20

    # 2) 在副本上真跑
    python tools/回挂记忆证据.py --db 副本.db --apply --batch b1

    # 3) 按批次回滚
    python tools/回挂记忆证据.py --db 副本.db --rollback --batch b1

生产库需要 `--allow-production` 才允许 `--apply`（默认拒绝，防误操作）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.store import TRUSTED_TRUST_LEVELS, Store  # noqa: E402


#: 锚点最小长度（规范化后，按字符算）。低于这个长度的一句话（"喜欢猫"）在
#: 几万条闲聊里几乎必然出现，用它当证据等于没有证据。6 是经验下界，
#: 实测命中率对它的敏感度写在 --report 的 too_short 里，可以随时调。
MIN_ANCHOR_CHARS = 6
#: 记忆原文上限。超过 200 字的记忆是归纳产物，严格子串几乎不可能命中，
#: 扫它们只是浪费；也避免一条超长串拖慢整轮匹配。
MAX_ANCHOR_CHARS = 200

CQ_RE = re.compile(r"\[CQ:[^\]]*\]")

# ── 证据原话清洗（2026-10-24 语义回挂抽样暴露的问题）──
# chat_log 里存的**不全是用户原话**：有的行带着系统注入前缀（如
# 「【同时有 4 个人找你…】名字不能取太长别人会读 说：哪」）、图片/表情占位符、
# 引用占位符。这些文本被当成「原话证据」时会出现两种伤害：
#   ① 语义闸门被系统指令文本带偏（实测 mem#10211「是男性（爷们）」的"证据"
#      是那段「【…】」注入文本，第二次语义校验还说 yes）；
#   ② 占位符被当成事实（mem#1497「发图片、动画表情」的证据就是 `[图片:[动画表情]]`）。
# 政策：**清洗而不是丢弃整条**——引用前缀后面往往就是真实原话，丢掉可惜；
# 清洗后太短的才丢。
_SYS_BLOCK_RE = re.compile(r"【[^】]*】")
_PLACEHOLDER_RE = re.compile(
    r"\[图片\s*[:：]?\s*[^\]]*\]|\[表情\]|\[回复了上面的消息\]")


def clean_user_utterance(text: str) -> str:
    """去掉消息里的**非用户内容**（系统注入块、媒体/引用占位符）。

    只做删除，不做改写——任何"补全"都会让引文闸门失去意义
    （它比对的必须是原始文本里逐字存在的片段）。
    """
    cleaned = _SYS_BLOCK_RE.sub(" ", str(text or ""))
    cleaned = _PLACEHOLDER_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


#: 清洗后短于这个长度的消息不作为证据候选：剩下的多半是「哪」「嗯」这类残留。
MIN_CLEANED_CHARS = 4
#: 明细列表上限。大库全程 matched 可能是几千条，明细只留前 500 条够抽样核对；
#: 统计数字不受影响（那是逐条累加的）。
SAMPLE_LIMIT = 500


def _keep(bucket: list, item: dict) -> None:
    if len(bucket) < SAMPLE_LIMIT:
        bucket.append(item)


def _norm(text: str) -> str:
    """与 `Store._validate_memory_evidence` 完全同口径的规范化。

    必须一致：本工具用规范化后的子串关系找候选，校验器用同一套规范化判断
    "引文是否被原文支持"。两边口径一旦不同，dry-run 的预测就会和 apply 结果
    分家（那样 dry-run 就没有意义了）。
    """
    return " ".join(CQ_RE.sub("", str(text or "")).split())


def load_candidates(store: Store, qq: str = "", limit: int = 0,
                    min_importance: int = 0) -> list[dict]:
    """取出待回挂的 legacy 记忆（不动已验证的行 = 天然幂等）。

    `min_importance` 是给"小批次试跑"用的：先只补高重要度的那几百条，
    看落地质量和行为变化，再决定要不要铺到全量（立项文档方案 C 的执行顺序）。
    注意 importance 是提取器打分，高重要度里同样有大量琐碎对话——
    它筛的是"影响面"，不是"质量"，别把它当质量门槛。
    """
    marks = ",".join("'%s'" % level for level in sorted(TRUSTED_TRUST_LEVELS))
    conditions = [
        "COALESCE(status,'active')='active'",
        "COALESCE(origin,'')='extracted'",
        f"COALESCE(trust_level,'legacy_unverified') NOT IN ({marks})",
        "TRIM(COALESCE(value,''))<>''",
    ]
    params: list = []
    if min_importance > 0:
        conditions.append("COALESCE(importance,0)>=?")
        params.append(int(min_importance))
    if qq:
        conditions.append("qq_id=?")
        params.append(str(qq))
    sql = (
        "SELECT id, qq_id, COALESCE(value,'') AS value, "
        "       COALESCE(source_group_id,'') AS source_group_id, "
        "       COALESCE(origin,'') AS origin, "
        "       COALESCE(trust_level,'legacy_unverified') AS trust_level, "
        "       COALESCE(timestamp,'') AS timestamp "
        "FROM memories WHERE " + " AND ".join(conditions) + " ORDER BY qq_id, id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def load_messages(conn, qq_id: str) -> list[dict]:
    """该人所有**非 bot** 消息（只有对方说的原话才能给记忆当证据）。

    2026-10-24：返回前先清洗掉系统注入块与占位符（见 clean_user_utterance），
    清洗后过短的行整条丢弃——它们不可能是任何记忆的来源。
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, COALESCE(group_id,'') AS group_id, "
        "       COALESCE(timestamp,'') AS timestamp, "
        "       COALESCE(message,'') AS message "
        "FROM chat_log WHERE qq_id=? AND COALESCE(is_bot_reply,0)=0 "
        "ORDER BY id",
        (str(qq_id),),
    ).fetchall()
    out = []
    for r in rows:
        cleaned = clean_user_utterance(r["message"])
        if len(_norm(cleaned)) < MIN_CLEANED_CHARS:
            continue
        out.append({"id": int(r["id"]), "group_id": str(r["group_id"]),
                    "timestamp": str(r["timestamp"]), "message": cleaned,
                    "norm": _norm(cleaned)})
    return out


def _closest(hits: list[dict], when: str) -> dict:
    """同一条原话被说过多次时选哪一条：优先"记忆写入之前最近的一次"。

    记忆是提取器在某条消息之后写的，所以"写入前的最后一条同文消息"最可能是
    它的来源。没有更早的（时间戳缺失/倒挂）就取最早的一条。
    """
    if when:
        before = [m for m in hits if m["timestamp"] and m["timestamp"] <= when]
        if before:
            return max(before, key=lambda m: (m["timestamp"], m["id"]))
    return min(hits, key=lambda m: (m["timestamp"] or "9999", m["id"]))


def match_memory(memory: dict, messages: list[dict],
                 min_chars: int = MIN_ANCHOR_CHARS) -> dict:
    """纯函数：给定一条记忆和这个人的全部消息，判断能不能挂、挂哪条。

    返回 status ∈ {too_short, too_long, unmatched, ambiguous, matched}。
    **不做信任判断**——那只属于 `Store.update_memory_evidence`。

    `min_chars` 可调是为了做敏感度分析：阈值越高越保守、覆盖率越低，两个数字
    一起看才知道线该画在哪（报告里的 too_short 就是被它挡掉的量）。
    """
    value = _norm(memory.get("value"))
    if len(value) < min_chars:
        return {"status": "too_short", "anchor": value}
    if len(value) > MAX_ANCHOR_CHARS:
        return {"status": "too_long", "anchor": value}
    hits = [m for m in messages if len(m["norm"]) >= len(value) and value in m["norm"]]
    if not hits:
        return {"status": "unmatched", "anchor": value}

    declared_group = str(memory.get("source_group_id") or "")
    groups = {m["group_id"] for m in hits}
    if declared_group:
        scoped = [m for m in hits if m["group_id"] == declared_group]
        if not scoped:
            # 记忆声明了自己来自某个群，但那句话不在该群 → 不是同一条语境
            return {"status": "unmatched", "anchor": value, "reason": "group_mismatch"}
        hits = scoped
    elif len(groups) > 1:
        # 同一句话横跨多个场景：无法确定记忆来自哪个群，而回挂会把
        # source_group_id 一并钉死——猜错就是给记忆安错来源。放弃。
        return {"status": "ambiguous", "anchor": value, "groups": sorted(groups)}

    chosen = _closest(hits, str(memory.get("timestamp") or ""))
    return {
        "status": "matched",
        "anchor": value,
        "chat_id": int(chosen["id"]),
        "group_id": chosen["group_id"],
        "hit_count": len(hits),
        "chat_timestamp": chosen["timestamp"],
        "chat_message": chosen["message"][:300],
    }


def _predict_verdict(store: Store, memory: dict, plan: dict) -> tuple[bool, str]:
    """dry-run 用：直接问真实校验器"这条会不会过"，不另写一套判断。

    只用传入的现有连接做只读 SELECT，不产生任何写入。
    """
    with store._connect() as conn:
        rows = store._validate_memory_evidence(
            conn, str(memory.get("qq_id") or ""), str(memory.get("origin") or ""),
            "", str(plan.get("group_id") or ""), str(plan.get("chat_id")),
            evidence_quote=str(memory.get("value") or ""),
        )
    return bool(rows), ("" if rows else "validator_would_reject")


def run(store: Store, *, apply: bool, batch: str, qq: str, limit: int,
        min_chars: int = MIN_ANCHOR_CHARS,
        min_importance: int = 0) -> dict:
    candidates = load_candidates(store, qq=qq, limit=limit,
                                 min_importance=min_importance)
    stats = {"attempted": len(candidates), "matched": 0, "ambiguous": 0,
             "unmatched": 0, "too_short": 0, "too_long": 0,
             "landed": 0, "rejected_by_validator": 0, "by_qq": {}}
    samples: list[dict] = []
    rejected: list[dict] = []

    by_qq: dict[str, list[dict]] = {}
    for memory in candidates:
        by_qq.setdefault(str(memory.get("qq_id") or ""), []).append(memory)

    reviewer = f"backfill:substring:{batch}"
    actor = f"backfill:evidence:{batch}"

    for person, memories in by_qq.items():
        with store._connect() as conn:
            messages = load_messages(conn, person)
        person_stats = stats["by_qq"].setdefault(
            person, {"attempted": 0, "matched": 0, "landed": 0, "ambiguous": 0})
        for memory in memories:
            person_stats["attempted"] += 1
            plan = match_memory(memory, messages, min_chars)
            status = plan["status"]
            if status in ("too_short", "too_long", "unmatched"):
                stats[status] += 1
                continue
            if status == "ambiguous":
                stats["ambiguous"] += 1
                person_stats["ambiguous"] += 1
                _keep(samples, {"memory_id": memory["id"], **plan})
                continue

            stats["matched"] += 1
            person_stats["matched"] += 1
            entry = {
                "memory_id": memory["id"], "qq_id": person,
                "value": memory["value"], "chat_id": plan["chat_id"],
                "group_id": plan["group_id"], "hit_count": plan["hit_count"],
                "chat_timestamp": plan["chat_timestamp"],
                "chat_message": plan["chat_message"],
            }
            ok, why = _predict_verdict(store, memory, plan)
            if not ok:
                stats["rejected_by_validator"] += 1
                _keep(rejected, {**entry, "reason": why})
                continue
            if not apply:
                entry["dry_run"] = "would_land"
                _keep(samples, entry)
                continue
            landed = store.update_memory_evidence(
                int(memory["id"]), str(plan["chat_id"]), str(plan["group_id"]),
                str(memory["value"]),
                allow_legacy_promotion=True, reviewer=reviewer,
                review_reason=f"严格子串回挂：原文见 chat_log#{plan['chat_id']}",
                actor=actor,
            )
            if landed:
                stats["landed"] += 1
                person_stats["landed"] += 1
                entry["landed"] = True
                _keep(samples, entry)
            else:
                stats["rejected_by_validator"] += 1
                _keep(rejected, {**entry,
                                 "reason": "update_memory_evidence_rejected"})

    stats["samples"] = samples
    stats["rejected"] = rejected
    stats["sample_limit"] = SAMPLE_LIMIT
    stats["min_anchor_chars"] = int(min_chars)
    stats["min_importance"] = int(min_importance)
    stats["batch"] = batch
    stats["applied"] = bool(apply)
    stats["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return stats


def rollback(store: Store, *, batch: str, keep_evidence: bool) -> dict:
    """按批次撤销：把该批次提升的 trust_level 打回上一档，并删**本次插入的**证据行。

    只处理"当前仍是该批次提升的那一档"的行——重复执行天然幂等。

    删除条件带 `created_at = 本次复核时间`：`update_memory_evidence` 用同一个
    时间变量写 `memory_evidence.created_at` 和 `memory_reverification_events.reviewed_at`，
    所以它能精确圈定本批次插入的行。不加这个条件就会误删"回挂之前就存在的同
    (memory, chat) 关系行"（实测副本上 2038 条里有 8 条属于这种情况），
    那样回滚就不精确了。
    """
    reviewer = f"backfill:substring:{batch}"
    # 两个工具写不同的 reviewer 前缀：第一期（子串）backfill:substring:，
    # 第二期（语义）backfill:semantic:。回滚按**批次号**定位，所以两种都认——
    # 否则语义批次一写就撤不掉（本函数上一版只认 substring，而语义工具里写着
    # "同一套回滚"，那正是注释与代码分家，2026-10-24 修）。
    reviewers = (reviewer, f"backfill:semantic:{batch}")
    marks = ",".join("'%s'" % level for level in sorted(TRUSTED_TRUST_LEVELS))
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT e.memory_id, e.evidence_ids, e.new_trust_level, e.reviewed_at "
            "FROM memory_reverification_events e "
            "JOIN memories m ON m.id=e.memory_id "
            "WHERE e.reviewer IN (?,?) "
            "  AND COALESCE(e.previous_trust_level,'') NOT IN (" + marks + ") "
            "  AND m.trust_level=e.new_trust_level",
            reviewers,
        ).fetchall()
    result = {"batch": batch, "candidates": len(rows), "rolled_back": 0,
              "evidence_removed": 0, "evidence_ids_restored": 0,
              "source_group_restored": 0, "failed": 0, "at":
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    actor = f"backfill:evidence:{batch}"
    restore_fields = {
        "evidence_ids": "evidence_ids_restored",
        "source_group_id": "source_group_restored",
    }
    for memory_id, evidence_ids, _new_trust, reviewed_at in rows:
        ok = store.rollback_memory_reverification(
            int(memory_id), reviewer=reviewer,
            review_reason=f"按批次撤销回挂：{batch}",
            actor=f"backfill:rollback:{batch}",
        )
        if not ok:
            result["failed"] += 1
            continue
        result["rolled_back"] += 1
        ids = [int(token) for token in str(evidence_ids or "").split(",")
               if token.strip().isdigit() and int(token) > 0]
        if not keep_evidence and ids:
            placeholders = ",".join("?" * len(ids))
            with store._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM memory_evidence WHERE memory_id=? "
                    f"AND chat_id IN ({placeholders}) AND created_at=?",
                    (int(memory_id), *ids, str(reviewed_at or "")),
                )
                conn.commit()
                result["evidence_removed"] += int(cursor.rowcount or 0)
        # 还原被这次回挂改写的字段：`rollback_memory_reverification` 只降
        # trust_level，**不动** evidence_ids / source_group_id。不还原会留下
        # 两种不一致：
        #   · 「legacy 记忆却挂着证据 CSV」（实测副本回滚后 memories_with_evidence
        #     停在 7285，基线 5256）；
        #   · 「legacy 记忆被钉在某个群里」（回挂会顺带确定 scope，实测 411 条里
        #     有 361 条原 scope 为空）。
        # 本批次写过的流水里 before_value 就是改前的值，按它恢复最准确。
        with store._connect() as conn:
            changed = False
            for field, counter in restore_fields.items():
                previous = conn.execute(
                    "SELECT before_value FROM memory_revisions "
                    "WHERE entity='memories' AND entity_id=? AND field=? "
                    "AND actor=? ORDER BY id DESC LIMIT 1",
                    (str(memory_id), field, actor),
                ).fetchone()
                if previous is None:
                    continue
                conn.execute(
                    f"UPDATE memories SET {field}=? WHERE id=?",
                    (str(previous[0] or ""), int(memory_id)),
                )
                changed = True
                result[counter] += 1
            if changed:
                conn.commit()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="严格子串回挂 legacy 记忆的证据（默认 dry-run）")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预演）")
    parser.add_argument("--rollback", action="store_true", help="按批次撤销")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许 --apply 作用于仓库根的 memory.db")
    parser.add_argument("--batch", default="", help="批次号（回滚按它定位）")
    parser.add_argument("--qq", default="", help="只处理某个人")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条候选")
    parser.add_argument("--sample", type=int, default=0, help="额外打印多少条抽样明细")
    parser.add_argument("--keep-evidence", action="store_true",
                        help="回滚时保留 memory_evidence 关系行（只降 trust_level）")
    parser.add_argument("--report", default="", help="把统计写成 JSON")
    parser.add_argument("--min-anchor-chars", type=int, default=MIN_ANCHOR_CHARS,
                        help="锚点最小长度（默认 %d）。调高=更保守，用于敏感度分析"
                             % MIN_ANCHOR_CHARS)
    parser.add_argument("--min-importance", type=int, default=0,
                        help="只处理 importance>=N 的记忆（0=不限）。小批次试跑用")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2
    batch = str(args.batch or "").strip()
    if not batch:
        batch = datetime.now().strftime("%Y%m%d%H%M%S")
    production = db_path == (PROJECT_ROOT / "memory.db").resolve()
    if args.apply and production and not args.allow_production:
        print(f"{db_path} 是生产库。先备份，或显式加 --allow-production。",
              file=sys.stderr)
        return 2
    if args.apply and args.rollback:
        print("--apply 与 --rollback 不能同时用", file=sys.stderr)
        return 2

    store = Store(str(db_path))
    if args.rollback:
        result = rollback(store, batch=batch, keep_evidence=args.keep_evidence)
    else:
        result = run(store, apply=args.apply, batch=batch,
                     qq=args.qq, limit=args.limit,
                     min_chars=args.min_anchor_chars,
                     min_importance=args.min_importance)

    if args.sample and result.get("samples"):
        # 抽样只打印人类可核对的最小信息：记忆原文 ↔ 原话，一眼能判断是不是同一条
        shown = result["samples"][: max(0, int(args.sample))]
        for index, item in enumerate(shown, 1):
            print(f"--- 抽样 {index} 记忆 #{item['memory_id']} (qq={item.get('qq_id','')})")
            print(f"    记忆原文：{item.get('value', '')}")
            print(f"    原话#{item.get('chat_id')}（{item.get('chat_timestamp', '')}"
                  f"，group={item.get('group_id') or '私聊'}）："
                  f"{item.get('chat_message', '')}")

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
        print(f"报告已写入 {args.report}")
    summary = {key: value for key, value in result.items()
               if key not in ("samples", "rejected", "by_qq")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
