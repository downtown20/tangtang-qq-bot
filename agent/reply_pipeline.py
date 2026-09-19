"""
回复处理管道 💬
清洗 LLM 输出 → 贴图替换 → @ 解析 → 润色 → 分句 → 发送

从 handler.py 提取，不改行为只移代码。
"""

import asyncio
import logging
import random
import re

from onebot.ws_client import SendResult

from .async_io import run_bounded_blocking, run_bounded_store_io
from .sticker import get_face_for_text

logger = logging.getLogger("糖糖.Reply")


def _has_structured_content(text: str) -> bool:
    """检测回复是否包含结构化内容（标题/列表/代码块等），若有则保留 Markdown 格式"""
    return bool(re.search(
        r'^#{1,3}\s|'          # 标题
        r'^\d+\.\s|'           # 有序列表
        r'^[-*]\s|'            # 无序列表
        r'```|'                # 代码块
        r'^\s*[-=]{3,}\s*$|'   # 分隔线
        r'^\s*[│┌├└].*[│┌├└]',  # 表格/ASCII art
        text, re.MULTILINE
    ))


def _has_affirmed_nsfw_context(text: str) -> bool:
    """仅在完整 NSFW 词未被邻近否定时放行成人贴图。"""
    import jieba

    words = [word.strip() for word in jieba.cut(text) if word.strip()]
    nsfw_words = {"色色", "涩涩", "淫", "淫秽", "骚", "肉棒", "高潮", "呻吟", "欲火", "裸", "裸露"}
    negations = {"不要", "别", "不", "反对", "抵制", "禁止", "拒绝", "避免", "制止"}
    for pos, word in enumerate(words):
        if word not in nsfw_words:
            continue
        if not any(previous in negations for previous in words[max(0, pos - 3):pos]):
            return True
    return False


class ReplyPipeline:
    """回复后处理管道"""

    def __init__(self, napcat, stickers, store, short_term: dict, bot_nicknames: list[str], bot_qq: str,
                 embed_engine=None):
        self.napcat = napcat
        self.stickers = stickers
        self.store = store
        self.short_term = short_term
        self.bot_nicknames = set(bot_nicknames)
        self.bot_qq = bot_qq
        self.embed_engine = embed_engine
        # 最近一次整条回复的聚合发送证据。公开 send() 仍返回 bool 以兼容
        # 旧调用点，但 bool 现在表示「所有分段已确认送达」。
        self.last_send_result: SendResult | None = None
        # 只在整条回复 confirmed 时保存实际送达正文。富媒体硬拒绝后若降级为
        # 纯文字，上层必须提交纯文字，不能把未送达的 CQ 图片写进历史/记忆。
        self.last_confirmed_payload = ""

    @staticmethod
    def _coerce_send_result(result) -> SendResult:
        """把旧版 bool/第三方替身转换为统一的发送结果。"""
        if isinstance(result, SendResult):
            return result
        accepted = bool(result)
        delivered = getattr(result, "delivered", None)
        if delivered is None:
            delivered = accepted
        return SendResult(
            accepted,
            bool(delivered),
            error="" if accepted else "SEND_FAILED",
        )

    # ═══════════════════════════════════════
    # 引用判断
    # ═══════════════════════════════════════

    def should_quote(self, reply: str, msg: dict) -> bool:
        """判断是否应该引用回复——只在有必要时引用，像真人。
        引用：@了糖糖、或直接回应具体问题。
        不引用：主动插话、日常闲聊。"""
        # @糖糖 → 总是引用，表示"我在回你这句"（平台惯例）
        if msg.get("is_at_bot"):
            return True
        # 主动插话 → 不引用，像真人突然加入话题一样自然
        if msg.get("_is_interjection"):
            return False
        # 2026-08-16 范式转换：名字提及分支已删——加不加引用前缀是系统按
        # 内容匹配追加格式，交给 LLM 的 [贴图:/@] 类显式协议，不再自动判断
        return False

    # ═══════════════════════════════════════
    # 清洗
    # ═══════════════════════════════════════
    # 2026-08-16 范式转换（教训 #24）：公式化开头黑名单已删——
    # 系统不按内容匹配改写 LLM 输出；偶尔公式化由提示词自纠，可接受

    def clean(self, reply: str) -> str:
        """清洗回复——去情绪标签、假CQ码、控制字符"""
        original = reply
        # 2026-08-15：剥掉困难轮次的隐藏思考段（<思考>…</思考> 是 LLM 的内心活动，
        # 协议要求它写下来理清思路——但绝不能让对方看到）
        reply = re.sub(r'<思考>.*?</思考>', '', reply, flags=re.DOTALL)
        # 2026-08-15 Codex：未闭合的 <思考>（LLM 忘写 </思考>）整段都是内心活动，全剥
        reply = re.sub(r'<思考>.*$', '', reply, flags=re.DOTALL)
        # 删除开头的情绪标签：[温柔] [开心] [害羞] 等
        reply = re.sub(r'^\s*\[(?!贴图[：:])[^\]]{1,5}\]\s*', '', reply)
        # 删除LLM瞎编的CQ码——只保留真实CQ码（file=32位hex MD5），
        # LLM会编造假路径甚至半截碎片（[CQ:im...），全部洗掉
        reply = re.sub(
            r'\[CQ:image,'
            r'file=(?![\da-fA-F]{32}(?:,|\]|$))'  # 不是32位hex → 假的
            r'.*?'
            r'(?:\]|$)',
            '', reply, flags=re.DOTALL
        )
        # 补刀：清洗任何不完整的 [CQ: 碎片（LLM 有时只输出半截）
        reply = re.sub(r'\[CQ:\S{0,20}$', '', reply)
        # LLM 文本不允许直接携带 OneBot 动作码。仅暂留 CQ:at 给后续群成员
        # 校验；图片/语音等媒体必须由工具动作在清洗后单独发送。
        reply = re.sub(r'\[CQ:(?!at(?:,|\]))[^\]]*\]', '', reply)
        # 2026-08-16：LLM 回显的图片占位符（「[图片:[动画表情]]」现场）不是
        # 糖糖的输出协议——发送前洗掉（历史/当前消息层另有中性化）
        reply = re.sub(r'\[图片\s*[:：]?\s*[^\]]{0,30}\]+', '', reply)
        # QQ 不渲染 **bold** 和 *italic*，永远洗掉
        # 但保留颜文字（如 (*^▽^*) (๑´ㅂ`๑)）——只洗掉纯文字用 * 包裹的情况
        reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
        reply = re.sub(r'(?<!\*)\*(?!\*)([^*\n]*[\w一-鿿぀-ヿ][^*\n]*)(?<!\*)\*(?!\*)', r'\1', reply)
        # 其他 Markdown 格式——结构化内容（列表/代码块等）保留
        if not _has_structured_content(reply):
            reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
            reply = re.sub(r'__(.+?)__', r'\1', reply)
            reply = re.sub(r'~~(.+?)~~', r'\1', reply)
        # 洗掉 [SING:...] / [SING] 唱歌标记（不显示在文本里，音频由系统单独发送）
        reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
        reply = re.sub(r'\[SING\]', '', reply)
        # 洗掉色色模式切换标记（2026-08-17）——状态机在 handler 处理；
        # 这里是兜底防线：任何发送路径都不许把协议标记漏给用户
        reply = reply.replace("[进入色色]", "").replace("[退出色色]", "")
        # 洗掉控制字符和不可见 Unicode
        reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏- ⁠-⁯﻿]', '', reply)
        reply = reply.strip()
        # 拦截空白/无意义回复
        if not reply or (len(reply) < 2 and not original.strip()):
            logger.warning(f"clean() 丢弃空白回复 → 原始: {original[:80]!r}")
            return ""  # 真的空内容，放弃
        # 洗得太狠了，回退到"经过安全清洗"的原始回复
        # 2026-08-10 修复：之前直接回退原始原文——会把刚移除的
        # [SING:] 唱歌标记/[贴图:] 标签/[CQ:] 控制码/不可见字符重新带回来
        if len(reply) < 3:  # （not reply 是死条件——137 行已对空返回）
            logger.warning(f"clean() 洗太狠了 ({len(original)}字变{len(reply)}字)，回退到安全清洗版")
            safe = original.strip()
            # 2026-08-15 Codex 复查：回退分支必须同样剥思考段——
            # 「<思考>…</思考>好」剥离后剩 1 字会走到这里，不剥就把内心活动发给了用户
            safe = re.sub(r'<思考>.*?</思考>', '', safe, flags=re.DOTALL)
            # 2026-08-15 整体审查 Critical：闭合段剥离后可能还留未闭合尾巴
            # （「<思考>…</思考>好<思考>还没想完」→ 剩「好<思考>还没想完」）
            # ——未闭合的 <思考> 整段是内心活动，与主路径同规则全剥
            safe = re.sub(r'<思考>.*$', '', safe, flags=re.DOTALL)
            safe = re.sub(r'\[SING:[^\]]*\]', '', safe)
            safe = re.sub(r'\[SING\]', '', safe)
            safe = re.sub(r'\[贴图:[^\]]*\]', '', safe)
            safe = re.sub(r'\[CQ:[^\]]*\]', '', safe)
            safe = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏⁠-⁯﻿]', '', safe)
            safe = safe.strip()
            if not safe:
                logger.warning("clean() 回退后仍为空（可能只有思考段）→ 放弃发送")
                return ""
            return safe
        return reply

    # ═══════════════════════════════════════
    # 贴图替换
    # ═══════════════════════════════════════

    def resolve_sticker_tags(self, reply: str) -> str:
        """把 LLM 输出的 [贴图:关键词] 替换为真正的表情包 CQ 码。
        自动过滤色色标签（除非回复内容已有色色上下文）。"""
        allow_nsfw = _has_affirmed_nsfw_context(reply)

        def _replace_sticker(match: re.Match) -> str:
            keyword = match.group(1).strip()
            if not keyword:
                return ""

            excluded = set() if allow_nsfw else {"色色"}
            results = self.stickers.match_by_emotion_text(
                keyword, embed_engine=self.embed_engine, count=1, excluded=excluded
            )
            return results[0] if results else ""

        pattern = re.compile(r'\[贴图[：:]([^\]]{1,10})\]')
        return pattern.sub(_replace_sticker, reply)

    # ═══════════════════════════════════════
    # @ 提及解析
    # ═══════════════════════════════════════

    def resolve_at_mentions(self, reply: str, group_id: str) -> str:
        """
        把 LLM 输出的 @昵称 替换为真正的 QQ @ CQ 码。
        只在记忆库中能找到的昵称才替换，避免 LLM 瞎编的名字变成无效 @。
        找不到匹配的 @xx 会被移除，避免残留无效的 @ 文本。
        """
        pattern = re.compile(r'(?<!\w)@([^\s@,，。！？!?\n　\'\"\(\)\[\]【】《》（）\/\\:“”‘’…—]{2,15})')

        # 收集当前群聊中活跃的群友
        active_nicks: dict[str, str] = {}
        if group_id in self.short_term:
            for msg in self.short_term[group_id]:
                nick = msg.get("nickname", "")
                qq = msg.get("qq_id", "")
                if nick and qq and qq != self.bot_qq:
                    active_nicks[nick] = qq

        def _resolve_one(match: re.Match) -> str:
            nickname = match.group(1)

            # 过滤：纯数字/纯标点/单字母不像真名 → 删除整个 @
            if re.match(r'^[\d\W_]+$', nickname):
                return ""

            # 不替换糖糖自己 → 删除（自己 @ 自己没意义）
            if nickname in self.bot_nicknames:
                return ""

            # 1. 活跃群友
            if nickname in active_nicks:
                qq = active_nicks[nickname]
                logger.info(f"👆 @{nickname} → QQ:{qq}")
                return f"[CQ:at,qq={qq}] "

            # 2. people / aliases 表
            qq = self.store.find_qq_for_at(nickname, self.bot_qq)
            if qq:
                logger.info(f"👆 @{nickname} → QQ:{qq} (from DB)")
                return f"[CQ:at,qq={qq}] "

            # 3. 找不到 → 删除这个 @xx
            logger.debug(f"👆 @{nickname} 未找到匹配，已移除")
            return ""

        result = pattern.sub(_resolve_one, reply)
        # 清理可能残留的孤立 @ 符号
        result = re.sub(r'(?<!\S)@(?=\s|$)', '', result)
        # 2026-08-16 Codex I3：CQ@ 校验改 fail-closed——匹配**所有** [CQ:at...]
        # 变体（qq=all/带参/空格）；目标必须属于当前群（活跃 buffer + group_members），
        # 不再用全局 person_exists 放行跨群 QQ；坏标签一律删（保正文）。
        def _group_member_qqs() -> set:
            qqs = {v for v in active_nicks.values()}
            if group_id:
                try:
                    for m in self.store.get_group_members(group_id):
                        qqs.add(str(m.get("qq_id", "")))
                except Exception:
                    pass
            return {q for q in qqs if q}

        def _validate_cq_at(match: re.Match) -> str:
            inner = match.group(1)
            m = re.search(r'qq=(\d+)', inner)
            if not m:
                logger.warning(f"👆 无效 CQ@变体已移除: [CQ:at{inner}]")
                return ""  # qq=all / 缺 qq / 无法解析——fail-closed
            qq = m.group(1)
            if qq == self.bot_qq:
                return ""  # @自己无意义
            members = _group_member_qqs()
            if qq in members:
                return f"[CQ:at,qq={qq}] "  # 规范化干净形式（剥掉多余参数）
            if len(qq) < 6:
                # 截断修复：恰好一个当前群成员 QQ 以该短号结尾才算（不猜）
                candidates = [v for v in members if v.endswith(qq)]
                if len(candidates) == 1:
                    logger.info(f"👆 CQ@截断修复 {qq} → {candidates[0]}")
                    return f"[CQ:at,qq={candidates[0]}] "
            logger.warning(f"👆 无效 CQ@已移除: qq={qq}（不在当前群）")
            return ""

        return re.sub(r'\[CQ:at([^\]]*)\]', _validate_cq_at, result)

    # ═══════════════════════════════════════
    # 回复润色
    # ═══════════════════════════════════════

    def enrich(self, reply: str, user_text: str = "", intimacy: int = 0,
               group_id: str = "") -> str:
        """回复润色——亲密度越高，表情包越多"""
        reply = self.clean(reply)
        if not reply:
            return ""  # 清洗后无内容，跳过发送

        # [贴图:关键词] → 替换为实际表情包
        reply = self.resolve_sticker_tags(reply)

        # @提及解析
        if group_id:
            reply = self.resolve_at_mentions(reply, group_id)
        else:
            # 私聊没有群成员范围可供校验，任何 LLM 直写 CQ@ 都 fail-closed。
            reply = re.sub(r'\[CQ:at[^\]]*\]', '', reply).strip()

        # 2026-08-16 范式转换（教训 #24）：随机附加贴图已删——贴图时机与内容
        # 100% 由 LLM 决定（[贴图:xx] / send_stickers 工具）。此前系统按
        # 亲密度+随机给回复附加贴图、并按关键词匹配 LLM 回复正文选图——
        # 系统替 LLM 决定"要不要贴图、贴哪张"。
        return reply

    async def enrich_async(self, reply: str, user_text: str = "", intimacy: int = 0,
                           group_id: str = "") -> str:
        """异步回复润色：将贴图匹配和 @解析移出事件循环。"""
        reply = self.clean(reply)
        if not reply:
            return ""
        reply = await run_bounded_blocking(
            "reply.resolve_sticker_tags",
            self.resolve_sticker_tags,
            reply,
            logger=logger,
            log_prefix="💬 回复贴图解析较慢",
        )
        if group_id:
            reply = await run_bounded_store_io(
                "reply.resolve_at_mentions",
                self.resolve_at_mentions,
                reply,
                group_id,
                logger=logger,
                log_prefix="💬 回复@解析 Store 调用较慢",
            )
        else:
            reply = re.sub(r'\[CQ:at[^\]]*\]', '', reply).strip()
        return reply

    def _pick_sticker_by_emotion(self, text: str) -> str | None:
        """根据文字情绪选表情包，与工具/标签路径共用匹配器。"""
        allow_nsfw = _has_affirmed_nsfw_context(text)
        excluded = set() if allow_nsfw else {"色色"}
        results = self.stickers.match_by_emotion_text(
            text, embed_engine=self.embed_engine, count=1, excluded=excluded
        )
        return results[0] if results else None

    # ═══════════════════════════════════════
    # 分句 + 发送
    # ═══════════════════════════════════════

    def maybe_split_reply(self, reply: str) -> list[str]:
        """将长回复按句号/感叹号拆成2段，模拟真人一句句发"""
        if len(reply) < 40:
            return [reply]

        # 基于长度决断而非随机：>80 字必拆，40-80 按段落拆
        if len(reply) < 80 and random.random() > 0.50:
            return [reply]

        if reply.startswith("[CQ:"):
            return [reply]

        breaks = []
        for i, ch in enumerate(reply):
            if ch in "。！？!?\n":
                breaks.append(i + 1)

        if not breaks:
            return [reply]

        mid = len(reply) // 2
        best = None
        for b in breaks:
            if b < 15:
                continue
            if b > len(reply) - 10:
                break
            best = b
            if b >= mid:
                break

        if best is None:
            return [reply]

        part1 = reply[:best].strip()
        part2 = reply[best:].strip()

        if len(part2) < 5:
            return [reply]

        return [part1, part2]

    async def send(self, target_type: str, target_id: str, reply: str,
                   group_id: str = "") -> bool:
        """发送回复，长回复可能拆成2段分开发。含富媒体时增加间隔防限流。
        如果含CQ图片的段落发送失败，自动剥掉图片重试——至少文字内容要送达。
        group_id：私聊目标的最近群（2026-08-15）——非好友时 send_private_message
        走群临时会话兜底，不再直接失败。返回 True 表示全部发送成功。"""
        # 2026-08-15：私聊不拆句——一条回复拆成两条气泡，主人会觉得第二条是
        # 与对话无关的主动消息（现场：回复被拆成「睡饱了没」+「肚子饿不饿」两条）。
        parts = self.maybe_split_reply(reply) if target_type == "group" else [reply]
        results: list[SendResult] = []
        actual_parts: list[str] = []
        self.last_send_result = None
        self.last_confirmed_payload = ""

        for i, part in enumerate(parts):
            actual_part = part
            # 含图片/表情/语音时多等一会儿，避免触发 QQ 的富媒体频率限制
            has_media = any(tag in part for tag in ("[CQ:image", "[CQ:record", "[CQ:video"))
            if has_media and i > 0:
                await asyncio.sleep(random.uniform(2.0, 4.0))

            try:
                if target_type == "group":
                    result = self._coerce_send_result(
                        await self.napcat.send_group_message(target_id, part)
                    )
                else:
                    result = self._coerce_send_result(
                        await self.napcat.send_private_message(
                            target_id, part, group_id=group_id,
                        )
                    )
            except Exception as exc:
                # POST 后响应丢失不能当成确定失败。把已确认的前段 ID 一并
                # 聚合，避免上层重发整条回复造成前段重复。
                result = SendResult(
                    False, False, error="NETWORK_UNCERTAIN",
                    retryable=False, uncertain=True,
                )
                logger.exception(
                    "发送第%d/%d段响应丢失，冻结为未确认: %s",
                    i + 1, len(parts), exc,
                )

            # 图片被网关明确拒绝时才剥图重试。网络失败与响应丢失不能证明
            # 图片有问题；尤其 POST 可能已执行，第二次发纯文字会制造重复。
            if (result.delivery_state == "failed"
                    and not result.retryable and has_media):
                text_only = re.sub(r'\[CQ:image,[^\]]+\]', '', part).strip()
                if text_only and text_only != part.strip():
                    logger.info(f"🔄 图片发送失败，剥掉图片重试纯文字 ({i+1}/{len(parts)})")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    actual_part = text_only
                    try:
                        if target_type == "group":
                            result = self._coerce_send_result(
                                await self.napcat.send_group_message(target_id, text_only)
                            )
                        else:
                            result = self._coerce_send_result(
                                await self.napcat.send_private_message(
                                    target_id, text_only, group_id=group_id,
                                )
                            )
                    except Exception as exc:
                        result = SendResult(
                            False, False, error="NETWORK_UNCERTAIN",
                            retryable=False, uncertain=True,
                        )
                        logger.exception(
                            "纯文字兜底响应丢失，冻结为未确认: %s", exc,
                        )

            results.append(result)
            actual_parts.append(actual_part)
            if not result.ok:
                logger.error(f"❌ 发送失败 ({i+1}/{len(parts)}): target={target_id} content={part[:60]!r}")
            elif not result.delivered:
                logger.warning(
                    f"⚠️ 发送结果未确认 ({i+1}/{len(parts)}): target={target_id} "
                    f"error={result.error or 'MESSAGE_ID_UNCONFIRMED'}"
                )

            # 分段回复有顺序依赖：前段未确认或明确失败后继续发后段，会把
            # 一条回复撕成无法核验的半截对话，并扩大重复发送的不确定面。
            if result.delivery_state != "confirmed":
                break

            if len(parts) > 1 and i < len(parts) - 1:
                await asyncio.sleep(random.uniform(1.0, 2.5))
                logger.info(f"📤 分段发送 ({i+1}/{len(parts)})")

        accepted = bool(results) and all(result.ok for result in results)
        delivered = bool(results) and all(result.delivered for result in results)
        message_ids: list[int] = []
        for result in results:
            if result.chunk_ids:
                message_ids.extend(result.chunk_ids)
            elif result.message_id:
                message_ids.append(result.message_id)
        first_problem = next((r for r in results if not r.delivered), None)
        partial_delivery = first_problem is not None and any(
            result.delivered for result in results
        )
        aggregate = SendResult(
            accepted,
            delivered,
            message_id=message_ids[-1] if message_ids else 0,
            chunk_ids=tuple(message_ids),
            error=(
                "PARTIAL_DELIVERY:" + (first_problem.error or "UNCONFIRMED")
                if partial_delivery
                else (first_problem.error if first_problem else "")
            ),
            retcode=(first_problem.retcode if first_problem else None),
            retryable=(
                not partial_delivery
                and not any(r.delivery_state == "uncertain" for r in results)
                and any(r.retryable for r in results)
            ),
            uncertain=(partial_delivery or any(
                r.delivery_state == "uncertain" for r in results
            )),
        )
        self.last_send_result = aggregate
        if delivered:
            self.last_confirmed_payload = "".join(actual_parts)
        # 保持公开 bool 契约，但以 confirmed 为准，避免 SendResult.ok 泄漏到
        # 上层状态提交。
        return aggregate.delivered
