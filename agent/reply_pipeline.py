"""
回复处理管道 💬
清洗 LLM 输出 → 贴图替换 → @ 解析 → 润色 → 分句 → 发送

从 handler.py 提取，不改行为只移代码。
"""

import asyncio
import inspect
import logging
import random
import re

from onebot.ws_client import SendResult

from .async_io import run_bounded_blocking, run_bounded_store_io
from .protocols import strip_protocol_markers, strip_quote_marker, strip_split_markers

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
        # T2.2/T2.3（2026-10-13，审计 P0-3 / ADR-002:64/72 / ADR-003:23/128）：
        # 按**确认粒度**保存实际送达的段与段边界。
        #   - last_confirmed_payload：整条 confirmed 才写、无边界拼接 —— 只回答
        #     「整条是否送达」，多气泡在它眼里就是一条（审计现状 ②）；
        #   - 本字段：有确认就写、逐段保真 —— 回答「哪几段送达了、各自是什么」，
        #     供 handler 逐段归账（审计现状 ① 零归账的正解）。
        # 两个字段同时维护：旧调用方读旧字段，语义零变化。
        self.last_delivered_parts: list[str] = []
        self.last_delivered_chunk_ids: tuple[int, ...] = ()

    @staticmethod
    def _accepts_receipt_template(sender) -> bool:
        """发送器是否接受 receipt_template 参数。

        T2.4 的模板只对**真实**适配器有意义；旧替身/第三方适配器没有这个
        参数。直接传会抛 TypeError，而发送循环的 except 会把它误判成
        **网络不确定**——那是最糟的一种错：把程序错误记成网络故障，
        还会让上层以为消息可能已发出。所以先问签名，再决定传不传。
        """
        try:
            parameters = inspect.signature(sender).parameters
        except (TypeError, ValueError):
            # **问不出来 != 不接受**。AsyncMock / 某些 C 实现 / 带 __signature__
            # 异常的可调用体查不出签名，但它们通常照单全收（实测 AsyncMock 能
            # 接受该关键字）。这里返回 False 会造成**静默丢弃模板**（教训 #30 同型），
            # 且已让 tests/test_handler_send_confirmation.py 的一条断言变红。
            # 返回 True 的风险面已被上面两条「程序错误不记成网络故障」兜住：
            # 真不接受时会抛 TypeError，被归类为 SEND_PROGRAMMING_ERROR（确定失败、
            # 不重试、大声报错），而不是伪装成 NETWORK_UNCERTAIN。
            return True
        parameter = parameters.get("receipt_template")
        if parameter is not None:
            # 仅位置参数（def f(t, m, receipt_template=None, /)）名字在签名里，
            # 但**不接受关键字**——按名字判定会误判成可传，实调抛 TypeError，
            # 而发送循环会把它记成 NETWORK_UNCERTAIN（程序错误伪装成网络故障）。
            return parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        return any(
            candidate.kind is inspect.Parameter.VAR_KEYWORD
            for candidate in parameters.values()
        )

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
    # 引用判断 —— 已迁走（2026-09-23），别在这里加回来
    # ═══════════════════════════════════════
    # 这里曾有 should_quote(reply, msg)：`is_at_bot` → 无条件引用。
    # 它让群聊 **53.2%** 的回复都带引用前缀（实测 4836/9086），而真人不这样——
    # 被 @ 到时对方已经知道你在回谁，再引用是冗余。
    # 更根本的是它**替 LLM 做了决策**（教训 #9/#24：系统不替 LLM 判断）。
    #
    # 现在唯一决策源是 `[引用]` 标记：她写才挂，不写就不挂。
    # 系统只保留「轮次」职责——用哪个 msg_id、标记挂在第一段。
    # 读取点在 handle_group_message（**清洗之前**，见 protocols.has_quote_marker）。

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
        # 2026-10-14 T26：原来的判据是「星号之间至少有 1 个字词字符」，
        # 而 `_` 和 ASCII 字母都算 \w —— 于是 (*^_^*) 这种常见颜文字被吃成 (^_^)。
        # 改成要求**至少 2 个连续**字词/字符：^_^ 里没有连续两个字词字符，
        # 颜文字因此保住；*abc* / *重点* 仍被正常洗掉。
        reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
        reply = re.sub(r'(?<!\*)\*(?!\*)([^*\n]*[\w一-鿿぀-ヿ]{2,}[^*\n]*)(?<!\*)\*(?!\*)', r'\1', reply)
        # 其他 Markdown 格式——结构化内容（列表/代码块等）保留
        if not _has_structured_content(reply):
            reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
            reply = re.sub(r'__(.+?)__', r'\1', reply)
            reply = re.sub(r'~~(.+?)~~', r'\1', reply)
        # 洗掉**全部**协议标记：唱歌 [SING:]、色色切换、[分段]、[引用]、[不说话]。
        # 2026-10-12 M2（审计 P2）：从「一条条列」改成统一出口
        # strip_protocol_markers()——上一轮正是因为一条条列，漏了 [不说话]
        # （grep 0 命中），而语音路径又另有一套清洗，标记会被念出来。
        # 正常路径都在 clean() **之前**就把这些标记读成结构化事实了
        # （parse_split_markers / has_quote_marker / parse_silence）；
        # 这里只剥不判，是兜底防线——只剥不拆是刻意的失败方向：
        # 少拆一条不影响对话，漏出标记会当众穿帮。新增标记只改 protocols 一处。
        reply = strip_protocol_markers(reply)
        # 洗掉控制字符和不可见 Unicode
        reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏ ⁠-⁯﻿]', '', reply)
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
            safe = re.sub(r'\[贴图:[^\]]*\]', '', safe)
            safe = re.sub(r'\[CQ:[^\]]*\]', '', safe)
            # 2026-09-22/09-23 同主路径：回退分支最容易把刚剥掉的协议标记带回来
            safe = strip_protocol_markers(safe)
            safe = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏ ⁠-⁯﻿]', '', safe)
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


    # ═══════════════════════════════════════
    # 分句 + 发送
    # ═══════════════════════════════════════

    def maybe_split_reply(self, reply: str) -> list[str]:
        """将长回复按句号/感叹号拆成2段，模拟真人一句句发"""
        if len(reply) < 40:
            return [reply]

        # 基于长度决断：>80 字必拆，40-80 不拆（按段落整条发）。
        # 2026-10-14 台账 T13：这里原本在 40-80 区间用一个随机数决定拆不拆，
        # 同一输入两次发送行为不同——这是「消息密度不合理」的直接机制原因，
        # 也违反单脑架构（CLAUDE.md 教训 #9/#10：系统不替 LLM 做决策，
        # 尤其不该用抛硬币）。原注释自己写着「基于长度决断而非随机」，
        # 代码没做到那句话——现改为事实。
        # 群聊的系统拆分只是长度兜底；真要不要拆应由 LLM 用 [分段] 决定
        # （私聊路径已如此；群聊开放 [分段] 是另一个决定，不在本任务范围）。
        if len(reply) < 80:
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
                   group_id: str = "", *,
                   presplit_parts: list[str] | None = None,
                   receipt_template: dict | None = None) -> bool:
        """发送回复，长回复可能拆成2段分开发。含富媒体时增加间隔防限流。
        如果含CQ图片的段落发送失败，自动剥掉图片重试——至少文字内容要送达。
        group_id：私聊目标的最近群（2026-08-15）——非好友时 send_private_message
        走群临时会话兜底，不再直接失败。返回 True 表示全部发送成功。

        presplit_parts（2026-09-22）：LLM 用 [分段] 自己切好、且**已逐段 enrich 过**
        的段。给了它就直接用——LLM 决定在哪切之后，系统不许再用长度/随机二次切分
        （教训 #9：系统不替 LLM 做决策）。空列表或单元素一律退回旧路径（绝不发空）。

        receipt_template（2026-10-13，T2.4）：冻结了 domain 身份的 v2 回执模板，
        原样交给 outbox —— 平台确认后由 settle_send_outbox 完成永久事实归账
        （ADR-003:57-68）。**只对单气泡成立**：ADR-003:23 要求多段批次等
        「逻辑动作 + receipt.parts 聚合」完成后才能接入，那部分**尚未实现**，
        因此多段时会显式丢弃模板并记一条 warning（绝不拿一个模板冒充一批动作）。
        """
        # 系统切分（旧路径，未给 presplit 时）：
        #   群聊 = 按长度拆 ≤2 段；私聊 = 恒一条。
        # 2026-08-15 私聊禁拆的由来：系统按长度随机拆出的第二个气泡，被主人读成
        # 「与对话无关的主动消息」。2026-09-22 主人连问三次「为什么不多发几条」后
        # 改为 LLM 自主拆——**那条禁令锁的是「系统随机拆」，不是「分条」本身**。
        # 两者的区别在「每条自足」：LLM 能让每条单独可读，随机切分做不到。
        if presplit_parts and len(presplit_parts) > 1:
            parts = list(presplit_parts)
        else:
            parts = self.maybe_split_reply(reply) if target_type == "group" else [reply]
        results: list[SendResult] = []
        actual_parts: list[str] = []
        self.last_send_result = None
        self.last_confirmed_payload = ""
        self.last_delivered_parts = []
        self.last_delivered_chunk_ids = ()
        # 多段批次没有「一个」domain 身份——模板只能服务单气泡（见 docstring）。
        multi_part = len(parts) > 1
        transport_template = receipt_template if not multi_part else None
        _sender = (self.napcat.send_group_message if target_type == "group"
                   else self.napcat.send_private_message)
        if transport_template is not None and not self._accepts_receipt_template(_sender):
            logger.info(
                "🧾 发送器不接受 receipt_template，本次跳过模板挂载"
                "（旧适配器/替身：模板对它无意义，硬传会被误判成网络故障）",
            )
            transport_template = None

        async def _dispatch_send(text: str, template: dict | None):
            """单次发送。**只有真的要带模板时才传那个关键字**——
            多传一个 None 也是多传，同样会抛 TypeError 并被下面误判成网络故障。"""
            if target_type == "group":
                if template is None:
                    return await self.napcat.send_group_message(target_id, text)
                return await self.napcat.send_group_message(
                    target_id, text, receipt_template=template)
            if template is None:
                return await self.napcat.send_private_message(
                    target_id, text, group_id=group_id)
            return await self.napcat.send_private_message(
                target_id, text, group_id=group_id, receipt_template=template)
        if receipt_template is not None and multi_part:
            logger.warning(
                "🧾 多段批次丢弃 receipt 模板（receipt.parts 聚合尚未实现，"
                "禁止用单模板冒充一批动作）: parts=%d", len(parts),
            )

        for i, part in enumerate(parts):
            actual_part = part
            # 含图片/表情/语音时多等一会儿，避免触发 QQ 的富媒体频率限制
            has_media = any(tag in part for tag in ("[CQ:image", "[CQ:record", "[CQ:video"))
            if has_media and i > 0:
                await asyncio.sleep(random.uniform(2.0, 4.0))

            try:
                result = self._coerce_send_result(
                    await _dispatch_send(part, transport_template)
                )
            except (TypeError, AttributeError) as exc:
                # 程序错误（签名不符/属性不存在）**不是**网络故障。
                # 把它记成 NETWORK_UNCERTAIN 会让上层以为「消息可能已发出」
                # 而冻结重试——那是把 bug 伪装成网络抖动（本次实测踩到过：
                # 替身不接受 receipt_template，整轮发送全被吞成 uncertain）。
                result = SendResult(
                    False, False, error="SEND_PROGRAMMING_ERROR",
                    retryable=False, uncertain=False,
                )
                logger.error(
                    "发送第%d/%d段调用出错（程序错误，非网络）: %s",
                    i + 1, len(parts), exc,
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
                        result = self._coerce_send_result(
                            await _dispatch_send(text_only, transport_template)
                        )
                    except (TypeError, AttributeError) as exc:
                        # 与主发送路径同一纪律：程序错误不是网络故障。
                        # 这里若记成 uncertain，上层会以为「可能已发出」而冻结重试。
                        result = SendResult(
                            False, False, error="SEND_PROGRAMMING_ERROR",
                            retryable=False, uncertain=False,
                        )
                        logger.error(
                            "纯文字兜底调用出错（程序错误，非网络）: %s", exc,
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
        # T2.2b（2026-10-13，T2.1 复现时发现的独立缺陷）：first_problem 按定义
        # 是「第一个没有 delivered 的段」，而 SendResult.__bool__ 只返回 ok
        # （ws_client.py:49-52）。注意它**不是恒为假值**——ok=True/delivered=False
        # 时 bool() 为真；但**确定失败**（ok=False，本次实测的场景 C）下它必为假，
        # 于是 `x if first_problem else y` 走 else，把 error/retcode 静默吞成空串，
        # 上层只能看到 state=failed 看不到为什么（08-30 事故排查因此多花一轮）。
        # 判据必须落在 `is not None`——它对两种取值都成立，不依赖真值假设。
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
                else (first_problem.error if first_problem is not None else "")
            ),
            retcode=(
                first_problem.retcode if first_problem is not None else None
            ),
            retryable=(
                not partial_delivery
                and not any(r.delivery_state == "uncertain" for r in results)
                and any(r.retryable for r in results)
            ),
            uncertain=(partial_delivery or any(
                r.delivery_state == "uncertain" for r in results
            )),
        )
        # T2.2/T2.3（审计 P0-3）：按确认粒度取出「实际送达的段」。
        # 只有平台给出送达证据（delivered / 非零 message_id）的段才进账——
        # 08-30 反向事故（段 2 明确失败仍打「已发送」）就是漏了这一层。
        # 顺序与段边界**逐段保真**：这正是「多气泡在账本里压成一条」的解药，
        # 而 last_confirmed_payload 的 "".join 只保留旧语义，不动它。
        delivered_parts: list[str] = []
        delivered_ids: list[int] = []
        for index, result in enumerate(results):
            if not result.delivered:
                continue
            text = actual_parts[index] if index < len(actual_parts) else ""
            if not text:
                continue
            delivered_parts.append(text)
            for _mid in (result.chunk_ids or (
                    (result.message_id,) if result.message_id else ())):
                if _mid and _mid not in delivered_ids:
                    delivered_ids.append(_mid)
        self.last_delivered_parts = delivered_parts
        self.last_delivered_chunk_ids = tuple(delivered_ids)
        self.last_send_result = aggregate
        if delivered:
            self.last_confirmed_payload = "".join(actual_parts)
        # 保持公开 bool 契约，但以 confirmed 为准，避免 SendResult.ok 泄漏到
        # 上层状态提交。
        return aggregate.delivered
