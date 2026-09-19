"""
📋 意见征集（2026-08-16）——主人发起、糖糖私聊发布、窗口收集。

流程（主人遥控 → LLM 工具）：
    主人「征集意见：话题」→ set_opinion_campaign 工具
    → 选人（活跃用户按主动私聊意愿分降序，跳过黑名单/意愿分<0.1）
    → 糖糖逐个生成自然文案私聊发布（带对方记忆，像人开口）
    → 用户回复 → LLM 判定参与意向（agree/refuse/other）——语义判定，无关键词表
    → 开窗后用户消息全量落库（零信息丢失，主人自己筛选）
    → 「就这些/没了」→ LLM 判定结束（close/keep），致谢
    → 30 分钟无消息自动关（兜底，防挂死）
    主人「结束征集」→ 汇总成 data/意见收集/xxx.md（知识库扫描范围外——
    意见含私聊内容与 QQ 号，不能被群友经 search_knowledge 检索；2026-08-16 Codex）

边界：
- 任务型主动发布不登记观察回路 pending（对方不参与不扣 seek_willingness）
- 判定 LLM 失败时 fail-open：参与判定 → other（不打扰）；结束判定 → keep（不丢意见）
- 同时最多一个 open 活动（store.get_open_opinion_campaign）
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from napcat.ws_client import is_send_confirmed, send_delivery_state

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.Opinion")


class OpinionManager:
    """意见征集活动管理——发布、判定、收集、导出"""

    def __init__(self, store, llm_caller, send_private, personality_base: str = "",
                 recall=None, self_state=None, bot_qq: str = "",
                 invite_retry_delay: float = 20.0, notify_owner=None,
                 enrich=None, blacklist=None, owner_qq: str = ""):
        """
        store: Store 实例（opinion_* 三表）
        llm_caller: async (system, user) -> str（轻量 LLM）
        send_private: async (qq_id, text) -> bool（handler 包装：含非好友兜底+自检）
        personality_base: 完整人格（生成征集文案用）
        recall: (qq_id, limit) -> list（记忆，文案个性化）
        self_state: 关系场（选人按 seek_willingness 排序）
        invite_retry_delay: 邀请文案重试间隔（测试可调小）
        notify_owner: async (text) -> None——自动收尾时向主人汇报
        enrich: (reply, group_id) -> str——发送前清洗+贴图解析
                （2026-08-16 Codex：邀请文案此前直发，标签会泄露）
        blacklist: set[str]——自动选人跳过（主人显式 targets 保留覆盖权）
        owner_qq: 自动选人排除主人（2026-08-16 Codex）
        """
        self._store = store
        self._llm = llm_caller
        self._send = send_private
        self._base = personality_base
        self._recall = recall
        self._self_state = self_state
        self._bot_qq = bot_qq
        self._invite_retry_delay = invite_retry_delay
        self._notify_owner = notify_owner
        self._enrich = enrich
        self._blacklist = blacklist or set()
        self._owner_qq = owner_qq
        self._background_tasks: set[asyncio.Task] = set()
        self._claimed_invites: set[tuple[int, str]] = set()
        # queued 邀请恢复必须等 QQ lifecycle 报告 online 后才运行。
        # OpinionManager 在 TangTang.__init__ 阶段构造，此时 NapCat 传输层
        # 尚未 ready；构造时启动恢复会把一次暂时的 send=False 永久写成
        # invite_failed。由 main._on_qq_online 显式触发，并用锁合并重连竞态。
        self._recovery_lock = asyncio.Lock()

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """登记后台任务，完成后自动移除，供停机与测试观察。"""
        self._background_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.warning(f"📋 征集后台任务异常: {error}")

        task.add_done_callback(_done)
        return task

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """在线程边界执行同步 Store 调用，并在取消时等待线程收口。

        意见征集的调用点同时承担 LLM/QQ 异步流程；SQLite 的同步连接不能占用
        事件循环。这里保持每个调用的原有顺序，不并发提交写事务；取消时先等
        底层线程释放连接，再把取消继续向上传播，避免 Windows 重启/测试清理
        与仍在运行的 SQLite worker 竞争。
        """
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="📋 Opinion 阻塞调用较慢", **kwargs,
        )

    async def _schedule_invites(self, campaign_id: int, topic: str,
                                picked: list[tuple[str, str]]) -> asyncio.Task | None:
        """只认领仍为 queued 且本进程尚未认领的邀请。

        正式 Store 使用数据库租约裁决多 manager 竞争；旧 fake 仅保留
        进程内集合作为兼容。
        """
        claimed = []
        claim_invite = getattr(self._store, "claim_opinion_invite", None)
        for qq_id, nickname in picked:
            qq_id = str(qq_id)
            key = (campaign_id, qq_id)
            if key in self._claimed_invites:
                continue
            claim_token = ""
            if callable(claim_invite):
                claim_token = uuid4().hex
                claimed_ok = await self._run_store_io(
                    "schedule_invites.claim", claim_invite,
                    campaign_id, qq_id, claim_token,
                )
                if not claimed_ok:
                    continue
            else:
                participant = await self._run_store_io(
                    "schedule_invites.get_participant",
                    self._store.get_opinion_participant,
                    campaign_id, qq_id,
                )
                if not participant or participant.get("status") != "queued":
                    continue
            self._claimed_invites.add(key)
            claimed.append((qq_id, nickname, claim_token))
        if not claimed:
            return None

        async def _run():
            try:
                await self._invite_all(campaign_id, topic, claimed)
            finally:
                for qq_id, _nickname, _claim_token in claimed:
                    self._claimed_invites.discard((campaign_id, qq_id))

        return self._track_task(asyncio.create_task(
            _run(), name=f"opinion_invites_{campaign_id}",
        ))

    async def _recover_queued_invitations(self) -> None:
        """重启恢复持久化 queued；未确认邀请绝不重放。"""
        campaign = await self._run_store_io(
            "recover.get_open_campaign", self._store.get_open_opinion_campaign,
        )
        if not campaign:
            return
        participants = await self._run_store_io(
            "recover.get_participants", self._store.get_opinion_participants,
            campaign["id"],
        )
        queued = [
            (str(p["qq_id"]), p.get("nickname") or str(p["qq_id"]))
            for p in participants
            if p.get("status") == "queued"
        ]
        if queued:
            logger.info(f"📋 恢复 {len(queued)} 条排队中的征集邀请")
            await self._schedule_invites(campaign["id"], campaign["topic"], queued)

    async def recover_queued_invitations(self) -> None:
        """在 QQ lifecycle online 后恢复 queued 邀请。

        该入口刻意不在构造函数中自动执行：构造时 NapCat 可能尚未登录，
        失败发送不能被误记为终态。每次 online 都可安全调用；数据库 claim
        与本地锁共同保证重连/重复 lifecycle 事件不会重复外发。
        """
        async with self._recovery_lock:
            await self._recover_queued_invitations()

    # ═══════════════════════════════════════
    # 发起
    # ═══════════════════════════════════════

    async def start_campaign(self, topic: str, targets: list[str] | None = None,
                             max_targets: int = 20) -> dict:
        """创建活动，邀请在后台逐个发布。返回 {campaign_id, invited, targets}

        2026-08-16 现场教训：邀请同步执行时死在 _call_llm_light 的锁超时上——
        工具循环持有 _llm_lock，嵌套 LLM 调用等 15s 返回空串，_invite 静默跳过
        （提示已发送实际没发）。邀请改为后台任务：主调用结束锁释放后再逐个发。
        """
        existing = await self._run_store_io(
            "start.get_open_campaign", self._store.get_open_opinion_campaign,
        )
        if existing:
            return {"error": f"已有进行中的征集（话题：{existing['topic']}）——先结束它"}

        picked = await self._pick_targets(targets, max_targets)
        if not picked:
            return {"error": "没有找到可邀请的目标，本次征集未创建"}

        atomic_create = getattr(
            self._store, "create_opinion_campaign_with_participants", None,
        )
        if callable(atomic_create):
            campaign_id = await self._run_store_io(
                "start.create_campaign_atomic", atomic_create,
                topic, picked, self._bot_qq, f"【征集发起】话题：{topic}",
            )
            if campaign_id is None:
                # 另一个实例可能在首次 open 查询之后抢先创建；数据库唯一索引
                # 和事务方法已经完成并发裁决，这里只把结果转换成用户可理解的错误。
                return {"error": "已有进行中的征集——先结束它"}
        else:
            # 仅兼容旧版离线 fake Store；正式 Store 始终走上面的原子 API。
            campaign_id = await self._run_store_io(
                "start.create_campaign_legacy", self._store.create_opinion_campaign,
                topic,
            )
            for qq_id, nickname in picked:
                await self._run_store_io(
                    "start.add_participant_legacy",
                    self._store.add_opinion_participant,
                    campaign_id, qq_id, nickname, status="queued",
                )
            await self._run_store_io(
                "start.add_message_legacy", self._store.add_opinion_message,
                campaign_id, self._bot_qq, "糖糖", f"【征集发起】话题：{topic}",
                is_bot=True,
            )
        await self._schedule_invites(campaign_id, topic, picked)
        return {"campaign_id": campaign_id, "queued": len(picked), "invited": 0,
                "targets": [f"{n}({q})" for q, n in picked]}

    async def _invite_all(self, campaign_id: int, topic: str, picked):
        """后台逐个发出邀请——带限速与失败日志（2026-08-16）"""
        for i, item in enumerate(picked):
            qq_id, nickname = item[:2]
            claim_token = item[2] if len(item) > 2 else ""
            try:
                if i > 0:
                    await asyncio.sleep(1.5)  # 限速，避免刷屏风控
                await self._invite(
                    campaign_id, topic, qq_id, nickname, claim_token=claim_token,
                )
            except Exception as e:
                # _invite 在外部发送前先落 invite_uncertain。若异常发生在
                # POST 之后，不得再把它降级成 failed；pending 也不能回滚。
                participant = await self._run_store_io(
                    "invite.exception.get_participant",
                    self._store.get_opinion_participant,
                    campaign_id, qq_id,
                )
                if not participant or participant.get("status") not in {
                    "invite_uncertain", "pending",
                }:
                    await self._run_store_io(
                        "invite.exception.mark_failed",
                        self._store.update_opinion_participant,
                        campaign_id, qq_id, "invite_failed",
                    )
                logger.warning(f"📋 征集邀请失败 ({qq_id}): {e}")

    async def _pick_targets(self, targets: list[str] | None, max_targets: int) -> list[tuple[str, str]]:
        """目标名单：主人指定 or 活跃用户按主动私聊意愿分降序"""
        if targets:
            out = []
            seen = set()
            for raw_q in targets:
                q = str(raw_q or "").strip()
                if not q or q in seen:
                    continue
                seen.add(q)
                nick = await self._nickname_async(q)
                out.append((q, nick))
                if len(out) >= max_targets:
                    break
            return out
        picked = []
        if not self._store:
            return picked
        try:
            rows = await self._run_store_io(
                "pick_targets.get_recent_active_users",
                self._store.get_recent_active_users,
                days=7, limit=50, exclude_qq=self._bot_qq,
            )
        except Exception:
            rows = []

        def _seek_w(qq: str) -> float:
            rels = (self._self_state.relationships or {}) if self._self_state else {}
            rel = rels.get(qq)
            return rel.seek_willingness if rel else 0.5

        ordered = sorted(rows, key=lambda r: _seek_w(str(r[0])), reverse=True)
        for r in ordered:
            qq_id = str(r[0])
            if _seek_w(qq_id) < 0.1:  # 冷场学会不打扰的人，征集也不打扰
                continue
            if qq_id in self._blacklist:  # 2026-08-16 Codex：黑名单不许主动打扰
                continue
            if self._owner_qq and qq_id == str(self._owner_qq):  # 主人不给自己发征集
                continue
            if qq_id in {p[0] for p in picked}:
                continue
            picked.append((qq_id, await self._nickname_async(qq_id)))
            if len(picked) >= max_targets:
                break
        return picked

    def _nickname(self, qq_id: str) -> str:
        try:
            person = self._store.get_or_create_person(qq_id)
            return (person.get("nickname") or "").strip() or qq_id
        except Exception:
            return qq_id

    async def _nickname_async(self, qq_id: str) -> str:
        """异步路径的人物昵称读取，保持旧同步兼容入口。"""
        try:
            person = await self._run_store_io(
                "resolve_nickname", self._store.get_or_create_person, qq_id,
            )
            return (person.get("nickname") or "").strip() or qq_id
        except Exception:
            return qq_id

    async def _invite(self, campaign_id: int, topic: str, qq_id: str,
                      nickname: str, claim_token: str = ""):
        """生成自然征集文案并私发。

        2026-08-16 现场教训：LLM 空回复不许静默跳过——重试 3 次（主回复流式
        可能仍占着 LLM 锁），仍失败用模板兜底，保证邀请一定发出去且日志可见。
        """
        mem_ctx = ""
        if self._recall:
            try:
                mems = await self._run_store_io(
                    "invite.recall", self._recall, qq_id, limit=5,
                )
                if mems:
                    mem_ctx = "关于ta你记得的事：" + "、".join(
                        m.value for m in mems[:5]) + "\n"
            except Exception:
                pass

        reply = ""
        for attempt in range(3):
            try:
                reply = await self._llm(
                    self._base + "\n\n"
                    f"你想听听大家对你的看法，话题是：「{topic}」。\n"
                    f"你要私聊邀请 {nickname} 参与。像你平时主动找ta聊天那样自然开口——"
                    f"先寒暄一句再提正事，不要像发问卷通知。\n"
                    f"❌ 禁止提到「主人让我来」「有人让我问」——就是你自己想听听大家怎么说。\n"
                    f"{mem_ctx}"
                    f"让ta知道：随时回复你就行，想到什么说什么，说完了说声「就这些」。\n"
                    f"2-4句话，直接输出你要对{nickname}说的话。",
                    f"现在对{nickname}发出邀请：",
                )
                reply = (reply or "").strip().strip('"').strip("'")
                if reply and len(reply) >= 5:
                    break
            except Exception as e:
                logger.warning(f"📋 邀请文案生成失败（第{attempt + 1}次）: {e}")
            if attempt < 2:
                await asyncio.sleep(self._invite_retry_delay)  # 主回复可能还在流式占用 LLM 锁

        if not reply or len(reply) < 5:
            reply = (
                f"{nickname}，糖糖想听听你的想法～「{topic}」\n"
                f"想到什么直接跟糖糖说就行，说完了说声「就这些」～"
            )
            logger.info(f"📋 邀请文案用模板兜底 → {nickname}({qq_id})")

        # 2026-08-16 Codex：LLM 文案（含模板兜底）发送前过 enrich——
        # 此前直发原文，[贴图:xx]/<思考> 会字面泄露给被邀请人
        if self._enrich:
            # enrich 可能触发贴图语义匹配/嵌入模型，不能在意见协程的
            # 事件循环中同步执行；线程边界只改变调度位置，不改变文案。
            # 保留原有 ``self._enrich(reply, "")`` 的调用契约，实际在线程中执行。
            reply = await self._run_store_io(
                "invite.enrich", self._enrich, reply, "",
            )
            if not reply:
                await self._run_store_io(
                    "invite.enrich_failed", self._store.update_opinion_participant,
                    campaign_id, qq_id, "invite_failed",
                )
                return
        # 外部副作用前先落保守占位。若 POST 后进程崩溃或回调抛异常，
        # 重启后仍保持 invite_uncertain，不会把可能已送达的邀请盲目重发。
        mark_uncertain = getattr(
            self._store, "mark_opinion_invite_uncertain", None,
        )
        if callable(mark_uncertain):
            marked = await self._run_store_io(
                "invite.mark_uncertain", mark_uncertain,
                campaign_id, qq_id, claim_token,
            )
            if not marked:
                logger.info(
                    "📋 邀请租约已失效，跳过外部发送 → %s(%s)", nickname, qq_id,
                )
                return
        else:
            # 仅兼容旧版离线 fake Store；正式调度使用 token CAS。
            await self._run_store_io(
                "invite.mark_uncertain_legacy",
                self._store.update_opinion_participant,
                campaign_id, qq_id, "invite_uncertain",
            )
        try:
            result = await self._send(qq_id, reply)
        except Exception as e:
            logger.warning(
                f"📋 征集邀请响应丢失 → {nickname}({qq_id})，冻结为未确认: {e}"
            )
            return
        if is_send_confirmed(result):
            settle = getattr(self._store, "settle_opinion_invite_confirmed", None)
            if callable(settle):
                settled = await self._run_store_io(
                    "invite.settle_confirmed", settle,
                    campaign_id, qq_id, nickname, reply, claim_token,
                )
                if not settled:
                    logger.warning(
                        "📋 征集邀请确认后状态已变化，保留未确认状态 "
                        "→ %s(%s)", nickname, qq_id,
                    )
                    return
            else:
                # 仅兼容旧版离线 fake Store；正式 Store 使用上面的原子事务。
                await self._run_store_io(
                    "invite.mark_pending_legacy", self._store.update_opinion_participant,
                    campaign_id, qq_id, "pending",
                )
                await self._run_store_io(
                    "invite.add_message_legacy", self._store.add_opinion_message,
                    campaign_id, qq_id, nickname, reply, is_bot=True,
                )
            logger.info(f"📋 征集邀请已发 → {nickname}({qq_id})")
        else:
            state = send_delivery_state(result)
            set_state = getattr(
                self._store, "set_opinion_invite_delivery_state", None,
            )
            if callable(set_state) and claim_token:
                await self._run_store_io(
                    "invite.mark_delivery_state", set_state,
                    campaign_id, qq_id, claim_token,
                    "invite_uncertain" if state == "uncertain" else "invite_failed",
                )
            else:
                await self._run_store_io(
                    "invite.mark_delivery_state_legacy",
                    self._store.update_opinion_participant,
                    campaign_id, qq_id,
                    "invite_uncertain" if state == "uncertain" else "invite_failed",
                )
            logger.warning(
                f"📋 征集邀请发送未确认 ({state}) "
                f"→ {nickname}({qq_id})，不写已发送记录"
            )

    # ═══════════════════════════════════════
    # 用户消息处理（handler 私聊钩子调用）
    # ═══════════════════════════════════════

    async def _record_response_async(
            self, campaign_id: int, qq_id: str, nickname: str,
            text: str, verdict: str, previous_status: str) -> str:
        """把判定结果交给 Store 做状态 CAS + 消息写入。

        正式 Store 的原子 API 会重新检查 ``previous_status`` 对应的当前状态；
        旧版离线 fake 走兼容分支，仅用于保留历史测试/适配器行为。
        """
        record = getattr(self._store, "record_opinion_response", None)
        if callable(record):
            return await self._run_store_io(
                "message.record_response", record,
                campaign_id, str(qq_id), nickname, text, verdict,
            )

        # 兼容没有 CAS API 的旧 fake Store；正式 Store 不会走这里。
        if verdict in {"agree", "refuse"}:
            next_status = "participating" if verdict == "agree" else "refused"
            await self._run_store_io(
                "message.mark_response_legacy",
                self._store.update_opinion_participant,
                campaign_id, qq_id, next_status,
            )
            await self._run_store_io(
                "message.add_response_legacy", self._store.add_opinion_message,
                campaign_id, qq_id, nickname, text,
            )
            return next_status
        if previous_status == "participating" and verdict == "keep":
            await self._run_store_io(
                "message.add_response_legacy", self._store.add_opinion_message,
                campaign_id, qq_id, nickname, text,
            )
            return "recorded"
        if previous_status == "participating" and verdict == "close":
            await self._run_store_io(
                "message.mark_done_legacy",
                self._store.update_opinion_participant,
                campaign_id, qq_id, "done",
            )
            await self._run_store_io(
                "message.add_response_legacy", self._store.add_opinion_message,
                campaign_id, qq_id, nickname, text,
            )
            return "done"
        return "ignored"

    async def handle_user_message(self, qq_id: str, nickname: str, text: str) -> str | None:
        """open 活动期间的用户私聊 → 判定+收集。返回要给用户的回复（无则 None）。"""
        camp = await self._run_store_io(
            "message.get_open_campaign", self._store.get_open_opinion_campaign,
        )
        if not camp:
            return None
        p = await self._run_store_io(
            "message.get_participant", self._store.get_opinion_participant,
            camp["id"], str(qq_id),
        )
        if not p:
            return None

        if p["status"] in {"pending", "invite_uncertain", "queued"}:
            verdict = await self._judge_participation(camp["topic"], nickname, text)
            if verdict == "agree":
                outcome = await self._record_response_async(
                    camp["id"], qq_id, nickname, text, verdict, p["status"],
                )
                if outcome != "participating":
                    logger.info(
                        "📋 意见回复状态已变化，忽略过期 agree: %s(%s)",
                        nickname, qq_id,
                    )
                    return None
                return (
                    "太好啦～那糖糖就记下你说的啦，想到什么继续跟糖糖说就行，"
                    "不着急，说完了说声「就这些」就成～"
                )
            if verdict == "refuse":
                outcome = await self._record_response_async(
                    camp["id"], qq_id, nickname, text, verdict, p["status"],
                )
                if outcome != "refused":
                    logger.info(
                        "📋 意见回复状态已变化，忽略过期 refuse: %s(%s)",
                        nickname, qq_id,
                    )
                    return None
                return "没关系呀～等你有空了随时再找糖糖聊！"
            return None  # other：寒暄，不打扰

        if p["status"] == "participating":
            # 2026-08-16 Codex：无操作写已删（update participating→participating）
            verdict = await self._judge_close(camp["topic"], nickname, text)
            outcome = await self._record_response_async(
                camp["id"], qq_id, nickname, text, verdict, p["status"],
            )
            if outcome == "done":
                return "收到！糖糖都记下来啦，谢谢你愿意跟糖糖说这些～（比心）"
            return None  # 继续收集，不打断

        return None

    async def _judge_participation(self, topic: str, nickname: str, text: str) -> str:
        """LLM 判定参与意向：agree/refuse/other。失败 fail-open → other"""
        try:
            r = await self._llm(
                "判断用户对征集邀请的态度。只输出一个词：agree/refuse/other。",
                f"糖糖向{nickname}征集意见（话题：「{topic}」）。\n"
                f"{nickname} 回复：「{text}」\n\n"
                f"愿意参与 → agree；明确拒绝或说没空 → refuse；只是寒暄或无关内容 → other。",
            )
            # 2026-08-16 Codex：容错带标点/多词输出——按词首匹配
            r = (r or "").strip().lower().rstrip("。.!！？?")
            r = r.split()[0] if r.split() else ""
            return r if r in ("agree", "refuse", "other") else "other"
        except Exception:
            return "other"

    async def _judge_close(self, topic: str, nickname: str, text: str) -> str:
        """LLM 判定结束信号：close/keep。失败 fail-open → keep（不丢意见）"""
        try:
            r = await self._llm(
                "判断用户是否想结束意见交流。只输出一个词：close/keep。",
                f"糖糖在收集{nickname}的意见（话题：「{topic}」）。\n"
                f"{nickname} 说：「{text}」\n\n"
                f"ta 在表达结束（如「就这些」「没了」「先这样」）→ close；"
                f"还在说意见或闲聊 → keep。",
            )
            # 2026-08-16 Codex：容错带标点/多词输出——按词首匹配
            r = (r or "").strip().lower().rstrip("。.!！？?")
            r = r.split()[0] if r.split() else ""
            return r if r in ("close", "keep") else "keep"
        except Exception:
            return "keep"

    # ═══════════════════════════════════════
    # 自动关窗（自治循环每周期调用）
    # ═══════════════════════════════════════

    async def auto_close_stale(self, minutes: int = 30, pending_hours: float = 24.0) -> None:
        """自治循环每周期调用：
        0. pending 超时（已确认邀请发出后仍无回应）→ no_reply；
           queued/invite_uncertain 超时 → invite_expired，不把未确认邀请
           伪装成“对方没回复”。
           2026-08-16 Codex：此前 pending 永不到终态，一人不回整个活动挂死，
           且与重启丢邀请任务叠加（被跳过的人永远 pending）
        1. participating 超时无消息 → done + 致谢（消息时间从 DB 推导，无状态）
        2. 全员终态（done/refused/no_reply）→ 自动结束 + 导出 + 汇报主人——
           糖糖承诺「等ta回复后整理成文档给你过目」，文档必须自动出现（2026-08-16）
        """
        camp = await self._run_store_io(
            "auto_close.get_open_campaign", self._store.get_open_opinion_campaign,
        )
        if not camp:
            return
        parts = await self._run_store_io(
            "auto_close.get_participants", self._store.get_opinion_participants,
            camp["id"],
        )
        now_ts = datetime.now()
        messages_by_qq = None
        for p in parts:
            if p["status"] in {"pending", "queued", "invite_uncertain"}:
                if p["status"] == "invite_uncertain" and p.get("claim_ts"):
                    try:
                        claim_t = datetime.strptime(
                            p["claim_ts"], "%Y-%m-%d %H:%M:%S",
                        )
                        # 外部发送仍在租约内时不能被自动关窗标成
                        # invite_expired；否则确认回调到达后会丢失事实。
                        if (now_ts - claim_t).total_seconds() < 900:
                            continue
                    except Exception:
                        pass
                try:
                    sent = datetime.strptime(p["last_msg_ts"] or "", "%Y-%m-%d %H:%M:%S")
                    if (now_ts - sent).total_seconds() / 3600 >= pending_hours:
                        expired_status = (
                            "no_reply" if p["status"] == "pending"
                            else "invite_expired"
                        )
                        await self._run_store_io(
                            "auto_close.mark_expired",
                            self._store.update_opinion_participant,
                            camp["id"], p["qq_id"], expired_status,
                        )
                        logger.info(f"📋 征集邀请超时 → {expired_status}: "
                                    f"{p['nickname']}({p['qq_id']})")
                except Exception:
                    continue
                continue
            if p["status"] != "participating":
                continue
            if messages_by_qq is None:
                all_messages = await self._run_store_io(
                    "auto_close.get_messages", self._store.get_opinion_messages,
                    camp["id"],
                )
                messages_by_qq = {}
                for message in all_messages:
                    if not message["is_bot"]:
                        messages_by_qq.setdefault(str(message["qq_id"]), []).append(message)
            msgs = messages_by_qq.get(str(p["qq_id"]), [])
            if not msgs:
                continue
            last = msgs[-1]["timestamp"]
            try:
                last_t = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                idle = (datetime.now() - last_t).total_seconds() / 60
            except Exception:
                continue
            if idle >= minutes:
                close_idle = getattr(
                    self._store, "close_opinion_participant_if_idle", None,
                )
                if callable(close_idle) and msgs[-1].get("id") is not None:
                    closed = await self._run_store_io(
                        "auto_close.close_if_idle", close_idle,
                        camp["id"], p["qq_id"], msgs[-1]["id"],
                    )
                else:
                    # 旧版离线 fake Store 兼容；正式 Store 使用消息 ID CAS。
                    await self._run_store_io(
                        "auto_close.mark_done_legacy",
                        self._store.update_opinion_participant,
                        camp["id"], p["qq_id"], "done",
                    )
                    closed = True
                if not closed:
                    continue
                try:
                    await self._send(p["qq_id"],
                                     "糖糖先不打扰你啦～刚才说的都记下了，谢谢你呀！")
                except Exception:
                    pass
                logger.info(f"📋 意见窗口自动关闭（{idle:.0f}分钟无消息）→ {p['nickname']}({p['qq_id']})")

        # 全员终态 → 自动收尾
        parts = await self._run_store_io(
            "auto_close.get_final_participants",
            self._store.get_opinion_participants, camp["id"],
        )
        if parts and all(p["status"] in (
                "done", "refused", "no_reply", "invite_failed", "invite_expired",
        ) for p in parts):
            result = await self._run_store_io(
                "auto_close.end_campaign", self.end_campaign,
            )
            logger.info(f"📋 意见征集自动收尾：{result[:100]}")
            if self._notify_owner:
                try:
                    await self._notify_owner(result)
                except Exception:
                    pass

    # ═══════════════════════════════════════
    # 状态汇报与结束导出
    # ═══════════════════════════════════════

    def campaign_status(self) -> str:
        camp = self._store.get_open_opinion_campaign()
        if not camp:
            return "当前没有进行中的意见征集。"
        parts = self._store.get_opinion_participants(camp["id"])
        counts = {
            "participating": 0, "done": 0, "refused": 0, "pending": 0,
            "no_reply": 0, "queued": 0, "invite_uncertain": 0,
            "invite_failed": 0, "invite_expired": 0,
        }
        for p in parts:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        lines = [f"📋 意见征集进行中：{camp['topic']}",
                 f"目标 {len(parts)} 人："
                 f"{counts['done']} 已完成 / {counts['participating']} 正在说 / "
                 f"{counts['refused']} 婉拒 / {counts['pending']} 已邀请未回应 / "
                 f"{counts['queued']} 排队 / {counts['invite_uncertain']} 发送未确认 / "
                 f"{counts['invite_failed'] + counts['invite_expired']} 未邀请成功"]
        for p in parts:
            if p["status"] in ("done", "participating"):
                lines.append(f"  · {p['nickname']} — {p['status']}")
        return "\n".join(lines)

    def end_campaign(self, out_dir: str = "") -> str:
        """关闭活动 + 汇总导出 MD 到 knowledge/意见收集/——返回文档路径"""
        snapshot_fn = getattr(self._store, "close_opinion_campaign_snapshot", None)
        if callable(snapshot_fn):
            open_campaign = self._store.get_open_opinion_campaign()
            if not open_campaign:
                return "当前没有进行中的意见征集。"
            snapshot = snapshot_fn(open_campaign["id"])
            if not snapshot:
                return "当前没有进行中的意见征集。"
            camp = snapshot["campaign"]
            parts = snapshot["parts"]
            msgs = snapshot["messages"]
        else:
            # 旧版离线 fake Store 兼容；正式 Store 使用原子快照事务。
            camp = self._store.get_open_opinion_campaign()
            if not camp:
                return "当前没有进行中的意见征集。"
            cid = camp["id"]
            parts = self._store.get_opinion_participants(cid)
            msgs = self._store.get_opinion_messages(cid)
            # 只有已确认发出的邀请才可标记 no_reply；其余是邀请未确认。
            for p in parts:
                if p["status"] == "pending":
                    self._store.update_opinion_participant(cid, p["qq_id"], "no_reply")
                elif p["status"] in {"queued", "invite_uncertain"}:
                    self._store.update_opinion_participant(
                        cid, p["qq_id"], "invite_expired",
                    )
            self._store.close_opinion_campaign(cid)

        doc_path = self._export_markdown(camp, parts, msgs, out_dir=out_dir)
        stats = {}
        for p in parts:
            stats[p["status"]] = stats.get(p["status"], 0) + 1
        return (f"📋 征集已结束。{stats.get('done', 0)} 人给了意见、"
                f"{stats.get('refused', 0)} 人婉拒、{stats.get('no_reply', 0) + stats.get('pending', 0)} 人未回应。\n"
                f"意见文档：{doc_path}（已入知识库，你可以问我大家都说了什么）")

    def _export_markdown(self, camp: dict, parts: list[dict], msgs: list[dict],
                         out_dir: str = "") -> str:
        """汇总成 MD 文档写入 data/意见收集/——
        2026-08-16 Codex：原放 knowledge/ 会被知识库递归扫描收录，群里任何人
        经 search_knowledge 可检索到参与者 QQ 号与私聊内容（违背交叉上下文限权）。
        data/ 在知识库扫描范围之外，主人自己打开文件看。"""
        out_dir = Path(out_dir) if out_dir else Path("data") / "意见收集"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_topic = re.sub(r'[\\/:*?"<>|\n\r]', " ", camp["topic"]).strip()[:30] or "未命名"
        fname = f"{camp['created_at'][:10]}-{safe_topic}.md"
        path = out_dir / fname

        lines = [
            f"# 意见征集：{camp['topic']}",
            "",
            f"- 发起时间：{camp['created_at']}",
            f"- 结束时间：{camp.get('closed_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 发出邀请：{len(parts)} 人",
            "",
        ]
        for p in sorted(parts, key=lambda x: x["id"]):
            status_label = {"done": "✅ 已收集", "refused": "🚫 婉拒",
                            "participating": "🔵 未结束", "pending": "⏳ 未回应",
                            "no_reply": "⏳ 未回应"}.get(p["status"], p["status"])
            lines.append(f"## {p['nickname']}（{p['qq_id']}）— {status_label}")
            pmsgs = [m for m in msgs if m["qq_id"] == p["qq_id"]]
            if not pmsgs:
                lines.append("（无消息）")
            for m in pmsgs:
                who = "糖糖" if m["is_bot"] else p["nickname"]
                lines.append(f"- [{m['timestamp'][11:16]}] **{who}**: {m['message']}")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"📋 意见文档已导出: {path}")
        return str(path)
