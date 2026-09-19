"""
小糖糖 任务/提醒系统
持久化定时任务——重启不丢失。每分钟检查一次到期任务，自动私聊/群聊提醒。

用法：
    tm = TaskManager(store, napcat, bot_name="小糖糖")
    await tm.start()          # 启动后台检查循环
    tm.add(minutes=30, description="喝水", owner_qq="123456")
    tm.add_at("08:00", "起床", owner_qq="123456")

2026-08-16 范式转换（教训表 #24）：自然语言解析 parse() 已删除——定时意图
由 LLM 工具 set_reminder / group_say_later 决定；本模块只负责执行与持久化。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

from onebot.ws_client import send_delivery_state

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.Tasks")


class TaskManager:
    _TYPED_PAYLOAD_FIELDS = frozenset({
        "text", "sticker_emotion", "voice_text",
        "action_version", "sticker_transport", "sticker_asset_ref",
        "sticker_asset_sha256", "sticker_asset_valid", "sticker_role_id",
        "sticker_library_id", "voice_emotion", "voice_speed", "voice_pause",
        "voice_model_profile", "voice_speaker", "voice_lang",
    })

    def __init__(self, store, napcat, bot_name="小糖糖", llm_call=None,
                 stickers=None, voice_sender=None, on_confirmed=None,
                 sticker_snapshot=None, voice_snapshot=None,
                 voice_preparer=None):
        self.store = store
        self.napcat = napcat
        self.bot_name = bot_name
        # 2026-08-18 事故：set_reminder 的 description 是 LLM 写给自己的备忘
        # （「主动找米雪儿报到…提醒她：那个计划不作数」），到点后被系统原样
        # 发给了对方——对方收到一份看不懂的指令备忘。正解：到点时交给 LLM
        # 把备忘改写成自然开口；LLM 未提供/失败/返回空则原样兜底。
        self._llm_call = llm_call
        # P0-C（2026-08-28）typed 动作依赖（均可选，未注入则对应动作视为失败）：
        #   stickers:      StickerManager——sticker_emotion 匹配贴图
        #   voice_sender:  async (scope_key, text) -> SendResult——语音发送
        #   on_confirmed:  (task, scope_key) -> None——全部子动作确认后开窗钩子
        self.stickers = stickers
        self.voice_sender = voice_sender
        self.on_confirmed = on_confirmed
        # 新 scheduled media 路径的能力注入：快照在创建任务时冻结，
        # voice_preparer 在到期时只生成可持久化的 CQ/actual，不执行 QQ 发送。
        self.sticker_snapshot = sticker_snapshot
        self.voice_snapshot = voice_snapshot
        self.voice_preparer = voice_preparer
        self._running = False
        self._task: asyncio.Task | None = None
        self._media_gate_warned_task_ids: set[int] = set()

    async def start(self):
        """启动后台检查循环（每分钟一次）"""
        if self._running:
            return
        recovered = await self._run_store_io(
            "recover_sending_tasks", self.store.recover_sending_tasks,
        )
        if recovered:
            logger.warning(f"📋 隔离 {recovered} 条崩溃前发送中的提醒，等待人工确认")
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("📋 任务提醒系统已启动")

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """在线程池执行单次 Store 调用，并在取消时等待线程收口。

        TaskManager 的循环位于主事件循环；Store 每次调用都创建独立 SQLite
        连接，因此可以顺序移出事件循环。取消任务时仍要等待底层线程释放连接，
        否则优雅重启/临时库清理可能与未结束的 SQLite I/O 竞争。
        """
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="📋 任务 Store 调用较慢", **kwargs,
        )

    async def stop(self):
        self._running = False
        task = self._task
        self._task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """每分钟检查一次到期任务"""
        while self._running:
            try:
                await self._check_and_send()
                logger.info("🫀 任务提醒循环心跳")
            except Exception as e:
                logger.warning(f"任务检查异常: {e}")
            await asyncio.sleep(60)

    async def _check_and_send(self):
        """检查到期任务并执行动作（2026-08-15：支持群提醒——「在群里叫我」）。

        P1（2026-08-28）：legacy/typed 纯文本先冻结为 ActionPlan + linked
        outbox；2026-08-29 起已冻结的 v2 sticker/voice 组合也走同一 ordered
        child/outbox 链路，旧 v1 media payload 保留兼容路径。
        旧链路仍按以下聚合送达语义：
          - 全部子动作 confirmed → done + on_confirmed 开窗钩子
          - 任一 uncertain 或部分成功 → 冻结为 uncertain（重放会重复已确认部分）
          - 全部确定失败 → release（下一轮可重试）
        """
        due = await self._run_store_io("get_due_tasks", self.store.get_due_tasks)
        due_task_ids = {int(task["id"]) for task in due}
        self._media_gate_warned_task_ids.intersection_update(due_task_ids)
        for task in due:
            payload = self._parse_payload(task)
            is_text_only = (
                not payload
                or (
                    bool(str(payload.get("text") or "").strip())
                    and not str(payload.get("sticker_emotion") or "").strip()
                    and not str(payload.get("voice_text") or "").strip()
                    and not payload.get("__invalid_payload__")
                )
            )
            is_media = bool(payload.get("sticker_emotion") or payload.get("voice_text"))
            is_unified_media = is_media and payload.get("action_version") == 2
            if (is_unified_media
                    and not getattr(self.napcat, "_task_media_action_outbox_enabled", False)):
                # 新媒体计划默认关闭：未打开 outbox owner 前保留 pending，
                # 不提前生成语音、不解析资产，也不触碰平台。
                task_id = int(task["id"])
                if task_id not in self._media_gate_warned_task_ids:
                    logger.warning(
                        "📋 媒体任务因闸门关闭而跳过: "
                        f"task_id={task_id} "
                        "gate=tasks.media_action_outbox_enabled"
                    )
                    self._media_gate_warned_task_ids.add(task_id)
                continue
            if (is_text_only
                    and not getattr(
                        self.napcat, "_task_text_outbox_enabled", True,
                    )):
                # 回滚闸门必须在 claim 前生效：保留 pending，既不调用 LLM，
                # 也不创建新 linked attempt/outbox。
                continue
            claimed = False
            try:
                claimed = await self._run_store_io(
                    "claim_task_for_send", self.store.claim_task_for_send,
                    task["id"],
                )
                if not claimed:
                    continue
                if is_text_only:
                    try:
                        frozen_text = (
                            str(payload.get("text") or "").strip()
                            if payload else await self._compose_message(task)
                        )
                        action = await self._run_store_io(
                            "persist_task_text_action",
                            self.store.persist_task_text_action,
                            task["id"], frozen_text,
                        )
                    except Exception as e:
                        # 尚未发生任何网络副作用，原子事务失败可安全释放 claim。
                        released = await self._run_store_io(
                            "release_task_claim", self.store.release_task_claim,
                            task["id"],
                        )
                        if released:
                            logger.warning(
                                "📋 纯文本任务冻结失败，等待下轮重试: "
                                f"task_id={task['id']} err={e}"
                            )
                        else:
                            # commit 结果本地不明时，若 durable owner 已存在就绝不
                            # 抢回 task；outbox worker 会继续唯一持有该 generation。
                            logger.error(
                                "📋 纯文本任务冻结返回异常但 claim 未释放，"
                                "保留 durable owner: "
                                f"task_id={task['id']} err={e}"
                            )
                        continue
                    logger.info(
                        "📋 纯文本任务已冻结并进入 outbox: "
                        f"task_id={task['id']} attempt={action['generation']} "
                        f"outbox_id={action['outbox_id']}"
                    )
                    continue
                if is_unified_media:
                    try:
                        specs = await self._prepare_unified_media_plan(
                            task, payload,
                            task.get("group_id") or f"_private_{task['owner_qq']}",
                        )
                        action = await self._run_store_io(
                            "persist_task_action_plan",
                            self.store.persist_task_action_plan,
                            task["id"], specs,
                            role_id=str(payload.get("voice_speaker") or
                                       payload.get("sticker_role_id") or ""),
                            library_id=str(payload.get("sticker_library_id") or ""),
                        )
                    except Exception as e:
                        released = await self._run_store_io(
                            "release_task_claim", self.store.release_task_claim,
                            task["id"],
                        )
                        if released:
                            logger.warning(
                                "📋 媒体任务冻结失败，等待下轮重试: "
                                f"task_id={task['id']} err={e}"
                            )
                        else:
                            logger.error(
                                "📋 媒体任务冻结异常但 claim 未释放，保留 durable owner: "
                                f"task_id={task['id']} err={e}"
                            )
                        continue
                    logger.info(
                        "📋 媒体任务已冻结并进入 outbox: "
                        f"task_id={task['id']} attempt={action['generation']} "
                        f"children={len(action['children'])}"
                    )
                    continue
                scope_key = task.get("group_id") or f"_private_{task['owner_qq']}"
                outcome = await self._execute_payload(task, payload, scope_key)
                if outcome == "confirmed":
                    await self._run_store_io(
                        "mark_task_done", self.store.mark_task_done, task["id"],
                    )
                    logger.info(
                        f"📋 任务动作已确认送达: {task['owner_qq']} — {task['description'][:40]}"
                    )
                    if self.on_confirmed:
                        try:
                            self.on_confirmed(task, scope_key)
                        except Exception as e:
                            logger.warning(f"任务确认开窗钩子异常: {e}")
                elif outcome == "uncertain":
                    # API 已接受但没有 message_id 证据 / 部分子动作失败：
                    # 持久化为人工确认态，防止每分钟循环重放导致重复。
                    await self._run_store_io(
                        "mark_task_uncertain", self.store.mark_task_uncertain,
                        task["id"],
                    )
                    logger.warning(
                        f"📋 任务动作未完全确认，暂停自动重试: task_id={task['id']}"
                    )
                else:
                    await self._run_store_io(
                        "release_task_claim", self.store.release_task_claim,
                        task["id"],
                    )
                    logger.warning(f"任务动作全部失败，等待重试: task_id={task['id']}")
            except Exception as e:
                if claimed:
                    # 外部发送抛异常时无法证明请求未到达；先冻结，禁止盲重放。
                    await self._run_store_io(
                        "mark_task_uncertain", self.store.mark_task_uncertain,
                        task["id"],
                    )
                logger.warning(f"任务提醒异常: task_id={task['id']} err={e}")

    @staticmethod
    def _parse_payload(task: dict) -> dict:
        """typed payload 安全读取。

        空 payload 仍代表旧版 text-only 任务；坏 JSON 或未知字段则返回
        显式 invalid 标记，由执行层冻结为 uncertain，禁止把不完整动作
        当作普通文本重试。
        """
        raw = task.get("action_payload") or ""
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            unknown = set(payload) - TaskManager._TYPED_PAYLOAD_FIELDS
            if unknown:
                raise ValueError(f"unknown fields: {sorted(unknown)}")
            for key, value in payload.items():
                if key == "sticker_asset_valid":
                    if not isinstance(value, bool):
                        raise ValueError("sticker_asset_valid must be boolean")
                elif key == "action_version":
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError("action_version must be integer")
                elif key == "voice_speed":
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not __import__("math").isfinite(float(value))):
                        raise ValueError("voice_speed must be finite number")
                elif not isinstance(value, str):
                    raise ValueError("payload text values must be strings")
            nonempty = [
                value.strip() if isinstance(value, str) else value
                for value in payload.values()
            ]
            if not payload or not any(value for value in nonempty):
                raise ValueError("payload must contain a non-empty action")
            if any(isinstance(value, str) and len(value) > 4000
                   for value in payload.values()):
                raise ValueError("payload value too long")
            return payload
        except (ValueError, TypeError):
            logger.error(f"任务 payload 解析失败，冻结待人工确认: task_id={task.get('id')}")
            return {"__invalid_payload__": True}

    def _freeze_payload(self, payload: dict | None) -> dict | None:
        """在创建 scheduled media 时冻结 resolver/model 快照。"""
        if not isinstance(payload, dict) or not payload:
            return payload
        has_media = bool(str(payload.get("sticker_emotion") or "").strip()
                         or str(payload.get("voice_text") or "").strip())
        can_freeze_sticker = bool(
            str(payload.get("sticker_emotion") or "").strip()
            and callable(self.sticker_snapshot)
        )
        can_freeze_voice = bool(
            str(payload.get("voice_text") or "").strip()
            and callable(self.voice_snapshot)
        )
        # 只有请求中的每一种媒体都有对应快照能力时才升级 v2；否则保留
        # legacy payload 交给旧路径，避免某个可选注入缺失后把任务永久
        # 标成 v2、每轮 fail-closed 却永远无法成功。
        if (not has_media
                or (str(payload.get("sticker_emotion") or "").strip()
                    and not can_freeze_sticker)
                or (str(payload.get("voice_text") or "").strip()
                    and not can_freeze_voice)):
            return dict(payload)
        frozen = dict(payload)
        frozen["action_version"] = 2
        emotion = str(frozen.get("sticker_emotion") or "").strip()
        if emotion and callable(self.sticker_snapshot):
            sticker = self.sticker_snapshot(emotion)
            if not isinstance(sticker, dict):
                raise ValueError("sticker snapshot must be an object")
            transport = str(sticker.get("transport") or "").strip()
            asset_ref = str(sticker.get("asset_ref") or "").strip()
            library_id = str(sticker.get("library_id") or "").strip()
            role_id = str(sticker.get("role_id") or "").strip()
            digest = str(sticker.get("asset_sha256") or "").strip().lower()
            if not (transport and asset_ref and library_id and role_id
                    and len(digest) == 64
                    and all(c in "0123456789abcdef" for c in digest)):
                raise ValueError("sticker snapshot is incomplete")
            frozen.update({
                "sticker_transport": transport,
                "sticker_asset_ref": asset_ref,
                "sticker_asset_sha256": digest,
                "sticker_asset_valid": bool(sticker.get("asset_valid", True)),
                "sticker_role_id": role_id,
                "sticker_library_id": library_id,
            })
        if str(frozen.get("voice_text") or "").strip() and callable(self.voice_snapshot):
            voice = self.voice_snapshot()
            if not isinstance(voice, dict):
                raise ValueError("voice snapshot must be an object")
            requested_speed = frozen.get("voice_speed")
            if requested_speed is None:
                requested_speed = voice.get("speed", 1.0)
            frozen.update({
                "voice_emotion": str(frozen.get("voice_emotion")
                                      or voice.get("emotion") or "自动"),
                "voice_speed": float(requested_speed),
                "voice_pause": str(frozen.get("voice_pause")
                                    or voice.get("pause") or "自然"),
                "voice_model_profile": str(frozen.get("voice_model_profile")
                                            or voice.get("model_profile") or "v4"),
                # 空 speaker 是合法快照：米雪儿角色用空值表示按中文情绪
                # 自动选择其专属参考音，不能被迁移层误替成丛雨 murasame。
                "voice_speaker": str(
                    frozen["voice_speaker"] if "voice_speaker" in frozen
                    else voice.get("speaker", "murasame")
                ),
                "voice_lang": str(frozen.get("voice_lang")
                                   or voice.get("voice_lang") or "zh"),
            })
        return frozen

    async def _prepare_unified_media_plan(self, task: dict, payload: dict,
                                          scope_key: str) -> list[dict]:
        """将已冻结的 scheduled media payload 转为持久化 child specs。"""
        specs: list[dict] = []
        text = str(payload.get("text") or "").strip()
        if text:
            specs.append({
                "kind": "text", "payload": {"text": text}, "message": text,
                "actual": {"requested": text, "text": text,
                            "delivery_kind": "text", "mode": "verbatim",
                            "attribution": "none"},
            })
        sticker_emotion = str(payload.get("sticker_emotion") or "").strip()
        if sticker_emotion:
            transport = str(payload.get("sticker_transport") or "").strip()
            if not transport:
                raise ValueError("frozen sticker transport is missing")
            sticker_payload = {
                "emotion": sticker_emotion, "count": 1,
                "asset_ref": str(payload.get("sticker_asset_ref") or ""),
                "asset_sha256": str(payload.get("sticker_asset_sha256") or "").lower(),
                "asset_valid": payload.get("sticker_asset_valid") is True,
                "role_id": str(payload.get("sticker_role_id") or ""),
                "library_id": str(payload.get("sticker_library_id") or ""),
            }
            specs.append({
                "kind": "sticker", "payload": sticker_payload,
                "message": transport,
                "actual": {"delivery_kind": "sticker", "text": "",
                            "asset_ref": sticker_payload["asset_ref"],
                            "asset_sha256": sticker_payload["asset_sha256"],
                            "role_id": sticker_payload["role_id"],
                            "library_id": sticker_payload["library_id"]},
            })
        voice_text = str(payload.get("voice_text") or "").strip()
        if voice_text:
            if not callable(self.voice_preparer):
                raise ValueError("voice preparer is unavailable")
            prepared = await self.voice_preparer(dict(payload), scope_key)
            if not isinstance(prepared, dict):
                raise ValueError("voice preparer returned invalid result")
            message = str(prepared.get("message") or "").strip()
            if not message:
                raise ValueError("voice preparer returned empty message")
            voice_payload = {
                "text": voice_text,
                "emotion": str(payload.get("voice_emotion") or "自动"),
                "speed": float(payload.get("voice_speed", 1.0)),
                "pause": str(payload.get("voice_pause") or "自然"),
                "model_profile": str(payload.get("voice_model_profile") or "v4"),
                "speaker": str(
                    payload["voice_speaker"] if "voice_speaker" in payload
                    else "murasame"
                ),
                "voice_lang": str(payload.get("voice_lang") or "zh"),
            }
            actual = dict(prepared.get("actual") or {})
            actual.setdefault("delivery_kind", "voice")
            actual.setdefault("voice_generated", True)
            actual.setdefault("text", voice_text)
            specs.append({"kind": "voice", "payload": voice_payload,
                          "message": message, "actual": actual})
        if not specs:
            raise ValueError("unified media plan is empty")
        return specs

    async def _execute_payload(self, task: dict, payload: dict,
                               scope_key: str) -> str:
        """按 typed payload 执行动作（text/sticker/voice 可组合）。

        返回聚合送达状态：confirmed=全部确认 / uncertain=任一不确定或部分
        成功（冻结防重复）/ failed=全部确定失败（可重试）。
        贴图与语音不再退化为发送 description 文本（审查 Important 5/6）。
        """
        target = task.get("group_id") or task["owner_qq"]
        is_group = bool(task.get("group_id"))
        if payload.get("__invalid_payload__"):
            return "uncertain"
        sender = (self.napcat.send_group_message if is_group
                  else self.napcat.send_private_message)
        # legacy 媒体任务仍由 TaskManager 持有重试权；若 NapCat 也把同一次
        # retryable 失败写进普通 outbox，下一分钟两边都会重发。
        sender_kwargs = (
            {"_allow_outbox_enqueue": False}
            if getattr(self.napcat, "_outbox_store", None) is self.store else {}
        )
        text = str(payload.get("text") or "").strip()
        sticker_emotion = str(payload.get("sticker_emotion") or "").strip()
        voice_text = str(payload.get("voice_text") or "").strip()
        results: list[str] = []

        async def _dispatch(coro):
            try:
                result = await coro
            except Exception as e:
                logger.warning(f"任务子动作执行异常: {e}")
                # 请求可能已到达 QQ 但响应在异常中丢失，不能把它当作
                # 确定失败并自动 release 重试，否则下一轮会重复发送。
                results.append("uncertain")
                return
            state = send_delivery_state(result)
            results.append(state if state in ("confirmed", "uncertain") else "failed")

        # 1) 文本：typed text 原样发送（已是面向对方的话）；无任何 typed 动作
        #    → 备忘 LLM 改写兜底（老行为，2026-08-18 小闹钟事故修复链）
        if text:
            await _dispatch(sender(target, text, **sender_kwargs))
        elif not sticker_emotion and not voice_text:
            msg = await self._compose_message(task)
            await _dispatch(sender(target, msg, **sender_kwargs))
        # 2) 贴图：情绪匹配 → 发送 CQ；未匹配 = 确定失败
        if sticker_emotion:
            cq = self._match_sticker(sticker_emotion)
            if cq:
                await _dispatch(sender(target, cq, **sender_kwargs))
            else:
                results.append("failed")
        # 3) 语音：voice_sender 未注入视为确定失败
        if voice_text:
            if self.voice_sender:
                await _dispatch(self.voice_sender(scope_key, voice_text))
            else:
                results.append("failed")

        if not results:
            return "failed"
        if all(r == "failed" for r in results):
            return "failed"  # 全部确定失败 → release（下一轮可重试）
        if any(r == "uncertain" for r in results):
            return "uncertain"
        if all(r == "confirmed" for r in results):
            return "confirmed"
        return "uncertain"  # 混合 confirmed+failed——冻结，不重放已确认部分

    def _match_sticker(self, emotion: str) -> str:
        """贴图匹配：情绪/关键词 → 单张 CQ 码。返回空串 = 未匹配（确定失败）。"""
        if not self.stickers:
            return ""
        try:
            paths = self.stickers.match_by_emotion_text(emotion, count=1)
        except Exception as e:
            logger.warning(f"任务贴图匹配异常: {e}")
            return ""
        if not paths:
            return ""
        return str(paths[0]) if isinstance(paths, (list, tuple)) else str(paths)

    async def _compose_message(self, task: dict) -> str:
        """把任务备忘改写成给对方看的消息。

        2026-08-18 事故：备忘是 LLM 写给自己的指令（「主动找米雪儿报到…」），
        原样发出去对方读不懂。有 LLM 时把备忘翻译成糖糖自然开口说的话；
        无 LLM / 失败 / 空回复 → 原样兜底（降级链）。群提醒同样走改写。"""
        raw = f"⏰ 糖糖小闹钟～\n{task['description']}"
        if not self._llm_call:
            return raw
        try:
            nickname = ""
            person = await self._run_store_io(
                "get_or_create_person", self.store.get_or_create_person,
                task["owner_qq"],
            )
            nickname = person.get("nickname") or "对方"
            reply = await self._llm_call(
                "你是糖糖。到了你自己设的提醒时间，现在要把这条提醒说给对方。",
                f"你之前留的提醒备忘：{task['description']}\n\n"
                f"现在给 {nickname} 发这条提醒。把备忘用你的口吻自然地讲出来；"
                f"备忘里写「主动找ta说X/提醒ta X」的，就把 X 说给ta听。"
                f"不要提「提醒」「备忘」「系统」这类词；材料外的事不编。",
            )
            if reply and reply.strip():
                return reply.strip()
        except Exception as e:
            logger.warning(f"提醒 LLM 改写失败，原样发送: {e}")
        return raw

    # ---- 添加任务 ----

    def add(self, *, minutes: int = 0, description: str, owner_qq: str, group_id: str = "",
            payload: dict | None = None, idempotency_key: str = "") -> int:
        """N分钟后提醒。group_id 非空 = 到点发群里。payload = typed 动作（P0-C）"""
        remind_at = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
        return self.store.create_task(owner_qq, description, remind_at, group_id,
                                      action_payload=self._freeze_payload(payload),
                                      idempotency_key=idempotency_key)

    def add_at(self, time_str: str, description: str, owner_qq: str,
               date_offset: int = 0, group_id: str = "",
               payload: dict | None = None, idempotency_key: str = "") -> int:
        """指定时间提醒，如 '08:00' '14:30'。date_offset: 0=今天, 1=明天。group_id 非空 = 发群里"""
        hour, minute = time_str.split(":")
        dt = datetime.now().replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if date_offset > 0:
            dt += timedelta(days=date_offset)
        remind_at = dt.strftime("%Y-%m-%d %H:%M")
        return self.store.create_task(owner_qq, description, remind_at, group_id,
                                      action_payload=self._freeze_payload(payload),
                                      idempotency_key=idempotency_key)

    # ---- 查询/取消 ----

    def list_for(self, owner_qq: str) -> list[dict]:
        return self.store.list_tasks(owner_qq)

    def cancel(self, task_id: int, owner_qq: str) -> bool:
        return self.store.cancel_task(task_id, owner_qq)

    def retry(self, task_id: int, owner_qq: str) -> bool:
        """主人确认未收到后，显式恢复不确定提醒。"""
        return self.store.retry_task(task_id, owner_qq)

    def retry_generation(self, task_id: int, owner_qq: str, *,
                         expected_attempt_id: int, request_id: str) -> dict:
        """Phase 2a 只开放确定失败文本；核验与重复风险模式留给 Phase 2b。"""
        return self.store.retry_task_generation(
            task_id, owner_qq, expected_attempt_id=expected_attempt_id,
            request_id=request_id, verification_result="NOT_REQUIRED",
            force_resend_ack=False,
        )
