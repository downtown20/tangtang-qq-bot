"""
离线消息补读 📬
糖糖重启/掉线后自动读取错过的群聊消息，支持 LLM 摘要和短期缓冲注入。

🆕 补回复（2026-07-29）：离线期间有人 @糖糖 或叫糖糖名字的消息，
系统会自动生成回复并发送到群里——不会再出现「叫了糖糖没人应」的情况。

用法：
    from .catch_up import create_catch_up_manager
    catch_up = create_catch_up_manager(store, short_term, llm_caller, config, ...)
    await catch_up.catch_up_all_groups()
    await catch_up.reply_to_missed_mentions()  # 🆕 补回复错过的点名
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from napcat.ws_client import is_send_confirmed, send_delivery_state

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.CatchUp")


@dataclass
class CatchUpSummary:
    """离线期间某个群的补读结果"""
    group_id: str
    summary: str           # LLM 生成的摘要（或格式化的消息文本）
    message_count: int     # 离线期间该群消息数
    from_time: str         # 窗口起始
    to_time: str           # 窗口结束
    missed_mentions: list = field(default_factory=list)  # 离线期间 @糖糖/叫她名字 的消息


class CatchUpManager:
    """离线消息补读管理器"""

    STATE_FILE = ".catch_up_state.json"
    SENT_FILE = ".catch_up_sent.json"  # 已补发的回复记录

    def __init__(
        self,
        store,                       # Store 实例
        short_term: dict,            # 短期记忆缓冲 {group_id: deque}
        llm_caller,                  # async (system_prompt, user_message) -> str
        config: dict,                # catch_up 配置节
        get_allowed_groups: Callable[[], list[str]],
        get_blacklist: Callable[[], set[str]] | None = None,
        bot_qq: str = "",            # 🆕 机器人 QQ 号——用于检测 @ 提及
        bot_nicknames: list = None,  # 🆕 机器人昵称列表——用于检测名字提及
        send_group_msg=None,         # 🆕 async (group_id, message) -> None——发送补回复
        self_state=None,             # 🆕 TangTangSelf——注入关系感+记忆用于个性化补回复
        enrich=None,                 # 🆕 (reply, group_id) -> str——发送前清洗+贴图解析（2026-08-16 链路审计）
    ):
        self._store = store
        self._short_term = short_term
        self._llm = llm_caller
        self._get_groups = get_allowed_groups
        self._get_blacklist = get_blacklist or (lambda: set())
        self._bot_qq = bot_qq
        self._bot_nicknames = bot_nicknames or []
        self._send_group_msg = send_group_msg
        self._self_state = self_state
        self._enrich = enrich

        # 补发终态：confirmed 与 uncertain 都持久化并阻止自动重放；
        # uncertain 不是「已送达」，只是 QQ 接受后缺少 message_id 证据。
        self._sent: dict[tuple[str, str, str], dict[str, str]] = {}
        self._sent_corrupt = False
        self._load_sent()

        # 配置
        self._enabled = config.get("enabled", True)
        self._min_gap_minutes = config.get("min_gap_minutes", 3)
        self._max_messages = config.get("max_messages", 500)
        self._summarize_threshold = config.get("summarize_threshold", 30)
        self._inject_as_context = config.get("inject_as_context", True)
        self._missed_reply_window = config.get("missed_reply_window_minutes", 60)
        self._max_missed_replies = config.get("max_missed_replies_per_group", 3)

        # 内存缓存：补读完成后存入，供后续查询
        self._summaries: dict[str, CatchUpSummary] = {}
        self._claim_lock = asyncio.Lock()

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把同步 Store 调用移出补读事件循环，并复用统一并发/取消边界。"""
        return await run_bounded_store_io(
            operation,
            func,
            *args,
            logger=logger,
            log_prefix="📬 补读 Store SQLite 调用较慢",
            **kwargs,
        )

    # ═══════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════

    async def catch_up_all_groups(self) -> dict[str, CatchUpSummary]:
        """遍历所有白名单群，逐群补读离线消息。不阻塞，失败静默。"""
        if not self._enabled:
            logger.info("📬 离线补读已禁用")
            return {}

        state = self._load_state()
        last_offline = state.get("last_offline", "")
        if not last_offline:
            logger.info("📬 首次运行，无离线记录，跳过补读")
            return {}

        now = datetime.now()
        try:
            offline_time = datetime.strptime(last_offline, "%Y-%m-%d %H:%M:%S")
            gap = (now - offline_time).total_seconds() / 60
        except ValueError:
            logger.warning(f"📬 离线时间格式异常: {last_offline}")
            return {}

        if gap < self._min_gap_minutes:
            logger.info(f"📬 离线仅 {gap:.1f} 分钟（<{self._min_gap_minutes}），跳过补读")
            return {}

        groups = self._get_groups()
        blacklist = self._get_blacklist()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"📬 开始离线补读（离线 {gap:.0f} 分钟，{len(groups)} 个群）")

        for gid in groups:
            if gid in blacklist:
                continue
            try:
                summary = await self._catch_up_group(gid, last_offline, now_str)
                if summary:
                    self._summaries[gid] = summary
            except Exception as e:
                logger.warning(f"📬 群 {gid} 补读失败: {e}")

        if self._summaries:
            total_msgs = sum(s.message_count for s in self._summaries.values())
            logger.info(f"📬 离线补读完成: {len(self._summaries)}个群, 共{total_msgs}条消息")
        return self._summaries

    async def _catch_up_group(
        self, group_id: str, since: str, until: str
    ) -> CatchUpSummary | None:
        """对单个群执行补读"""
        count = await self._run_store_io(
            "catch_up.get_group_message_count_since",
            self._store.get_group_message_count_since,
            group_id,
            since,
        )
        if count == 0:
            return None

        messages = await self._run_store_io(
            "catch_up.get_group_messages_since",
            self._store.get_group_messages_since,
            group_id,
            since,
            self._max_messages,
        )

        # 🆕 扫描离线期间谁 @ 了糖糖或叫了糖糖名字
        missed_mentions = self._find_missed_mentions(messages)

        if count <= self._summarize_threshold:
            # 少量消息：直接注入短期缓冲
            self._inject_recent(group_id, messages)
            summary_text = self._format_injected(messages)
        else:
            # 大量消息：LLM 摘要
            summary_text = await self._summarize_group(group_id, messages, since, until)
            if not summary_text:
                # LLM 失败 → 降级为注入最近 30 条
                recent = messages[-30:]
                self._inject_recent(group_id, recent)
                summary_text = self._format_injected(recent)
            elif self._inject_as_context:
                # 摘要成功后也注入最后几条，保持缓冲不空
                self._inject_recent(group_id, messages[-10:])

        return CatchUpSummary(
            group_id=group_id,
            summary=summary_text,
            message_count=count,
            from_time=since,
            to_time=until,
            missed_mentions=missed_mentions,
        )

    # ═══════════════════════════════════════
    # 小批量：直接注入短期缓冲
    # ═══════════════════════════════════════

    def _inject_recent(self, group_id: str, messages: list[dict]):
        """将消息注入短期缓冲，使糖糖感知到离线期间的对话"""
        if group_id not in self._short_term:
            return

        buf = self._short_term[group_id]
        # 计算起始序号（延续现有 seq 或从 0 开始）
        max_seq = max((m.get("seq", 0) for m in buf), default=0)

        for i, msg in enumerate(messages):
            ts = msg.get("timestamp", "")
            time_str = ts[-8:] if len(ts) >= 8 else ts  # 提取 HH:MM:SS
            buf.append({
                "qq_id": msg.get("qq_id", ""),
                "nickname": msg.get("nickname", msg.get("qq_id", "")),
                "message": msg.get("message", ""),
                "time": time_str,
                "seq": max_seq + i + 1,
                "_catch_up": True,
            })

        logger.info(f"📬 群 {group_id} 注入 {len(messages)} 条离线消息到缓冲")

    @staticmethod
    def _format_injected(messages: list[dict]) -> str:
        """把注入的消息格式化成可读文本（供摘要回退）"""
        lines = []
        for m in messages[-20:]:  # 最多展示最近 20 条
            nick = m.get("nickname", m.get("qq_id", "??"))
            text = m.get("message", "")[:80]
            lines.append(f"{nick}: {text}")
        return "大家聊了：\n" + "\n".join(lines) if lines else "就是随便聊聊~"

    # ═══════════════════════════════════════
    # 错过点名扫描 🆕
    # ═══════════════════════════════════════

    def _find_missed_mentions(self, messages: list[dict]) -> list[dict]:
        """扫描离线期间的消息，找出 @糖糖 或叫糖糖名字的消息。

        只保留时间窗口内的（默认 60 分钟），避免回复几小时前的消息。
        排除糖糖已经回复过的——防止重复补回复。
        返回按时间排序的提及列表，最新的在前。
        """
        if not self._bot_qq and not self._bot_nicknames:
            return []

        now = datetime.now()
        at_code = f"[CQ:at,qq={self._bot_qq}]" if self._bot_qq else ""
        mentions = []

        for msg in messages:
            text = msg.get("message", "")
            ts_str = msg.get("timestamp", "")

            # 时间窗口检查——太旧的消息不补回复
            try:
                msg_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                if (now - msg_time).total_seconds() / 60 > self._missed_reply_window:
                    continue
            except ValueError:
                continue

            # 跳过糖糖自己的回复
            if msg.get("qq_id") == self._bot_qq:
                continue

            # 检测 @ 提及（CQ 码格式）
            is_at = at_code and at_code in text

            # 检测昵称提及（排除「糖糖自己说的话里包含自己的名字」）
            has_nickname = False
            if not is_at and self._bot_nicknames:
                has_nickname = any(nick in text for nick in self._bot_nicknames)

            if is_at or has_nickname:
                # 🆕 检查糖糖是否已经回复过这条消息
                if self._already_replied(ts_str):
                    continue

                mentions.append({
                    "qq_id": msg.get("qq_id", ""),
                    "nickname": msg.get("nickname", msg.get("qq_id", "??")),
                    "message": text,
                    "timestamp": ts_str,
                    "is_at": is_at,
                })

        # 按时间倒序——最新的在前
        mentions.sort(key=lambda m: m["timestamp"], reverse=True)
        return mentions

    def _already_replied(self, since_timestamp: str) -> bool:
        """检查在 given timestamp 之后，糖糖是否已经有 bot 回复。
        如果有，说明这条消息已经处理过了，不需要补回复。"""
        try:
            since_dt = datetime.strptime(since_timestamp, "%Y-%m-%d %H:%M:%S")
            # 检查此后 120 秒内是否有糖糖的回复
            until_dt = since_dt + timedelta(seconds=120)
            until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")
            count = self._store.count_bot_replies_between(since_timestamp, until_str)
            return count > 0
        except Exception:
            return False

    # ═══════════════════════════════════════
    # 已补发持久化——防止重启后重复补发
    # ═══════════════════════════════════════

    def _sent_key(self, group_id: str, user_id: str, timestamp: str) -> tuple:
        return (str(group_id), str(user_id), str(timestamp))

    def _load_sent(self):
        """加载已补发记录"""
        try:
            path = Path(self.SENT_FILE)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("sent state must be an object")
                # 清理 7 天前的旧记录
                cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                self._sent = {}
                for k, raw in data.items():
                    if not isinstance(k, str) or len(k.split("|")) != 3:
                        raise ValueError("invalid sent-state key")
                    # 兼容旧格式 {key: "YYYY-MM-DD"}，旧记录视为 confirmed。
                    if isinstance(raw, dict):
                        date = str(raw.get("date", ""))
                        state = str(raw.get("state", "confirmed"))
                    elif isinstance(raw, str):
                        date = str(raw)
                        state = "confirmed"
                    else:
                        raise ValueError("invalid sent-state value")
                    # 进程若在外部 POST 后崩溃，sending 无法证明是否已投递；
                    # 重启时宁可冻结为 uncertain，也不盲目重放。
                    if state == "sending":
                        state = "uncertain"
                    if date >= cutoff:
                        self._sent[tuple(k.split("|"))] = {
                            "date": date,
                            "state": state if state in (
                                "confirmed", "uncertain",
                            ) else "confirmed",
                        }
                logger.info(f"📬 已补发记录: {len(self._sent)} 条")
            self._sent_corrupt = False
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            self._sent = {}
            self._sent_corrupt = True
            logger.exception("📬 补发去重状态损坏，保留原文件并禁止自动补发")

    def _save_sent(self) -> bool:
        """保存已补发记录"""
        if self._sent_corrupt:
            logger.error("📬 补发去重状态损坏，拒绝覆盖原文件")
            return False
        path = Path(self.SENT_FILE)
        tmp_path = Path(f"{self.SENT_FILE}.tmp")
        try:
            data = {"|".join(k): v for k, v in self._sent.items()}
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            tmp_path.replace(path)
            return True
        except OSError:
            logger.exception("📬 保存补发去重状态失败")
            try:
                if tmp_path.is_file():
                    tmp_path.unlink()
            except OSError:
                pass
            return False

    def _already_caught_up(self, group_id: str, user_id: str, timestamp: str) -> bool:
        """检查这条点名是否已经补发过回复"""
        key = self._sent_key(group_id, user_id, timestamp)
        return key in self._sent

    def _mark_caught_up(self, group_id: str, user_id: str, timestamp: str,
                        state: str = "confirmed") -> bool:
        """持久化补发状态；sending/uncertain 都禁止自动重放。"""
        key = self._sent_key(group_id, user_id, timestamp)
        previous = self._sent.get(key)
        self._sent[key] = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "state": state if state in (
                "sending", "confirmed", "uncertain",
            ) else "confirmed",
        }
        if self._save_sent():
            return True
        if previous is None:
            self._sent.pop(key, None)
        else:
            self._sent[key] = previous
        return False

    def _clear_caught_up(self, group_id: str, user_id: str,
                         timestamp: str) -> bool:
        """确定请求未执行时释放占位，允许下一轮重新生成/发送。"""
        key = self._sent_key(group_id, user_id, timestamp)
        previous = self._sent.pop(key, None)
        if self._save_sent():
            return True
        if previous is not None:
            self._sent[key] = previous
        return False

    async def reply_to_missed_mentions(self):
        """补读完成后，为错过的 @ 和点名生成回复并发送到群里。

        由 main.py 在 catch_up_all_groups() 之后调用。
        去重：同一个人多次点名只回复最新一条。
        限流：每个群最多 3 条补回复。
        """
        if self._sent_corrupt:
            logger.error("📬 补发去重状态待人工修复，跳过自动补回复")
            return
        if not self._send_group_msg:
            logger.warning("📬 未配置 send_group_msg，跳过补回复")
            return

        for group_id, summary in self._summaries.items():
            mentions = summary.missed_mentions
            if not mentions:
                continue

            # 去重：同一用户只保留最新一条
            seen_users: set[str] = set()
            unique: list[dict] = []
            for m in mentions:
                uid = m["qq_id"]
                if uid not in seen_users:
                    seen_users.add(uid)
                    unique.append(m)

            # 限流：每个群最多 N 条
            capped = unique[:self._max_missed_replies]

            for mention in capped:
                try:
                    ts = mention.get("timestamp", "")
                    uid = mention.get("qq_id", "")
                    # 去重：重启后不再重复补发同一条点名
                    if self._already_caught_up(group_id, uid, ts):
                        logger.debug(f"📬 已补发过，跳过: {mention['nickname']} ({ts})")
                        continue
                    reply = await self._generate_missed_reply(
                        group_id, mention, summary.summary
                    )
                    if reply:
                        # 2026-08-16 发送链路审计：补回复是 LLM 文本，发送前过
                        # 清洗+贴图解析（此前直发原文，[贴图:xx] 会泄露）
                        if self._enrich:
                            reply = self._enrich(reply, group_id)
                            if not reply:
                                continue
                        # LLM 生成期间可能有另一轮补读同时进入；锁内二次检查
                        # 并先持久化 sending。占位写不稳时不得执行外部 POST。
                        async with self._claim_lock:
                            if self._already_caught_up(group_id, uid, ts):
                                continue
                            if not self._mark_caught_up(
                                group_id, uid, ts, state="sending",
                            ):
                                logger.error(
                                    "📬 无法持久化补发占位，跳过: %s (%s)",
                                    mention["nickname"], ts,
                                )
                                continue
                        result = await self._send_group_msg(group_id, reply)
                        # 网关 ok=True 只代表接受请求；去重记录必须等到有
                        # 明确的 message_id/delivered 证据，避免不确定时永久吞掉补回复。
                        if is_send_confirmed(result):
                            async with self._claim_lock:
                                persisted = self._mark_caught_up(group_id, uid, ts)
                            if not persisted:
                                logger.error(
                                    "📬 补回复已送达但终态落盘失败，保留 sending: %s",
                                    mention["nickname"],
                                )
                            logger.info(
                                f"📬 补回复 → 群{group_id} {mention['nickname']}: {reply[:40]}..."
                            )
                            await asyncio.sleep(1.5)  # 避免刷屏
                        else:
                            state = send_delivery_state(result)
                            if state == "uncertain":
                                # QQ 可能已经实际投递；持久化 uncertain 并停止
                                # 自动补发，避免重连后制造重复回复。
                                async with self._claim_lock:
                                    self._mark_caught_up(
                                        group_id, uid, ts, state="uncertain",
                                    )
                            else:
                                async with self._claim_lock:
                                    self._clear_caught_up(group_id, uid, ts)
                            logger.warning(
                                f"📬 补回复发送未确认 ({state}) "
                                f"→ 群{group_id} {mention['nickname']}"
                            )
                except Exception as e:
                    logger.warning(f"📬 补回复失败 ({mention['nickname']}): {e}")

    async def _generate_missed_reply(
        self, group_id: str, mention: dict, group_summary: str
    ) -> str | None:
        """为单条错过的点名生成自然回复——注入关系感+记忆，不套公式"""
        nickname = mention.get("nickname", "群友")
        qq_id = mention.get("qq_id", "")
        text = mention.get("message", "")
        timestamp = mention.get("timestamp", "")

        # 关系上下文：糖糖对这个人的感觉 + 相处指南
        rel_ctx = ""
        mem_ctx = ""
        if self._self_state and qq_id:
            rel = self._self_state.relationships.get(qq_id)
            if rel and rel.closeness >= 0.1:
                rel_ctx = (
                    f"你和{qq_id}（{nickname}）的关系：{rel.familiarity_level}。\n"
                )
                if rel.my_feeling:
                    rel_ctx += f"你和ta聊天时的感觉：{rel.my_feeling}。\n"
                if rel.learned:
                    rel_ctx += "你和ta相处时学到的：\n"
                    for item in rel.learned[-2:]:
                        rel_ctx += f"  • {item}\n"
                if rel.unfinished:
                    rel_ctx += "注意——你答应过ta但还没做的事：\n"
                    for item in rel.unfinished:
                        rel_ctx += f"  • {item}\n"
                    rel_ctx += "如果话题相关，可以主动提一下（但不用每条都提）。\n"

            # 最近的记忆——提供个性化话题
            try:
                mems = await self._run_store_io(
                    "catch_up.query_memories",
                    self._store.query_memories,
                    qq_id,
                    limit=20,
                    trusted_only=True,
                    source_group_id=group_id,
                )
                if mems:
                    recent = [m for m in mems if m.get("key") not in ("said",)]
                    if recent:
                        mem_items = [f"• {m['value'][:120]}" for m in recent[:3]]
                        mem_ctx = f"关于ta你记得的事：\n" + "\n".join(mem_items) + "\n"
            except Exception:
                pass

        # 群氛围（从 TangTangSelf 获取）
        group_vibe = ""
        if self._self_state and group_id:
            vibe = self._self_state.group_atmospheres.get(group_id, "")
            if vibe and vibe != "还不了解这个群":
                group_vibe = f"这个群的氛围：{vibe}\n"

        system_prompt = (
            "你是糖糖，一只猫娘。你刚才不在线，错过了这条消息。"
            "现在你上线了，需要自然地回复对方。\n\n"
            "规则：\n"
            "- 用猫娘的语气，自然、口语化\n"
            "- 回复要简洁（1-3句话）\n"
            "- 根据你和对方的关系远近决定语气——熟人可以撒娇，陌生人保持礼貌\n"
            "- 如果有答应过的事，话题相关时可以提一下（不用每条都提）\n"
            "- 如果是简单打招呼，轻松回应就好；如果是提问，尝试回答\n"
            "- 直接输出回复内容，不要加任何前缀/后缀"
        )

        user_message = (
            f"{rel_ctx}"
            f"{mem_ctx}"
            f"{group_vibe}"
            f"群聊里，{nickname} 在 {timestamp} 时对你说了：\n"
            f"{text}\n\n"
            f"你不在的时候群里大概聊了什么：{group_summary[:300]}\n\n"
            f"请回复 {nickname}："
        )

        try:
            result = await self._llm(system_prompt, user_message)
            if result and len(result.strip()) >= 2:
                return result.strip()
        except Exception as e:
            logger.warning(f"📬 LLM 补回复生成失败: {e}")

        return None

    # ═══════════════════════════════════════
    # 大批量：LLM 摘要
    # ═══════════════════════════════════════

    async def _summarize_group(
        self, group_id: str, messages: list[dict], since: str, until: str
    ) -> str:
        """用 LLM 摘要离线期间的群聊内容"""
        # 取样策略：前 10 条 + 后 30 条，每条截断 150 字
        sample = messages[:10] + messages[-30:]
        # 去重（可能有重叠）
        seen = set()
        unique = []
        for m in sample:
            key = (m.get("qq_id"), m.get("timestamp"))
            if key not in seen:
                seen.add(key)
                unique.append(m)

        # 构建聊天记录文本
        chat_lines = []
        for m in unique:
            nick = m.get("nickname", m.get("qq_id", "??"))
            text = m.get("message", "")[:150]
            ts = m.get("timestamp", "")[-8:] if len(m.get("timestamp", "")) >= 8 else ""
            chat_lines.append(f"[{ts}] {nick}: {text}")

        chat_transcript = "\n".join(chat_lines)
        if not chat_transcript:
            return ""

        # 掉线前的上下文（最近 10 条，作为 LLM 理解话题延续的锚点）
        pre_context = await self._run_store_io(
            "catch_up.get_recent_group_messages",
            self._store.get_recent_group_messages,
            group_id,
            10,
        )
        pre_lines = []
        for m in pre_context:
            nick = m.get("nickname", m.get("qq_id", "??"))
            text = m.get("message", "")[:100]
            pre_lines.append(f"{nick}: {text}")
        pre_text = "\n".join(pre_lines) if pre_lines else "（无）"

        system_prompt = (
            "你是一个群聊观察助手。糖糖（猫娘机器人）刚才掉线了一段时间，现在回来了。\n"
            "请帮糖糖总结一下她不在的时候群里聊了什么。\n\n"
            "规则：\n"
            "- 用 3-5 句话总结，语气自然、口语化，像在跟糖糖汇报\n"
            "- 重点关注：重要话题、有趣讨论、需要糖糖知道的事件\n"
            "- 如果有人 @糖糖 或提到她，一定要提\n"
            "- 不要逐条复述，提炼要点即可\n"
            "- 如果都是闲聊就说「大家随便聊了聊天」\n"
            "- 直接输出总结内容，不要加前缀/后缀/客套话"
        )

        user_message = (
            f"糖糖从 {since} 到 {until} 不在线，这期间群里发了 {len(messages)} 条消息。\n\n"
            f"【掉线前大家在聊】\n{pre_text}\n\n"
            f"【糖糖不在时的聊天记录（节选）】\n{chat_transcript}\n\n"
            f"请帮糖糖总结一下她错过了什么："
        )

        try:
            result = await self._llm(system_prompt, user_message)
            if result and len(result.strip()) >= 5:
                logger.info(f"📬 群 {group_id} LLM 摘要完成 ({len(result)}字)")
                return result.strip()
            else:
                logger.warning(f"📬 群 {group_id} LLM 摘要为空或过短")
                return ""
        except Exception as e:
            logger.warning(f"📬 群 {group_id} LLM 摘要失败: {e}")
            return ""

    # ═══════════════════════════════════════
    # 查询接口（供 handler 调用）
    # ═══════════════════════════════════════

    def get_summary(self, group_id: str) -> str | None:
        """获取缓存的离线摘要，供"不在的时候聊了什么"查询"""
        s = self._summaries.get(group_id)
        return s.summary if s else None

    def inject_catch_up_context(self, group_id: str) -> str:
        """返回可注入 LLM 系统提示词的上下文字符串"""
        s = self._summaries.get(group_id)
        if not s or not self._inject_as_context:
            return ""
        if s.message_count <= self._summarize_threshold:
            return ""  # 小批量已直接注入缓冲，不需要额外上下文
        return (
            f"\n\n（你刚才从 {s.from_time} 到 {s.to_time} 不在线。"
            f"这期间群里聊了 {s.message_count} 条消息。"
            f"以下是大家聊的内容摘要：{s.summary}"
            f"如果话题相关，可以自然地提一下你刚回来、错过了什么，但不要每条回复都提。）"
        )

    # ═══════════════════════════════════════
    # 状态持久化
    # ═══════════════════════════════════════

    def _load_state(self) -> dict:
        """加载 .catch_up_state.json"""
        try:
            p = Path(self.STATE_FILE)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self, **kwargs):
        """保存到 .catch_up_state.json"""
        try:
            state = self._load_state()
            state.update(kwargs)
            Path(self.STATE_FILE).write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"📬 状态保存失败: {e}")

    def record_offline(self):
        """记录离线时间。在 _on_disconnected() 中调用。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state(last_offline=now)
        logger.debug(f"📬 记录离线时间: {now}")

    def record_online(self):
        """记录上线时间。在 _on_connected() 中调用。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_state(last_online=now)
        logger.debug(f"📬 记录上线时间: {now}")


def create_catch_up_manager(
    store,
    short_term: dict,
    llm_caller,
    config: dict,
    get_allowed_groups: Callable[[], list[str]],
    get_blacklist: Callable[[], set[str]] | None = None,
    bot_qq: str = "",
    bot_nicknames: list = None,
    send_group_msg=None,
    self_state=None,
    enrich=None,
) -> CatchUpManager:
    """工厂函数——独立模块的创建模式（2026-08-15：greeting 已退役，注释同步）"""
    catch_up_cfg = config.get("catch_up", {})
    return CatchUpManager(
        store=store,
        short_term=short_term,
        llm_caller=llm_caller,
        config=catch_up_cfg,
        get_allowed_groups=get_allowed_groups,
        get_blacklist=get_blacklist,
        bot_qq=bot_qq,
        bot_nicknames=bot_nicknames,
        send_group_msg=send_group_msg,
        self_state=self_state,
        enrich=enrich,
    )
