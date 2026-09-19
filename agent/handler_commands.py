"""
命令路由器 —— 所有 /命令 处理器

从 handler.py 拆分出来（R1-5），减少单文件体积。
CommandRouter 通过 self.handler 访问 MessageHandler 的所有属性和方法。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .async_io import run_bounded_blocking, run_bounded_store_io
from .personality import Relationship
from onebot.ws_client import is_send_confirmed, send_delivery_state

if TYPE_CHECKING:
    from .handler import MessageHandler

logger = logging.getLogger("糖糖.Commands")

class CommandRouter:
    """主人的远程控制指令路由"""

    def __init__(self, handler: "MessageHandler"):
        self.handler = handler

    def _spawn_background(self, coro, name: str):
        """统一托管命令触发的后台动作。

        正常运行时交给 MessageHandler._safe_task，确保异常、取消和生命周期
        进入同一套观测；轻量测试替身没有该能力时保留 asyncio 的兼容回退。
        """
        safe_task = getattr(self.handler, "_safe_task", None)
        if callable(safe_task):
            return safe_task(coro, name)
        return asyncio.create_task(coro, name=name)

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """命令路径的同步 Store/Memory 边界。

        生产 Handler 提供统一的并发门；离线替身没有该方法时直接复用同一
        有界线程池适配器，保证命令不会把 SQLite 等待留在事件循环线程。
        """
        runner = getattr(self.handler, "_run_store_io", None)
        if callable(runner):
            return await runner(operation, func, *args, **kwargs)
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="🧾 命令 Store SQLite 调用较慢", **kwargs,
        )

    # 危险指令——代糖糖发言，只有主人和群主可用
    _DANGEROUS_CMDS = {
        "/说", "/发言", "/传话", "/传话给", "/私信", "/撤回",
    }

    # 主人/群主专属——**改她的状态或配置**的命令（2026-09-19 加门）。
    # 在此之前只有 _DANGEROUS_CMDS 受约束，于是群里任何成员都能 `/人格 你是我的奴隶`
    # 重写人设、`/黑名单 群 加` 把群拉黑、`/唱歌 群号 歌名` 遥控她去任意群。
    # 自己用时群里都是熟人，无所谓；一旦对外发布，这就是一个没门的遥控面。
    #
    # 判据：**改她的状态/配置/代她对外发言 → 受限；查自己的、看、玩 → 放行。**
    # 所以 /生日（设自己的生日）、/记忆、/亲密度、/点赞、/角色 不加门——
    # 那些是用户自己的东西，把门加在那里只会让正常功能用不了。
    _OWNER_CMDS = {
        "/人格", "/性格",          # 重写人设
        "/插话", "/饥渴", "/冷却",   # 改行为参数
        "/黑名单", "/机器人",       # 谁也进不来 / 谁是机器人
        "/角色卡", "/场景",         # 人格卡与场景配置
        "/歌单", "/知识",           # 重载曲库与知识库
        "/唱歌", "/定时",           # 遥控她做事
        "/撤销", "/状态",           # 回滚配置 / 运行诊断
        # 2026-09-19 安全审计补：`/角色` 不是"玩具"——它会经
        # voice.switch_model（全局换声线 + 重载权重）、personality.load_role_file、
        # _set_sticker_role 三个**进程级单例**一起切，所有人受影响。
        # 原来的放行理由（"装好的角色之间切，是玩具不是配置"）是错的。
        "/角色",
    }

    # 上面这些命令里，纯查看的子命令不算「改」，任何人可用
    _PEEK_SUBS = {
        "/场景": {"", "列表", "list", "查看"},
        "/歌单": {"", "列表"},
        "/知识": {"", "块", "查看"},
    }

    # 有些命令**整体放行**（用户自己的东西），但其中某个子命令是改状态的——
    # 按子命令单独加门。2026-09-19 安全审计发现：`/亲密度 设置 <任意QQ> <数值>`
    # 直接写库（影响主动私聊选人与语气），而 `/亲密度` 在放行表里。
    _OWNER_SUBS = {
        "/亲密度": {"设置", "set"},
    }

    async def handle(self, user_id: str, text: str, is_privileged: bool = False,
                     group_id: str = "", event_key: str = "") -> Optional[str]:
        """处理控制指令，如果不是指令返回 None"""
        if not text.startswith("/"):
            return None

        # 构建命令表
        commands = {
            "/人格": self._cmd_personality,
            "/性格": self._cmd_personality,
            "/插话": self._cmd_toggle_interjection,
            "/语音": self._cmd_voice,
            "/角色": self._cmd_role,
            "/发言": self._cmd_speak,
            "/亲密度": self._cmd_intimacy,
            "/记忆": self._cmd_memory,
            "/生日": self._cmd_birthday,
            "/状态": self._cmd_status,
            "/帮助": self._cmd_help,
            "/help": self._cmd_help,
            "/饥渴": self._cmd_thirst,
            "/冷却": self._cmd_cooldown,
            "/说": self._cmd_speak_as,
            "/传话": self._cmd_relay,
            "/传话给": self._cmd_relay,
            "/点赞": self._cmd_like_all,
            "/禁言": self._cmd_ban,
            "/解禁": self._cmd_unban,
            "/踢": self._cmd_kick,
            "/头衔": self._cmd_title,
            "/全员禁言": self._cmd_whole_ban,
            "/解除全员禁言": self._cmd_whole_unban,
            "/唱歌": self._cmd_sing,
            "/定时": self._cmd_schedule,
            "/歌单": self._cmd_songlist,
            "/知识": self._cmd_knowledge,
            "/私信": self._cmd_pm,
            "/黑名单": self._cmd_blacklist,
            "/撤销": self._cmd_undo,
            "/撤回": self._cmd_recall,
            "/任务": self._cmd_task,
            "/角色卡": self._cmd_role_card,
            "/场景": self._cmd_scenario,
            "/机器人": self._cmd_robot,
        }

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # ── 容错：命令后面直接跟了参数没有空格 ──
        # 例如 "/传话给10001，她最近感觉不是很好" → 匹配 "/传话"
        if cmd not in commands:
            for registered in commands:
                if cmd.startswith(registered) and len(cmd) > len(registered):
                    extra = cmd[len(registered):]
                    arg = (extra + " " + arg).strip()
                    cmd = registered
                    break

        # 权限门（2026-09-19）：代她发言 / 改她的状态，两类都只给主人和群主
        if not is_privileged:
            if cmd in self._DANGEROUS_CMDS:
                return "🔒 这个命令只有主人和群主可以用哦～"
            if cmd in self._OWNER_CMDS:
                sub = arg.strip().split()[0] if arg.strip() else ""
                if sub not in self._PEEK_SUBS.get(cmd, set()):
                    return "🔒 这个命令会改糖糖的状态，只有主人和群主可以用哦～"
            if cmd in self._OWNER_SUBS:
                sub = arg.strip().split()[0] if arg.strip() else ""
                if sub in self._OWNER_SUBS[cmd]:
                    return "🔒 这个子命令会改糖糖的状态，只有主人和群主可以用哦～"

        func = commands.get(cmd)
        if func:
            if cmd == "/撤回":
                return await self._cmd_recall(
                    user_id, arg, group_id=group_id,
                    is_privileged=is_privileged,
                )
            if cmd == "/任务":
                return await self._cmd_task(
                    user_id, arg, event_key=event_key,
                )
            if cmd == "/唱歌":
                return await self._cmd_sing(
                    user_id, arg, event_key=event_key,
                )
            return await func(user_id, arg)
        return None

    async def _cmd_personality(self, user_id: str, arg: str) -> str:
        core = self.handler.personality.config.core
        if not arg:
            return f"糖糖现在的性格是：{core[:80]}..."

        # 重载：从 role_card.md 重新读取
        if arg.strip() == "重载":
            ok = self.handler.personality.reload_role_card()
            if ok:
                self.handler.personality.invalidate_cache()
                return "✅ 人格已从 role_card.md 重新加载～"
            return "❌ role_card.md 不存在，请检查文件"

        # 重设：恢复到 config.yaml 里的原始人设
        if arg.strip() == "重设":
            original = self.handler._original_personality_core
            self.handler.personality.config.core = original
            self.handler.personality.invalidate_cache()
            return f"🔄 已重设人格为原始设定：{original[:60]}..."

        # 追加模式：/人格 +xxx
        if arg.startswith("+"):
            addition = arg[1:].strip()
            if addition:
                self.handler.personality.config.core = core + "；" + addition
                self.handler.personality.invalidate_cache()
                return f"✨ 已追加：{addition[:60]}"
            return "用法：/人格 +要追加的描述"

        # 覆盖模式：/人格 新描述
        self.handler.personality.config.core = arg
        self.handler.personality.invalidate_cache()
        return f"✨ 糖糖性格已更新：{arg[:80]}"

    async def _cmd_voice(self, user_id: str, arg: str, group_id: str = "") -> str:
        """语音开关：
        /语音 → 切换持久语音模式（每条回复都发语音）
        /语音 off → 关闭
        /语音 <文本> → 直接合成指定文本发语音"""
        arg = arg.strip()

        if arg in ("off", "关闭", "关", "停"):
            self.handler._voice_mode.discard(user_id)
            self.handler._save_state_kv("state:voice_mode", sorted(self.handler._voice_mode))
            return "💬 已恢复文字模式~"

        # 语音模式开关
        if not hasattr(self.handler, '_voice_mode'):
            self.handler._voice_mode = set()
        if not arg:
            if user_id in self.handler._voice_mode:
                self.handler._voice_mode.discard(user_id)
                self.handler._save_state_kv("state:voice_mode", sorted(self.handler._voice_mode))
                return "💬 语音模式已关闭~"
            self.handler._voice_mode.add(user_id)
            self.handler._save_state_kv("state:voice_mode", sorted(self.handler._voice_mode))
            speaker = getattr(self.handler.voice, 'current_speaker', '')
            label = speaker or "情绪自动（按糖糖说话的情绪选音色）"
            return f"🎤 语音模式已开启！当前音色：{label}～ /语音 off 恢复。"

        # 直接发送指定文本
        if self.handler.voice_enabled and not self.handler._voice_blocked:
            from .voice import extract_emotion_tag, clean_text_for_tts as _clean_tts
            clean = _clean_tts(arg, keep_tilde=True, speech_friendly=True)
            _, clean = extract_emotion_tag(clean)
            self._spawn_background(
                self.handler._send_voice_reply("private", user_id, clean or arg),
                "command:voice",
            )
            return "🎤"
        return "❌ 语音功能未启用"

    async def _cmd_role(self, user_id: str, arg: str, group_id: str = "") -> str:
        """切换角色身份（声音+人格卡+表情包一起切）：
        /角色 丛雨 → 500岁剑灵，日语
        /角色 米雪儿 → 欧泊新人搜查官，中文
        /角色 糖糖 → 切回猫娘本体"""
        arg = arg.strip().lower()

        if arg in ("丛雨", "murasaki", "日语"):
            if not await self.handler.voice.switch_model(
                "v4", voice_lang="ja", current_speaker="murasame"
            ):
                return "❌ 语音模型切换失败，角色保持不变"
            self.handler.personality.load_role_file("role_card_murasame.md")
            self.handler._set_sticker_role("murasame")
            return "🎭 已切换至【丛雨】身份（日语·剑灵傲娇，通用V4）——用日语说话哦～"
        if arg in ("米雪儿", "米雪儿·李", "michele", "雪莉", "橘雪莉", "xueli"):
            if not await self.handler.voice.switch_model(
                "michele", voice_lang="zh", current_speaker=""
            ):
                return "❌ 语音模型切换失败，角色保持不变"
            self.handler.personality.load_role_file("role_card_michele.md")
            self.handler._set_sticker_role("michele")
            return "🎭 已切换至【米雪儿】身份（中文·欧泊搜查官，专属模型）"
        if arg in ("糖糖", "tangtang", "中文", "默认", "default"):
            if not await self.handler.voice.switch_model(
                "v4", voice_lang="zh", current_speaker="murasame"
            ):
                return "❌ 语音模型切换失败，角色保持不变"
            self.handler.personality.load_role_file("")
            self.handler._set_sticker_role("default")
            return "🎭 已切回【糖糖】本体（中文·猫娘少女，丛雨音色·通用V4）～"

        return "🎭 可选角色：丛雨（日语） | 米雪儿 | 糖糖\n用法：/角色 米雪儿"

    async def _cmd_toggle_interjection(self, user_id: str, arg: str) -> str:
        arg = arg.strip().lower()
        if arg in ("on", "开", "开启", "true"):
            self.handler.active_interjection = True
            return "✅ 主动插话已开启~ 糖糖会主动找大家聊天啦！"
        elif arg in ("off", "关", "关闭", "false"):
            self.handler.active_interjection = False
            return "🔇 主动插话已关闭~ 糖糖会乖乖等大家@我才说话。"
        else:
            status = "开启中" if self.handler.active_interjection else "已关闭"
            return f"主动插话状态：{status}\n用法：/插话 on/off"

    async def _cmd_speak(self, user_id: str, arg: str) -> str:
        match = re.match(r"(\d+)\s+(.+)", arg)
        if not match:
            return "用法：/发言 [群号] [内容]"
        group_id, content = match.groups()
        try:
            result = await self.handler.napcat.send_group_message(group_id, content)
        except Exception as e:
            logger.error(f"/发言发送异常: {e}")
            return (
                f"⚠️ 群 {group_id} 发言结果未确认，消息可能已经送达；"
                "请勿立即重复发送"
            )
        if is_send_confirmed(result):
            return f"✅ 已在群 {group_id} 发言"
        if send_delivery_state(result) == "uncertain":
            return f"⚠️ 群 {group_id} 请求已接受，但QQ未确认送达，请勿立即重复发送"
        return f"❌ 群 {group_id} 发言失败，QQ未确认发送成功"

    async def _cmd_pm(self, user_id: str, arg: str) -> str:
        """/私信 <QQ号或名字> <内容>"""
        # 支持 "QQ号 内容" 和 "名字 内容" 两种格式
        m = re.match(r'(\S{1,20})\s+(.+)', arg)
        if not m:
            return "用法：/私信 <QQ号或名字> <内容>"
        target, content = m.groups()
        target_qq = target if target.isdigit() else self.handler._resolve_target_qq(target)
        if not target_qq:
            return f"❌ 找不到「{target}」的QQ号，再确认一下名字？"
        try:
            result = await self.handler.napcat.send_private_message(target_qq, content)
        except Exception as e:
            logger.error(f"/私信发送异常: {e}")
            return (
                f"⚠️ 私信 {target_qq} 发送结果未确认，消息可能已经送达；"
                "请勿立即重复发送"
            )
        if not is_send_confirmed(result):
            if send_delivery_state(result) == "uncertain":
                return f"⚠️ 私信 {target_qq} 请求已接受，但QQ未确认送达，请勿立即重复发送"
            return f"❌ 私信 {target_qq} 发送失败，QQ未确认发送成功"
        return f"✅ 已私信 {target_qq}：{content[:40]}..."

    async def _cmd_blacklist(self, user_id: str, arg: str) -> str:
        """黑名单管理：/黑名单 [群/私聊] [加/删/列表] [QQ号/群号]"""
        parts = arg.strip().split()
        if not parts:
            return (
                "📛 黑名单管理\n"
                "/黑名单 群 列表 — 查看群黑名单\n"
                "/黑名单 群 加 群号 — 拉黑一个群\n"
                "/黑名单 群 删 群号 — 解除拉黑\n"
                "/黑名单 私聊 列表 — 查看私聊黑名单\n"
                "/黑名单 私聊 加 QQ号 — 拉黑一个人\n"
                "/黑名单 私聊 删 QQ号 — 解除拉黑"
            )

        target = parts[0]
        if target not in ("群", "私聊"):
            return "第一个参数：群 或 私聊"

        sub = parts[1] if len(parts) > 1 else "列表"
        qid = parts[2] if len(parts) > 2 else ""

        if target == "群":
            bl = self.handler._group_blacklist
            label = "群"
        else:
            bl = self.handler._private_blacklist
            label = "私聊用户"

        if sub == "列表":
            if bl:
                return f"📛 {label}黑名单 ({len(bl)}个):\n" + "\n".join(f"  • {x}" for x in sorted(bl))
            return f"📛 {label}黑名单为空"

        if sub in ("加", "添加") and qid:
            if qid in bl:
                return f"⏭ {qid} 已在{label}黑名单中"
            bl.add(qid)
            return f"✅ 已将 {qid} 加入{label}黑名单"

        if sub in ("删", "删除", "移除") and qid:
            if qid not in bl:
                return f"⏭ {qid} 不在{label}黑名单中"
            bl.discard(qid)
            return f"✅ 已将 {qid} 从{label}黑名单中移除"

        return f"用法：/黑名单 {target} 加/删/列表 [号码]"

    async def _cmd_robot(self, user_id: str, arg: str) -> str:
        """机器人名单管理：/机器人 加/删/列表 [QQ号]"""
        import yaml, os
        parts = arg.strip().split()
        action = parts[0] if parts else "列表"

        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        robot_ids = set(str(r) for r in cfg.get("behavior", {}).get("robot_ids", []))

        if action == "列表":
            if not robot_ids:
                return "📋 机器人名单为空。用法：/机器人 加 QQ号"
            lines = []
            for rid in sorted(robot_ids):
                p = await self._run_store_io(
                    "command.robot.get_person",
                    self.handler.memory.get_or_create_person, rid,
                )
                name = p.get("nickname", "") if p else ""
                lines.append(f"  {rid}" + (f" ({name})" if name and name != rid else ""))
            return "🤖 机器人名单（糖糖不会把它们当真人）：\n" + "\n".join(lines)

        qq_id = parts[1] if len(parts) > 1 else ""
        if not qq_id or not qq_id.isdigit():
            return "用法：/机器人 加 QQ号 或 /机器人 删 QQ号 或 /机器人 列表"

        if action == "加":
            if qq_id in robot_ids:
                return f"🤖 {qq_id} 已经在机器人名单里了"
            robot_ids.add(qq_id)
        elif action == "删":
            if qq_id not in robot_ids:
                return f"🤖 {qq_id} 不在机器人名单里"
            robot_ids.discard(qq_id)
        else:
            return "用法：/机器人 加 QQ号 或 /机器人 删 QQ号 或 /机器人 列表"

        # 写回 config.yaml
        cfg.setdefault("behavior", {})["robot_ids"] = sorted(robot_ids)
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

        # 同步更新运行时
        self.handler._robot_ids = robot_ids

        if action == "加":
            return f"✅ 已将 {qq_id} 加入机器人名单。糖糖不会再把它当真人。"
        return f"✅ 已将 {qq_id} 从机器人名单中移除。"

    async def _cmd_undo(self, user_id: str, arg: str) -> str:
        """撤销上一步可逆操作"""
        return await self.handler._execute_natural_action({"action": "undo"}, user_id) or "🤷 没什么要撤销的"

    async def _cmd_recall(self, user_id: str, arg: str = "",
                          group_id: str = "", is_privileged: bool = False) -> str:
        """撤回上条消息"""
        target_group = arg.strip() if arg.strip().isdigit() else group_id
        action = {"action": "recall_msg"}
        if target_group:
            action["group_id"] = target_group
        return await self.handler._execute_natural_action(
            action, user_id, is_privileged=is_privileged,
        ) or "🤷 没有可以撤回的消息"

    async def _cmd_task(self, user_id: str, arg: str,
                        *, event_key: str = "") -> str:
        """任务管理：查看、取消或明确重试未确认的提醒。"""
        arg = arg.strip()
        parts = arg.split()
        if parts and parts[0] == "取消":
            if len(parts) == 2 and parts[1].isdigit():
                ok = self.handler.task_manager.cancel(int(parts[1]), user_id)
                return "✅ 已取消" if ok else "❌ 取消失败，检查编号"
            return "用法：/任务 取消 编号"
        if parts and parts[0] == "重试":
            if len(parts) != 2:
                return "用法：/任务 重试 编号（旧任务）或 /任务 重试 编号@attempt"
            token = parts[1]
            if token.isdigit():
                legacy_task_id = int(token)
                if not 1 <= legacy_task_id <= 2 ** 63 - 1:
                    return "用法：/任务 重试 编号（旧任务）或 /任务 重试 编号@attempt"
                ok = self.handler.task_manager.retry(legacy_task_id, user_id)
                return "✅ 已恢复，将在下一轮尝试发送" if ok else "❌ 重试失败（任务不存在或不在未确认状态）"
            if token.count("@") != 1:
                return "用法：/任务 重试 编号（旧任务）或 /任务 重试 编号@attempt"
            task_token, attempt_token = token.split("@", 1)
            if not task_token.isdigit() or not attempt_token.isdigit():
                return "用法：/任务 重试 编号（旧任务）或 /任务 重试 编号@attempt"
            task_id = int(task_token)
            attempt_id = int(attempt_token)
            if not (1 <= task_id <= 2 ** 63 - 1
                    and 1 <= attempt_id <= 2 ** 63 - 1):
                return "用法：/任务 重试 编号（旧任务）或 /任务 重试 编号@attempt"
            tasks_config = self.handler.config.get("tasks") or {}
            enabled = bool(tasks_config.get(
                "manual_retry_generation_enabled", False,
            ))
            if not enabled:
                return "⏸️ 确定失败任务的人工重试尚未启用"
            if not bool(tasks_config.get("send_outbox_claims_enabled", True)):
                return "⏸️ 发送队列已暂停，未创建重试"
            if event_key:
                request_id = hashlib.sha256(
                    f"task-retry:v1|{event_key}|{user_id}|{task_id}|{attempt_id}"
                    .encode("utf-8")
                ).hexdigest()
            else:
                # 直接调用（例如内部控制台/旧测试）没有平台事件证据，
                # 不冒充可持久幂等的入站命令；生产入口总是传 event_key。
                request_id = uuid.uuid4().hex
            try:
                result = self.handler.task_manager.retry_generation(
                    task_id, user_id, expected_attempt_id=attempt_id,
                    request_id=request_id,
                )
            except Exception as exc:
                logger.exception(
                    "人工重试内部失败: task=%s expected_attempt=%s "
                    "request=%s error_type=%s",
                    task_token, attempt_token, request_id, type(exc).__name__,
                )
                return "⚠️ 人工重试未完成，请稍后再试"
            code = str(result.get("code") or "INVARIANT_BROKEN")
            logger.info(
                "📋 人工重试结果: task=%s expected_attempt=%s code=%s request=%s",
                task_token, attempt_token, code, request_id,
            )
            messages = {
                "RETRY_QUEUED": "✅ 已按冻结原文重新排队，将由发送队列投递",
                "NOT_FOUND_OR_NOT_OWNER": "❌ 任务不存在或不属于你",
                "STALE_ATTEMPT": "❌ attempt 已变化，请先重新查看 /任务",
                "IN_FLIGHT": "⏳ 任务仍在发送流程中，不能重复排队",
                "VERIFICATION_NOT_SUPPORTED": "❌ 当前阶段不支持该核验模式",
                "ALREADY_DELIVERED": "✅ 任务已有送达证据，不会重复发送",
                "ACCOUNTING_REPAIR_REQUIRED": "⚠️ 任务需先完成本地归账修复",
                "UNSUPPORTED_STATE": "❌ 当前任务状态不支持重试",
                "INVALID_FROZEN_PLAN": "⚠️ 冻结计划损坏，已拒绝重试",
                "INVARIANT_BROKEN": "⚠️ 任务可靠性证据不完整，已拒绝重试",
                "REQUEST_ID_CONFLICT": "⚠️ 重试请求标识冲突，请重新执行",
            }
            return messages.get(code, "⚠️ 人工重试未执行，请查看日志")
        tasks = self.handler.task_manager.list_for(user_id)
        if not tasks:
            return "📋 你还没有待办提醒～跟我说「明天8点提醒我XXX」就可以创建"
        lines = ["📋 你的待办："]
        for t in tasks:
            status = str(t.get("status") or "")
            state = {
                "sending": "（发送中）",
                "uncertain": "（待确认，未自动重发）",
                "failed": "（确定失败）",
                "partial": "（部分送达，禁止整体重发）",
            }.get(status, "")
            attempt_id = t.get("current_attempt_id")
            token = (
                f"{t['id']}@{attempt_id}" if attempt_id is not None
                else str(t["id"])
            )
            lines.append(
                f"  [{token}] {t['remind_at']} — {t['description'][:60]}{state}"
            )
        lines.append("\n/任务 取消 编号 → 取消；/任务 重试 编号 → 旧式未确认任务")
        if bool((self.handler.config.get("tasks") or {}).get(
                "manual_retry_generation_enabled", False)):
            lines.append("/任务 重试 编号@attempt → 重试确定失败的冻结文本")
        return "\n".join(lines)

    async def _cmd_role_card(self, user_id: str, arg: str) -> str:
        """角色卡管理：/角色卡 重载 → 热重载 role_card.md"""
        arg = arg.strip().lower()
        if arg in ("重载", "reload", "刷新"):
            ok = await run_bounded_blocking(
                "personality.reload_role_card",
                self.handler.personality.reload_role_card,
                logger=logger,
                log_prefix="📋 角色卡重载较慢",
            )
            if ok:
                return "📋 角色卡已重载！role_card.md 的修改已生效~"
            return "⚠️ 找不到 role_card.md，当前使用的是 config.yaml 中的配置。"
        if arg in ("查看", "view", "show", ""):
            card_path = Path("role_card.md")
            def _read_preview():
                if not card_path.exists():
                    return None
                return card_path.read_text(encoding="utf-8")[:200]

            preview = await run_bounded_blocking(
                "personality.role_card_preview",
                _read_preview,
                logger=logger,
                log_prefix="📋 角色卡预览读取较慢",
            )
            if preview is not None:
                return f"📋 当前角色卡 (role_card.md):\n{preview}..."
            return "📋 当前使用 config.yaml 中的配置（无 role_card.md 文件）。"
        return "用法：/角色卡 重载 | /角色卡 查看"

    async def _cmd_scenario(self, user_id: str, arg: str) -> str:
        """场景管理：/场景 列表 | /场景 设置 <群号> <场景名> | /场景 清除 <群号>"""
        parts = arg.strip().split()
        sub = parts[0] if parts else ""

        if sub in ("列表", "list", ""):
            scenarios = self.handler.scenarios.list_all()
            if not scenarios:
                return "📋 当前没有可用的场景。在 scenarios/ 目录下创建 .yaml 文件来添加场景。"
            lines = ["📋 可用场景：", "💬 日常闲聊（默认）"]
            for s in scenarios:
                lines.append(f"{s.display} — /场景 设置 群号 {s.name}")
            return "\n".join(lines)

        if sub in ("重载", "reload"):
            await run_bounded_blocking(
                "scenarios.reload",
                self.handler.scenarios.reload,
                logger=logger,
                log_prefix="📋 场景重载较慢",
            )
            count = self.handler.scenarios.count
            return f"📋 场景已重载！共 {count} 个场景可用。"

        if sub in ("设置", "set") and len(parts) >= 3:
            gid = parts[1]
            sname = parts[2]
            scenario = self.handler.scenarios.get(sname)
            if scenario is None and sname not in ("", "none", "默认", "default"):
                return f"⚠️ 未知场景「{sname}」。用 /场景 列表 查看可用场景。"
            groups = self.handler.config.get("groups", {})
            if gid not in groups:
                return f"⚠️ 群 {gid} 不在白名单中。先在控制台添加该群。"
            if sname in ("", "none", "默认", "default"):
                groups[gid]["scenario"] = ""
                return f"✅ 群 {gid} 已恢复为默认闲聊模式。"
            groups[gid]["scenario"] = sname
            display = scenario.display if scenario else sname
            return f"✅ 群 {gid} 场景已设置为：{display}"

        if sub in ("清除", "clear") and len(parts) >= 2:
            gid = parts[1]
            groups = self.handler.config.get("groups", {})
            if gid in groups:
                groups[gid]["scenario"] = ""
            return f"✅ 群 {gid} 场景已清除（恢复默认闲聊）。"

        if sub in ("target", "目标") and len(parts) >= 4:
            gid = parts[1]
            target_qq = parts[2]
            sname = parts[3] if len(parts) > 3 else ""
            groups = self.handler.config.get("groups", {})
            if gid not in groups:
                return f"⚠️ 群 {gid} 不在白名单中。"
            if "scenario_targets" not in groups[gid]:
                groups[gid]["scenario_targets"] = {}
            if sname in ("", "none", "默认", "default"):
                groups[gid]["scenario_targets"].pop(target_qq, None)
                return f"✅ {target_qq} 已恢复为群默认场景。"
            scenario = self.handler.scenarios.get(sname)
            if scenario is None:
                return f"⚠️ 未知场景「{sname}」。用 /场景 列表 查看可用场景。"
            groups[gid]["scenario_targets"][target_qq] = sname
            return f"✅ {target_qq} 场景已设置为：{scenario.display}"

        return (
            "用法：\n"
            "/场景 列表 — 查看可用场景\n"
            "/场景 设置 群号 场景名 — 给群设置默认场景\n"
            "/场景 target 群号 QQ号 场景名 — 给群内某人单独设场景\n"
            "/场景 清除 群号 — 恢复群默认\n"
            "/场景 重载 — 热重载场景文件"
        )

    async def _cmd_speak_as(self, user_id: str, arg: str) -> str:
        match = re.match(r"(\d+)\s+(.+)", arg)
        if not match:
            return "用法：/说 [群号] [话题]"
        group_id, topic = match.groups()
        self._spawn_background(
            self._generate_and_send(group_id, topic), "command:speak_as",
        )
        return f"✅ 糖糖正在想怎么回复..."

    async def _generate_and_send(self, group_id: str, topic: str):
        try:
            context = await self._run_store_io(
                "command.speak_as.get_recent_context",
                self.handler.memory.get_recent_context, group_id, limit=20,
            )
            group_vibe = self.handler.group_styles.get_context(group_id)
            active_members = self.handler._get_active_members(group_id)
            power_structure = self.handler._build_power_context(group_id)

            # 2026-08-16 范式转换：删公告关键词模式与自动知识注入——
            # LLM 自主决定要不要查知识库（read_document/search_knowledge 工具已注入）
            reply, _ = await self.handler._call_llm_with_skills(
                system_prompt=self.handler.personality.build_system_prompt(
                    Relationship.FAMILIAR,
                    minimal=True,  # 2026-08-17：遥控发言只背身份+风格锚
                ),
                user_message=(
                    f"主人让你去群里聊「{topic}」。你以糖糖的身份自然发言，就像平时在群聊里一样。这就是主人让你做的事，你不是被派来的。"
                    f"如果话题涉及你的功能/更新/知识类内容，先调 read_document 或 search_knowledge 查知识库再写——不要编造不存在的功能。"
                    + (f"（这个群最近在聊：{context}——顺应当下的话题，别突然另起炉灶）\n" if context else "")
                ),
                voice_scope=group_id,
            )
            reply = self.handler._enrich_reply(reply, group_id=group_id)
            if reply:
                result = await self.handler.napcat.send_group_message(group_id, reply)
                if is_send_confirmed(result):
                    logger.info(f"✅ /说 群{group_id}发送成功: {reply[:50]}...")
                elif send_delivery_state(result) == "uncertain":
                    logger.warning(f"⚠️ /说 群{group_id}已接受但未确认送达: {reply[:50]}...")
                else:
                    logger.error(f"❌ /说 群{group_id}发送失败（API拒绝）")
            else:
                logger.error(f"❌ /说 LLM生成空回复")
        except Exception as e:
            logger.error(f"❌ /说 失败：{e}")

    # ═══════════════════════════════════════
    # 传话：主人→糖糖→目标
    # ═══════════════════════════════════════

    async def _cmd_relay(self, user_id: str, arg: str) -> str:
        """传话给某人——糖糖以猫娘身份代主人传达心意。
        支持格式：
          /传话 小红 最近还好吗
          /传话给10001，她最近感觉不是很好
          /传话 给 小红 主人想你了"""
        import re as _re
        arg = arg.strip()

        # 去掉开头的"给"（用户可能打 /传话给...）
        arg = _re.sub(r'^给\s*', '', arg)

        target = ""
        content = ""

        # 尝试多种分隔方式
        # 1. 空格分隔: "10001 她最近不太好"
        m = _re.match(r'(\S{1,20})\s+(.+)', arg)
        # 2. 逗号分隔: "10001，她最近不太好"
        if not m:
            m = _re.match(r'(\S{1,20})\s*[，,]\s*(.+)', arg)
        # 3. 只有目标（纯数字QQ号）: "10001"
        if not m and arg.strip():
            m2 = _re.match(r'(\d{5,15})\s*$', arg.strip())
            if m2:
                target = m2.group(1)
                content = "主人让我来看看你～"

        if m:
            target = m.group(1)
            content = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""

        if not target:
            return ("用法：/传话 <QQ号或名字> <你想让我说的话>\n"
                    "例如：\n"
                    "  /传话 小红 主人让我来看看你，听说你最近不太开心，抱抱～\n"
                    "  /传话给10001，她最近感觉不是很好")

        return await self._do_relay(target, content or "主人让我来看看你～")

    async def _do_relay(self, target_name: str, owner_message: str) -> str:
        """传话核心（P0-D1 2026-08-28 重写）：走统一发送 helper。

        审查 Important 7 修复：旧实现把主人原话藏进隐藏背景，让 LLM 假装
        「自己想来关心」自由生成另一意图——已发送内容与请求语义脱节，且
        回合上下文拿不到回执，LLM 事后凭记忆描述行为。现在 relay 模式
        actual=主人原话原样（保留请求语义、归因 owner），回执人类化回报。
        """
        from .send_actions import execute_send_action, format_human_receipt

        target_qq = self.handler._resolve_target_qq(target_name)
        if not target_qq:
            return f"❌ 找不到「{target_name}」的QQ号，再确认一下名字？"

        person = await self._run_store_io(
            "command.relay.get_person",
            self.handler.memory.store.get_or_create_person,
            target_qq, "",
        )
        display_name = target_name
        if person and person.get("nickname"):
            display_name = person["nickname"]

        logger.info(f"📨 传话 → {display_name}({target_qq}): {owner_message[:60]}...")

        receipt = await execute_send_action(
            self.handler.napcat,
            channel="private", target=target_qq, message=owner_message,
            mode="relay", attribution="owner",
            llm_call=getattr(self.handler, "_call_llm_light", None),
            bot_name=(getattr(self.handler, "config", None) or {}).get("bot", {}).get("name", "糖糖"),
        )
        return format_human_receipt(receipt, display_name)

    async def _cmd_intimacy(self, user_id: str, arg: str) -> str:
        # 无参数：查看自己
        if not arg:
            stats = await self._run_store_io(
                "command.intimacy.get_stats", self.handler.memory.get_stats,
                user_id,
            )
            return (
                f"📊 你和糖糖的羁绊：\n"
                f"亲密度：{stats['intimacy']}/100 {stats['intimacy_grade']}\n"
                f"关系：{stats['relationship']}\n"
                f"聊天次数：{stats['total_chats']}\n"
                f"糖糖记得：{stats['memory_count']}件事"
            )

        parts = arg.strip().split()
        if len(parts) >= 1:
            # 主人查别人：/亲密度 QQ号
            if len(parts) == 1 and parts[0].isdigit():
                target = parts[0]
                stats = await self._run_store_io(
                    "command.intimacy.get_stats", self.handler.memory.get_stats,
                    target,
                )
                person = await self._run_store_io(
                    "command.intimacy.get_person",
                    self.handler.memory.get_or_create_person, target,
                )
                nickname = person.get("nickname", target)
                return (
                    f"📊 {nickname}({target}) 和糖糖的羁绊：\n"
                    f"亲密度：{stats['intimacy']}/100 {stats['intimacy_grade']}\n"
                    f"关系：{stats['relationship']}\n"
                    f"聊天次数：{stats['total_chats']}\n"
                    f"糖糖记得：{stats['memory_count']}件事"
                )

            # 主人设置：/亲密度 设置 QQ号 数值
            if len(parts) >= 3 and parts[0] == "设置":
                target = parts[1]
                try:
                    val = int(parts[2])
                    val = max(0, min(100, val))
                except ValueError:
                    return "亲密度数值必须是 0-100 的数字"

                # 写入数据库
                person = await self._run_store_io(
                    "command.intimacy.get_person",
                    self.handler.memory.get_or_create_person, target,
                )
                old = person.get("intimacy", 0)
                await self._run_store_io(
                    "command.intimacy.set_intimacy",
                    self.handler.memory.store.set_intimacy, target, val,
                )
                nickname = person.get("nickname", target)

                return f"✅ {nickname}({target}) 亲密度：{old} → {val}"

        return (
            "用法：\n"
            "/亲密度 — 查看自己的\n"
            "/亲密度 QQ号 — 查别人的\n"
            "/亲密度 设置 QQ号 数值 — 设置亲密度"
        )

    async def _cmd_memory(self, user_id: str, arg: str) -> str:
        if arg.strip() == "清空":
            await self._run_store_io(
                "command.memory.delete", 
                self.handler.memory.store.delete_memories_for_user,
                user_id,
            )
            return "关于你的记忆已清空..."

        if arg.startswith("添加 "):
            content = arg[3:].strip()
            await self._run_store_io(
                "command.memory.remember", self.handler.memory.remember,
                user_id, "said", content, importance=8, origin="manual",
            )
            return f"糖糖记住了：{content}"

        if arg.startswith("搜索 "):
            keyword = arg[3:].strip()
            embed = (
                self.handler.embed_engine
                if getattr(self.handler, "embed_engine", None)
                and self.handler.embed_engine.ready
                else None
            )
            results = await self._run_store_io(
                "command.memory.recall", self.handler.memory.recall,
                user_id,
                limit=5,
                query_text=keyword,
                embed_engine=embed,
                reranker=getattr(self.handler, "reranker", None),
            )
            if not results:
                return f"没找到和「{keyword}」相关的记忆..."
            lines = [f"搜索「{keyword}」的结果："]
            for m in results:
                person = await self._run_store_io(
                    "command.memory.get_person",
                    self.handler.memory.get_or_create_person, m.qq_id,
                )
                name = person.get("nickname", m.qq_id)
                aliases = person.get("aliases", [])
                if aliases:
                    name += f"（{'/'.join(aliases[:3])}）"
                lines.append(f"- [{name}] {m.value}")
            return "\n".join(lines)

        memories = await self._run_store_io(
            "command.memory.recall_formatted",
            self.handler.memory.recall_formatted, user_id,
        )
        return f"糖糖记得关于你的事：\n{memories}\n\n用法：\n/记忆 添加 [内容]\n/记忆 搜索 [关键词]\n/记忆 清空"

    async def _cmd_birthday(self, user_id: str, arg: str) -> str:
        """生日管理命令"""
        store = self.handler.memory.store
        arg = arg.strip()

        if arg.startswith("设置 ") or arg.startswith("设为 "):
            date_str = arg.replace("设置 ", "").replace("设为 ", "").strip()
            # 支持 "3月15日" 或 "3-15" 格式
            ok = await self._run_store_io(
                "command.birthday.set", store.set_birthday, user_id, date_str,
            )
            if ok:
                return f"🎂 糖糖记住了！你的生日是 {date_str}～到时候糖糖会在群里祝你生日快乐的！"
            return "格式不太对喵～试试「/生日 设置 3月15日」或「/生日 设置 03-15」"

        if arg == "列表" or arg == "list":
            all_bdays = await self._run_store_io(
                "command.birthday.list", store.get_all_birthdays,
            )
            if not all_bdays:
                return "糖糖还不知道任何人的生日喵…你可以用「/生日 设置 X月X日」告诉我～"
            lines = ["🎂 糖糖记得的生日："]
            for b in all_bdays:
                lines.append(f"  {b['nickname'] or b['qq_id']} — {b['birthday']}")
            return "\n".join(lines)

        if arg == "今天":
            today = await self._run_store_io(
                "command.birthday.today", store.get_today_birthdays,
            )
            if today:
                names = "、".join(b["nickname"] or b["qq_id"] for b in today)
                return f"🎂 今天过生日的有：{names}！快去送祝福吧～"
            return "今天没有人过生日喵～"

        # 无参数 → 查看自己的
        my_bday = await self._run_store_io(
            "command.birthday.get", store.get_birthday, user_id,
        )
        if my_bday:
            return f"🎂 你的生日是 {my_bday}～糖糖到时候会第一个在群里祝福你！"
        return (
            "糖糖还不知道你的生日喵…\n"
            "告诉我呀～ 比如「我生日是3月15日」或者「/生日 设置 3月15日」"
        )

    async def _cmd_status(self, user_id: str, arg: str) -> str:
        quiet_info = ""
        if self.handler._quiet_groups:
            scopes = ", ".join(sorted(self.handler._quiet_groups))
            quiet_info = f"🤫 静默模式：开启（{scopes}）\n"
        metrics_text = ""
        try:
            metrics_text = "\n\n" + self.handler.get_metrics_summary()
        except Exception:
            pass
        health_text = ""
        try:
            health_text = "\n\n" + self.handler.health.summary()
        except Exception:
            pass
        return (
            f"🍬 小糖糖状态面板\n"
            f"━━━━━━━━━━━━━━━\n"
            f"性格：{self.handler.personality.config.core[:40]}...\n"
            f"主动插话：{'✅ ON' if self.handler.active_interjection else '❌ OFF'}\n"
            f"{quiet_info}"
            f"插话饥渴度：{self.handler.interjection.thirst:.1f}\n"
            f"冷却时间：{self.handler.interjection.cooldown_seconds}s\n"
            f"━━━━━━━━━━━━━━━\n"
            f"当前时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
            f"{metrics_text}"
            f"{health_text}"
        )

    async def _cmd_help(self, user_id: str, arg: str) -> str:
        return (
            f"🍬 糖糖指令\n\n"
            f"📊 状态与信息\n"
            f"  /状态              查看运行状态            🔒\n"
            f"  /歌单              查看曲库\n"
            f"  /歌单 重载          热更新曲库              🔒\n"
            f"  /知识 重载          热更新知识库            🔒\n"
            f"  /知识 块            查看知识库分块\n\n"
            f"🎭 人格与角色\n"
            f"  /人格              查看当前人设            🔒\n"
            f"  /人格 +内容         追加/覆盖人设          🔒\n"
            f"  /人格 重载/重设      重载角色卡或恢复默认    🔒\n"
            f"  /角色 丛雨/米雪儿/糖糖  切换角色（全局，所有人受影响）  🔒\n"
            f"  /角色卡 重载/查看     热重载或查看角色卡      🔒\n"
            f"  /场景 列表/设置/重载   管理群场景绑定（列表随便看，改要🔒）\n"
            f"  /语音               开关语音模式（每条回复都发语音）\n"
            f"  /语音 off            关闭语音模式\n"
            f"  /语音 文本           直接合成文字为语音发送（≤100字）\n\n"
            f"💬 群聊控制\n"
            f"  /插话 on/off        开关主动插话          🔒\n"
            f"  /饥渴 0.0-1.0       调整插话活跃度        🔒\n"
            f"  /冷却 秒数           调整插话间隔          🔒\n"
            f"  /点赞 [more]        点赞发言的群友\n"
            f"  /唱歌 群号 歌名      遥控到群里唱歌        🔒\n"
            f"  /说 群号 话题        AI写好后发到群里  🔒\n"
            f"  /发言 群号 内容      原文直发到群里   🔒\n\n"
            f"📨 私信与传话\n"
            f"  /私信 QQ号/名字 内容  糖糖私聊某人     🔒\n"
            f"  /传话 QQ号/名字 内容  代你传话给某人   🔒\n\n"
            f"🔧 群管理\n"
            f"  /禁言 @人 分钟       禁言群成员\n"
            f"  /解禁 @人            解除禁言\n"
            f"  /踢 @人              踢出群聊\n"
            f"  /头衔 @人 文字        设置群专属头衔\n"
            f"  /全员禁言            全员禁言\n"
            f"  /解除全员禁言         解除全员禁言\n"
            f"  /黑名单 加/删/列表    管理群/私聊黑名单      🔒\n"
            f"  /机器人 加/删/列表    管理机器人账号        🔒\n\n"
            f"📝 记忆与关系\n"
            f"  /记忆               查看糖糖记得你的事\n"
            f"  /记忆 添加 内容       手动添加记忆\n"
            f"  /记忆 搜索 关键词      全文搜索记忆\n"
            f"  /亲密度              查看你的羁绊值\n"
            f"  /亲密度 QQ号          查别人的\n"
            f"  /亲密度 设置 QQ号 数值  手动设置\n"
            f"  /生日                查群友生日\n\n"
            f"⏰ 任务与工具\n"
            f"  直接说「明天8点提醒我」   创建提醒（糖糖自己记）\n"
            f"  /定时 列表              查看所有定时        🔒\n"
            f"  /定时 删除 编号          删除定时            🔒\n"
            f"  /任务                   查看待办提醒\n"
            f"  /任务 取消 编号           取消提醒\n"
            f"  /撤回                   撤回上条消息        🔒\n"
            f"  /撤销                   撤销上次设置修改    🔒\n\n"
            f"🔒 = 仅主人和群主可用\n"
            f"     （看和玩不受限：/生日 /记忆 /亲密度 /点赞 /角色 /歌单 /场景 列表 随便用）"
        )

    async def _cmd_thirst(self, user_id: str, arg: str) -> str:
        try:
            val = float(arg)
            self.handler.interjection.thirst = max(0.0, min(1.0, val))
            return f"🔧 插话饥渴度 → {self.handler.interjection.thirst:.1f}"
        except ValueError:
            return f"当前饥渴度：{self.handler.interjection.thirst:.1f}"

    async def _cmd_like_all(self, user_id: str, arg: str, group_id: str = "") -> str:
        """给群友点赞，自动风控。
        group_id 非空时：群内调用，只点赞该群的群主和管理员"""
        # 权限：主人全局可用；群内调用时群主/管理也可用
        is_group_call = bool(group_id)
        # 解析参数：/点赞 [模式]
        arg = arg.strip()
        mode = "safe"
        limit_override = 0
        if arg:
            parts = arg.split()
            for p in parts:
                if p in ("safe", "安全", "温柔"):
                    mode = "safe"
                elif p in ("more", "多点", "更多"):
                    mode = "more"
                elif p.isdigit():
                    limit_override = int(p)

        if is_group_call:
            # 群内模式：只取本群的群主和管理员
            power = self.handler._group_power.get(group_id, {})
            target_qqs = set()
            owner = power.get("owner", "")
            if owner: target_qqs.add(owner)
            for a in power.get("admins", set()):
                target_qqs.add(a)
            if not target_qqs:
                return "这个群还没有配置群主和管理员哦～"

            rows = await self._run_store_io(
                "command.like.find_admins",
                self.handler.memory.store.find_admin_qqs, target_qqs,
            )
        else:
            # 私聊模式：使用 daily_like 目标列表（与每日定时点赞一致）
            import yaml as _yaml
            try:
                with open("config.yaml", "r", encoding="utf-8") as _f:
                    _cfg = _yaml.safe_load(_f) or {}
            except Exception:
                _cfg = {}
            dl_cfg = _cfg.get("daily_like", {})
            target_list = dl_cfg.get("targets", [])
            if not target_list:
                return "还没有配置每日点赞目标哦～请在 config.yaml → daily_like → targets 中添加QQ号"
            target_qqs = set(str(t).strip() for t in target_list if str(t).strip())
            rows = await self._run_store_io(
                "command.like.find_admins",
                self.handler.memory.store.find_admin_qqs, target_qqs,
            )
        if not rows:
            return "还没有群友数据"

        eligible = []
        skipped = 0
        today = __import__('time').strftime("%Y-%m-%d")
        total_today = sum(
            v.get("count", 0) for k, v in self.handler._like_tracker.items()
            if v.get("date", "") == today
        )

        for qq_id, nickname in rows:
            if self.handler._can_like(qq_id):
                eligible.append((qq_id, nickname))
            else:
                skipped += 1

        max_people = min(len(eligible), limit_override or (15 if mode == "more" else 8))
        targets = eligible[:max_people]

        if not targets:
            return f"今天点赞配额用完了（已用{total_today}/{self.handler.LIKE_DAILY_CAP_TOTAL}），明天再来吧～"

        count = len(targets)
        self._spawn_background(
            self._do_like_safe(targets, mode), "command:like_all",
        )
        scope = f"群{group_id}的群主/管理" if is_group_call else ""
        return (
            f"✅ 开始给 {count} 人点赞{'（'+scope+'）' if scope else ''}（{'温柔' if mode == 'safe' else '加强'}模式）\n"
            f"已跳过 {skipped} 人（今日已满/冷却中）\n"
            f"今日已用: {total_today}/{self.handler.LIKE_DAILY_CAP_TOTAL}"
        )

    async def _do_like_safe(self, rows, mode: str = "safe"):
        """安全点赞：长间隔+少量+随机抖动。
        注意：不重复检查 _can_like（人选在 _cmd_like_all 中已筛选），
        只检查单人和全局日上限防止跨午夜溢出。"""
        import random as _random
        today = __import__('time').strftime("%Y-%m-%d")
        success = fail = 0

        for qq_id, nickname in rows:
            # 仅检查日上限（防跨午夜），不检查冷却（批量模式下由 sleep 控速）
            t = self.handler._like_tracker.get(qq_id, {})
            if t.get("date", "") != today:
                t["count"] = 0
                t["date"] = today
            if t.get("count", 0) >= self.handler.LIKE_DAILY_CAP_PER_USER:
                continue

            total_today = sum(
                v.get("count", 0) for k, v in self.handler._like_tracker.items()
                if v.get("date", "") == today
            )
            if total_today >= self.handler.LIKE_DAILY_CAP_TOTAL:
                break  # 全局配额满了，后面都不用点了

            try:
                result = await self.handler.napcat._call_api("send_like", {
                    "user_id": int(qq_id),
                    "times": 1,
                })
                if result.get("status") == "ok":
                    self.handler._record_like(qq_id)
                    success += 1
                    logger.info(f"  ✓ 点赞 {nickname}({qq_id})")
                else:
                    fail += 1
                    logger.warning(f"  ✗ 点赞失败 {nickname}({qq_id})")
            except Exception as e:
                fail += 1
                logger.warning(f"  ✗ 点赞异常 {nickname}({qq_id}): {e}")

            # 随机间隔：温柔模式 4-8s，加强模式 2.5-5s
            delay = _random.uniform(4, 8) if mode == "safe" else _random.uniform(2.5, 5)
            await asyncio.sleep(delay)

        today = __import__('time').strftime("%Y-%m-%d")
        total = sum(
            v.get("count", 0) for k, v in self.handler._like_tracker.items()
            if v.get("date", "") == today
        )
        logger.info(f"点赞完成！成功 {success}，失败 {fail}，今日累计 {total}/{self.handler.LIKE_DAILY_CAP_TOTAL}")

    async def _cmd_cooldown(self, user_id: str, arg: str) -> str:
        try:
            val = int(arg)
            self.handler.interjection.cooldown_seconds = max(10, min(300, val))
            return f"🔧 插话冷却 → {self.handler.interjection.cooldown_seconds}s"
        except ValueError:
            return f"当前冷却：{self.handler.interjection.cooldown_seconds}s"

    # ═══════════════════════════════════════
    # 群管理命令
    # ═══════════════════════════════════════

    def _check_admin(self, user_id: str, group_id: str) -> bool:
        """检查用户是否是主人/群主/管理"""
        if user_id == self.handler.owner_qq:
            return True
        power = self.handler._group_power.get(group_id, {})
        if user_id == power.get("owner", ""):
            return True
        return user_id in power.get("admins", set())

    def _check_group_owner(self, user_id: str, group_id: str) -> bool:
        """检查用户是否是主人/群主（需要更高权限）"""
        if user_id == self.handler.owner_qq:
            return True
        power = self.handler._group_power.get(group_id, {})
        return user_id == power.get("owner", "")

    def _parse_at_user(self, arg: str) -> str | None:
        """从参数中提取 @ 的 QQ 号。支持 CQ 码、@名字、纯数字。"""
        import re as _re
        # 1. CQ 码: [CQ:at,qq=123456]（NapCat 格式）
        m = _re.search(r'\[CQ:at,qq=(\d+)\]', arg)
        if m:
            return m.group(1)
        # 2. @名字（SnowLuma 格式）: "@浪浪 5" → 查 people 表解析
        m = _re.match(r'@(\S+)', arg.strip())
        if m:
            name = m.group(1)
            qq = self.handler._resolve_target_qq(name)
            if qq:
                return qq
        # 3. 纯数字 QQ 号
        m = _re.match(r'(\d{5,11})', arg.strip())
        if m:
            return m.group(1)
        return None

    async def _cmd_ban(self, user_id: str, arg: str, group_id: str = "") -> str:
        """禁言群成员: /禁言 @人 分钟数"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：QQ API 校验的是糖糖账号的权限，不是发指令的人——
        # 糖糖是管理员时任何群成员都能借她之手禁言。调用者鉴权必须自己查。
        if not self._check_admin(user_id, group_id):
            return "🔒 只有群管理才能用哦～"

        target = self._parse_at_user(arg)
        if not target:
            return "用法：/禁言 @某人 分钟数\n比如 /禁言 @小明 5"

        # 提取分钟数——先剔除 @名字 和 CQ 码，避免 QQ 号被当作时长
        import re as _re
        clean = _re.sub(r'\[CQ:[^\]]+\]', '', arg)  # NapCat CQ 码
        clean = _re.sub(r'@\S+', '', clean)          # SnowLuma @名字
        m = _re.search(r'(\d+)\s*(?:分钟|分|min)?', clean)
        minutes = int(m.group(1)) if m else 5
        minutes = max(1, min(minutes, 43200))  # 1分钟 ~ 30天

        ok = await self.handler.napcat.set_group_ban(group_id, target, minutes * 60)
        if ok:
            return f"🔇 已禁言 {minutes} 分钟～"
        return "禁言失败了喵…可能是权限不够？"

    async def _cmd_unban(self, user_id: str, arg: str, group_id: str = "") -> str:
        """解除禁言: /解禁 @人"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：与 /禁言 同理由——调用者鉴权必须自己查
        if not self._check_admin(user_id, group_id):
            return "🔒 只有群管理才能用哦～"

        target = self._parse_at_user(arg)
        if not target:
            return "用法：/解禁 @某人"

        ok = await self.handler.napcat.set_group_ban(group_id, target, 0)
        return "🔊 已解除禁言～" if ok else "解禁失败了喵…"

    async def _cmd_kick(self, user_id: str, arg: str, group_id: str = "") -> str:
        """踢人: /踢 @人"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：与 /禁言 同理由——调用者鉴权必须自己查
        if not self._check_admin(user_id, group_id):
            return "🔒 只有群管理才能用哦～"

        target = self._parse_at_user(arg)
        if not target:
            return "用法：/踢 @某人"

        ok = await self.handler.napcat.set_group_kick(group_id, target)
        return "已踢出群聊～" if ok else "踢人失败了喵…可能是权限不够？"

    async def _cmd_title(self, user_id: str, arg: str, group_id: str = "") -> str:
        """设置群头衔: /头衔 @人 头衔文字"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：与 /禁言 同理由——头衔用更严的群主门槛
        if not self._check_group_owner(user_id, group_id):
            return "🔒 只有群主才能用哦～"

        target = self._parse_at_user(arg)
        if not target:
            return "用法：/头衔 @某人 头衔文字"

        # 去掉 @ 部分（CQ 码 + @名字），剩余是头衔文字
        import re as _re
        title = _re.sub(r'\[CQ:[^\]]+\]', '', arg)  # NapCat CQ 码
        title = _re.sub(r'@\S+', '', title)          # SnowLuma @名字
        title = title.strip()
        if not title:
            title = ""  # 空=取消头衔

        ok = await self.handler.napcat.set_group_special_title(group_id, target, title)
        return f"✅ 已设置头衔「{title}」～" if ok else "设置头衔失败了喵…"

    async def _cmd_whole_ban(self, user_id: str, arg: str, group_id: str = "") -> str:
        """全员禁言"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：与 /禁言 同理由——全员禁言用群主门槛
        if not self._check_group_owner(user_id, group_id):
            return "🔒 只有群主才能用哦～"
        ok = await self.handler.napcat.set_group_whole_ban(group_id, True)
        return "🔇 已开启全员禁言～" if ok else "全员禁言失败了喵…"

    async def _cmd_whole_unban(self, user_id: str, arg: str, group_id: str = "") -> str:
        """解除全员禁言"""
        if not group_id:
            return "这个命令只能在群里用喵～"
        # 2026-08-15 整体审查 Critical：与 /禁言 同理由——全员禁言用群主门槛
        if not self._check_group_owner(user_id, group_id):
            return "🔒 只有群主才能用哦～"
        ok = await self.handler.napcat.set_group_whole_ban(group_id, False)
        return "🔊 已解除全员禁言～" if ok else "解除全员禁言失败了喵…"

    async def _cmd_schedule(self, user_id: str, arg: str) -> str:
        """定时任务: /定时 列表|删除 [id]|或直接描述定时任务"""
        s = self.handler.scheduler
        arg = arg.strip()

        if arg == "列表" or arg == "list":
            return s.list_tasks()

        if arg.startswith("删除 ") or arg.startswith("del "):
            tid_str = arg.replace("删除 ", "").replace("del ", "").strip()
            try:
                tid = int(tid_str)
                ok = s.remove_task(tid)
                return f"✅ 已删除任务 #{tid}" if ok else f"找不到任务 #{tid}"
            except ValueError:
                return "用法：/定时 删除 [任务编号]"

        # 2026-08-16 范式转换（教训 #24）：自然语言定时意图由 LLM 工具解析——
        # /定时 只做管理操作；创建提醒直接说「明天8点提醒我」即可
        return ("创建提醒不用打命令啦——直接说「5分钟后提醒我喝水」「明天8点叫我」"
                "这类话，糖糖会自己记住的喵～\n管理已有提醒：/定时 列表 / /定时 删除 [编号]")

    async def _cmd_sing(self, user_id: str, arg: str, *, event_key: str = "") -> str:
        """遥控糖糖在群里唱歌 /唱歌 群号 歌名"""
        import re
        match = re.match(r"(\d+)\s+(.+)", arg)
        if not match:
            return "用法：/唱歌 [群号] [歌名]\n歌名可以是完整歌名或关键词"
        group_id, query = match.groups()
        song = self.handler.songs.search(query.strip())
        if not song:
            available = "、".join(self.handler.songs.list_songs()[:10])
            return f"曲库里没找到「{query}」...\n有的歌：{available}"
        # 生产命令必有平台 event_key；同一入站事件的重试必须复用 sing child
        # receipt，不同命令事件即使歌名相同也应生成新动作。直接调用/旧测试没有
        # 入站证据时，只生成本次临时 source，不冒充可跨调用幂等。
        action_source_id = (
            f"command_sing:{event_key}" if event_key
            else f"command_sing:{uuid.uuid4().hex}"
        )
        self._spawn_background(
            self._sing_in_group(
                group_id, song, action_source_id=action_source_id,
            ), "command:sing",
        )
        return f"🎤 糖糖要在群{group_id}唱《{song['title']}》啦~"

    async def _sing_in_group(self, group_id: str, song: dict, *, action_source_id: str):
        """后台让糖糖在群里唱一首歌（主人遥控 /唱歌 命令）"""
        try:
            sing_prompt = self.handler.songs.build_sing_prompt(song)
            system_prompt = self.handler.personality.build_system_prompt(
                relationship=Relationship.FAMILIAR,
                minimal=True,  # 2026-08-17：遥控唱歌只背身份+风格锚
            )
            reply = await self.handler._call_llm(
                system_prompt=system_prompt + "\n\n" + sing_prompt,
                user_message="主人让你给大家唱首歌，认真唱哦～。唱《" + song.get("title", "") + "》"
            )
            # 必须保留清洗前的 marker：ActionPlan 在文字确认后从它冻结
            # sing child，不能把 ReplyPipeline 清掉 [SING:] 后再解析。
            sing_marker_reply = reply
            # 先发文字（清洗掉 [SING] 标记），再发音频
            cleaned = self.handler.reply.clean(reply)
            if cleaned:
                result = await self.handler.napcat.send_group_message(group_id, cleaned)
                if not is_send_confirmed(result):
                    logger.warning(
                        f"唱歌文字发送未确认 ({send_delivery_state(result)})，不追加音频"
                    )
                    return
            await self.handler._send_singing_actions(
                "group", group_id, song, sing_marker_reply,
                action_source_id=action_source_id,
            )
        except Exception as e:
            logger.error(f"唱歌失败：{e}")

    async def _cmd_songlist(self, user_id: str, arg: str) -> str:
        """查看曲库或重载"""
        if arg.strip() == "重载":
            await run_bounded_blocking(
                "songs.reload",
                self.handler.songs.reload,
                logger=logger,
                log_prefix="🎤 曲库重载较慢",
            )
            return f"✅ 曲库已重载！共 {len(self.handler.songs.songs)} 首歌"
        songs = self.handler.songs.list_songs()
        if not songs:
            return "曲库还是空的...把歌词.txt放进 songs/ 文件夹就能唱啦"
        return f"🎤 糖糖会唱的歌（{len(songs)}首）：\n" + "\n".join(f"  🎵 {s}" for s in songs)

    async def _cmd_knowledge(self, user_id: str, arg: str) -> str:
        """查看知识库或重载"""
        if arg.strip() == "重载":
            await run_bounded_blocking(
                "knowledge.reload",
                self.handler.knowledge.reload,
                logger=logger,
                log_prefix="📚 知识库重载较慢",
            )
            return f"✅ 知识库已重载！{self.handler.knowledge._file_count} 个文件 → {len(self.handler.knowledge.chunks)} 个块"
        if arg.strip() == "块":
            lines = [f"📚 知识库: {self.handler.knowledge._file_count} 文件 → {len(self.handler.knowledge.chunks)} 块"]
            for c in self.handler.knowledge.chunks:
                lines.append(f"  [{c.source}] {c.label} ({c.char_count}字)")
            return "\n".join(lines)
        return (
            "📚 知识库指令：\n"
            "/知识 重载 — 重新加载所有 .md 文件\n"
            "/知识 块 — 查看所有分块信息"
        )


# ============================================================

