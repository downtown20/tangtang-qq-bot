#!python3.10
# ⚠️ 上面这行是 py launcher 的 shebang——只能有 #!python3.10，不能带行内注释（launcher 会把 "#" 当文件打开报错）。
# 用途：机器上有 3.14 时 py 默认指向新版本，而本体依赖(sentence-transformers/BGE)只在 3.10 环境。
"""
小糖糖 - QQ群智能体主入口
温柔又爱撩人的小女生，想贴近每一个人的心

启动方式：
    python main.py

前置要求：
    1. 安装 SnowLuma 并登录机器人QQ号
    2. 运行 tools/配置SnowLuma.py 自动配置 OneBot（反向 WS + HTTP）
    3. 反向 WebSocket: ws://127.0.0.1:3001，HTTP: http://127.0.0.1:3000
    4. 修改 config.yaml 中的配置
    5. pip install -r requirements.txt
"""

import sys
import io
import warnings
import os as _os

# 静默无意义的启动噪声
warnings.filterwarnings('ignore', category=FutureWarning, module='transformers')
_os.environ['JIEBA_LOG_LEVEL'] = 'ERROR'  # jieba "Building prefix dict" 日志
_os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'  # huggingface 下载进度条

# 强制 UTF-8 输出（Windows 中文版默认 GBK，不认 emoji）
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import atexit
import logging
import os
import re
import sys
import signal
import socket

import yaml
from dotenv import load_dotenv

from onebot.ws_client import NapCatClient
from agent.handler import MessageHandler
from agent.telemetry import build_log_handlers

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | boot=%(boot_id)s cid=%(correlation_id)s | "
        "%(name)s | %(levelname)s | %(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S%z",
    handlers=build_log_handlers("tangtang.log", sys.stdout),
)
logger = logging.getLogger("糖糖")

BANNER = r"""
  . . . . . . . . . . . . . . . . .
  .    小 糖 糖  TangTang          .
  .  温柔又爱撩人的小女生          .
  .  想贴近每一个人的心            .
  . . . . . . . . . . . . . . . . .
"""


def _resolve_env_vars(obj):
    """递归遍历 dict/list，将 '${VAR_NAME}' 字符串替换为环境变量值。
    不匹配 ${} 格式的字符串原样保留。"""
    _ENV_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    elif isinstance(obj, str):
        m = _ENV_RE.match(obj)
        if m:
            return os.environ.get(m.group(1), obj)
        return obj
    else:
        return obj


class LogBufferHandler(logging.Handler):
    """将日志推送到 handler 的 _log_buffer 供 Web API 读取"""

    def __init__(self, tangtang_app):
        super().__init__()
        self._app = tangtang_app

    def emit(self, record):
        try:
            handler = self._app.handler
            if handler and hasattr(handler, '_log_buffer'):
                from datetime import datetime
                ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
                handler._log_buffer.append({
                    "timestamp": ts,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                })
        except Exception as e:
            import sys
            print(f"[LogBufferHandler ERROR] {e}", file=sys.stderr)


def _assert_ws_port_available(host: str, port: int) -> None:
    """在构造 Handler 前拒绝已经被实例占用的反向 WS 端口。

    手动重启如果忘了先关闭旧实例，不能让第二个实例先加载模型、启动后台
    任务再在 ``start_server`` 阶段失败；这里只做 TCP 探针，不连接协议、更
    不终止占用端口的进程。连接探针不会完成 WebSocket 握手，因此不会触发
    SnowLuma 回调或业务副作用。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        if probe.connect_ex((host, int(port))) == 0:
            raise RuntimeError(
                f"反向 WS 端口 {host}:{port} 已被占用；请先停止旧的小糖糖实例再重启"
            )
    finally:
        probe.close()


class TangTang:
    """小糖糖主程序"""

    def __init__(self, config_path: str = "config.yaml"):
        # 加载 .env 文件中的密钥
        load_dotenv()
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = _resolve_env_vars(yaml.safe_load(f))
        logger.info(f"配置已加载：{config_path}")

        behavior = self.config.get("behavior", {})
        tasks_cfg = self.config.get("tasks", {})
        nc = self.config["napcat"]
        self.napcat = NapCatClient(
            ws_host="127.0.0.1",
            ws_port=3001,
            http_url=nc.get("http_url", "http://127.0.0.1:3000"),
            access_token=nc.get("access_token", ""),
            self_id=str(self.config["bot"]["qq_id"]),
            testing_mode=behavior.get("testing_mode", False),
        )
        # 端口冲突必须在 MessageHandler 构造前 fail-closed，避免第二个手动
        # 启动实例产生模型加载、点赞/任务循环等不可逆副作用。
        _assert_ws_port_available(self.napcat.ws_host, self.napcat.ws_port)
        text_claims_enabled = bool(
            tasks_cfg.get("text_action_outbox_enabled", True)
        )
        media_claims_enabled = bool(
            tasks_cfg.get("media_action_outbox_enabled", False)
        )
        outbox_claims_enabled = bool(
            tasks_cfg.get("send_outbox_claims_enabled", True)
        )
        if not outbox_claims_enabled and text_claims_enabled:
            # 禁止在停掉唯一发送 owner 后继续制造 linked pending 行。
            text_claims_enabled = False
            logger.warning(
                "⚠️ send_outbox_claims_enabled=false：已联动暂停纯文本任务领取"
            )
        if not outbox_claims_enabled and media_claims_enabled:
            media_claims_enabled = False
            logger.warning(
                "⚠️ send_outbox_claims_enabled=false：已联动暂停统一媒体任务领取"
            )
        self.napcat._task_text_outbox_enabled = text_claims_enabled
        self.napcat._task_media_action_outbox_enabled = media_claims_enabled
        self.napcat._outbox_claims_enabled = outbox_claims_enabled
        if not bool(tasks_cfg.get("manual_retry_generation_enabled", False)):
            logger.warning(
                "⚠️ manual_retry_generation_enabled=false：确定失败任务的人工重试已关闭"
            )
        if not text_claims_enabled:
            logger.warning(
                "⚠️ 纯文本提醒领取已暂停：到期任务保留 pending，不生成或发送"
            )
        if not media_claims_enabled:
            logger.warning(
                "⚠️ 统一媒体提醒领取已暂停：到期贴图/语音保留 pending，不合成或发送"
            )
        if not outbox_claims_enabled:
            logger.warning(
                "⚠️ 发送 outbox 领取已暂停：现有 pending 行不会发送"
            )

        self.handler = MessageHandler(self.config, self.napcat)
        # 发送层只把明确可重试的网络/网关失败写入持久 outbox；普通回复
        # 仍由调用方同步确认，不在离线恢复后盲目重放。
        self.napcat.bind_outbox_store(self.handler.memory.store)
        self.napcat.bind_metrics(self.handler.metrics)

        self.napcat.on_group_message = self.handler.handle_group_message
        self.napcat.on_private_message = self.handler.handle_private_message
        self.napcat.on_poke = self.handler.handle_poke
        self.napcat.on_group_increase = self.handler.handle_group_increase
        self.napcat.on_friend_request = self.handler.handle_friend_request
        self.napcat.on_group_invite = self.handler.handle_group_invite
        self.napcat.on_connected = self._on_connected
        self.napcat.on_disconnected = self._on_disconnected
        self.napcat.on_qq_offline = self._on_qq_offline
        self.napcat.on_qq_online = self._on_qq_online

        # 日志缓冲
        log_buffer = LogBufferHandler(self)
        log_buffer.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_buffer)

        # atexit 兜底清理——Windows SIGTERM 不可用时的最后防线
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self):
        """atexit 兜底：只清理本实例创建的 GPT-SoVITS 子进程。

        不能按 9880 端口盲杀：启动失败的第二个 bot 实例可能从未拥有
        该服务，却会在 atexit 时误伤仍由第一个实例使用的 TTS 进程。
        """
        try:
            service_mgr = getattr(getattr(self, "handler", None), "_service_mgr", None)
            proc = getattr(service_mgr, "_gpt_sovits_proc", None)
            pid = getattr(proc, "pid", None)
            if pid and getattr(proc, "returncode", None) is None:
                # asyncio.subprocess.Process 已经绑定到本实例；直接杀掉该
                # 对象可避免按 PID 查找时误伤后来复用同一 PID 的进程。
                proc.kill()
        except Exception:
            pass

    async def _on_connected(self):
        # 防止 WebSocket 重连时重复触发
        if getattr(self, '_connected_already', False):
            return
        self._connected_already = True

        logger.info("小糖糖已上线！等待消息中...")
        # WebSocket 连接只证明传输层可达；QQ 登录态必须由 lifecycle/心跳确认。
        # 发送路径在确认前 fail-closed，避免上线竞态下的“假成功”。
        # 刷新群信息（fire-and-forget，不阻塞上线）
        asyncio.create_task(self.handler.refresh_group_info())
        # 🎤 启动 CosyVoice 3 TTS 服务（GPU 推理，Python 3.10 子进程）
        if self.config.get("voice", {}).get("provider") == "cosyvoice":
            from agent.cosy_voice import get_cosy_engine
            engine = get_cosy_engine()
            if engine.available and not engine.start_server():
                logger.warning("⚠ CosyVoice 3 启动失败，语音将不可用")
        # 启动图片分享
        if self.handler.image_share:
            self.handler.image_share.start()
        # 启动生日检查
        if self.handler.birthday_greeter:
            self.handler.birthday_greeter.start()
        # 启动定时任务调度器
        if self.handler.scheduler:
            self.handler.scheduler.start()
        # 启动每日播报
        if self.handler.daily_report:
            self.handler.daily_report.start()
        # 陈旧记忆清理
        deleted = self.handler.memory.cleanup_stale_memories(days=90)
        if deleted:
            logger.info(f"🧹 陈旧记忆清理: {deleted} 条已删除")

        # 🎤 预启动 DiffSinger MiniEngine（避免首次点歌等待，不阻塞启动流程）
        try:
            from agent.diff_singer import get_diff_singer_engine
            get_diff_singer_engine().is_available()  # 快速检查，不等待
            # 把慢启动扔到后台
            asyncio.create_task(get_diff_singer_engine().ensure_server_async())
        except Exception:
            pass

    async def _notify_owner_updates(self):
        """启动时读取更新日志，私信主人最新更新摘要"""
        try:
            import os
            update_file = "knowledge/更新日志.md"
            if not os.path.exists(update_file):
                return
            with open(update_file, "r", encoding="utf-8") as f:
                content = f.read()
            # 提取第一个 ## 标题到下一个 ## 之间的内容（最新版本）
            import re
            match = re.search(r'##\s+(.+?)\n\n(.+?)(?=\n##\s|\Z)', content, re.DOTALL)
            if not match:
                return
            version_title = match.group(1).strip()
            version_body = match.group(2).strip()

            # 用 LLM 生成一条自然的口语化更新通知
            owner_qq = self.config["bot"]["owner_qq"]
            summary_lines = []
            for line in version_body.split("\n"):
                line = line.strip()
                if line.startswith("###"):
                    summary_lines.append(f"\n{line.replace('#', '').strip()}")
                elif line.startswith("-"):
                    summary_lines.append(line)

            # 过滤空行和纯占位符
            summary_lines = [l for l in summary_lines if l.strip() and l.strip() != "-"]
            summary = "\n".join(summary_lines[:15])
            if not summary:
                return

            msg = (
                f"🍬 糖糖启动完毕！\n\n"
                f"📢 {version_title} 更新已生效：\n{summary}\n\n"
                f"说「糖糖你有什么新功能」让我详细给你介绍~"
            )
            await self.napcat.send_private_message(owner_qq, msg)
            logger.info("📢 已向主人发送更新公告")
        except Exception as e:
            logger.warning(f"更新公告发送失败: {e}")

    async def _on_disconnected(self):
        logger.warning("小糖糖暂时下线...")
        # 记录离线时间（用于下次启动补读）
        if self.handler.catch_up:
            self.handler.catch_up.record_offline()

    async def _on_qq_offline(self):
        """QQ 掉线但 SnowLuma 进程仍在（WebSocket 未断）"""
        logger.warning("🔴 QQ 账号已掉线！请检查是否被挤下线或网络异常")

    async def _on_qq_online(self):
        """QQ 恢复上线"""
        logger.info("🟢 QQ 账号已恢复在线")
        # QQ 依赖动作只在首次确认登录后执行；WebSocket 建连不能代替认证。
        if not getattr(self, "_qq_ready_initialized", False):
            self._qq_ready_initialized = True
            await self.handler.refresh_group_info()
            await self._notify_owner_updates()
            # 补读必须等 QQ 真正登录，不能在仅 WebSocket 建连时开始倒计时。
            if self.handler.catch_up:
                self.handler.catch_up.record_online()
                state = self.handler.catch_up._load_state()
                last_offline = state.get("last_offline", "")
                if last_offline:
                    try:
                        from datetime import datetime as _dt
                        offline_t = _dt.strptime(last_offline, "%Y-%m-%d %H:%M:%S")
                        gap_hours = (_dt.now() - offline_t).total_seconds() / 3600
                        if gap_hours > 6:
                            from datetime import timedelta as _td
                            fallback = (_dt.now() - _td(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
                            self.handler.catch_up._save_state(last_offline=fallback)
                            logger.info(
                                f"📬 last_offline 过期({gap_hours:.0f}小时)，"
                                "用当前时间兜底"
                            )
                    except Exception:
                        pass
                asyncio.create_task(
                    self._catch_up_missed_messages(), name="catch_up_missed_messages",
                )
        # 每次从离线切回在线都尝试消化网络失败任务；
        # uncertain/dead 状态不会在这里重放。
        # 意见征集的 queued 邀请也只能在 QQ lifecycle online 后恢复。
        # OpinionManager 构造早于 NapCat ready，不能在 __init__ 中抢跑。
        opinion = getattr(self.handler, "opinion", None)
        if opinion and callable(getattr(opinion, "recover_queued_invitations", None)):
            self.handler._safe_task(
                opinion.recover_queued_invitations(),
                name="opinion_invite_recovery_ready",
            )
        asyncio.create_task(self.napcat.process_send_outbox(), name="send_outbox_replay")

    async def _catch_up_missed_messages(self):
        """后台补读离线消息。等群列表就绪后再执行。"""
        try:
            # 等待 refresh_group_info() 完成（最多等 90 秒——QQ 上线可能很慢）
            for i in range(30):
                if self.handler._allowed_groups:
                    break
                await asyncio.sleep(3)
            else:
                logger.warning("📬 等待群列表超时(90s)，用 config.yaml 兜底")
                config_groups = set(str(g) for g in (self.config.get("groups") or {}).keys())
                blacklist = getattr(self.handler, '_group_blacklist', set())
                if config_groups:
                    self.handler._allowed_groups = config_groups - blacklist

            if self.handler._allowed_groups:
                summaries = await self.handler.catch_up.catch_up_all_groups()
                if summaries:
                    n = len(summaries)
                    total = sum(s.message_count for s in summaries.values())
                    logger.info(f"📬 离线消息补读完成: {n}个群, 共{total}条消息")

                    total_mentions = sum(len(s.missed_mentions) for s in summaries.values())
                    if total_mentions > 0:
                        logger.info(f"📬 离线期间有 {total_mentions} 条点名，开始补回复…")
                        await self.handler.catch_up.reply_to_missed_mentions()
            else:
                logger.warning("📬 跳过补读：无可用群列表")
        except Exception as e:
            logger.warning(f"📬 离线消息补读失败: {e}")

    async def start(self):
        print(BANNER)
        logger.info("小糖糖正在苏醒...")
        logger.info(f"机器人QQ：{self.config['bot']['qq_id']}")
        logger.info(f"主人QQ：{self.config['bot']['owner_qq']}")
        logger.info(f"LLM：{self.config['llm']['provider']}/{self.config['llm']['model']}")
        logger.info(f"主动插话：{'开' if self.config['behavior']['active_interjection'] else '关'}")

        # 启动 WebSocket 服务器
        await self.napcat.start_server()

        # 🆕 启动外部服务（使用本地语音时自动启动 TTS）
        await self.handler.start_services()

        # 保持运行直到被中断
        while self.napcat._running:
            await asyncio.sleep(1)

    async def stop(self):
        logger.info("小糖糖去睡觉了...")
        # 🆕 关闭外部服务
        await self.handler.stop_services()
        # 记录离线时间（用于下次启动补读）
        if self.handler.catch_up:
            try:
                self.handler.catch_up.record_offline()
            except Exception:
                pass
        self.napcat._running = False
        await self.napcat.close()
        await self.handler.llm.aclose()
        # 2026-08-15 接线审计：close_cosy_engine 此前零调用（引擎从不释放）。
        # 进程内 CosyVoice 引擎单例在优雅退出时关闭——失败不影响退出流程。
        try:
            from agent.cosy_voice import close_cosy_engine
            await close_cosy_engine()
        except Exception as e:
            logger.warning(f"CosyVoice 引擎关闭失败（忽略）: {e}")


async def main():
    tangtang = TangTang()

    # SIGTERM（控制台 kill）→ 优雅退出
    loop = asyncio.get_running_loop()
    if hasattr(signal, 'SIGTERM'):
        try:
            loop.add_signal_handler(
                signal.SIGTERM,
                lambda: asyncio.create_task(tangtang.stop())
            )
        except NotImplementedError:
            pass  # Windows 部分版本不支持 add_signal_handler

    try:
        await tangtang.start()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C")
        await tangtang.stop()
    except Exception as e:
        logger.error(f"小糖糖崩溃了：{e}")
        # 启动阶段也可能已创建任务提醒、LLM 客户端或外部服务；
        # 绑定端口等早期失败不能只依赖 asyncio.run 的粗粒度取消。
        try:
            await tangtang.stop()
        except Exception as cleanup_error:
            logger.warning("启动失败清理未完全成功: %s", cleanup_error)
        raise


if __name__ == "__main__":
    asyncio.run(main())
