"""重生 legacy 画像：让「有证据但画像被封」的人重新获得一份**可信**画像。

背景
────
`active_notes()` 只放行 verified/manual/corrected，而 57 人的画像是 legacy——
他们不是没有画像，是那份画像**进不了上下文**。这些画像当初由旧的合成路径产出，
没有证据谱系，被 2026-08-16 治理降级（正确的防线）。

现在证据补上来了：这批人已有 ≥5 条可信记忆。重跑一次合成，就能产出
**有谱系**（`source_memory_ids` 全部 verified + 每条有 `memory_evidence`）的
verified 画像，替换掉那份被封的旧画像。

三条不能让步
────────────
1. **只走既有合成入口** `MemorySystem.synthesize_profile`——它会走
   `replace_profile_snapshot` 的谱系校验（source 必须全 verified）。工具里
   **不许**直接 UPDATE people.notes：那等于绕过整条信任链。
2. **单域才合成**。`people.notes` 只有一个槽位，而 `synthesize_profile` 要求
   `len(source_scopes)==1`（多域用户返回 None，代码里已写明「待有分域画像表
   再合成」）。所以工具按「该人可信记忆的 distinct scope」筛，多域跳过。
3. **默认 dry-run**，`--apply` 才调 LLM。先看名单和每个人的域，再花这笔钱。

已知限制（写在这里，避免以后误判）
──────────────────────────────────
合成出的是**单域画像**（实测 31 人全是群域）。`people.notes` 是单槽位，
而 `active_notes()` 只判信任等级、不判 scope——所以私聊注入时会用到这份
「群画像」。这是单槽位设计的固有代价（旧 legacy 画像本身也是混合的），
不是本工具引入的；等有分域画像表再改。

用法
────
    python tools/重生legacy画像.py                 # 只看名单
    python tools/重生legacy画像.py --apply --limit 3   # 先跑 3 个人看效果
    python tools/重生legacy画像.py --apply            # 批量
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import protocols as _protocols  # noqa: E402
from agent.memory import MemorySystem  # noqa: E402
from agent.secret_refs import PROVIDER_KEY_ENV  # noqa: E402
from agent.store import Store, TRUSTED_TRUST_LEVELS  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
MIN_MEMORIES = 5
MAX_INPUT_MEMORIES = 50


def _endpoint(base: str, path: str) -> str:
    """与 handler._endpoint 同口径（base 不带 /v1，由这里补）。"""
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return f"{base}/v1{path}"


def load_llm_config() -> dict:
    """读 llm 配置并按 handler 的同一张表用 .env 覆盖 api_key。

    必须复现这条逻辑：config.yaml 里的 api_key 可能是 `${DEEPSEEK_KEY}` 占位符，
    真正的值在 .env——直接拿占位符发请求只会 401，而报错看不出原因。
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    llm = dict(data.get("llm") or {})
    provider = str(llm.get("provider") or "deepseek")
    env_name = PROVIDER_KEY_ENV.get(provider, "")
    if env_name and os.getenv(env_name):
        llm["api_key"] = os.getenv(env_name)
    # config.yaml 里的 ${VAR} 引用（若未被 env 覆盖）在这里展开一次
    api_key = str(llm.get("api_key") or "")
    if api_key.startswith("${") and api_key.endswith("}"):
        llm["api_key"] = os.getenv(api_key[2:-1], "")
    if provider == "deepseek" and os.getenv("DEEPSEEK_BASE_URL"):
        llm["base_url"] = os.getenv("DEEPSEEK_BASE_URL")
    return llm


def make_llm_call(llm: dict):
    """`async def f(system, user) -> str` —— 就是 synthesize_profile 要的 llm_call。

    非流式、不带工具：这里只需要「按 prompt 写一段 200 字画像」，
    拉全套工具循环只会引入无关失败模式。
    """
    url = _endpoint(str(llm.get("base_url") or ""), "/chat/completions")
    model = str(llm.get("model") or "deepseek-chat")
    headers = {"Authorization": f"Bearer {llm.get('api_key', '')}",
               "Content-Type": "application/json"}

    async def call(system_prompt: str, user_message: str) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_message}],
            "temperature": 0.3,
            "max_tokens": 900,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return str(data["choices"][0]["message"].get("content") or "")

    return call


def find_candidates(store: Store, min_memories: int = MIN_MEMORIES) -> list[dict]:
    marks = ",".join("'%s'" % level for level in sorted(TRUSTED_TRUST_LEVELS))
    synth_marks = ",".join("'%s'" % key for key in _protocols.SYNTHESIS_KEYS)
    # usable = 真正能进 synthesize_profile 的那些（口径与它的输入过滤一致）：
    # 排除合成行，排除低置信度（除 importance≥8 与 episodic 外，0.75 以下全丢
    # ——那是 2026-08-16「0.55 种子洗白成 0.7 画像」的防线，不能放松）。
    # 只用「可信记忆总数」筛会挑出一批「看着够、过滤后不够」的人：实测 13 个
    # 白跑的失败里 8 个就是这种（0 秒返回 None，表面看不出原因）。
    usable_cond = (
        "COALESCE(m.status,'active')='active' "
        f"AND COALESCE(m.trust_level,'legacy_unverified') IN ({marks}) "
        f"AND COALESCE(m.key,'') NOT IN ({synth_marks}) "
        "AND (COALESCE(m.importance,0) >= 8 "
        "     OR COALESCE(m.confidence,0.7) >= 0.75 "
        "     OR COALESCE(m.cognitive,'semantic') = 'episodic')"
    )
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT p.qq_id, COALESCE(p.nickname,''), "
            "       COALESCE(p.notes_trust_level,'legacy_unverified'), "
            "       (SELECT COUNT(*) FROM memories m WHERE m.qq_id=p.qq_id "
            f"        AND {usable_cond}), "
            "       (SELECT COUNT(*) FROM memories m WHERE m.qq_id=p.qq_id "
            "        AND COALESCE(m.status,'active')='active' "
            f"       AND COALESCE(m.trust_level,'legacy_unverified') IN ({marks})) "
            "FROM people p WHERE COALESCE(p.notes,'')<>'' "
            "  AND COALESCE(p.notes_dirty,0)=0 "
            f"  AND COALESCE(p.notes_trust_level,'legacy_unverified') NOT IN ({marks})"
        ).fetchall()
    candidates = []
    for qq_id, nickname, trust, usable_count, trusted_count in rows:
        usable_count = int(usable_count or 0)
        base = {"qq_id": str(qq_id), "nickname": str(nickname or ""),
                "usable_memories": usable_count,
                "trusted_memories": int(trusted_count or 0)}
        with store._connect() as conn:
            scopes = [str(row[0] or "") for row in conn.execute(
                "SELECT DISTINCT COALESCE(source_group_id,'') FROM memories "
                "WHERE qq_id=? AND COALESCE(status,'active')='active' "
                f"AND COALESCE(trust_level,'legacy_unverified') IN ({marks})",
                (str(qq_id),)).fetchall()]
        if not scopes:
            candidates.append({**base, "status": "skip_no_trusted", "scopes": []})
            continue
        if len(scopes) != 1:
            candidates.append({**base, "status": "skip_multi_scope", "scopes": scopes})
            continue
        if usable_count < min_memories:
            candidates.append({**base, "status": "skip_not_enough_usable",
                               "scope": scopes[0]})
            continue
        candidates.append({**base, "status": "ready", "scope": scopes[0]})
    return candidates


async def rebind(store: Store, memory: MemorySystem, candidate: dict,
                 llm_call) -> dict:
    qq_id = candidate["qq_id"]
    scope = candidate.get("scope", "")
    memories = memory.recall(qq_id, limit=MAX_INPUT_MEMORIES,
                             source_group_id=scope, trusted_only=True)
    if len(memories) < MIN_MEMORIES:
        return {"qq_id": qq_id, "ok": False, "reason": "recall_too_few",
                "count": len(memories)}
    # 预检合成输入：口径抄自 memory.synthesize_profile 的输入过滤（排除合成行、
    # 低置信度不进画像——那是 2026-08-16「0.55 种子洗白成 0.7 画像」的防线）。
    # 不预检的话，过滤后不足 5 条会一路走到 LLM 之前才 return None，
    # 表面现象是「0.0 秒失败」，看不出真正原因。
    usable = [m for m in memories
              if m.key not in _protocols.SYNTHESIS_KEYS
              and (int(getattr(m, "importance", 0) or 0) >= 8
                   or float(getattr(m, "confidence", 0.7) or 0) >= 0.75
                   or str(getattr(m, "cognitive", "semantic")) == "episodic")]
    if len(usable) < MIN_MEMORIES:
        return {"qq_id": qq_id, "ok": False, "reason": "unusable_after_input_filter",
                "count": len(memories), "usable": len(usable)}
    profile = await memory.synthesize_profile(
        qq_id, llm_call, memories=memories, bypass_cooldown=True,
    )
    if not profile:
        return {"qq_id": qq_id, "ok": False, "reason": "synthesis_returned_none",
                "count": len(memories), "usable": len(usable)}
    person = store.get_or_create_person(qq_id)
    return {"qq_id": qq_id, "ok": True,
            "trust_level": str(person.get("notes_trust_level") or ""),
            "notes_dirty": int(person.get("notes_dirty") or 0),
            "profile_head": profile[:60],
            "input_memories": len(memories), "usable": len(usable),
            "scope": scope}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="重生 legacy 画像（默认 dry-run）")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--apply", action="store_true", help="实际调 LLM 并写库")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--qq", default="", help="只处理某人")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几人")
    parser.add_argument("--min-memories", type=int, default=MIN_MEMORIES)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 2
    production = db_path == (PROJECT_ROOT / "memory.db").resolve()
    if args.apply and production and not args.allow_production:
        print(f"{db_path} 是生产库。先备份，或显式加 --allow-production。",
              file=sys.stderr)
        return 2

    store = Store(str(db_path))
    memory = MemorySystem(store=store)
    candidates = find_candidates(store, min_memories=args.min_memories)
    if args.qq:
        candidates = [c for c in candidates if c["qq_id"] == str(args.qq)]
    ready = [c for c in candidates if c["status"] == "ready"]
    # 可信记忆多的先跑：输入越长画像越稳，小批试跑也更容易看出质量
    ready.sort(key=lambda c: -c["trusted_memories"])
    if args.limit:
        ready = ready[: args.limit]

    summary = {
        "db": str(db_path), "applied": bool(args.apply),
        "candidates": len(candidates),
        "ready": len(ready),
        "skip_multi_scope": sum(1 for c in candidates
                                if c["status"] == "skip_multi_scope"),
        "skip_no_trusted": sum(1 for c in candidates
                               if c["status"] == "skip_no_trusted"),
        "skip_not_enough_usable": sum(
            1 for c in candidates if c["status"] == "skip_not_enough_usable"),
        "results": [], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"候选 {len(candidates)} → 可重生 {len(ready)} "
          f"（无可信 {summary['skip_no_trusted']} / 多域 "
          f"{summary['skip_multi_scope']} / 可用不足 "
          f"{summary['skip_not_enough_usable']}）")
    for candidate in ready:
        print(f"  {candidate['qq_id']:12s} {candidate['nickname'][:14]:16s} "
              f"可用 {candidate['usable_memories']:4d} "
              f"(可信 {candidate['trusted_memories']:4d})  "
              f"域 {'私聊' if not candidate['scope'] else candidate['scope']}")
    if not args.apply:
        print("（dry-run：没有调用 LLM，也没有写库）")
        if args.report:
            Path(args.report).write_text(
                json.dumps({**summary, "ready_list": ready}, ensure_ascii=False,
                           indent=2) + "\n", encoding="utf-8")
        return 0

    llm = load_llm_config()
    if not llm.get("api_key"):
        print("找不到 llm.api_key（config.yaml 与 .env 都没有）——无法合成",
              file=sys.stderr)
        return 2
    llm_call = make_llm_call(llm)

    async def run_all() -> None:
        for index, candidate in enumerate(ready, 1):
            started = time.time()
            try:
                result = await rebind(store, memory, candidate, llm_call)
            except Exception as exc:  # 单人失败不拖垮整批
                result = {"qq_id": candidate["qq_id"], "ok": False,
                          "reason": f"{type(exc).__name__}: {exc}"}
            result["nickname"] = candidate["nickname"]
            result["elapsed_s"] = round(time.time() - started, 1)
            summary["results"].append(result)
            flag = "OK " if result.get("ok") else "FAIL"
            print(f"[{index}/{len(ready)}] {flag} {candidate['qq_id']} "
                  f"{candidate['nickname'][:12]} "
                  f"({result['elapsed_s']}s) {result.get('reason', '')}"
                  f"{result.get('trust_level', '')}")

    asyncio.run(run_all())
    ok = sum(1 for item in summary["results"] if item.get("ok"))
    verified = sum(1 for item in summary["results"]
                   if item.get("trust_level") in TRUSTED_TRUST_LEVELS)
    summary["ok"] = ok
    summary["verified"] = verified
    print(f"\n成功 {ok}/{len(ready)}，其中画像已变可信 {verified}")
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"报告已写入 {args.report}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
