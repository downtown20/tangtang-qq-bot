"""提取抽样 —— 验证 L2「未闭合事件」识别的**真实质量**（只读，不落任何库）。

## 为什么要有这个工具

`tests/test_open_loop_extraction.py` 证明的是**代码管路**：prompt 里有规则、
校验器透传字段、落库调用了 `add_open_loop`。它证明不了**LLM 判得对不对**——
而 open_loops 的错误代价很高：判错 = 她追着问一件已经收了尾的事（骚扰机器），
或者把「以后想环游世界」这种空泛愿望当悬事（每轮都冒出来）。

方案（docs/开发规划/未闭合事件_技术方案_20261024.md §三 L2）的验收就是：
**用真实历史消息重跑一批，人工抽样判定对不对**。

## 2026-10-25 第 12 轮修正：这把尺子自己撒了谎

旧版本的 `llm_call` 直接复用 `tools/重生legacy画像.py` 的 `make_llm_call`——
那条调用**没有关思考**。推理模型把预算烧在思考上，回复 `content` 是空串，
提取器把空串记成 `invalid_json`；而本工具只在 `loops` 为空时打印
「（这一批没有识别出未闭合事件）」——**「调用失败」被显示成「没有未闭合事件」**，
最后汇总成「合计识别出 0 条未闭合事件」。零批失败、零提示、退出码 0。

实测（同一条 prompt、同一批消息，只差 `thinking` 一个参数）：

    thinking 默认      → stats={"outcome":"invalid_json","raw_length":0}
    thinking=disabled  → stats={"outcome":"success_with_items","raw":5,...}

生产 `handler._do_extract_memories` 传的是 `extra_body=THINKING_OFF`，所以本工具
从诞生起就在**用一条生产不走的调用做验收**。换句话说：L2 的验收从未真正执行过
——它每次都"通过"了，因为它每次都什么都没看见（教训 #37 同族：尺子必须能红）。

修正四处（每一条都对应上面的证据）：

1. 本文件自建 `make_llm_call()`，显式 `"thinking": {"type": "disabled"}`——
   与生产同一条请求口径（闸门 `test_tool_llm_call_disables_thinking` 钉住）；
2. `judge()` 把 `protocol_ok=False` 的批次单独判负：**失败不许伪装成"没有"**，
   全失败或部分失败的退出码是 2，而不是 0；
3. `--cases` 阳性对照：内置 4 条必提样本 + 2 条不该提样本，先证明尺子能看见
   open_loop，再用它去量真实数据。`--future` 则解决"随机抽到的都是表情包"——
   只抽带将来时词汇的真实消息，L2 才有可能被测到；
4. **预算也必须同口径**：第一版把 `max_tokens` 写死 900，第一次跑 `--future`
   就撞上「回复 2258 字符 → JSON 被截断 → invalid_json → 判负」，而生产的
   `_call_deepseek` 用的是 `config.yaml` 的 `llm.max_tokens`（本仓 **64000**）。
   写死的小预算会让工具在长批次上**假红**——两个方向的失真都要修。
   现在走 `resolve_max_tokens()`，闸门 `TestRequestBudget` 钉住。

## 用法

    python tools/提取抽样.py                      # 默认 4 个人 × 20 条（未处理消息）
    python tools/提取抽样.py --future             # 只抽带将来时词汇的真实消息
    python tools/提取抽样.py --cases              # 阳性对照（必须 PASS）
    python tools/提取抽样.py --users 6 --limit 20
    python tools/提取抽样.py --qq 12345678        # 只看某个人

退出码：0 = 判据通过；2 = 尺子失效 / 有批次没拿到回复（**不等于**"没有未闭合事件"）。

**只读**：不调用 `_persist_extracted_items`、不写 memories、不写 open_loops。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.memory import MemorySystem          # noqa: E402
from agent.store import Store                   # noqa: E402


def _load_module(name: str, relative: str):
    """按文件路径加载 tools/ 里的脚本——tools 不是包，中文模块名更难 import。"""
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 复用画像重生工具的 LLM 配置加载（与生产同一条 config.yaml + .env 口径）
REBIRTH = _load_module("profile_rebirth", "tools/重生legacy画像.py")


# ══════════════════════════════════════════════════════════════════
# LLM 调用——必须与生产同口径（关思考 + 同一预算）
# ══════════════════════════════════════════════════════════════════
def resolve_max_tokens(llm: dict) -> int:
    """请求预算与生产同口径：`config.yaml` 的 `llm.max_tokens`，缺省 512（同 handler）。

    为什么单独抽一个函数：本工具第一版把 `max_tokens` 写死 **900**，实测撞上
    「回复 2258 字符 → JSON 被截断 → invalid_json → 判负」。而生产
    `_call_deepseek` 用的是 `cfg.get("max_tokens", 512)`，本仓 config.yaml 里
    这个值是 **64000**——写死 900 会让工具在**长批次**上假红，量出来的召回率
    与生产无关。闸门 `test_tool_uses_configured_max_tokens` 钉住它。
    """
    try:
        value = int(llm.get("max_tokens") or 512)
    except (TypeError, ValueError):
        return 512
    return value if value > 0 else 512


def build_payload(system_prompt: str, user_message: str, *, model: str,
                  max_tokens: int) -> dict:
    """请求体——纯函数，闸门直接断言它（关思考 + 预算不被写死）。"""
    return {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_message}],
        "temperature": 0.3,
        "max_tokens": int(max_tokens),
        "stream": False,
        # 纯 JSON 提取关闭思考——与 handler.THINKING_OFF 完全一致。
        "thinking": {"type": "disabled"},
    }


def make_llm_call():
    """`async def f(system, user) -> str`，与 `handler._call_llm_light(THINKING_OFF)` 同口径。

    为什么不能复用 `REBIRTH.make_llm_call`：那条没有 `thinking` 参数（见模块
    docstring 第 12 轮记录），对推理模型会拿到空回复，而提取器把它算成
    `invalid_json`——于是"没拿到回复"和"没有未闭合事件"在输出里长得一模一样。
    """
    import httpx

    llm = REBIRTH.load_llm_config()
    url = REBIRTH._endpoint(str(llm.get("base_url") or ""), "/chat/completions")
    model = str(llm.get("model") or "deepseek-chat")
    max_tokens = resolve_max_tokens(llm)
    headers = {"Authorization": f"Bearer {llm.get('api_key', '')}",
               "Content-Type": "application/json"}

    async def call(system_prompt: str, user_message: str) -> str:
        payload = build_payload(system_prompt, user_message, model=model,
                                max_tokens=max_tokens)
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return str(data["choices"][0]["message"].get("content") or "")

    return call


# ══════════════════════════════════════════════════════════════════
# 判据——纯函数，闸门直接测它（不许"失败当没有"）
# ══════════════════════════════════════════════════════════════════
def judge(results: list[dict]) -> dict:
    """把一批抽样结果判成「通过 / 尺子失效」，返回 dict（不打印、不退出）。

    三档，区别只在**失败批次**怎么算：

    - 全批 `protocol_ok=False` → 尺子没量到东西，`ok=False`、退出码 2；
    - 部分失败 → 合计数字不完整（可能正好漏掉那一条），`ok=False`、退出码 2；
    - 全成功 → `ok=True`、退出码 0。**0 条也是合法结论**——它意味着
      「这批里确实没有未闭合事件」，而不是「工具没跑通」。

    旧实现把三档混成一档（都打印"合计 0 条"、退出码 0），所以它永远不会红。
    """
    total = len(results)
    failed = 0
    loops = 0
    total_items = 0
    for r in results:
        stats = r.get("stats") or {}
        if not bool(stats.get("protocol_ok")):
            failed += 1
        loops += len(r.get("loops") or [])
        total_items += int(r.get("total") or 0)

    if total == 0:
        return {"ok": False, "exit_code": 2, "batches": 0, "failed": 0,
                "loops": 0, "items": 0,
                "message": "没有抽到任何批次（数据不足，不能算通过）"}
    if failed == total:
        return {"ok": False, "exit_code": 2, "batches": total, "failed": failed,
                "loops": loops, "items": total_items,
                "message": f"全部 {total} 批都没拿到有效回复——这不是"
                           f"「没有未闭合事件」，是尺子失效（检查 LLM 配置/思考开关）"}
    if failed:
        return {"ok": False, "exit_code": 2, "batches": total, "failed": failed,
                "loops": loops, "items": total_items,
                "message": f"{failed}/{total} 批没拿到有效回复——合计数字不完整"}
    return {"ok": True, "exit_code": 0, "batches": total, "failed": 0,
            "loops": loops, "items": total_items,
            "message": f"{total} 批全部有效：{total_items} 条记忆、{loops} 条未闭合事件"}


# ══════════════════════════════════════════════════════════════════
# 阳性对照——先证明尺子能看见，再用它量真实数据
# ══════════════════════════════════════════════════════════════════
# (聊天 id, 原话)：101~104 构造组、107~109 真实数据金标准组必须被判成 open_loop；
# 105/106/110 对照组必须**不**判成。
# 真实数据组来自 2026-10-25 第 14 轮取证：107/108 在历史 result_json 里被记成
# `event`、109 被记成 `event`（另附一条 open_loop）——**只有构造句能命中不算尺子有效**。
CASE_FIXTURES = [
    (101, "我明天要去面试，有点紧张"),
    (102, "下个月15号要体检，还没预约"),
    (103, "答应给她的那篇文章我还没动笔"),
    (104, "周末想跟老王和好，还没开口"),
    # 对照组 1：已经完成 → 普通 event，不是未结的事
    (105, "面试完了，感觉还行"),
    # 对照组 2：空泛愿望、没有落点 → 不该入库成悬事
    (106, "以后想环游世界"),
    # ── 真实数据金标准（历史被错判成 event 的原句）──
    (107, "明天去学校拿录取通知书"),
    (108, "明天10点有事，得有人叫醒我"),
    (109, "国庆假期还剩两天，明天有亲人来做客，作业一笔没动"),
    # 对照组 3：含「还没」但说的是**已经过去**的事 → 不该成悬事
    (110, "之前还没交作业，被老爸批评了一顿"),
]
EXPECT_OPEN_IDS = {101, 102, 103, 104}
REAL_EXPECT_OPEN_IDS = {107, 108, 109}
EXPECT_NOT_OPEN_IDS = {105, 106, 110}
CASE_MIN_HIT = 3          # 构造组 4 条里至少命中 3 条（留 1 条容错）
REAL_MIN_HIT = 2          # 真实组 3 条里至少命中 2 条（真实数据更难，但不能 0）


def check_cases(items: list[dict]) -> dict:
    """阳性对照判定：能看见几条必提样本、有没有误判对照组。纯函数，闸门测它。"""
    hit_ids: set[int] = set()
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "open_loop":
            continue
        for ev in item.get("evidence_ids") or []:
            try:
                hit_ids.add(int(ev))
            except (TypeError, ValueError):
                continue
    hit = sorted(hit_ids & EXPECT_OPEN_IDS)
    real_hit = sorted(hit_ids & REAL_EXPECT_OPEN_IDS)
    leaked = sorted(hit_ids & EXPECT_NOT_OPEN_IDS)
    ok = (len(hit) >= CASE_MIN_HIT and len(real_hit) >= REAL_MIN_HIT
          and not leaked)
    if not ok and leaked:
        message = f"阳性对照失败：对照组被误判成未闭合事件 {leaked}"
    elif not ok and len(real_hit) < REAL_MIN_HIT:
        # 构造句能看见、真实句看不见 = 尺子只在教科书上有效（第 14 轮的教训）
        message = (f"阳性对照失败：真实数据句只命中 {len(real_hit)}/{len(REAL_EXPECT_OPEN_IDS)}"
                   f"（需要 ≥{REAL_MIN_HIT}）——提取器仍把'还没发生的事'当成 event")
    elif not ok:
        message = (f"阳性对照失败：构造句只命中 {len(hit)}/{len(EXPECT_OPEN_IDS)}"
                   f"（需要 ≥{CASE_MIN_HIT}）")
    else:
        message = (f"阳性对照通过：构造句 {len(hit)}/{len(EXPECT_OPEN_IDS)}、"
                   f"真实数据句 {len(real_hit)}/{len(REAL_EXPECT_OPEN_IDS)}，对照组无误判")
    return {"ok": ok, "exit_code": 0 if ok else 2,
            "hit": hit, "real_hit": real_hit, "leaked": leaked,
            "message": message}


# ══════════════════════════════════════════════════════════════════
# 取样
# ══════════════════════════════════════════════════════════════════
def pick_recent_speakers(store: Store, users: int) -> list[str]:
    """最近在说话的真人（排除机器人号）——只有他们才有新鲜的将来时。"""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT qq_id, COUNT(*) c, MAX(id) mx FROM chat_log "
            "WHERE is_bot_reply=0 AND qq_id != '' "
            "AND COALESCE(is_synthetic,0)=0 "
            "AND timestamp >= datetime('now','-7 day') "
            "GROUP BY qq_id ORDER BY mx DESC LIMIT ?",
            (max(1, int(users)),),
        ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


# 「将来时」词汇：随机窗口里全是表情包和单字回复，L2 的召回率根本测不到。
FUTURE_WORDS = ("明天", "后天", "下周", "下个月", "周末", "打算", "准备",
                "还没", "要去", "得去", "答应", "计划", "回头")


def pick_future_messages(store: Store, users: int, limit: int) -> list[tuple[str, list[dict]]]:
    """挑「含将来时词汇」的真实用户消息，按人分组（每人最多 limit 条）。"""
    sql = ("SELECT qq_id, group_id, id, message, timestamp FROM chat_log "
           "WHERE is_bot_reply=0 AND COALESCE(is_synthetic,0)=0 "
           "AND qq_id != '' AND timestamp >= datetime('now','-21 day') "
           "AND (" + " OR ".join("message LIKE ?" for _ in FUTURE_WORDS) + ") "
           "ORDER BY id DESC LIMIT 600")
    with store._connect() as conn:
        rows = conn.execute(
            sql, tuple("%" + w + "%" for w in FUTURE_WORDS)).fetchall()
    by_user: dict[str, list[dict]] = {}
    for qq, gid, mid, msg, ts in rows:
        by_user.setdefault(str(qq), []).append(
            {"id": int(mid), "message": str(msg), "timestamp": str(ts or ""),
             "is_bot_reply": False, "group_id": str(gid or "")})
    picked = sorted(by_user.items(), key=lambda kv: -len(kv[1]))[:max(1, int(users))]
    return [(qq, msgs[:max(1, int(limit))]) for qq, msgs in picked]


async def sample_one(memory: MemorySystem, store: Store, qq: str, limit: int,
                     llm_call, messages: list[dict] | None = None) -> dict:
    person = store.get_or_create_person(qq) or {}
    nickname = str(person.get("nickname") or qq)
    if messages is None:
        messages = memory.get_unprocessed_messages(
            qq, limit=limit, include_bot_replies=True,
        )
    if len(messages) < 3:
        return {"qq": qq, "nickname": nickname, "skipped": "消息太少"}
    items, stats = await memory.extract_semantic_memories(
        messages=messages, nickname=nickname, qq_id=qq, llm_call=llm_call,
    )
    loops = [i for i in items if i.get("type") == "open_loop"]
    return {
        "qq": qq, "nickname": nickname, "stats": stats,
        "total": len(items), "loops": loops,
        "messages": [
            {"id": m.get("id"), "bot": bool(m.get("is_bot_reply")),
             "msg": str(m.get("message") or "")[:120]}
            for m in messages
        ],
    }


def render(result: dict) -> None:
    print("=" * 78)
    if result.get("skipped"):
        print(f"{result.get('qq')} {result.get('nickname')} —— 跳过：{result['skipped']}")
        return
    stats = result.get("stats") or {}
    print(f"{result.get('qq')} {result.get('nickname')}  "
          f"raw={stats.get('raw')} valid={stats.get('valid')} "
          f"rejected={stats.get('rejected')}  "
          f"open_loop={len(result['loops'])}/{result.get('total')}")
    if not bool(stats.get("protocol_ok")):
        # 关键：失败必须显眼，不能长得像"没有未闭合事件"
        print(f"  ⚠ 这一批没拿到有效回复：outcome={stats.get('outcome')} "
              f"raw_length={stats.get('raw_length')} —— 不能当成「没有未闭合事件」")
    if not result["loops"]:
        print("  （这一批没有识别出未闭合事件）")
    for loop in result["loops"]:
        print(f"  🧵 topic    : {loop.get('value')}")
        print(f"     kind      : {loop.get('kind')}   "
              f"expected_at: {loop.get('expected_at') or '（无）'}")
        print(f"     置信度    : {loop.get('confidence')}   "
              f"claim_type: {loop.get('claim_type')}")
        print(f"     原话证据  : {loop.get('evidence_quote')}")
        print(f"     evidence  : {loop.get('evidence_ids')}")
    print("  ── 该批原文 ──")
    for m in result["messages"]:
        who = "糖糖" if m["bot"] else "对方"
        print(f"    #{m['id']} {who}: {m['msg']}")


async def run_cases(memory: MemorySystem, llm_call) -> int:
    """阳性对照：内置样本跑一遍，证明尺子能看见 open_loop。"""
    messages = [
        {"id": cid, "message": text,
         "timestamp": "2026-10-25 10:%02d:00" % (i * 7 % 60),
         "is_bot_reply": False, "group_id": "0"}
        for i, (cid, text) in enumerate(CASE_FIXTURES)
    ]
    print("=" * 78)
    print("阳性对照——构造句 4 + 真实数据句 3 必提 + 对照组 3 不该提"
          "，先证明这把尺子能看见 open_loop")
    for cid, text in CASE_FIXTURES:
        if cid in EXPECT_OPEN_IDS:
            mark = "必提·构造"
        elif cid in REAL_EXPECT_OPEN_IDS:
            mark = "必提·真实"
        else:
            mark = "不该提"
        print(f"  #{cid} [{mark}] {text}")
    items, stats = await memory.extract_semantic_memories(
        messages=messages, nickname="阳性对照", qq_id="0",
        llm_call=llm_call,
    )
    print(f"\nstats={json.dumps(stats, ensure_ascii=False)}  items={len(items)}")
    for it in items:
        print("  -", json.dumps(it, ensure_ascii=False)[:300])
    verdict = check_cases(items)
    print()
    print(("✅ " if verdict["ok"] else "❌ ") + verdict["message"])
    return int(verdict["exit_code"])


async def main() -> int:
    ap = argparse.ArgumentParser(description="提取抽样（只读）")
    ap.add_argument("--users", type=int, default=4, help="抽样人数")
    ap.add_argument("--limit", type=int, default=20, help="每人取多少条消息")
    ap.add_argument("--qq", default="", help="只抽某个人")
    ap.add_argument("--future", action="store_true",
                    help="只抽带将来时词汇的真实消息（L2 召回率才有意义）")
    ap.add_argument("--cases", action="store_true",
                    help="阳性对照：内置样本必须能看见 open_loop")
    args = ap.parse_args()

    db = str(PROJECT_ROOT / "memory.db")
    os.chdir(PROJECT_ROOT)
    store = Store(db)
    memory = MemorySystem(db, store=store)
    llm_call = make_llm_call()

    if args.cases:
        return await run_cases(memory, llm_call)

    results: list[dict] = []
    loops_all = 0
    if args.future:
        targets = pick_future_messages(store, args.users, args.limit)
        if not targets:
            print("最近 21 天没有带将来时词汇的消息")
            return 2
        for qq, messages in targets:
            try:
                result = await sample_one(memory, store, qq, args.limit,
                                          llm_call, messages=messages)
            except Exception as exc:                   # 单批失败不拖垮整次抽样
                print(f"!! {qq} 抽样失败：{exc}")
                continue
            results.append(result)
            render(result)
            loops_all += len(result.get("loops") or [])
    else:
        targets = [args.qq] if args.qq else pick_recent_speakers(store, args.users)
        if not targets:
            print("最近 7 天没有可抽样的发言人")
            return 2
        for qq in targets:
            try:
                result = await sample_one(memory, store, qq, args.limit, llm_call)
            except Exception as exc:                   # 单批失败不拖垮整次抽样
                print(f"!! {qq} 抽样失败：{exc}")
                continue
            results.append(result)
            render(result)
            loops_all += len(result.get("loops") or [])

    out = PROJECT_ROOT / "日志" / "_提取抽样.json"
    try:
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细已存 {out}")
    except OSError:
        pass

    verdict = judge(results)
    print(f"\n合计识别出 {loops_all} 条未闭合事件，请人工判定：")
    print("  · 已完成的事有没有被当没结？（判错 → 她会追问已收尾的事）")
    print("  · 空泛愿望有没有被当悬事？（判错 → 每轮都冒出来）")
    print("  · expected_at 是不是从原话推的，不是编的？")
    print()
    if verdict["ok"]:
        print("✅ 判据：" + verdict["message"])
    else:
        print("❌ 判据：" + verdict["message"])
    return int(verdict["exit_code"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
