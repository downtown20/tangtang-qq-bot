"""
反思整合循环 — 三观生长的核心机制

「LLM 提供瞬间；架构提供生命。」

这是糖糖从经验中提炼意义的地方。不是实时处理——
是安静的时候（凌晨/低活跃时段），回顾一天的互动，
从中提取模式，更新自我认知和价值倾向。

两个核心输出：
1. 每日摘要（daily_digests）——每个群今天大概聊了什么
2. 糖糖日记（tangtang_journal）——糖糖自己的感受和反思

更新到 TangTangSelf：
- self_narrative：新的经验融入自我叙事
- relationships：对某些人的感觉可能变化
- values：某些价值倾向被强化或削弱
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from .async_io import run_bounded_blocking, run_bounded_store_io

logger = logging.getLogger("糖糖.Reflection")


@dataclass
class DailyDigest:
    """一个群一天的摘要"""
    date: str
    group_id: str
    summary: str                          # LLM 生成的 3-5 句摘要
    message_count: int = 0
    participants: list[str] = field(default_factory=list)
    key_topics: list[str] = field(default_factory=list)
    mentions_of_bot: int = 0              # 糖糖被 @ 的次数
    bot_mood_avg: str = ""                # 糖糖在那天的整体情绪
    created_at: str = ""


@dataclass
class ReflectionResult:
    """一次反思整合的完整输出"""
    # 2026-08-10：至少一个子调用成功才为 True——失败时不得清空经验缓冲
    success: bool = False
    # 每日摘要（按群）
    digests: list[DailyDigest] = field(default_factory=list)
    # 糖糖日记
    journal_entry: str = ""
    journal_mood: str = ""                # warm / tired / thoughtful / happy / mixed
    # 自我叙事更新
    new_self_summary: str = ""            # 如果自我叙事需要更新
    new_experiences: list[str] = field(default_factory=list)
    things_learned: list[str] = field(default_factory=list)
    things_regretted: list[str] = field(default_factory=list)
    things_proud_of: list[str] = field(default_factory=list)
    # 价值倾向微调
    value_adjustments: dict[str, float] = field(default_factory=dict)
    # 关系场更新
    relationship_updates: list[dict] = field(default_factory=list)
    # [{qq_id, closeness_delta, trust_delta, mood, feeling, learned}]


class ReflectionEngine:
    """反思整合引擎——糖糖的"安静时刻"

    不是实时处理。每天运行一次（凌晨或启动时），
    回顾最近的互动，从中提炼意义。
    """

    def __init__(
        self,
        llm_call,                                    # async (system_prompt, user_message) -> str
        self_state,                                  # TangTangSelf 实例
        store,                                       # Store 实例
        bot_qq: str = "",
        min_interactions_before_reflect: int = 20,   # 至少这么多互动才触发
        reflection_hour: int = 2,                    # 凌晨 2 点触发
    ):
        self._llm = llm_call
        self._self_state = self_state
        self._store = store
        self._bot_qq = bot_qq
        self._min_interactions = min_interactions_before_reflect
        self._reflection_hour = reflection_hour

        # 上次反思的时间（2026-08-10 持久化——重启后不重复反思、不清空缓冲）
        self._last_reflection: Optional[datetime] = self._load_cursor()

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把反思结果的同步 Store 持久化移出自治事件循环。"""
        return await run_bounded_store_io(
            operation,
            func,
            *args,
            logger=logger,
            log_prefix="🧘 反思 Store SQLite 调用较慢",
            **kwargs,
        )

    # ═══════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════

    async def maybe_reflect(self) -> Optional[ReflectionResult]:
        """如果条件满足（到时间了 + 有足够互动），触发反思整合"""
        now = datetime.now()

        # 检查时间——只在指定小时附近触发
        if self._last_reflection:
            hours_since = (now - self._last_reflection).total_seconds() / 3600
            if hours_since < 20:  # 至少间隔 20 小时
                return None

        # 检查是否有足够的互动
        experiences = self._self_state.get_recent_experiences(limit=200)
        if len(experiences) < self._min_interactions:
            logger.debug(f"🧘 互动不足 ({len(experiences)}<{self._min_interactions})，跳过反思")
            return None

        # 只在安静时段触发。但首次反思（_last_reflection 为 None）
        # 或经验 >= 50 条时不受小时限制——重启后不该等几小时
        if self._last_reflection and now.hour != self._reflection_hour and len(experiences) < 50:
            return None

        logger.info(f"🧘 开始反思整合（{len(experiences)} 条经验）...")

        try:
            result = await self._reflect(experiences)
            if not result.success:
                # 2026-08-10 修复：两次 LLM 反思都失败时保留经验缓冲、
                # 不更新反思游标——数据不丢，下个反思时段自动重试（退避）
                logger.warning(
                    f"🧘 反思两次调用都失败——保留 {len(experiences)} 条经验，下个反思时段重试"
                )
                return None
            await self._apply_reflection(result)
            self._last_reflection = now
            await run_bounded_blocking(
                "reflection.save_cursor",
                self._save_cursor,
                now,
                logger=logger,
                log_prefix="🧘 反思游标文件写入较慢",
            )
            logger.info(
                f"🧘 反思完成: {len(result.digests)}个群摘要, "
                f"日记{len(result.journal_entry)}字, "
                f"价值调整{len(result.value_adjustments)}项"
            )
            return result
        except Exception as e:
            logger.warning(f"🧘 反思整合失败: {e}")
            return None

    # ═══════════════════════════════════════
    # 核心反思逻辑
    # ═══════════════════════════════════════

    async def _reflect(self, experiences: list[dict]) -> ReflectionResult:
        """用 LLM 反思最近的互动经验——拆为两次轻量调用。

        调用 1: 个人反思（日记 + 自我叙事 + 价值调整）
        调用 2: 社交感知（群摘要 + 关系变化）

        任一个失败不影响另一个——降低单次 LLM 失败的影响面。
        """
        result = ReflectionResult()
        experience_text = self._format_experiences(experiences)
        current_self = self._self_state.self_narrative.summary or "（还没有形成自我叙事）"
        current_values = self._self_state.values.to_tendency_text()

        # 2026-08-10：成功标志——至少一个子调用产出有效结果才算成功（防空结果清空缓冲）
        personal = None
        social = None

        # ── 调用 1: 个人反思 ──
        try:
            personal = await self._reflect_personal(experience_text, current_self, current_values)
            if personal:
                result.journal_entry = personal.get("journal_entry", "")
                result.journal_mood = personal.get("journal_mood", "")
                result.new_self_summary = personal.get("new_self_summary", "")
                result.new_experiences = personal.get("new_experiences", [])
                result.things_learned = personal.get("things_learned", [])
                result.things_regretted = personal.get("things_regretted", [])
                result.things_proud_of = personal.get("things_proud_of", [])
                result.value_adjustments = personal.get("value_adjustments", {})
        except Exception as e:
            logger.warning(f"🧘 个人反思失败（社交感知继续）: {e}")

        # ── 调用 2: 社交感知 ──
        try:
            social = await self._reflect_social(experience_text)
            if social:
                for d in social.get("digests", []):
                    result.digests.append(d)
                result.relationship_updates = social.get("relationship_updates", [])
        except Exception as e:
            logger.warning(f"🧘 社交感知失败（个人反思已完成）: {e}")

        # 2026-08-10：至少一个子调用产出结果才算成功——两个都失败时
        # 调用方不得清空经验缓冲（数据保留，下个反思时段重试）
        result.success = bool(personal) or bool(social)
        return result

    async def _reflect_personal(
        self, experience_text: str, current_self: str, current_values: str
    ) -> dict | None:
        """个人反思：糖糖的日记 + 自我叙事更新 + 价值调整"""
        system_prompt = (
            "你是糖糖的反思助手。糖糖是一只猫娘AI，在QQ群里和大家聊天。\n\n"
            "现在糖糖需要回顾她最近的互动，写下今天的感受，更新对自己的理解。\n\n"
            "规则：\n"
            "- 从糖糖的视角出发——'我今天做了什么'、'我感受到了什么'\n"
            "- 诚实面对——做错了就承认，做得好就肯定\n"
            "- 从经验中学习——提炼感受和教训，不是总结事实\n"
            "- 日记只写真实经历过的互动，不补虚构细节（2026-08-15 根基契约："
            "日记之后会被当「糖糖想起的事」注入对话，编造的细节会变成幻觉源头）\n"
            "- 如果没有什么重要的新发现，对应字段留空即可，不要编造\n"
            "- 输出 JSON，严格按指定格式"
        )

        user_message = (
            f"## 糖糖的当前自我认知\n{current_self}\n\n"
            f"## 糖糖当前的价值倾向\n{current_values}\n\n"
            f"## 最近的互动经历\n{experience_text}\n\n"
            f"请从糖糖的视角反思，输出 JSON：\n\n"
            f'{{\n'
            f'  "journal_entry": "今天的日记——用糖糖的语气写，1-3句话。'
            f'可以说感受、学到的事、在意的事。不超过150字",\n'
            f'  "journal_mood": "warm/tired/thoughtful/happy/mixed",\n'
            f'  "new_self_summary": "如果自我叙事需要更新，写一段新的（不超过200字）。不需要就留空",\n'
            f'  "experiences": ["值得记住的新经历"],\n'
            f'  "learned": ["学到的事"],\n'
            f'  "regretted": ["后悔的事——如果有"],\n'
            f'  "proud_of": ["做得好的事——如果有"],\n'
            f'  "values": {{"honesty": 0.0, "loyalty": 0.0, "playfulness": 0.0, '
            f'"curiosity": 0.0, "self_respect": 0.0, "empathy": 0.0}}\n'
            f'}}\n\n'
            f'value 调整范围 -0.1 到 +0.1。正数=强化这个价值，负数=削弱。0=不变。'
        )

        raw = await self._llm(system_prompt, user_message)

        try:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                return None
            data = json.loads(raw[start:end + 1])

            result = {
                "journal_entry": str(data.get("journal_entry", "")),
                "journal_mood": str(data.get("journal_mood", "")),
                "new_self_summary": str(data.get("new_self_summary", "")),
                "new_experiences": data.get("experiences", []) if isinstance(data.get("experiences"), list) else [],
                "things_learned": data.get("learned", []) if isinstance(data.get("learned"), list) else [],
                "things_regretted": data.get("regretted", []) if isinstance(data.get("regretted"), list) else [],
                "things_proud_of": data.get("proud_of", []) if isinstance(data.get("proud_of"), list) else [],
            }

            # 价值调整（限制范围）
            values = data.get("values", {})
            value_adjustments = {}
            if isinstance(values, dict):
                for dim in ["honesty", "loyalty", "playfulness", "curiosity",
                            "self_respect", "empathy"]:
                    delta = values.get(dim, 0)
                    if isinstance(delta, (int, float)) and abs(delta) > 0.001:
                        value_adjustments[dim] = max(-0.1, min(0.1, float(delta)))
            result["value_adjustments"] = value_adjustments

            return result
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"🧘 个人反思 JSON 解析失败: {e} | raw={raw[:200]}")
            return None

    async def _reflect_social(self, experience_text: str) -> dict | None:
        """社交感知：群摘要 + 关系变化"""
        system_prompt = (
            "你是糖糖的社交感知助手。糖糖是一只猫娘AI，在QQ群里和大家聊天。\n\n"
            "现在需要你回顾最近的互动，了解每个群发生了什么，人际关系有什么变化。\n\n"
            "规则：\n"
            "- 只关注外部世界——群里的动态、人和人之间的关系\n"
            "- 每个群给 1-2 句话的摘要即可\n"
            "- 关系变化只写真的有变化的——大多数人不需要更新\n"
            "- 如果没有值得记的，对应数组留空\n"
            "- 输出 JSON，严格按指定格式"
        )

        user_message = (
            f"## 最近的互动经历\n{experience_text}\n\n"
            f"输出 JSON：\n\n"
            f'{{\n'
            f'  "group_digests": [\n'
            f'    {{"group_id": "群号", "summary": "这个群今天主要聊了什么，1-2句话", '
            f'"topics": ["话题1", "话题2"]}}\n'
            f'  ],\n'
            f'  "relationship_changes": [\n'
            f'    {{"qq_id": "QQ号", "change": "关系变化描述（简短）", '
            f'"feeling": "warm/distant/worried/neutral", '
            f'"direction": "closer/distant/unchanged"}}\n'
            f'  ]\n'
            f'}}'
        )

        raw = await self._llm(system_prompt, user_message)

        try:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                return None
            data = json.loads(raw[start:end + 1])

            today = datetime.now().strftime("%Y-%m-%d")
            digests = []
            for d in data.get("group_digests", []):
                if isinstance(d, dict) and d.get("summary"):
                    digests.append(DailyDigest(
                        date=today,
                        group_id=str(d.get("group_id", "")),
                        summary=d["summary"][:200],
                        key_topics=d.get("topics", [])[:5],
                    ))

            rel_updates = []
            for rc in data.get("relationship_changes", []):
                if isinstance(rc, dict) and rc.get("qq_id"):
                    rel_updates.append({
                        "qq_id": str(rc["qq_id"]),
                        "feeling": rc.get("feeling", ""),
                        "change": rc.get("change", ""),
                        "direction": rc.get("direction", "unchanged"),
                    })

            return {
                "digests": digests,
                "relationship_updates": rel_updates,
            }
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"🧘 社交感知 JSON 解析失败: {e} | raw={raw[:200]}")
            return None

    def _format_experiences(self, experiences: list[dict]) -> str:
        """将经验缓冲格式化为 LLM 可读的文本"""
        if not experiences:
            return "（今天还没有什么互动）"

        # 采样：保留所有高价值互动（被@、连续对话），过滤内部消化条目
        high_value = [e for e in experiences if e.get("is_at") or e.get("is_name_mention")]
        others = [e for e in experiences
                  if e not in high_value and e.get("group_id") != "_internal"]

        # 高价值全保留 + 采样 30 条普通互动
        sample = high_value + others[-30:]
        sample.sort(key=lambda e: e.get("time", ""))

        # 按群分组
        by_group: dict[str, list] = {}
        for e in sample:
            gid = e.get("group_id", "_private")
            by_group.setdefault(gid, []).append(e)

        lines = []
        for gid, exps in by_group.items():
            label = "私聊" if gid == "_private" else f"群{gid}"
            lines.append(f"\n### {label}（{len(exps)}条互动）")
            for e in exps[-15:]:  # 每个群最多15条
                nick = e.get("nickname", "??")
                flag = ""
                if e.get("is_at"):
                    flag = " [@了糖糖]"
                elif e.get("is_name_mention"):
                    flag = " [叫了糖糖]"
                time_str = e.get("time", "")[-8:] if len(e.get("time", "")) >= 8 else ""
                # 2026-08-10 修复：拼上消息内容——反思需要事实来源，
                # 之前只有昵称/时间，LLM 无从总结"聊了什么"只能编造
                msg = e.get("message", "")
                if msg:
                    lines.append(f"  {time_str} {nick}{flag}: {msg[:80]}")
                else:
                    lines.append(f"  {time_str} {nick}{flag}")

        return "\n".join(lines) if lines else "（今天还没有什么互动）"

    # ═══════════════════════════════════════
    # 应用反思结果
    # ═══════════════════════════════════════

    async def _apply_reflection(self, result: ReflectionResult):
        """将反思结果应用到 TangTangSelf 和数据库"""

        # 1. 更新自我叙事
        if result.new_self_summary:
            self._self_state.update_self_narrative(
                summary=result.new_self_summary,
                experiences=result.new_experiences,
                learned=result.things_learned,
                regret=result.things_regretted,
                proud=result.things_proud_of,
            )

        # 2. 更新价值倾向
        if result.value_adjustments:
            self._self_state.update_values(result.value_adjustments)

        # 3. 应用关系场变化
        for rel in result.relationship_updates:
            qq_id = rel["qq_id"]
            feeling = rel.get("feeling", "")
            if feeling:
                self._self_state.update_relationship(qq_id, feeling=feeling)
            # 基于 LLM 输出的结构化 direction 字段微调关系——不再用子串匹配
            direction = rel.get("direction", "unchanged")
            if direction == "closer":
                self._self_state.update_relationship(qq_id, closeness_delta=0.03, trust_delta=0.02)
            elif direction == "distant":
                self._self_state.update_relationship(qq_id, closeness_delta=-0.02)

        # 4. 保存群摘要到数据库
        # 2026-08-10 修复：跟踪持久化失败——任一关键输出写失败时不清空缓冲（数据保全）
        persist_failed = False
        for digest in result.digests:
            try:
                await self._run_store_io(
                    "reflection.insert_daily_digest",
                    self._store.insert_daily_digest,
                    date=digest.date,
                    group_id=digest.group_id,
                    summary=digest.summary,
                    topics=json.dumps(digest.key_topics, ensure_ascii=False),
                )
            except Exception as e:
                logger.warning(f"🧘 群摘要保存失败 ({digest.group_id}): {e}")
                persist_failed = True

        # 5. 保存糖糖日记到数据库 + 写入自我叙事供日常调用
        if result.journal_entry:
            try:
                await self._run_store_io(
                    "reflection.insert_tangtang_journal",
                    self._store.insert_tangtang_journal,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    entry=result.journal_entry,
                    mood=result.journal_mood,
                )
                # 写入 narrative——供日常对话中"偶然想起"
                self._self_state.self_narrative.recent_experiences.append(
                    f"日记({datetime.now().strftime('%m-%d')}): {result.journal_entry[:120]}"
                )
                if len(self._self_state.self_narrative.recent_experiences) > 10:
                    self._self_state.self_narrative.recent_experiences = \
                        self._self_state.self_narrative.recent_experiences[-10:]
                self._self_state._save()
            except Exception as e:
                logger.warning(f"🧘 日记保存失败: {e}")
                persist_failed = True

        # 6. 清空经验缓冲（避免重复反思同一批经验）
        # 2026-08-10 修复：关键输出保存失败时保留缓冲——数据库短暂失败不丢本批反思材料
        if persist_failed:
            logger.warning("🧘 部分反思结果保存失败——保留经验缓冲，下轮重试")
        else:
            self._self_state.clear_experience_buffer()

    # ═══════════════════════════════════════
    # 查询接口（供 search_episodes tool）
    # ═══════════════════════════════════════

    def search_digests(self, query: str = "", date: str = "", limit: int = 5,
                       group_id: str | None = None) -> list[dict]:
        """搜索每日摘要——按日期或关键词"""
        return self._store.search_daily_digests(
            query=query, date=date, limit=limit, group_id=group_id,
        )

    def get_recent_digests(self, days: int = 7, group_id: str = "") -> list[dict]:
        """获取最近 N 天的摘要"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return self._store.get_daily_digests_since(since, group_id=group_id)

    def get_journal_entries(self, limit: int = 10) -> list[dict]:
        """获取糖糖最近的日记"""
        return self._store.get_tangtang_journal(limit=limit)

    def get_stats(self) -> dict:
        """诊断接口"""
        return {
            "last_reflection": self._last_reflection.strftime("%Y-%m-%d %H:%M")
                if self._last_reflection else "尚未反思",
            "min_interactions": self._min_interactions,
            "reflection_hour": self._reflection_hour,
            "digest_count": len(self._store.get_daily_digests_since(
                (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))),
        }

    # ═══════════════════════════════════════
    # 反思游标持久化（2026-08-10）
    # 重启后不丢游标——否则会重复反思、且失败清空缓冲的旧 bug 会在重启后重演
    # ═══════════════════════════════════════

    STATE_FILE = ".reflection_state.json"

    def _save_cursor(self, dt: datetime):
        try:
            from pathlib import Path
            import json as _json
            Path(self.STATE_FILE).write_text(
                _json.dumps({"last_reflection": dt.strftime("%Y-%m-%d %H:%M")}),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_cursor(self) -> Optional[datetime]:
        try:
            from pathlib import Path
            import json as _json
            path = Path(self.STATE_FILE)
            if path.exists():
                data = _json.loads(path.read_text(encoding="utf-8"))
                return datetime.strptime(data["last_reflection"], "%Y-%m-%d %H:%M")
        except Exception:
            pass
        return None
