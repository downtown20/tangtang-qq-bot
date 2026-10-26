"""语义回挂：严格子串挂不上的记忆，用 LLM 找原话（P2 立项研究 · 第二期）。

为什么需要它
────────────
第一期（严格子串）在存量上只覆盖 22.7%：剩下 6,129 条 unmatched 的记忆
**被提取器改写/归纳过**，原文不在任何一条消息里逐字出现（「喜欢看番茄小说上的
流行小说」← 原话可能是「番茄那个小说榜我看了一圈」）。这类只能让 LLM 判断
「这条记忆是不是由这句原话得来的」。

双闸门（本工具的全部安全性所在）
────────────────────────────────
1. **引文闸门（LLM 自己）**：要求它返回 `chat_id` + 一个能在原话里**逐字定位**的
   `quote`。编造的引文在下一步会被抓。
2. **校验器闸门（既有代码）**：`store.update_memory_evidence(...)` →
   `_validate_memory_evidence` 校验 quote 真的是那条 chat_log 的子串、且归属/作用域
   都对。**本工具不自己放行任何一条**——它只提出候选。
3. **语义闸门（第二次独立调用）**：另起一次调用只问「这句原话是否足以支持这条
   记忆的断言？yes/no」。两次独立调用比一次「请判断并给出引文」更不容易自洽地
   编造（一次调用里 LLM 会为自己的选择找理由）。

三条不能让步
────────────
· 只处理 `origin='extracted'`、`status='active'`、且**严格子串已确认挂不上**的行
  （unmatched / too_short / too_long）——能严格挂上的走第一期，不需要 LLM。
· 默认 dry-run；`--apply` 才写；生产库要 `--allow-production`。
· 每条落库都带 `reviewer=backfill:semantic:<批次>` + `actor=backfill:evidence:<批次>`，
  可用第一期的 `--rollback` 同一套回滚（`回挂记忆证据.py --rollback --batch <批次>`
  只认 reviewer，语义批次也能撤销）。

用法
────
    python tools/语义回挂证据.py --min-importance 9          # 看规模与抽样
    python tools/语义回挂证据.py --apply --allow-production --min-importance 9 --batch s1
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import jieba

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.store import Store  # noqa: E402


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 复用第一期工具：候选选择、消息加载、严格子串判定（避免两套口径）
BACKFILL = _load_module("evidence_backfill", "tools/回挂记忆证据.py")
REBIRTH = _load_module("profile_rebirth", "tools/重生legacy画像.py")

CANDIDATE_TOP_K = 5
MIN_CANDIDATE_CHARS = 6
CONCURRENCY = 8


def _terms(text: str) -> list[str]:
    """记忆原文的实词（长度≥2，去掉纯虚词/数字串）。"""
    out = []
    for token in jieba.cut(str(text or "")):
        token = token.strip()
        if len(token) >= 2 and any(
                ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in token):
            out.append(token)
    return list(dict.fromkeys(out))


def recall_candidates(memory: dict, messages: list[dict],
                      top_k: int = CANDIDATE_TOP_K) -> list[dict]:
    """从该人的消息里召回「可能对应这条记忆」的几句原话。

    打分为「记忆实词在该条消息里命中的个数」，同分时优先**记忆写入时间之前**
    且**时间距离近**的那条（记忆是提取器在某条消息之后写的）。纯本地计算，
    不调 LLM——这是把 6,000 条候选压到 LLM 能承受的规模的关键一步。
    """
    value = BACKFILL._norm(memory.get("value"))
    terms = _terms(value)
    if not terms:
        return []
    when = str(memory.get("timestamp") or "")
    scored = []
    for message in messages:
        text = message["norm"]
        if len(text) < MIN_CANDIDATE_CHARS:
            continue
        hit = sum(1 for term in terms if term in text)
        if not hit:
            continue
        # 时间距离：优先"记忆写入之前最近的一条"，无时间戳的排后面
        ts = message["timestamp"] or ""
        if when and ts and ts <= when:
            distance = 0
        elif when and ts:
            distance = 1
        else:
            distance = 2
        scored.append((hit, -distance, len(text), message))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [item[3] for item in scored[:top_k]]


EXTRACT_SYSTEM = (
    "你是记忆审计员。给定一条「记忆」和若干「候选原话」，判断这条记忆是否"
    "由其中某一句得来的。\n"
    "只输出一个 JSON 对象，不要解释：\n"
    '{"chat_id": <候选编号整数>, "quote": "<候选原话里能支持该记忆的连续片段>", '
    '"supported": true 或 false}\n'
    "规则：\n"
    "- quote 必须是从那句候选原话里**逐字复制**的连续片段，不许改写、不许拼接。\n"
    "- quote 必须真的能支持记忆的断言；只是主题相关不算支持。\n"
    "- 找不到能支持的原话就输出 {\"chat_id\": 0, \"quote\": \"\", \"supported\": false}。"
)

VERIFY_SYSTEM = (
    "只回答 yes 或 no，不要任何其他文字。\n"
    "问题：给出的原话是否**直接陈述**了这条记忆要断言的具体信息？\n"
    "判断标准（严格，宁可 no）：\n"
    "- yes：原话本身就说出了记忆里的那个具体信息（同一件事、同一主体、同一事实），"
    "不需要任何额外推断或联想。\n"
    "- no：只是话题相关、只是对同一个人的泛泛描述、需要多步推断才能得出结论、"
    "原话是系统提示/占位符/表情符号、或者原话讲的是另一件事。\n"
    "- 记忆是**归纳性描述**（例如「他是个刚满20岁的调酒师，17岁辍学，喜欢调酒」）"
    "而原话只提到其中一个碎片甚至无关时，一律 no。\n"
    "记住：这条原话会成为「记忆有证据」的凭据，宽松一次就等于给一条无据记忆发了"
    "可信等级。"
)


def build_extract_prompt(memory: dict, candidates: list[dict]) -> str:
    lines = ["【记忆】" + str(memory.get("value") or "")]
    lines.append("【候选原话】")
    for index, message in enumerate(candidates, 1):
        lines.append(f"{index}. {message['message'][:400]}")
    return "\n".join(lines)


def build_verify_prompt(memory: dict, quote: str, message: str) -> str:
    return (
        "【记忆】" + str(memory.get("value") or "") + "\n"
        "【原话】" + str(message or "")[:600] + "\n"
        "【要核对的支持片段】" + str(quote or "") + "\n"
        "这句原话是否足以支持这条记忆的断言？"
    )


def _parse_extract(raw: str) -> dict:
    text = str(raw or "").strip()
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"supported": False, "reason": "unparseable"}
    return {
        "supported": bool(data.get("supported")),
        "chat_id": int(data.get("chat_id") or 0),
        "quote": str(data.get("quote") or "").strip(),
    }


async def judge_one(memory: dict, messages: list[dict], llm_call,
                    semaphore: asyncio.Semaphore) -> dict:
    # 2026-10-24 并发修复：候选召回是**同步 CPU 密集**的（每条记忆遍历该人全部
    # 消息，实测有人 17,612 条消息 × 74 条候选），直接写在协程里会**阻塞事件循环**
    # ——8 并发名存实亡（实测 12 分钟处理不到 50 条，与串行同速）。挪到线程池后
    # 这层才真的并行。
    candidates = await asyncio.to_thread(recall_candidates, memory, messages)
    if not candidates:
        return {"status": "no_candidate"}
    async with semaphore:
        try:
            first = await llm_call(EXTRACT_SYSTEM,
                                   build_extract_prompt(memory, candidates))
        except Exception as exc:
            return {"status": "llm_error", "reason": str(exc)[:120]}
        picked = _parse_extract(first)
        if not picked["supported"] or not picked["quote"]:
            return {"status": "llm_declined", "parsed": picked}
        chat_id = picked["chat_id"]
        if not 1 <= chat_id <= len(candidates):
            return {"status": "bad_chat_id", "chat_id": chat_id}
        chosen = candidates[chat_id - 1]
        quote = picked["quote"]
        # 引文必须逐字出现在选中的那句原话里——不出现就是编的，直接判失败
        if BACKFILL._norm(quote) not in chosen["norm"]:
            return {"status": "quote_not_in_message", "quote": quote[:80]}
        try:
            second = await llm_call(
                VERIFY_SYSTEM, build_verify_prompt(memory, quote, chosen["message"]))
        except Exception as exc:
            return {"status": "llm_error", "reason": str(exc)[:120]}
        verdict = str(second or "").strip().lower()
        if not verdict.startswith("yes"):
            return {"status": "semantic_rejected", "verdict": verdict[:40]}
    return {
        "status": "accepted",
        "chat_id": int(chosen["id"]),
        "group_id": str(chosen["group_id"] or ""),
        "quote": quote,
        "chat_message": chosen["message"][:200],
        "chat_timestamp": chosen["timestamp"],
    }


async def run_semantic(store: Store, *, apply: bool, batch: str,
                       min_importance: int, limit: int,
                       min_anchor_chars: int, qq: str = "",
                       concurrency: int = CONCURRENCY) -> dict:
    candidates = BACKFILL.load_candidates(store, qq=qq,
                                          min_importance=min_importance)
    stats = {"attempted": 0, "no_candidate": 0, "llm_declined": 0,
             "quote_not_in_message": 0, "semantic_rejected": 0,
             "bad_chat_id": 0, "llm_error": 0,
             "validator_rejected": 0, "landed": 0, "skipped_matched": 0,
             "batch": batch, "applied": bool(apply),
             "min_importance": int(min_importance),
             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    samples: list[dict] = []

    llm_config = REBIRTH.load_llm_config()
    if not llm_config.get("api_key"):
        raise SystemExit("找不到 llm.api_key（config.yaml 与 .env 都没有）")
    llm_call = REBIRTH.make_llm_call(llm_config)
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    by_qq: dict[str, list[dict]] = {}
    for memory in candidates:
        by_qq.setdefault(str(memory.get("qq_id") or ""), []).append(memory)

    progress = {"done": 0}

    async def _handle_one(memory: dict, messages: list[dict]) -> None:
        # 严格子串能挂上的不进本工具（那是第一期的活，零误判）
        # 与 judge_one 同理：严格子串匹配也是全量遍历消息，必须离开事件循环。
        plan = await asyncio.to_thread(
            BACKFILL.match_memory, memory, messages, min_anchor_chars)
        if plan["status"] == "matched":
            stats["skipped_matched"] += 1
            return
        stats["attempted"] += 1
        result = await judge_one(memory, messages, llm_call, semaphore)
        status = result["status"]
        if status != "accepted":
            stats[status] = stats.get(status, 0) + 1
            return
        entry = {"memory_id": memory["id"], "qq_id": memory.get("qq_id"),
                 "value": memory.get("value"), **{k: v for k, v in result.items()
                                                  if k != "status"}}
        if len(samples) < 500:
            samples.append(entry)
        if not apply:
            stats["landed"] += 0  # dry-run 只统计"会落地"
            stats["would_land"] = stats.get("would_land", 0) + 1
            return
        landed = store.update_memory_evidence(
            int(memory["id"]), str(result["chat_id"]), str(result["group_id"]),
            str(result["quote"]),
            allow_legacy_promotion=True,
            reviewer=f"backfill:semantic:{batch}",
            review_reason="语义回挂：LLM 提取候选 + 二次语义确认 + 引文逐字定位",
            actor=f"backfill:evidence:{batch}",
        )
        if landed:
            stats["landed"] += 1
            entry["landed"] = True
        else:
            stats["validator_rejected"] += 1

    async def handle(memory: dict, messages: list[dict]) -> None:
        """带进度输出的包装——长跑时能看出在动。

        2026-10-24 的教训：一次 20 并发的全量跑，14 分钟内没有任何输出，
        我据此误判「工具卡住」并杀掉了 job。进度行是给下一次的自己看的。
        """
        try:
            await _handle_one(memory, messages)
        finally:
            progress["done"] += 1
            if progress["done"] % 50 == 0:
                print(f"  ...已处理 {progress['done']} 条"
                      f"（attempted {stats['attempted']} / "
                      f"landed {stats['landed']} / "
                      f"would_land {stats.get('would_land', 0)}）", flush=True)

    for person, memories in by_qq.items():
        with store._connect() as conn:
            messages = BACKFILL.load_messages(conn, person)
        batch_slice = memories[:limit] if limit else memories
        if not messages:
            # 只统计本批要处理的条数，**不是**这个人的全部记忆。
            # 2026-10-24 诊断时踩到：原实现对 memories 全量计数，被 --limit 放大，
            # 出现 no_candidate(69) > attempted(86) 这种不可能的比例，
            # 一度让我误判「语义回挂几乎无效」。
            stats["no_candidate"] += len(batch_slice)
            continue
        await asyncio.gather(*[handle(memory, messages) for memory in batch_slice])

    stats["samples"] = samples
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="语义回挂 legacy 记忆的证据（默认 dry-run，双闸门）")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--batch", default="")
    parser.add_argument("--qq", default="")
    parser.add_argument("--limit", type=int, default=0,
                        help="每人最多处理多少条（控制成本）")
    parser.add_argument("--min-importance", type=int, default=9)
    parser.add_argument("--min-anchor-chars", type=int,
                        default=BACKFILL.MIN_ANCHOR_CHARS)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY,
                        help=f"并发数（默认 {CONCURRENCY}）。⚠ 别盲调高：每条落地都是一次"
                             "SQLite 写事务，而 bot 也在写同一个库——2026-10-24 实测"
                             "并发 20 反而更慢（抢写锁）。调之前先测单条延迟")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2
    production = db_path == (PROJECT_ROOT / "memory.db").resolve()
    if args.apply and production and not args.allow_production:
        print("生产库请先备份，或显式加 --allow-production", file=sys.stderr)
        return 2
    batch = str(args.batch or "").strip() or time.strftime("%Y%m%d%H%M%S")

    store = Store(str(db_path))
    started = time.time()
    stats = asyncio.run(run_semantic(
        store, apply=args.apply, batch=batch,
        min_importance=args.min_importance, limit=args.limit,
        min_anchor_chars=args.min_anchor_chars, qq=args.qq,
        concurrency=args.concurrency))
    stats["elapsed_s"] = round(time.time() - started, 1)

    summary = {k: v for k, v in stats.items() if k != "samples"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(
            json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
