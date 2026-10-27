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
#
# ⚠ 2026-09-20：这里原来是 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`
#   ——**替换**而不是就地改编码。后果不在生产（生产里 stdout 就是真控制台），
#   而在任何"把 main.py 当模块 import"的场合：pytest 会把 sys.stdout 换成它自己的
#   捕获对象，我们再包一层之后，pytest 收工时关掉它那份、我们这份还攥着同一个
#   buffer → `ValueError: I/O operation on closed file`。
#   这也是为什么此前**没有任何测试 import 过 main**——一个 import 副作用把整个
#   模块挡在了单测之外，而它恰恰是最该被测的那一层（配置加载、密钥解析都在这儿）。
#
#   改用 `reconfigure`（就地改编码，不换对象）——与 start.py / 同步记忆.py /
#   体检.py / 安装糖糖.py 里的同款守卫一致。它们一直是对的，只有这里特殊。
for _stream in (sys.stdout, sys.stderr):
    if _stream is None or not hasattr(_stream, "reconfigure"):
        continue
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # 老解释器/非常规对象：退回包装（此时它多半不是 pytest 的捕获流）
        if sys.platform == "win32" and hasattr(_stream, "buffer"):
            try:
                if _stream is sys.stdout:
                    sys.stdout = io.TextIOWrapper(_stream.buffer, encoding="utf-8",
                                                  errors="replace")
                else:
                    sys.stderr = io.TextIOWrapper(_stream.buffer, encoding="utf-8",
                                                  errors="replace")
            except Exception:
                pass

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
from agent.paths import log_path, sweep_stale_runtime_files  # 日志落盘位置单一事实源（2026-10-15 T55-F）
from agent.secret_refs import (collapse_unresolved_secrets, literal_secret_fields,
                               normalize_secret_fields, unfilled_secrets)

# 让本进程与子进程认得项目自带的 ffmpeg（共享库版装在 依赖/ffmpeg/bin）。
# 为什么必须有这一句：把目录写进系统 PATH 只对**之后新启动**的进程生效，而 bot
# 常由一个早就启动好的控制台进程拉起——控制台的环境块是旧的，bot 继承下来就
# 还是找不到 ffmpeg（语音识别转码要用它），失败得很隐蔽。项目自带那份就在项目
# 目录里，没道理让「语音能不能听懂」取决于用户有没有重启过控制台。
try:
    from agent.paths import ensure_ffmpeg_on_path

    ensure_ffmpeg_on_path()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | boot=%(boot_id)s cid=%(correlation_id)s | "
        "%(name)s | %(levelname)s | %(message)s"
    ),
    datefmt="%Y-%m-%d %H:%M:%S%z",
    handlers=build_log_handlers(str(log_path()), sys.stdout),
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
                # ⚠ 2026-09-20：原话只说「请先停止旧的小糖糖实例再重启」——归因**漏了一半**，
                #   而漏掉的那一半恰好是每个新用户都会撞上的：
                #   SnowLuma 的出厂配置（config-D1dwzoRS.js: makeDefaultOneBotConfig）
                #   自带一个「WS 服务端」节点 ws-default，host 127.0.0.1、port 3001，
                #   也就是糖糖要绑的那个端口。用户照提示去找"旧糖糖进程"永远找不到，
                #   因为他根本没启动过第二次。
                f"反向 WS 端口 {host}:{port} 已被占用。两种可能：\n"
                f"  ① 已经有一个糖糖实例在跑 → 先停掉它。\n"
                f"  ② SnowLuma 出厂配置里的「WS 服务端」节点（ws-default）默认就占着这个端口\n"
                f"     → 打开 http://127.0.0.1:5099 → 节点配置 → 选中账号 →「WS 服务端」\n"
                f"       → 删掉 ws-default（或把它改成别的端口）→ 保存。\n"
                f"  分不清是哪种？点控制台的「🔍 连接自检」，它会告诉你是谁占着。"
            )
    finally:
        probe.close()


def _report_credentials(missing: list, config: dict) -> None:
    """把「密钥没填好」翻译成一句能照着做的话（2026-09-20 加）。

    为什么需要：在这之前，密钥缺失的唯一症状是 LLM 接口返回 401，
    日志里只有一行 `HTTP Request: POST ... "HTTP/1.1 401"`——
    它既不说是哪个字段，也不说该去哪儿填。最坏的一种（出厂模板写的是
    中文字面量）连 401 都没有：那串中文被当成 Key 发了出去，
    而界面上一切显示「已保存」。

    ⚠ **只报真的挡住功能的那一条。** 四个密钥字段里有三个本来就允许为空：
       · `llm.seductive_api_key` —— 设计上就是可选的（不填复用主 Key）
       · `llm.vision.api_key`    —— 识图选「本地 MiniCPM-V」根本不需要 Key
       · `napcat.access_token`   —— SnowLuma 两边都不设 Token 才是默认形态
    对它们无差别地打印「未填」，就又把日志变回了噪音——
    而「分不清哪些提示与自己有关」正是主人这次反馈的第二个问题。
    所以：可选项按**配置里的开关**决定要不要提，没开的功能一个字都不说。
    """
    by_path = {m[0]: m for m in missing}

    # ① 阻塞项：没有它糖糖一句话也说不出
    hit = by_path.get("llm.api_key")
    if hit:
        dotted, env_name, reason = hit
        logger.error(
            f"❌ LLM API Key 没填好（{reason}），糖糖无法回复任何消息。三种填法任选其一：\n"
            f"   ① 控制台 → 设置 →「🔑 密钥管理」→ 填「LLM API Key」→ 保存（推荐）\n"
            f"      （它写进 .env 的 {env_name}=，并把 config.yaml 的 {dotted} "
            f"保持成 ${{{env_name}}} 引用）\n"
            f"   ② 直接编辑 .env，加一行：{env_name}=你的Key\n"
            f"   ③ 不建 .env，直接把真 Key 写进 config.yaml 的 {dotted}"
        )

    # ② 条件项：只有用户明确开了对应功能、且选的方式确实需要 Key，才提
    vision = (config.get("llm", {}) or {}).get("vision") or {}
    # 2026-10-14 T42：判据与 handler.py:985 / 糖糖控制台_qt.py:1027 **对齐**。
    # 旧判据是「provider 默认 local 且 != local」——T1 把默认值改成 main 之后，
    # **选 main 会被误判成云端**：一开识图就弹「识图选了云端…改回 local」，
    # 而 main 恰恰是推荐值、且不需要任何 Key。
    # 这是 T1 改动漏掉的**第三处**：同一条判据散在三个文件里，改了两个漏了一个。
    if (vision.get("enabled") and str(vision.get("provider", "main")) == "api"
            and "llm.vision.api_key" in by_path):
        dotted, env_name, reason = by_path["llm.vision.api_key"]
        logger.warning(
            f"🔑 识图选了云端千问，但 {reason}。云端识图会一直失败。\n"
            f"   → 控制台 →「🔑 密钥管理」填「识图 API Key」；"
            f"或把「识图方式」改成 main（用主模型看图，不用 Key，推荐）"
        )

    # `llm.seductive_api_key` / `napcat.access_token` 为空的正常情形：
    # 一个字都不说。前者有复用主 Key 的降级路径，后者由「连接自检」按
    # 三边比对给出精确结论——在这里重复一遍只会增加噪音。


class TangTang:
    """小糖糖主程序"""

    def __init__(self, config_path: str = "config.yaml"):
        # 加载 .env 文件中的密钥
        load_dotenv()
        with open(config_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f)
        # 老配置迁移（2026-09-20）：出厂模板曾把密钥字段写成中文字面量，
        # 那串东西会让 `${VAR}` 机制整个失效——.env 从被创建起就没人读过。
        # 在内存里纠正即可生效，**不写盘**：写盘交给控制台保存那一次，
        # 免得两个进程抢同一个 config.yaml。
        migrated = normalize_secret_fields(raw_cfg)
        if migrated:
            logger.info("🔑 已把 %d 个密钥字段纠正为 ${VAR} 引用：%s（打开一次控制台"
                        "并保存，就会写回 config.yaml）", len(migrated), "、".join(migrated))
        # 2026-09-23 第三次 401 事故：config.yaml 里写死的**旧**明文 + .env 里又有
        # 同名变量 = 两者必然对不上，而它躲过了上面那道 normalize（那个判据是
        # 「值 == 当前 env 值」，.env 一换就不再成立）。只报警，不自动改写。
        literals = literal_secret_fields(raw_cfg)
        if literals:
            logger.warning(
                "🔑 有 %d 个密钥字段是**写死的明文**，而 .env 里又有同名变量——"
                "两者很可能对不上：%s。\n"
                "   这一格不会跟着 .env 更新，最典型的后果是 HTTP 接口一直 401"
                "（收得到消息、发不出去）。\n"
                "   → 打开一次控制台并保存（它会把该格改回 ${VAR} 引用），"
                "或手动把值改成 ${变量名}。\n"
                "   （如果你确实想写死在这里，忽略这条。）",
                len(literals),
                "、".join(f"{dotted} ← .env 有 {env_name}"
                          for dotted, env_name in literals),
            )
        self.config = _resolve_env_vars(raw_cfg)
        logger.info(f"配置已加载：{config_path}")
        # 密钥体检的顺序有讲究：必须在**解析之后、清空之前**判定——
        # 那一刻才分得清「变量名写对了但 .env 里没有它」和「这一项本来就是空的」。
        missing_secrets = unfilled_secrets(self.config)
        collapse_unresolved_secrets(self.config)   # 绝不让 `${VAR}` 漏进 Authorization 头
        _report_credentials(missing_secrets, self.config)

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

        # 🏠 会客厅（桌面端真身会话接口）——在 start() 里拉起，这里先占位：
        # 启动阶段失败（端口被占等）时必须能安全地当它不存在。
        self.console = None

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

    def _start_console(self):
        """起「会客厅」本地接口（桌面端的糖糖真身会话，2026-10-24）。

        为什么在 bot 进程内：会客厅要让桌面端看到**同一个糖糖**——同一份记忆、
        同一套驱动力、同一个关系场。独立进程会分裂状态文件与短期记忆窗口。
        代价是糖糖没启动时桌面端只能显示引导态。

        失败（端口被占 / 导入异常）一律降级为「没有会客厅」，绝不拖垮 bot：
        QQ 聊天是主干，观察窗是附加物。
        """
        try:
            from agent.console_api import ConsoleApi

            cfg = (self.config.get("console") or {})
            self.console = ConsoleApi(
                self.handler,
                asyncio.get_running_loop(),
                host=str(cfg.get("host") or "127.0.0.1"),
                port=int(cfg.get("port") or 8765),
                guest_qq=str(cfg.get("guest_qq") or "") or None,
            )
            if not self.console.start():
                self.console = None
        except Exception:
            logger.warning("🏠 会客厅启动失败（桌面端将显示未连接）", exc_info=True)
            self.console = None

    async def start(self):
        print(BANNER)
        logger.info("小糖糖正在苏醒...")
        logger.info(f"机器人QQ：{self.config['bot']['qq_id']}")
        logger.info(f"主人QQ：{self.config['bot']['owner_qq']}")
        logger.info(f"LLM：{self.config['llm']['provider']}/{self.config['llm']['model']}")
        logger.info(f"主动插话：{'开' if self.config['behavior']['active_interjection'] else '关'}")
        logger.info(f"语音：{self.config.get('voice', {}).get('provider', '(未配置)')}"
                    f"（{'开' if self.config.get('voice', {}).get('enabled', True) else '关'}）")

        # ⚠ 2026-10-15：testing_mode 是**破坏性**开关——打开后非主人的消息被静默丢弃
        # （群聊在 handle_group_message 里拦、私聊同样）。教训 #11 的原事故就是它
        # 留在生产配置里、主人一周没发现，而**当时同样没有任何启动警告**。
        # 教训 #11 的结论是「破坏性 boolean 配置开关必须启动时显式警告」，
        # 这条警告就是那句话的落地。
        if bool(self.config.get("behavior", {}).get("testing_mode", False)):
            logger.warning(
                "⚠️⚠️ testing_mode = ON：**只有主人**的消息会被处理，"
                "其他所有人的消息都会被静默丢弃（群里看不到糖糖回话）。"
                "这不是生产配置——要正常上线，请把 config.yaml 的 "
                "behavior.testing_mode 改成 false，然后重启。"
            )

        # 启动 WebSocket 服务器
        await self.napcat.start_server()

        # 🆕 启动外部服务（使用本地语音时自动启动 TTS）
        await self.handler.start_services()

        # 🏠 会客厅：桌面端的「糖糖真身」会话接口（2026-10-24）
        self._start_console()

        # 保持运行直到被中断
        while self.napcat._running:
            await asyncio.sleep(1)

    async def stop(self):
        logger.info("小糖糖去睡觉了...")
        # 🏠 先关会客厅：停掉 HTTP 线程，避免退出过程中还有请求投进已取消的 loop
        if getattr(self, "console", None) is not None:
            try:
                self.console.stop()
            except Exception:
                pass
            self.console = None
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


async def main():
    # 启动收尾：清掉第三方工具丢在项目根的临时 pcm。
    # `pilk.silk_to_wav` 用 `tempfile.mktemp(suffix='.pcm')` 建中间文件后**从不删除**
    # （2026-10-27 实测根目录堆了 10 个 / 55MB；`*.pcm` 被 .gitignore 挡着，
    # `git status` 看不出来，只有 dir 能看见）。放在最前面：先扫干净再干活。
    try:
        swept = sweep_stale_runtime_files()
        if swept:
            logger.info("清掉 %d 个遗留临时音频文件（pilk 解码残留）", len(swept))
    except Exception as exc:  # 收尾工作不该影响启动
        logger.debug("清理遗留临时文件失败：%s", exc)

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
