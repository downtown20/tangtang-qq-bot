"""会客厅 —— 糖糖本体的本地会话接口（2026-10-24）

## 这是什么

桌面端原来的「模型测试」对话框是**裸调 /chat/completions**：没有人格、没有记忆、
没有驱动力、没有存档。它测的是「这个模型通不通」，不是「糖糖是谁」。

会客厅把桌面端接到**糖糖本体**上：桌面端发一句话，走的是和 QQ 私聊**同一条**
生产路径（`MessageHandler.handle_private_message`），所以她真的会检索记忆、
真的会消耗驱动力、真的会改写关系场——只是最后那一步「发到 QQ」被截获下来，
变成桌面端的一条消息。

## 为什么在 bot 进程内

三个方案都试想过（见 2026-10-24 会话记录）：

1. 独立进程 + 假 NapCat —— 会同时有两个糖糖：`状态/.tangtang_self.json`、
   `驱动力`、`memory.db` 的短期窗口都会分裂，两个进程互相覆盖。
2. 桌面端 bridge 内嵌 —— 同上，而且 bridge 要背一遍 BGE/知识库的初始化。
3. **bot 进程内开本地 HTTP 口** ← 采用

只有第 3 种能保证「世界上只有一个糖糖」。代价是：糖糖没启动时桌面端看到的是
引导态（这是诚实的代价，不是缺陷）。

## 线程模型

HTTP 服务跑在自己的线程（stdlib ThreadingHTTPServer，零新依赖）；需要碰 async
handler 的请求通过 `asyncio.run_coroutine_threadsafe` 投回 bot 主循环。
所有跨线程访问的存档写库都走 `ConsoleStore` 内部的锁。

## 安全

- 只绑 `127.0.0.1`，并校验 Host 头（防 DNS rebinding）
- 不提供任何「改配置 / 执行命令」的端点——只有会话、快照、健康检查
- 会话里的消息默认**以主人身份**进入糖糖的关系场（guest_qq = owner_qq），
  因为这是「你自己在跟她说话」；要换成旁观者身份用 `guest_qq` 参数
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from . import media_cache
from .console_store import ConsoleStore
from .store import TRUSTED_TRUST_LEVELS

logger = logging.getLogger("糖糖.会客厅")

#: 当前是否处在一次「会客厅回合」里。发送截获器只在它非空时才动手——
#: 这样真实 QQ 消息的发送不会被误吞（并发下也精确，contextvar 随任务传播）。
_TURN_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "console_turn", default=None)

#: 单次回合的 HTTP 等待上限。LLM + 工具循环 + 可能的语音合成，给足余量。
TURN_TIMEOUT = 240.0

#: 请求体上限——会客厅只收短句，超大的直接拒
MAX_BODY = 64 * 1024

#: 回合痕迹的「可信起点」。P0 修复（2026-10-24 05:20）之前，trace 容器是挂在
#: handler 上的**单例开关**，感知分类器的 reasoning 会混进主回复的思维链
#: （实测混进 "reaction to her offer: he expresses preference for…"）。
#: 早于这个时刻的行在回放里标 `suspect=True`——界面要明说，不能拿噪声冒充
#: 她的思考（CLAUDE.md 教训 #37：假绿比假红危险，因为它长得和真的一样）。
TRACE_TRUST_SINCE = "2026-10-24 05:20:00"

#: `fact_clusters.category` 的中文名——图谱上直接画英文枚举太生硬。
#: 认不出的类别原样显示，不吞掉（新增分类时至少能看见，不会被合并成「其他」）。
CATEGORY_LABELS = {
    "experience": "经历",
    "note": "记录",
    "preference": "偏好",
    "habit": "习惯",
    "identity": "身份",
    "relationship": "关系",
    "work": "工作",
    "health": "健康",
}


def _clip(text: str, limit: int = 40) -> str:
    """单行预览：压掉换行、超长截断加省略号。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _ts(text: str) -> float:
    """宽进的时间解析 → unix 秒：'YYYY-MM-DD HH:MM:SS' / 'HH:MM' / 'YYYY-MM-DD' 都认。

    ⚠️ 为什么不是严格版：时间来源不统一——`chat_log.timestamp` 和
    `tangtang_journal.created_at` 是完整秒级，但 `experience_buffer` 的 `time`
    只有**分钟**（'2026-10-23 22:45'）。严格 strptime 会把它判成坏值返回 0，
    于是独白源的 `updated_at` 变成 0、侧栏时间栏空白（2026-10-24 实测）。
    宁可补零，也不要静默丢时间。

    另一头也宽：坏值返回 0 而不是抛异常——历史行里有空时间戳，一条坏数据
    不该让整个会话列表读不出来。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    if len(raw) == 16:          # '2026-10-23 22:45'
        raw += ":00"
    elif len(raw) == 10:        # '2026-10-23'
        raw += " 00:00:00"
    try:
        from datetime import datetime as _dt
        return _dt.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, OSError):
        return 0.0


def _bare_scope(value: str) -> str:
    """把 `desktop:<sid>` 归一成裸 session id。

    前端两套接口共用同一个「当前选中项」状态，很可能直接把 `scope` key
    传进旧的 `/say` `/turn`——这里接受两种写法，不要求它区分概念。
    """
    text = str(value or "")
    return text.split(":", 1)[1] if text.startswith("desktop:") else text


#: CQ 码（搜索片段里要洗掉——用户搜「猫」，不该看到 `[CQ:image,file=...]`）
_CQ_STRIP_RE = re.compile(r"\[CQ:[^\]]*\]")


def _clean_text(text: str) -> str:
    """搜索用：洗掉 CQ 码、压掉连续空白，保留可读正文。"""
    flat = _CQ_STRIP_RE.sub("", str(text or ""))
    return " ".join(flat.split())


def _find_span(text: str, keyword: str, width: int = 48) -> tuple[str, int, int] | None:
    """把（已清洗的）正文裁成「命中片段」，返回 (snippet, start, length)。

    长消息原样返回会撑爆侧栏——截命中前后各 `width` 字，两端加省略号，
    并把 `start` 校正到裁剪后的下标，前端拿它做高亮。
    命中的可能是被洗掉的 CQ 码里的字（洗完之后就没了），那种返回 None。
    """
    if not text:
        return None
    at = text.lower().find(keyword.lower())
    if at < 0:
        return None
    begin = max(0, at - width)
    end = min(len(text), at + len(keyword) + width)
    prefix = "…" if begin > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[begin:end] + suffix,
            len(prefix) + (at - begin),
            len(keyword))


#: 驱动力 key → 中文标签（独白卡片的副标题用，缺了退回 key 本身）
_DRIVE_LABELS = {
    "social": "社交渴望",
    "commitment": "承诺压力",
    "curiosity_info": "信息好奇",
    "express": "表达欲",
    "curiosity_explore": "探索欲",
    "avoid": "回避",
}


def _drive_label(key: Any) -> str:
    return _DRIVE_LABELS.get(str(key or ""), str(key or "未知"))


def _local_trace_from_meta(meta: dict | None) -> dict | None:
    """把本地会话的回合元信息投影成 `ConsoleTraceSummary`（2026-10-25 第 17 轮）。

    **为什么需要它**：QQ 镜像的 `trace` 来自 `turn_traces`（`chat_log.event_key`
    关联），而本地 `desktop:` 会话的消息存在 `.会客厅.db`，元信息在
    `console_messages.meta_json` 里——形状不同，但前端只认一种摘要。
    这里做一层投影，前端那套「💭 思考 N 字 / 展开 / 回放」组件就能
    **两处通用**，不用为本地会话写第二份渲染（复制粘贴必然漂移）。

    历史消息（本改动之前的回合）meta 里没有 `event_key`，返回 None——
    界面按「这一回合没留痕」隐藏那块，不报错。

    字段名严格对齐 `desktop/src/lib/api.ts::ConsoleTraceSummary`。
    """
    if not isinstance(meta, dict):
        return None
    event_key = str(meta.get("event_key") or "")
    if not event_key:
        return None
    try:
        reasoning_chars = int(meta.get("reasoning_chars") or 0)
    except (TypeError, ValueError):
        reasoning_chars = 0
    tools = meta.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    # 既没思考也没工具 → 没什么可看的，别在气泡下面塞一个空卡片。
    if reasoning_chars <= 0 and tool_count == 0:
        return None
    try:
        duration_ms = int(meta.get("ms") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    return {
        "run_id": "",
        "reasoning_chars": reasoning_chars,
        "tool_count": tool_count,
        "duration_ms": duration_ms,
        "status": "completed",
        "event_key": event_key,
    }


#: 每一站**交给下一站**的东西（2026-10-25 第 17 轮二次整改）。
#:
#: 主人要「像架构图谱一样，直观地展示环节与环节之间如何配合」。
#: 环节的职责写在每站的 `role` 里，但「上一个环节的输出怎么变成下一个环节的输入」
#: 是另一层信息——集中在这张表里而不是散在 9 处调用点，改口径只改一处。
_FLOW_CARRIES: dict[str, list[str]] = {
    "inbound": ["消息原文", "会话（群/私聊）", "聊天记录编号"],
    "routing": ["这一会话的历史窗口", "决策动作（回复/沉默）", "场景与关系档案"],
    "context": ["她的身份提示", "最近的历史对话"],
    "inject": ["背景材料（关系/记忆/自忆/悬事…）", "缺口告知（材料不全就别硬编）"],
    "recall": ["回忆条目", "✏️ 纠正标记（哪条被对方改过）"],
    "tools": ["工具返回结果"],
    "generate": ["回复正文", "思考链（不发送，只留档）"],
    "send": ["已发出的正文", "发送结果"],
    "feedback": ["情绪标签与反应延迟（下一轮参考）"],
}


def _build_turn_flow(*, message: dict | None, digest: dict | None,                     prompt_chars: int, trace: dict | None,
                     decision: dict | None, feedback: dict | None,
                     reply_text: str = "") -> list[dict]:
    """把一回合拆成**九段流程 + 每段的完整参数**（2026-10-25 第 17 轮 ④ 扩展）。

    ## 从 7 段扩到 9 段：补上"完整流程"缺的头和尾

    主人原话：「当前『完整流程』是一个固定模板，需要确认一条消息从开始到结束的
    完整流程能否用这个模板完整展示出来；如果展示不全，就扩展模板。」
    对着真实链路逐段核对后补了两段：

      1 inbound  收到消息      chat_log 行
      2 routing  路由与窗口    scope 分域 / 决策动作 / 窗口状态  ← 新增
      3 context  系统提示      system 段清单 + 历史窗口
      4 inject   背景注入      **每块三态 + 预算**（本轮重点）
      5 recall   记忆召回      记忆块逐条 + 三档标记
      6 tools    工具调用      入参/返回/耗时
      7 generate 生成          模型 / 思考 / token 账本
      8 send     发送          决策 / 正文
      9 feedback 对方反应      情绪 / 置信度 / 延迟  ← 从原第 7 段拆出

    ## 为什么是「段 + params」两层

    `items` 是给人看的摘要（一行一句）；`params` 是**全部参数**（界面折叠展示）。
    主人要的是「把参数也公开并展示出来，例如背景注入的背景块、记忆召回的记忆块」——
    这些原来是**只写日志、从不落库**的事实（`fit_backgrounds` 的淘汰/裁剪、
    `TokenReport` 的各段 token）。第 8 轮那个「裁剪尺子差 2.5 倍藏了 9 天」
    就是这种「有事实、没出口」。

    ## 字段

    每段：`{phase, key, label, ok, ms, items: [{k,v}], params: [{k,v}], note}`，
    另有两种专用结构：`inject` 带 `blocks`（每块的 label/优先级/token/三态/预览），
    `recall` 带 `entries`（记忆块拆成的条目）。

    ## 仍然是一个纯函数

    全部来自已落盘的数据（`chat_log` / `turn_traces.prompt_digest` /
    `decision_runs` / `feedback`），不查库、不改任何状态——能直接单测，
    也守住「回放是观察窗口，绝不能为了展示改动回合本身」（第 15 轮教训 #45）。
    """
    digest = digest if isinstance(digest, dict) else {}
    trace = trace if isinstance(trace, dict) else {}
    msg = message if isinstance(message, dict) else {}
    dec = decision if isinstance(decision, dict) else {}
    fb = feedback if isinstance(feedback, dict) else {}

    def _int(value, default: int = 0) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    def _clip(value, limit: int = 200) -> str:
        text = str(value or "")
        return text if len(text) <= limit else text[:limit] + "…"

    def _p(k: str, v: Any, doc: str, status: str = "") -> dict:
        """一个参数 = 键 + 值 + **一句人话解释**（2026-10-25 第 17 轮二次整改）。

        主人原话：「必要的参数解释（简短）」。`doc` 就是给不写代码的人看的：
        光有「预算上限 1100t」他不知道自己该不该在意，加上「这条链一轮最多
        花多少 token 买背景材料」才知道怎么看。解释写在数据里而不是前端——
        参数是后端造的，只有造它的人知道它是什么口径。
        """
        item: dict = {"k": k, "v": v, "doc": doc}
        if status:
            item["status"] = status
        return item

    def _station(*, phase: int, key: str, label: str, group: str,
                 role: str, logic: str, status: str, did: str = "",
                 ms: int = 0, items: list | None = None,
                 params: list | None = None, note: str = "",
                 **extra: Any) -> dict:
        """一站（流程里的一个环节）。

        ## 为什么是「四态」而不是布尔

        布尔只有「好/坏」，但真实链路里三种情况必须分开，否则小白运营者会误判：

          - `ok`    这一站正常走完
          - `warn`  走完了但打了折（记忆块被裁、没留决策记录）——**能用，但要看**
          - `error` 这一站该产出的东西完全没有（没采集到、注入链断了）
          - `skip`  这一轮本来就不该有（没调工具、她决定沉默、对方还没回话）

        `skip` **不是故障**。把「没调工具」标成红的，会让人去修一个不存在的 bug。

        `ok` 字段保留（= status 是 ok/skip），老前端与老测试不破。

        ## 三个说明字段各回答一个问题

          - `role` 这一站负责什么（她走流程时这一步在做什么）
          - `logic` 判断依据是什么（为什么这样走、按什么规则裁剪/选择）
          - `did`   这一轮实际发生了什么（结论，主人说的「仅做参考」）

        全部由后端算好，前端只画——判据只有一处，改口径不用改两处界面。
        """
        return {
            "phase": phase, "key": key, "label": label, "group": group,
            "role": role, "logic": logic, "did": did, "status": status,
            "ok": status in ("ok", "skip"),
            "ms": ms, "items": items or [], "params": params or [],
            # 交给下一站的东西（「环节之间如何配合」的显式数据）
            "carries": list(_FLOW_CARRIES.get(key, [])),
            "note": note, **extra,
        }

    flow: list[dict] = []

    # 后面几段共用的派生量（一次算好，别每段各猜一遍）
    labels = [str(x) for x in (digest.get("user_bg_labels") or []) if str(x)]
    sections = [str(x) for x in (digest.get("system_sections") or []) if str(x)]
    history_count = _int(digest.get("history_count"))
    history_chars = _int(digest.get("history_chars"))
    bg = digest.get("backgrounds") if isinstance(digest.get("backgrounds"), dict) else {}
    bg_budget = bg.get("budget") if isinstance(bg.get("budget"), dict) else {}
    bg_blocks = bg.get("blocks") if isinstance(bg.get("blocks"), list) else []
    scope = str(msg.get("scope") or "")
    channel = ("群聊" if scope.startswith("group:")
               else ("私聊" if scope.startswith("private:") else "—"))

    # ── 1 入站：消息从 QQ 进来 ──
    raw_content = str(msg.get("content") or "")
    if msg and raw_content.strip():
        inbound_status, inbound_did = "ok", (
            f"收到一条{channel}消息：「{_clip(raw_content.replace(chr(10), ' '), 40)}」")
    elif msg:
        inbound_status, inbound_did = "warn", "消息进来了，但正文是空的（可能是纯图片/表情消息）"
    else:
        inbound_status, inbound_did = "error", "找不到触发消息——这条痕迹对不上任何聊天记录"
    flow.append(_station(
        phase=1, key="inbound", label="收到消息", group="进入",
        role="消息从 QQ 进来。系统记下原文、说话人、时间，并分配一个聊天记录编号。",
        logic=("只要消息写进 chat_log 就算过站。这一站不管要不要回——"
               "回不回、怎么回由她在第 6~8 站自己决定。"),
        status=inbound_status, did=inbound_did,
        items=([
            {"k": "原文", "v": _clip(msg.get("content"), 160)},
            {"k": "时间", "v": str(msg.get("timestamp") or "—")},
            {"k": "来源", "v": scope or "—"},
            {"k": "说话人", "v": str(msg.get("speaker") or "—")},
        ] if msg else []),
        params=([
            _p("chat_log id", str(msg.get("id") or "—"),
               "聊天记录表里的编号。排查「这条回复对应哪条消息」时按它反查"),
            _p("全文长度", f"{len(raw_content)} 字",
               "消息一共多少字。很长的消息在拼 prompt 时可能被截断"),
            _p("会话", scope or "—",
               "private: 开头是私聊，group: 开头是群聊——两种会话的历史记录完全分开"),
            _p("时间戳", str(msg.get("timestamp") or "—"),
               "QQ 那边收到这条消息的时间，不是她开始回的时间"),
        ] if msg else []),
        note="" if msg else "这一回合找不到触发消息（可能是痕迹先于消息落库）",
    ))

    # ── 2 路由与窗口：消息进哪条路、决定了什么 ──
    if dec:
        routing_status = "ok"
        routing_did = (f"按{channel}走，这一轮判定为「{dec.get('decision') or '未记录'}」"
                       + (f"，用了 {len(dec.get('tool_calls') or [])} 个工具"
                          if dec.get("tool_calls") else ""))
    elif scope:
        routing_status, routing_did = "warn", (
            f"按{channel}走了，但没留下决策记录——「决定回复还是沉默」这一步看不到")
    else:
        routing_status, routing_did = "error", "既没有会话信息也没有决策记录"
    flow.append(_station(
        phase=2, key="routing", label="路由与窗口", group="进入",
        role=("按群聊/私聊分域，取出这个会话的历史窗口与场景，"
              "并记录她这一轮是决定回复还是沉默。"),
        logic=("私聊只读私聊记录、群聊只读这个群的记录，两边绝不混"
               "（群聊消息带 group_id，私聊的 group_id 为空）。"
               "窗口里的每条消息都要进模型，系统不替她筛掉哪条。"),
        status=routing_status, did=routing_did,
        items=([
            {"k": "通道", "v": channel},
            {"k": "决策动作", "v": str(dec.get("decision") or "—")},
        ] if (msg or dec) else []),
        params=([
            _p("会话", scope or "—",
               "这条消息属于哪个会话。后面所有材料的检索范围都由它决定"),
            _p("通道", channel,
               "群聊和私聊是两条不同的处理口径：注入的材料、能用的功能都不一样"),
            _p("决策", str(dec.get("decision") or "—"),
               "reply=决定回复，skip=决定沉默（不是失败），fade=淡出窗口"),
            _p("决策状态", str(dec.get("status") or "—"),
               "completed=这一轮走完了，failed=中途出错"),
            _p("开始", str(dec.get("started_at") or "—"),
               "她开始处理这条消息的时间"),
            _p("结束", str(dec.get("finished_at") or "—"),
               "她这一轮处理完的时间"),
            _p("工具调用数", f"{len(dec.get('tool_calls') or [])} 个",
               "决策记录里登记的工具数；具体调了什么看第 6 站"),
        ] if (msg or dec) else []),
        note="" if dec else "这一回合没有 decision_runs 记录（决策侧未采集）",
    ))

    # ── 3 系统提示：她的身份/规则/时间注入了哪些段 ──
    system_chars = _int(digest.get("system_chars"))
    if not digest:
        ctx_status, ctx_did = "error", "没留下 prompt 摘要，这一站无从判断"
    elif not sections:
        ctx_status, ctx_did = "warn", "prompt 留下了，但 system 提示一个段都没有——她可能不知道自己是谁"
    elif not history_count:
        ctx_status, ctx_did = "warn", f"身份提示 {len(sections)} 段已就位，但历史对话是空的（重启后首轮常见）"
    else:
        ctx_status, ctx_did = "ok", (
            f"身份提示 {len(sections)} 段共 {system_chars} 字，带上了最近 {history_count} 条历史对话")
    flow.append(_station(
        phase=3, key="context", label="系统提示与历史", group="理解",
        role=("搭好她的身份：system 提示（她是谁、什么性格、怎么说话、现在几点）"
              "＋ 最近的历史对话。"),
        logic=("system 段整段照发，不在这一步裁剪；历史对话按 token 预算从近到远保留，"
               "越早的越先被挤掉。"),
        status=ctx_status, did=ctx_did,
        items=([
            {"k": "system 段", "v": f"{len(sections)} 段"},
            {"k": "system 字符", "v": f"{system_chars} 字"},
            {"k": "历史条数", "v": f"{history_count} 条"},
            {"k": "历史字符", "v": f"{history_chars} 字"},
        ] if digest else []),
        params=([
            _p("system 段清单", "、".join(sections) if sections else "（一个段都没有）",
               "她的身份由这些段拼成：人格、性格、说话方式、称呼、此刻（时间）……少一段就少一层"),
            _p("system 字符", f"{system_chars} 字",
               "身份提示占多少字。这部分每轮都发，是固定的成本"),
            _p("user 字符", f"{_int(digest.get('user_chars'))} 字",
               "这条用户消息拼好之后有多长（包含背景材料）"),
            _p("历史条数", f"{history_count} 条",
               "这一轮带上了多少条最近的对话。太少她会「刚说过就忘」"),
            _p("历史字符", f"{history_chars} 字",
               "历史对话占多少字"),
            _p("prompt 合计", f"{_int(prompt_chars or digest.get('total_chars'))} 字符",
               "整份 prompt 的总字数（身份+历史+用户消息+工具说明），是她的输入总量"),
        ] if digest else []),
        note=("" if history_count else "历史窗口是空的（重启后从 chat_log 重建，首轮可能为空）")
        if digest else "这一回合没留 prompt 摘要（痕迹采集 2026-10-25 之前没有）",
    ))

    # ── 4 背景注入：这一轮给她塞了哪些材料、每块的命运 ──
    kept_rows = [b for b in bg_blocks if b.get("status") == "kept"]
    trunc_rows = [b for b in bg_blocks if b.get("status") == "truncated"]
    drop_rows = [b for b in bg_blocks if b.get("status") == "dropped"]
    in_prompt = len(kept_rows) + len(trunc_rows)
    # ⚠ 历史 trace 只有 `user_bg_labels`、没有 `backgrounds.blocks`（逐块命运
    # 是 2026-10-25 当天才落库的）。这种数据下 `in_prompt` 会算成 0，而同一站
    # 下面又列着 6 个「注入了哪些背景」——界面自相矛盾。
    # 2026-10-25 视觉验收实测发现（`v2:private:…1792899595` 那轮）。
    # 有标签就用标签数兜底，并在文案里标明这是按标签统计的估算。
    blocks_measured = bool(bg_blocks)
    if not blocks_measured and labels:
        in_prompt = len(labels)
    budget_limit = _int(bg_budget.get("limit"))
    budget_used = _int(bg_budget.get("used"))
    inject_params: list[dict] = []
    if bg_budget:
        inject_params += [
            _p("预算上限", f"{budget_limit}t",
               "背景材料这一轮最多花多少 token。超了就要裁（保留开头）或整块淘汰"),
            _p("已用", f"{budget_used}t" +
               (f"（{budget_used / max(1, budget_limit) * 100:.0f}%）" if budget_limit else ""),
               "实际花了多少。接近上限＝材料在抢预算，低优先级的会被牺牲"),
            _p("保护块超顶", "是（引用/协议挤占了预算）" if bg_budget.get("protected_overflow") else "否",
               "引用原文、行为协议这类「不能裁」的块本身就把预算吃满了，其余材料会被压掉"),
            _p("证据保底", "已动用" if bg_budget.get("evidence_reserve_used") else "未动用",
               "给「可能有用的材料」留的一小份保底额度，被动用时说明正常材料已经不够放"),
            _p("缺口告知", "已注入（她知道材料不全）" if bg.get("gap_notice") else "未注入",
               "预算不足时有没有告诉她「材料不全」——注入了她才不会拿空白硬编"),
        ]
    for i, b in enumerate(bg_blocks):
        status = str(b.get("status") or "")
        tokens = _int(b.get("tokens"))
        before = _int(b.get("tokens_before"))
        if status == "dropped":
            cost = f"{before}t · 被淘汰"
            fate = "整块没进 prompt，她这一轮完全看不到它"
        elif status == "truncated":
            cost = f"{before}→{tokens}t · 被裁"
            fate = "只留了开头一部分，后面的内容她看不到"
        else:
            cost = f"{tokens}t · 保留"
            fate = "完整进了 prompt"
        # ⚠ 序号不能省：同一个 label 可以在列表里出现多次（「协议」就有好几条），
        # 只用 label 做键会让后来的覆盖掉前面的——界面上等于**悄悄丢数据**。
        inject_params.append(_p(
            f"{i + 1}. {b.get('label') or '?'}（P{b.get('priority', 9)}）", cost,
            f"淘汰优先级 P{b.get('priority', 9)}（数字越小越该留下）；{fate}",
            status=status))
    if not digest:
        inject_status, inject_did = "error", "没留下 prompt 摘要，看不出她这轮拿到了什么材料"
    elif not labels:
        inject_status, inject_did = "error", "没有任何背景材料进 prompt——注入链可能整条断了"
    else:
        if not blocks_measured:
            # 有注入、但逐块命运没采到：能确认「给了材料」，不能确认「有没有被裁」。
            # 判 ok 会谎称一切正常，判 error 会让人去修不存在的问题 —— 打折最准。
            inject_status = "warn"
        else:
            inject_status = "ok" if (trunc_rows or drop_rows) == [] else "warn"
        inject_did = (f"{in_prompt} 块进了 prompt"
                      + ("" if blocks_measured
                         else "（按注入标签统计，逐块用量这次没采到）")
                      + (f"（其中 {len(trunc_rows)} 块被裁得只剩开头）" if trunc_rows else "")
                      + (f"，{len(drop_rows)} 块被整块淘汰" if drop_rows else "")
                      + (f"；预算 {budget_used}/{budget_limit}t" if bg_budget else ""))
    flow.append(_station(
        phase=4, key="inject", label="背景注入", group="理解",
        role=("把关系档案、记忆、自忆、悬着的事这些「关于对方的背景」塞进 prompt。"
              "它们不是聊天记录，是她对这个人的了解。"),
        logic=("先按淘汰优先级排序（P1 最该留、P10 最先被扔），预算用完就裁块"
               "（保留开头）或整块淘汰；被淘汰＝她这一轮完全看不到这块材料，"
               "而且通常没有工具能再把它取回来。"),
        status=inject_status, did=inject_did,
        items=([
            {"k": "进 prompt", "v": f"{in_prompt} 块" +
                                  (f"（{len(trunc_rows)} 块被裁）" if trunc_rows else "")
                                  + ("" if blocks_measured else "（按标签）")},
            {"k": "被淘汰", "v": f"{len(drop_rows)} 块" if blocks_measured else "未采到"},
            {"k": "预算", "v": f"{budget_used}t / {budget_limit}t" if bg_budget else "未采到"},
        ] if digest else []),
        params=inject_params,
        # 结构化给前端画表格：每块的标签/优先级/token/三态/内容预览
        blocks=bg_blocks,
        note=("" if labels
              else ("没有任何背景块进入 prompt——注入链可能整条断了" if digest
                    else "这一回合没留 prompt 摘要（痕迹采集 2026-10-25 之前没有）")),
    ))

    # ── 5 记忆召回：这一轮想起的是哪几条 ──
    marks = digest.get("memory_marks") if isinstance(digest.get("memory_marks"), dict) else {}
    memory_block = next((b for b in bg_blocks if b.get("label") == "记忆"), None)
    memory_status = str((memory_block or {}).get("status") or "")
    memory_tokens = _int((memory_block or {}).get("tokens"))
    memory_injected = memory_status in ("kept", "truncated") or "记忆" in labels
    # 记忆块的内容预览按行拆成条目（渲染格式是 `💭 内容`，见 format_compact_memories）。
    memory_preview = str((memory_block or {}).get("preview") or "")
    entries = [ln.strip() for ln in memory_preview.splitlines()
               if ln.strip().startswith(("💭", "·", "-"))]
    recall_items = [
        {"k": "记忆块", "v": {
            "kept": "已注入", "truncated": "注入了但被裁",
            "dropped": "被淘汰（预算不够）"}.get(memory_status,
                                              "已注入" if memory_injected else "未进 prompt")},
        {"k": "✏️ 纠正记忆", "v": f"{_int(marks.get('corrected'))} 条"},
        {"k": "不确定档", "v": f"{_int(marks.get('uncertain'))} 条"},
    ]
    if entries:
        recall_items.append({"k": "回忆起", "v": f"{len(entries)} 条"})
    if not digest:
        recall_status, recall_did = "error", "没留下 prompt 摘要，看不出她这轮想起了什么"
    elif memory_status == "kept":
        recall_status, recall_did = "ok", (
            f"想起 {len(entries)} 条，完整放进了 prompt"
            + (f"（其中 {_int(marks.get('corrected'))} 条是对方纠正过的）"
               if _int(marks.get("corrected")) else ""))
    elif memory_status == "truncated":
        recall_status, recall_did = "warn", (
            f"想起 {len(entries)} 条，但记忆块被裁，可能只留了开头")
    elif memory_status == "dropped":
        recall_status, recall_did = "warn", "记忆块被整块淘汰——她这一轮等于想不起来任何旧事"
    elif memory_injected:
        # ⚠ 2026-10-25 视觉验收抓到的自相矛盾：`user_bg_labels` 里有「记忆」，
        # 说明记忆块确实进了 prompt，但 `backgrounds.blocks` 没采到（老 trace），
        # 于是 `memory_status` 是空串，旧代码走进最后那个 else 说「记忆块没进 prompt」——
        # 而同一站的 items 又写着「记忆块：已注入」。**同一站内自相矛盾**比信息少更糟：
        # 运营者不知道该信哪一行。判 warn（有材料，但看不出有没有被裁）。
        recall_status, recall_did = "warn", (
            f"记忆块进了 prompt{('（' + str(len(entries)) + ' 条内容）') if entries else ''}"
            f"，但逐块用量这次没采到——没法确认它有没有被裁")
    else:
        recall_status, recall_did = "warn", "记忆块没进 prompt（检索为空或预算不足）"
    flow.append(_station(
        phase=5, key="recall", label="记忆召回", group="理解",
        role=("按这条消息去检索她的记忆，挑出最相关的几条放进 prompt——"
              "「她记得这件事」就靠这一站。"),
        logic=("语义检索＋关键词混合排序，取最相关的几条。✏️ 标记的是对方纠正过的事实"
               "（可信度最高，会顶掉旧版本）；「不确定」档会带上置信度，"
               "让她说话时别把不确定的当铁事实。"),
        status=recall_status, did=recall_did,
        items=recall_items if digest else [],
        params=([
            _p("记忆块状态",
               memory_status or ("已注入（逐块用量未采到）" if memory_injected else "（无数据）"),
               "kept=完整进 prompt，truncated=只留开头，dropped=整块被淘汰（她看不到）"),
            _p("占用 token", f"{memory_tokens}t" if memory_block else "—",
               "记忆块占了多少预算。太高会挤掉别的材料"),
            _p("✏️ 纠正记忆", f"{_int(marks.get('corrected'))} 条",
               "对方明确纠正过、已经改掉旧版本的记忆——这类最可靠"),
            _p("不确定档", f"{_int(marks.get('uncertain'))} 条",
               "置信度偏低的记忆。她会被提示「这条不一定准」"),
            _p("解析出的条目", f"{len(entries)} 条",
               "从记忆块里拆出来的实际内容条数（上面那段列表就是）"),
        ] if digest else []),
        # 逐条记忆内容（前端列表展示）——主人要的「记忆召回的记忆块参数」
        entries=entries,
        note=("" if memory_injected
              else ("记忆块这轮没进 prompt——群聊候选块多（协议/窗口/窗口姿态/"
                    "证据纪律/关系/自忆/交叉/提及/缺口/反思 共 11 块），预算紧时"
                    "记忆块先被淘汰（实测群聊注入率 61%、私聊 95%）"
                    if digest and scope.startswith("group:")
                    else ("记忆块这轮没进 prompt（预算裁剪或检索为空）" if digest
                          else "这一回合没留 prompt 摘要（痕迹采集 2026-10-25 之前没有）"))),
    ))

    # ── 6 工具调用 ──
    raw_tools = trace.get("tools") if isinstance(trace.get("tools"), list) else []
    tool_items = []
    tool_params: list[dict] = []
    tool_ms = 0
    for i, st in enumerate(raw_tools):
        if not isinstance(st, dict):
            continue
        ms = _int(st.get("ms"))
        tool_ms += ms
        name = str(st.get("name") or "（未命名）")
        tool_items.append({
            "k": name,
            "v": f"{ms}ms · 入参 {_clip(st.get('args'), 60)} · 返回 {_clip(st.get('result'), 80)}",
        })
        # params 里放**完整**入参/返回（逐工具一块，界面用等宽块展示）
        tool_params.append(_p(
            f"{i + 1}. {name}",
            (f"耗时 {ms}ms\n入参：{str(st.get('args') or '（无）')[:800]}"
             f"\n返回：{str(st.get('result') or '（无）')[:800]}"),
            "她这一轮为什么去查这个、查回来什么。返回为空也要看清——工具说「没查到」"
            "和系统说「没数据」是两件事"))
    # 工具效果（分类/空手/决策理由）——第 47 轮：同一份 steps，两处消费
    tool_effects = _build_tool_effects(raw_tools)
    if tool_items:
        # 结论句用 effects 的（「查了 3 次，2 次拿到材料」比「调用了 3 个工具」有用）
        tool_status = "warn" if tool_effects["warnings"] else "ok"
        tool_did = tool_effects["summary"] + (
            f"（共 {tool_ms}ms）" if tool_ms else "")
        if tool_effects["rationale"]:
            tool_did += f"；她的理由：{_clip(tool_effects['rationale'], 40)}"
    else:
        tool_status = "skip"
        tool_did = "这一轮没有调工具，直接凭已有材料回答——这是正常的，不是故障"
    flow.append(_station(
        phase=6, key="tools", label="工具调用", group="决策",
        role="模型自己决定要不要查资料、翻聊天记录、播一首歌；系统只负责执行，把结果交回给她。",
        logic=("系统只提供工具清单，查什么、查不查由她自己决定——"
               "不是关键词触发，也没有「必须先查记忆」的硬规则。"
               "所以这一站一次都不触发也完全正常。"),
        status=tool_status, did=tool_did,
        ms=tool_ms,
        items=tool_items,
        params=tool_params,
        # 结构化给前端：每个工具的性质/是否空手/她的理由（第 47 轮）
        effects=tool_effects,
        note="" if tool_items else "这一回合没调工具（纯文本生成）",
    ))

    # ── 7 生成：模型 / 思考 / token 账本 ──
    reasoning_chars = _int(trace.get("reasoning_chars"))
    token_sections = digest.get("token_sections") if isinstance(digest.get("token_sections"), dict) else {}
    gen_params = [
        _p("模型", str(trace.get("model") or "—"),
           "这一轮用的是哪个模型。不同模型的能力和思考深度不一样"),
        _p("思考链", f"{reasoning_chars} 字",
           "她动笔之前想了多少字（有的模型不输出思考过程，为 0 是正常的）"),
        _p("耗时", f"{_int(trace.get('duration_ms'))}ms",
           "从开始生成到拿到完整回复用了多久。包含工具来回的时间"),
    ]
    if digest.get("token_total"):
        # ⚠ 不带「/ 预算」：`token_total` 是**全部**输入（system+history+user+tools），
        # 而 `budget_limit` 只是 context_builder 给 system prompt 的那份预算——
        # 两个口径放一起会显示「9265t / 预算 6000t」这种看着像超支 54% 的假象。
        gen_params.append(_p(
            "token 合计", f"{_int(digest.get('token_total'))}t（估算）",
            "她这一轮的输入总量（身份+历史+用户消息+工具说明）。是成本，不是回复长度"))
    for name, count in sorted((token_sections or {}).items(), key=lambda x: -_int(x[1])):
        gen_params.append(_p(
            f"token · {name}", f"{_int(count)}t",
            f"输入里「{name}」这部分占多少 token——哪块最贵就看这一行"))
    if trace:
        gen_status = "ok"
        gen_did = (f"{trace.get('model') or '模型'} 用了 {_int(trace.get('duration_ms'))}ms"
                   + (f"、想了 {reasoning_chars} 字" if reasoning_chars else ""))
    else:
        gen_status, gen_did = "error", "没有生成痕迹——这一站的数据没采集到"
    flow.append(_station(
        phase=7, key="generate", label="生成", group="决策",
        role="模型把前面几站给的材料读进去，写出这一段回复（有的模型会先思考一轮再动笔）。",
        logic=("一次生成，不重试不拼接。思考过程会被剥掉，不发给对方；"
               "模型在生成过程中还可能决定调用工具（那些会在第 6 站显示）。"),
        status=gen_status, did=gen_did,
        ms=_int(trace.get("duration_ms")),
        items=([
            {"k": "模型", "v": str(trace.get("model") or "—")},
            {"k": "思考", "v": f"{reasoning_chars} 字"},
            {"k": "耗时", "v": f"{_int(trace.get('duration_ms'))}ms"},
        ] if trace else []),
        params=gen_params if trace else [],
        note="" if reasoning_chars else "这一轮没有思维链（可能是非推理模型或思考被关）",
    ))

    # ── 8 发送：她这一轮真正说出去的话 ──
    # **优先用调用方从 chat_log 取回的那条**（`delivery:` 行），
    # 而不是 `feedback.bot_reply`——feedback 要等感知系统回填（通常要等对方
    # 下一条消息），只靠它会让「发送」段在绝大多数回合显示「未采集到」。
    # 2026-10-25 第 17 轮实测：自检回合她回了 100+ 字，界面却写「正文长度 0 字」。
    sent_text = str(reply_text or fb.get("bot_reply") or "")
    decision_name = str(dec.get("decision") or "")
    if sent_text:
        send_status = "ok"
        send_did = f"发出去了 {len(sent_text)} 字：「{_clip(sent_text.replace(chr(10), ' '), 40)}」"
    elif decision_name == "skip":
        send_status = "skip"
        send_did = "她这一轮决定不说话（沉默是她的选择，不是发送失败）"
    else:
        send_status, send_did = "warn", "没取到她这轮发出去的正文——发送可能失败，或采集口径对不上"
    flow.append(_station(
        phase=8, key="send", label="发送", group="输出",
        role=("把回复清洗、贴图、按需要分条，然后真正发到 QQ。"
              "从这里开始，对方才看得见。"),
        logic=("贴图、分段、引用这些标记在这一站解析成真实动作，"
               "标记本身不会发给对方；只有她决定回复（reply）才会走到发送。"),
        status=send_status, did=send_did,
        items=([
            {"k": "决定", "v": decision_name or "—"},
            {"k": "正文", "v": _clip(sent_text, 120) if sent_text else "（未采集到）"},
        ] if (dec or sent_text) else []),
        params=([
            _p("决策", decision_name or "—",
               "reply=发出去，skip=她选择沉默，failed=生成或发送失败"),
            _p("状态", str(dec.get("status") or "—"),
               "completed=这一轮走完了，failed=中途出错"),
            _p("决策记录的工具", ("、".join(str(x) for x in (dec.get("tool_calls") or []))
                                  or "（无）"),
               "决策侧登记的工具名；实际入参和返回看第 6 站"),
            _p("正文长度", f"{len(sent_text)} 字",
               "实际发出去的字数。为 0 但决策是 reply，说明发送这一环出问题了"),
            _p("正文全文", sent_text or "（chat_log 与 feedback 都没取到她的回复正文）",
               "对方实际收到的内容（多气泡已拼接）——这是唯一能跟 QQ 上对照的原文"),
        ] if (dec or sent_text) else []),
        note="" if sent_text else "没取到她这轮发出去的正文（chat_log 里没有 delivery 行）",
    ))

    # ── 9 对方反应：感知系统事后回填 ──
    senti_cn = {"positive": "正面", "neutral": "中性", "negative": "负面"}.get(
        str(fb.get("sentiment") or ""), "未知")
    if fb:
        fb_status = "ok"
        fb_did = (f"对方{senti_cn}反应"
                  + (f"，隔了 {_int(fb.get('reply_ms'))}ms 才回" if _int(fb.get("reply_ms")) else "")
                  + (f"：「{_clip(fb.get('user_reaction'), 30)}」"
                     if str(fb.get("user_reaction") or "").strip() else ""))
    else:
        fb_status = "skip"
        fb_did = "对方还没回话，或感知系统没匹配到这一回合——不算故障，等对方开口"
    flow.append(_station(
        phase=9, key="feedback", label="对方反应", group="输出",
        role="对方回话之后，感知系统判断他的情绪、等了多久、这句话是不是冲她来的，写回数据库供下一轮参考。",
        logic=("这句回话要能和她的回复对上（文本前缀匹配）才算作对这条回复的反馈；"
               "对不上就记「未采集到」——不是没有反应，是没匹配上。"),
        status=fb_status, did=fb_did,
        ms=_int(fb.get("reply_ms")),
        items=([
            {"k": "情绪", "v": str(fb.get("sentiment") or "—")},
            {"k": "置信度", "v": f"{fb.get('confidence')}"},
            {"k": "对方反应", "v": _clip(fb.get("user_reaction"), 80) or "（无）"},
        ] if fb else []),
        params=([
            _p("情绪分类", str(fb.get("sentiment") or "—"),
               "对方这句话被判成正面/中性/负面——她下一轮的语气会参考它"),
            _p("置信度", f"{fb.get('confidence')}",
               "分类有多确定。太低说明这句话不好判断情绪"),
            _p("反应延迟", f"{_int(fb.get('reply_ms'))}ms",
               "她发出去之后，对方隔了多久才回。太久通常意味着对方没被这句话带动"),
            _p("方向已核验", "是" if fb.get("direction_verified") else "否",
               "这句话是否确认是对她说的。没核验的话这条反馈可能来自别人的对话"),
            _p("对方原话", str(fb.get("user_reaction") or "（无）"),
               "对方说的那句话原文——判断情绪时以它为准，别只看标签"),
            _p("记录时间", str(fb.get("timestamp") or "—"),
               "感知系统写回这条反馈的时间"),
        ] if fb else []),
        note="" if fb else "对方还没反应，或感知系统没匹配到这一回合",
    ))
    return flow


#: 四态的显示名（前端也有同一份，这里只用于汇总句与日志）
_FLOW_STATUS_CN = {"ok": "正常", "warn": "打折", "error": "异常", "skip": "未触发"}


def _summarize_turn_flow(flow: list[dict]) -> dict:
    """给整条链出一句体检结论（2026-10-25 第 17 轮二次整改）。

    ## 为什么要后端算

    「哪个环节没坏、哪个出了问题」是**判据**，不是样式。前端自己数
    `status === 'error'` 也能数，但那样一来「什么算问题」就有两个实现点，
    迟早漂移（第 47 条教训：同一语义 N 个实现点，漏一个就静默错）。
    所以汇总句在这里生成，前端只负责显示。

    ## 结论的措辞

    - 全绿：`9 站全部正常（3 站未触发属于正常）`
    - 有异常：`第 4 站「背景注入」异常` —— 只点**第一个**出问题的，
      因为运营者一次只能修一处；其余在明细里看
    - 只有打折没异常：`没有故障，但有 2 站打了折（材料被裁/记录缺失）`

    `focus_key` 给前端做「跳到出问题那站」的锚点。
    """
    total = len(flow)
    counts = {k: 0 for k in _FLOW_STATUS_CN}
    for p in flow:
        st = str(p.get("status") or "ok")
        if st not in counts:
            st = "ok"
        counts[st] += 1
    errors = [p for p in flow if p.get("status") == "error"]
    warns = [p for p in flow if p.get("status") == "warn"]
    if errors:
        focus = errors[0]
        verdict = f"第 {focus['phase']} 站「{focus['label']}」异常"
        if len(errors) > 1:
            verdict += f"（另有 {len(errors) - 1} 站也异常）"
    elif warns:
        focus = warns[0]
        verdict = (f"没有故障，但有 {len(warns)} 站打了折——"
                   f"先看第 {focus['phase']} 站「{focus['label']}」")
    else:
        focus = None
        verdict = f"{total} 站全部正常"
        if counts["skip"]:
            verdict += f"（其中 {counts['skip']} 站未触发，属于正常）"
    return {
        "total": total,
        "ok": counts["ok"],
        "warn": counts["warn"],
        "error": counts["error"],
        "skip": counts["skip"],
        "verdict": verdict,
        "focus_key": str((focus or {}).get("key") or ""),
        "focus_label": str((focus or {}).get("label") or ""),
        "broken": [{"phase": p["phase"], "key": p["key"], "label": p["label"],
                    "status": p["status"], "note": p.get("note") or p.get("did") or ""}
                   for p in (errors + warns)],
    }


#: 工具分类表（2026-10-25 第 47 轮「她做了什么」）。
#:
#: ## 为什么必须分类：同一个现象在不同类里意义相反
#:
#:   · 检索类返回 12 个字「(记忆库中没有关于'陶艺'的内容)」＝**她没查到**，坏消息；
#:   · 决策类返回 9 个字「好的，这一轮不回复」＝**指令回执**，完全正常。
#:
#: 不分类就只能按长度判「空手」，那会把实测 7 次正常的沉默决定全标成异常——
#: 假警报比没有警报更糟（#37 同族：假绿危险，假红也会让人把面板关掉）。
#:
#: 名字来自实测（`turn_traces.tool_steps_json` 里真实出现过的 10 种，加上
#: 代码里已注册但最近两天没被调用的），认不出的归 `unknown` 并**原样显示**。
_TOOL_KINDS: dict[str, str] = {
    # 检索：查回来的东西决定她「知不知道」，返回空就是她真没材料
    "search_facts": "retrieval", "search_chat_history": "retrieval",
    "search_memories": "retrieval", "search_episodes": "retrieval",
    "search_keywords": "retrieval", "search_relations": "retrieval",
    "history_query": "retrieval", "get_messages_by_date": "retrieval",
    "get_message": "retrieval", "get_recent_messages": "retrieval",
    "get_essence_msgs": "retrieval", "get_first_met": "retrieval",
    "get_group_activity": "retrieval", "get_last_conversation": "retrieval",
    "get_offline_summary": "retrieval", "count_messages": "retrieval",
    "web_search": "retrieval",
    # 感知：读图/时间/天气——读不出内容同样算没拿到料
    "analyze_image": "perception", "get_time": "perception",
    "get_weather": "perception",
    # 决定：调用本身就是她的选择（沉默/收尾/调自己状态），短回执正常
    "skip_response": "decision", "resolve_open_loop": "decision",
    "set_cooldown": "decision", "set_thirst": "decision",
    "toggle_interjection": "decision", "set_opinion_campaign": "decision",
    "end_opinion_campaign": "decision", "opinion_status": "decision",
    # 动作：真的对世界做了什么（发东西/点歌/设提醒），短回执正常
    "sing": "action", "send_voice": "action", "send_stickers": "action",
    "send_cg_sticker": "action", "send_message": "action",
    "set_reminder": "action", "group_say_later": "action",
    "like_user": "action", "like_speaker": "action",
    "poke_user": "action", "poke_speaker": "action",
    "generate_image": "action", "share_image": "action",
    "discover_capabilities": "action",
    # 改记忆：写操作
    "correct_memory": "memory_write", "forget_memory": "memory_write",
}

_TOOL_KIND_CN = {
    "retrieval": "检索", "perception": "感知", "decision": "决定",
    "action": "动作", "memory_write": "改记忆", "unknown": "其他",
}

#: 「空手」的**文案**判据（不是长度判据）。
#:
#: 实测 `memory.db` 里检索类的空返回长这样（原文，2026-10-25 抽样）：
#:   `(没有找到符合条件的聊天记录)`
#:   `(记忆库中没有关于'陶艺'的内容)`
#: 而有料的返回里一定有 `来源 chat_log#…` / `相似度0.72` 这类可核对的锚。
#: 用文案判空比用长度稳：一条 340 字的历史记录和一条 20 字的空结果，
#: 长度能分开；但 60 字的一条「只查到 1 条」和 12 字空结果，长度分不开。
_TOOL_EMPTY_RE = re.compile(
    r"没有找到|没有关于|记忆库中没有|未找到|没有相关|无匹配|没有符合条件|暂无")

#: 决策类工具里「她的理由」可能藏在这些参数键下（实测 args 是 JSON 字符串）
_TOOL_RATIONALE_KEYS = ("reason", "note", "outcome", "why")


def _build_tool_effects(steps: list | None) -> dict:
    """把这一轮的工具调用翻译成「她查到了什么、有没有白跑、依据是什么」。

    ## 为什么要有这一层

    原来「她做了什么」只有工具名 + 耗时 + 两段可折叠的 JSON（入参/返回）。
    那是**日志**的形态：能 grep，但读不出结论。运营者要回答的是三个问题：

      1. 她查了几次？—— 次数
      2. 查到了吗？—— 空手率（这是「她不知道」的**直接证据**，不是猜测）
      3. 她凭什么叫停？—— `skip_response.reason` 原文就是她的决策依据

    ## 判据只有一处

    `verdict`（hit/empty/done/decision）在这里算，前端只负责上色。
    前端自己按 `result` 长度判会得到不同答案——实测 `analyze_image` 13 次里
    有 4 次返回极短，按长度全算失败，而其中一半是「图里没字」的正常结果。

    ## 纯函数

    输入是 `trace["tools"]` 规范化后的 `[{name,args,result,ms}]`，
    不查库、不改状态，能直接单测。
    """
    items: list[dict] = []
    counts = {k: 0 for k in _TOOL_KIND_CN}
    empty_n = 0
    rationale = ""
    for st in (steps if isinstance(steps, list) else []):
        if not isinstance(st, dict):
            continue
        name = str(st.get("name") or "").strip() or "（未命名）"
        kind = _TOOL_KINDS.get(name, "unknown")
        result = str(st.get("result") or "")
        args = str(st.get("args") or "")
        try:
            ms = int(st.get("ms") or 0)
        except (TypeError, ValueError):
            ms = 0
        # 只有「拿材料」的类才谈得上空手；决定/动作类返回短是回执
        is_empty = kind in ("retrieval", "perception") and bool(
            _TOOL_EMPTY_RE.search(result))
        if is_empty:
            verdict, verdict_cn = "empty", "没查到"
        elif kind == "decision":
            verdict, verdict_cn = "decision", "她的决定"
        elif kind == "retrieval":
            verdict, verdict_cn = "hit", "查到了"
        else:
            verdict, verdict_cn = "done", "已执行"
        counts[kind] = counts.get(kind, 0) + 1
        if is_empty:
            empty_n += 1
        # 她的理由：决策类工具的入参里通常有一句人话（skip_response.reason 等）
        if not rationale and kind == "decision" and args:
            try:
                parsed = json.loads(args)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                for key in _TOOL_RATIONALE_KEYS:
                    val = str(parsed.get(key) or "").strip()
                    if val:
                        rationale = val
                        break
        items.append({
            "name": name,
            "kind": kind,
            "kind_cn": _TOOL_KIND_CN.get(kind, "其他"),
            "ms": ms,
            "chars": len(result),
            "empty": is_empty,
            "verdict": verdict,
            "verdict_cn": verdict_cn,
            "brief": _clip_text(result.replace("\n", " ").strip(), 90),
            "args": args,
            "result": result,
            "rationale": rationale if kind == "decision" and args else "",
        })
    total = len(items)
    counts["total"] = total
    counts["empty"] = empty_n
    # 一句话结论：先说查了几次、几次有料，再说动作
    parts: list[str] = []
    lookups = counts.get("retrieval", 0) + counts.get("perception", 0)
    if lookups:
        hit = lookups - empty_n
        parts.append(f"查了 {lookups} 次" + (f"，{hit} 次拿到材料" if hit
                                            else "，全都没查到"))
        if empty_n:
            parts.append(f"{empty_n} 次空手")
    acts = counts.get("action", 0) + counts.get("memory_write", 0)
    if acts:
        parts.append(f"做了 {acts} 个动作")
    if counts.get("decision", 0):
        parts.append(f"{counts['decision']} 个决定")
    summary = ("；".join(parts) if parts else "这一轮没有调工具")
    warnings: list[dict] = []
    if empty_n:
        warnings.append({
            "level": "warn", "key": "tool_empty",
            "text": (f"{empty_n} 次检索没查到东西——"
                     f"她这轮是凭已有材料在说，不是在编"),
            "evidence": "、".join(i["name"] for i in items if i["empty"]),
        })
    # 原来这里还有一条「一轮调了 ≥4 次工具」的预警，2026-10-25 实测后删掉：
    # `llm.tool_loop_limit` 默认就是 4，用满上限是**正常**的检索节奏
    # （实测那轮 3 次检索 1 次收尾，共 115ms）。每轮都弹的预警等于噪音，
    # 而噪音会让人把整个面板关掉——那比没有预警更糟。
    verdict = "warn" if warnings else ("ok" if total else "skip")
    return {
        "items": items,
        "counts": counts,
        "lookups": lookups,
        "empty_rate": (empty_n / lookups) if lookups else 0.0,
        "summary": summary,
        "rationale": rationale,
        "warnings": warnings,
        "verdict": verdict,
        "note": ("" if total else "这一轮没调工具（她凭已有材料直接回答，正常）"),
    }


def _clip_text(text: str, limit: int) -> str:
    """裁一段预览文本——不引入省略号以外的任何格式（界面直接 textContent）。"""
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def _context_budget_tokens() -> int:
    """上下文总预算（`ContextBuilder.BUDGET_TOTAL`）——**从代码常量读，不抄数字**。

    为什么惰性导入：`console_api` 被 `main.py` 在很早就导入，直接顶层导入
    `context_builder` 会把 BGE/分词那条依赖链提前拉起来（会客厅没开时也付这个代价）。
    失败就返回 0，账本会显示「预算未知」而不是显示一个猜的数字——**宁缺勿假**。
    """
    try:
        from .context_builder import ContextBuilder
        return int(getattr(ContextBuilder, "BUDGET_TOTAL", 0) or 0)
    except Exception:  # pragma: no cover - 只在裁剪过的环境里发生
        logger.debug("账本：读上下文预算失败", exc_info=True)
        return 0


def _build_prompt_ledger(*, digest: dict | None,
                         prompt_chars: int = 0) -> dict:
    """把「她这一轮收到的 prompt」折成一本可读的账（2026-10-25 第 47 轮）。

    ## 为什么单独成一本账，而不是继续往第 7 站的参数里加行

    第 7 站已经有 `token · tools` 这样的 KV，但那是**清单**不是**账**：
    没有分母、没有占比、没有「离上限还有多远」。运营者看完仍然不知道
    「够不够用」。主人原话：「我需要更多的参数显示，以及有效的预警，
    作为一个取代日志的存在。」账本要回答的就是这三个。

    ## 分母纪律（#38：同一个量两个换算点，只改了一个）

    这里有**三个不同分母**，混算就是又一次假警报（上一轮已经踩过
    「9265t / 预算 6000t = 超支 54%」，那个 54% 是不存在的）：

      · `context_budget`   = `ContextBuilder.BUDGET_TOTAL`（6000）：**只**约束
        system prompt 的裁剪，不是「所有输入的上限」
      · `sections.tools`   = 工具定义（实测 5954）：请求里的**另一块**，
        不在上下文预算里，但比整个预算还大
      · `measured_total`   = `report.total`（实测 9240）：实际输入总量

    所以本函数**只在一个分母内部算百分比**：背景块的 `used/limit`、
    单块被裁前后的比例。跨分母只并列展示，且每项自带 `doc` 说明它是谁。

    ## `warnings` 是「预警」，不是「样式」

    判据全在这里算（前端只上色）。三条规则，宁可少不要吵：
      · 背景预算 ≥90% → warn（快挤不下了，下一轮记忆就可能被裁）
      · 任一块 truncated → warn（这一轮她的材料被砍了，附前后 token）
      · 任一块 dropped  → warn（整块没进去，她这轮等于看不到它）
    没有 digest 时给 `skip`，**不是 error**——痕迹功能 2026-10-24 才上线，
    更早的回合没有账本是正常的，标红会让人去修一个不存在的问题。
    """
    dg = digest if isinstance(digest, dict) else {}
    sections_raw = dg.get("token_sections")
    sections_raw = sections_raw if isinstance(sections_raw, dict) else {}
    section_meta = (
        ("tools", "工具定义",
         "她这一轮能用的工具说明书（每个工具叫什么、要什么参数）。"
         "每轮都固定发一遍，是纯开销"),
        ("system", "系统提示词",
         "她的身份、性格、说话方式、协议——决定「她是谁」的那一段"),
        ("history", "历史对话",
         "这一会话前面几条消息，让她知道刚才聊到哪了"),
        ("user", "本轮消息",
         "对方这句话 + 注入的背景材料（关系档案/记忆/窗口…）"),
    )
    in_ctx = {"system", "history", "user"}
    sections: list[dict] = []
    ctx_used = 0
    for key, label, doc in section_meta:
        if key not in sections_raw:
            continue
        try:
            tok = int(sections_raw.get(key) or 0)
        except (TypeError, ValueError):
            tok = 0
        if key in in_ctx:
            ctx_used += tok
        sections.append({
            "key": key, "label": label, "tokens": tok,
            "in_context": key in in_ctx, "doc": doc,
        })
    # 同分母内算占比（只在「上下文」那三段之间比，工具那块不与它们混）
    inner = [s for s in sections if s["in_context"]]
    inner_total = sum(s["tokens"] for s in inner)
    for s in sections:
        s["share"] = (round(s["tokens"] / inner_total, 3)
                      if s["in_context"] and inner_total else None)
    ctx_budget = _context_budget_tokens()
    context = {
        "budget": ctx_budget,
        "used": ctx_used,
        "left": (max(0, ctx_budget - ctx_used) if ctx_budget else None),
        "used_pct": (round(ctx_used * 100.0 / ctx_budget, 1)
                     if ctx_budget else None),
        "over": bool(ctx_budget and ctx_used > ctx_budget),
        "doc": ("这个预算只约束「系统提示词 + 历史 + 本轮消息」三段的裁剪；"
                "工具定义不在其中，别把两个数相除"),
    }
    try:
        measured_total = int(dg.get("token_total") or 0)
    except (TypeError, ValueError):
        measured_total = 0
    # ⚠ 这里**绝不**用 `prompt_chars` 兜底：那是**字符数**，不是 token。
    # 2026-10-25 第 47 轮实测踩到：19:07 那条 `prompt_chars=5607`，
    # 兜底后账本显示「实测合计 5607t」，而真实 token 根本不知道。
    # 数字看错了不可怕，可怕的是它长得像真的（#37 假绿同族）。
    total_chars = int(dg.get("total_chars") or 0)
    if not total_chars:
        total_chars = int(prompt_chars or 0)
    # ── 背景块：唯一可以放心算百分比的同分母 ──
    bg = dg.get("backgrounds") if isinstance(dg.get("backgrounds"), dict) else {}
    bg_budget = bg.get("budget") if isinstance(bg.get("budget"), dict) else {}
    raw_blocks = bg.get("blocks") if isinstance(bg.get("blocks"), list) else []
    blocks: list[dict] = []
    for b in raw_blocks:
        if not isinstance(b, dict):
            continue
        try:
            tok = int(b.get("tokens") or 0)
        except (TypeError, ValueError):
            tok = 0
        try:
            before = int(b.get("tokens_before") or 0)
        except (TypeError, ValueError):
            before = 0
        stt = str(b.get("status") or "kept")
        cut_pct = (round((before - tok) * 100.0 / before, 1)
                   if (stt == "truncated" and before > tok) else None)
        blocks.append({
            "label": str(b.get("label") or "?"),
            "priority": b.get("priority"),
            "tokens": tok,
            "before": before or tok,
            "status": stt,
            "cut_pct": cut_pct,
            "preview": _clip_text(str(b.get("preview") or ""), 400),
        })
    try:
        bg_limit = int(bg_budget.get("limit") or 0)
    except (TypeError, ValueError):
        bg_limit = 0
    if not bg_limit:
        try:
            from .protocols import BACKGROUND_BUDGET_TOKENS
            bg_limit = int(BACKGROUND_BUDGET_TOKENS)
        except Exception:  # pragma: no cover
            bg_limit = 0
    try:
        bg_used = int(bg_budget.get("used") or 0)
    except (TypeError, ValueError):
        bg_used = 0
    if not bg_used and blocks:
        bg_used = sum(b["tokens"] for b in blocks)
    kept = sum(1 for b in blocks if b["status"] == "kept")
    truncated = [b for b in blocks if b["status"] == "truncated"]
    dropped = [b for b in blocks if b["status"] == "dropped"]
    # 这两个要在 `background` 之前算：`injected` 判据要用 bg_labels
    marks = dg.get("memory_marks") if isinstance(dg.get("memory_marks"), dict) else {}
    sys_sections = [str(x) for x in (dg.get("system_sections") or [])][:40]
    bg_labels = [str(x) for x in (dg.get("user_bg_labels") or [])][:20]
    background = {
        # 这一轮到底有没有背景材料参与。**没有时必须与「用了 0 元」分开**：
        # 实测 4/6 条 trace 是空跑（没有 backgrounds 字段），账本若显示
        # 「0/1100（0%）」会让运营者以为预算很宽松，实际是这一轮压根没注入。
        "injected": bool(blocks or bg_labels),
        # 预算与逐块命运有没有采到。`injected` 与 `measured` 是两件事：
        # 可能注入了材料却没采到预算（id 246~251 实测），也可能两者都缺。
        "measured": bool(bg_budget or raw_blocks),
        "limit": bg_limit,
        "used": bg_used,
        "left": (max(0, bg_limit - bg_used) if bg_limit else None),
        "used_pct": (round(bg_used * 100.0 / bg_limit, 1) if bg_limit else None),
        "protected_overflow": bool(bg_budget.get("protected_overflow")),
        "evidence_reserve_used": bool(bg_budget.get("evidence_reserve_used")),
        "gap_notice": bool(bg.get("gap_notice")),
        "kept": kept, "truncated": len(truncated), "dropped": len(dropped),
        "blocks": blocks,
        "doc": ("背景材料（关系档案/记忆/自忆/悬着的事…）共用这一份预算，"
                "按优先级淘汰：预算不够就先裁块、再整块扔掉"),
    }
    # ── 预警 ──
    #
    # ## 只报「真的丢了东西」，不报「用量高」
    #
    # 第一版这里有一条「背景预算 ≥90% → warn」。2026-10-25 端到端实测立刻
    # 看出它是噪音：`fit_backgrounds` 的设计就是**按优先级把预算填满**
    # （实测连续两轮 1033/1100、992/1100），所以它每轮都会响。
    # 每轮都响的预警等于没有预警——运营者会学会无视整个面板（和刚才
    # `tool_many` 那条同一个错）。
    #
    # 保留的三条都对应**可指认的损失**：有块被裁 / 有块被淘汰 / 注入链断了。
    # 预算用量仍然显示（账本顶部），只是不弹预警——看得见，但不吵。
    warnings: list[dict] = []
    budget_note = (f"，背景预算 {bg_used}/{bg_limit}t 已满"
                   if (bg_limit and bg_used and bg_used >= bg_limit * 0.9)
                   else "")
    if truncated:
        names = "、".join(f"『{b['label']}』{b['before']}→{b['tokens']}t"
                          for b in truncated)
        warnings.append({
            "level": "warn", "key": "bg_truncated",
            "text": f"{len(truncated)} 块材料被裁，只剩开头：{names}{budget_note}",
            "evidence": "backgrounds.blocks[].status=truncated",
        })
    if dropped:
        names = "、".join(f"『{b['label']}』" for b in dropped)
        warnings.append({
            "level": "warn", "key": "bg_dropped",
            "text": f"{len(dropped)} 块被整块淘汰，她这轮完全看不到：{names}{budget_note}",
            "evidence": "backgrounds.blocks[].status=dropped",
        })
    if dg and not blocks and not bg_labels:
        warnings.append({
            "level": "error", "key": "bg_missing",
            "text": "有 prompt 摘要，但没有任何背景材料进 prompt——注入链可能整条断了",
            "evidence": "backgrounds.blocks 为空且 user_bg_labels 为空",
        })
    # ⚠ 假绿防线：**有条目、没分账**时不能说「材料齐、预算够」。
    # 2026-10-25 实测：19:06~19:40 那 6 条 trace 有 `system_chars`/`user_chars`/
    # `user_bg_labels`（说明摘要采到了），却没有 `token_sections` 和 `token_total`
    # （`_last_token_report` 那一刻是 None）。账本会显示「context.used=0」再判 ok，
    # 等于告诉运营者「这轮没花 token」。缺数据就说缺数据（#37 假绿）。
    partial_ledger = bool(dg) and not any(s["in_context"] for s in sections)
    if partial_ledger:
        warnings.append({
            "level": "warn", "key": "ledger_partial",
            "text": ("四段 token 账本没采到（只有 prompt 结构摘要）——"
                     "这一轮花了多少、花在哪都看不到；是采集缺口，不是没花钱"),
            "evidence": "prompt_digest 缺 token_sections/token_total",
        })
    if context["over"]:
        warnings.append({
            "level": "warn", "key": "ctx_over",
            "text": (f"三段合计 {ctx_used}t 超过上下文预算 {ctx_budget}t，"
                     f"系统提示词已被按句边界裁过"),
            "evidence": f"token_sections 非 tools 段之和={ctx_used}",
        })
    if not dg:
        verdict = "skip"
    elif any(w["level"] == "error" for w in warnings):
        verdict = "error"
    elif warnings:
        verdict = "warn"
    else:
        verdict = "ok"
    if verdict == "skip":
        head = "这一回合没有 prompt 账本（痕迹功能 2026-10-24 才上线）"
    elif verdict == "error":
        head = "注入链有问题，她这轮可能什么都没拿到"
    elif partial_ledger:
        # 采集缺口优先于「打折」措辞：运营者要先知道「这个数不准」，
        # 再谈「数有多大」。顺序反了，他会拿一个残缺的数去做决定。
        head = "账本不全——这一轮花了多少没采到"
    elif verdict == "ok":
        head = "材料齐、预算够"
    else:
        head = "没有故障，但有材料被打折"
    return {
        "sections": sections,
        "context": context,
        "measured_total": measured_total,
        "background": background,
        "partial": partial_ledger,
        "system_sections": sys_sections,
        "user_bg_labels": bg_labels,
        "memory_marks": {
            "corrected": int(marks.get("corrected") or 0),
            "uncertain": int(marks.get("uncertain") or 0),
        },
        "history_count": int(dg.get("history_count") or 0),
        "total_chars": total_chars,
        "warnings": warnings,
        "verdict": verdict,
        "headline": head,
    }


def _send_result(message_id: int = 1):
    """构造一个「假装发成功」的 SendResult。

    延迟导入：agent/ 层不应该在 import 期就依赖 onebot/ 层（tools/ 里单测
    只加载 agent 时不希望拉进整条 onebot 依赖链）。拿不到真类型时退化为
    一个 bool 为真、带 delivered 属性的鸭子对象——handler 里对发送结果的
    判断都能过。
    """
    try:
        from onebot.ws_client import SendResult
        return SendResult(True, True, message_id=message_id)
    except Exception:  # pragma: no cover - 仅在裁剪过的环境里发生
        class _Duck:
            ok = True
            delivered = True
            error = ""
            retryable = False
            uncertain = False

            def __bool__(self):
                return True

        return _Duck()


class _SendCapture:
    """在一次会客厅回合期间，截获糖糖「发出去」的内容。

    实现方式是替换 `napcat` 实例上的两个发送方法（实例属性遮蔽类方法）——
    `MessageHandler` 和 `ReplyPipeline` 都通过 `self.napcat.send_*` 调用，
    所以两条路径都会经过这里。

    **只在该回合的 contextvar 存在时才截获**：真实 QQ 消息即使在同一时刻
    被处理，发送也会原样转发出去。
    """

    def __init__(self, napcat):
        self.napcat = napcat
        self.private: list[str] = []
        self.group: list[tuple[str, str]] = []
        self._orig_private: Callable | None = None
        self._orig_group: Callable | None = None
        self._mid = int(time.time()) % 100000

    def __enter__(self):
        self._orig_private = self.napcat.send_private_message
        self._orig_group = self.napcat.send_group_message
        self.napcat.send_private_message = self._patched_private
        self.napcat.send_group_message = self._patched_group
        return self

    def __exit__(self, *exc):
        try:
            self.napcat.send_private_message = self._orig_private
            self.napcat.send_group_message = self._orig_group
        except Exception:
            logger.warning("会客厅：恢复发送方法失败", exc_info=True)
        return False

    def _next_id(self) -> int:
        self._mid += 1
        return self._mid

    async def _patched_private(self, user_id, message, *args, **kwargs):
        if _TURN_CTX.get() is None:
            return await self._orig_private(user_id, message, *args, **kwargs)
        self.private.append(str(message))
        logger.info("🏠 [会客厅] 截获私聊内容 %s 字", len(str(message)))
        return _send_result(self._next_id())

    async def _patched_group(self, group_id, message, *args, **kwargs):
        if _TURN_CTX.get() is None:
            return await self._orig_group(group_id, message, *args, **kwargs)
        self.group.append((str(group_id), str(message)))
        logger.info("🏠 [会客厅] 截获群 %s 内容 %s 字", group_id, len(str(message)))
        return _send_result(self._next_id())

    def text_reply(self) -> str:
        """把分段发送拼回一条可读回复。"""
        parts = [p.strip() for p in self.private if p and p.strip()]
        return "\n".join(parts)


class ConsoleApi:
    """会客厅的本地 HTTP 端点。由 `TangTang.start()` 创建并 start()。"""

    def __init__(self, handler, loop: asyncio.AbstractEventLoop, *,
                 host: str = "127.0.0.1", port: int = 8765,
                 guest_qq: str | None = None):
        self.handler = handler
        self.loop = loop
        self.host = host
        self.port = int(port)
        self.guest_qq = str(guest_qq or getattr(handler, "owner_qq", "") or "")
        self.store = ConsoleStore()
        self.started_at = time.time()
        self.last_turn: dict | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        #: 会客厅回合串行化——两个回合同时跑会抢同一份短期记忆窗口
        self._turn_lock: asyncio.Lock | None = None

    # ── 生命周期 ──────────────────────────────────────────────────
    def start(self) -> bool:
        """起 HTTP 线程。端口被占 / 异常都返回 False（不拖垮 bot 启动）。"""
        api = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "TangTangConsole/1.0"

            def log_message(self, fmt, *args):  # 静音默认访问日志
                logger.debug("会客厅 HTTP: " + fmt, *args)

            # ── 工具 ──
            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0].strip()
                return host in ("127.0.0.1", "localhost", "")

            def _json(self, obj, status: int = 200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _bytes(self, data: bytes, ctype: str, status: int = 200):
                """发二进制（图片/语音）。媒体不走 JSON——base64 要多吃 33% 体积。"""
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    n = 0
                if n <= 0:
                    return {}
                if n > MAX_BODY:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(n)
                try:
                    got = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ValueError("请求体不是合法 JSON")
                return got if isinstance(got, dict) else {}

            def _guard(self, fn):
                if not self._host_ok():
                    self._json({"error": "非法 Host"}, 403)
                    return
                try:
                    fn()
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                except RuntimeError as e:
                    self._json({"error": str(e)}, 409)
                except Exception as e:
                    logger.error("会客厅端点异常: %s\n%s", e, traceback.format_exc())
                    self._json({"error": f"内部错误: {e}"}, 500)

            # ── 路由 ──
            def do_GET(self):
                self._guard(self._route_get)

            def do_POST(self):
                self._guard(self._route_post)

            def do_DELETE(self):
                self._guard(self._route_delete)

            def _route_get(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                if path == "/health":
                    self._json(api.health())
                elif path == "/snapshot":
                    self._json(api.state_snapshot())
                elif path == "/sessions":
                    self._json({"sessions": api.store.list_sessions()})
                elif path == "/messages":
                    sid = (query.get("session_id") or [""])[0]
                    if not sid:
                        raise ValueError("缺少 session_id")
                    limit = int((query.get("limit") or ["200"])[0] or 200)
                    self._json({"messages": api.store.list_messages(sid, limit)})
                elif path == "/sources":
                    self._json(api.sources())
                elif path == "/feed":
                    self._json(api.feed(
                        scope=(query.get("scope") or [""])[0],
                        after_id=int((query.get("after_id") or ["0"])[0] or 0),
                        limit=int((query.get("limit") or ["100"])[0] or 100),
                    ))
                elif path == "/traces":
                    scope_id = (query.get("scope_id") or [""])[0]
                    limit = int((query.get("limit") or ["50"])[0] or 50)
                    self._json({"traces": api.turn_traces(scope_id, limit)})
                elif path == "/trace":
                    self._json({"trace": api.turn_trace(
                        run_id=(query.get("run_id") or [""])[0],
                        event_key=(query.get("event_key") or [""])[0],
                    )})
                elif path == "/replay":
                    self._json({"replay": api.turn_replay(
                        event_key=(query.get("event_key") or [""])[0])})
                elif path == "/groups":
                    self._json(api.groups())
                elif path == "/memory-graph":
                    try:
                        limit = int((query.get("limit") or ["40"])[0] or 40)
                    except ValueError:
                        limit = 40
                    self._json(api.memory_graph(
                        center=(query.get("center") or [""])[0],
                        limit_people=limit))
                elif path == "/memory-profile":
                    self._json(api.memory_profile(
                        qq_id=(query.get("qq") or [""])[0]))
                elif path == "/memory-history":
                    self._json(api.memory_history(
                        qq_id=(query.get("qq") or [""])[0]))
                elif path == "/memory-revisions":
                    try:
                        limit = int((query.get("limit") or ["200"])[0] or 200)
                    except ValueError:
                        limit = 200
                    self._json(api.memory_revisions(
                        qq_id=(query.get("qq") or [""])[0], limit=limit))
                elif path == "/search":
                    q = (query.get("q") or [""])[0]
                    try:
                        limit = int((query.get("limit") or ["40"])[0] or 40)
                    except ValueError:
                        limit = 40
                    self._json(api.search(q, limit))
                elif path == "/media":
                    # 媒体不走 _guard 的统一错误格式：前端要能区分「过期了」
                    # 和「出错了」——过期是常态（QQ 链接几小时失效）。
                    try:
                        data, ctype = api.media(
                            scope=(query.get("scope") or [""])[0],
                            msg_id=int((query.get("id") or ["0"])[0] or 0),
                            index=int((query.get("n") or ["0"])[0] or 0),
                        )
                    except FileNotFoundError:
                        self._json({"error": "媒体已过期"}, 410)
                        return
                    self._bytes(data, ctype)
                else:
                    self._json({"error": "未知路径"}, 404)

            def _route_post(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._body()
                if path == "/sessions":
                    self._json(api.store.create_session(body.get("title") or ""))
                elif path == "/say":
                    self._json(api.say(_bare_scope(body.get("session_id") or ""),
                                       body.get("text") or ""))
                elif path == "/turn":
                    self._json(api.turn(_bare_scope(body.get("session_id") or "")))
                elif path == "/send":
                    self._json(api.send(body.get("scope") or "",
                                        body.get("text") or ""))
                else:
                    self._json({"error": "未知路径"}, 404)

            def _route_delete(self):
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 2 and parts[0] == "sessions":
                    self._json({"ok": api.store.delete_session(parts[1])})
                else:
                    self._json({"error": "未知路径"}, 404)

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            logger.warning("🔇 会客厅接口未启动（端口 %s 不可用）：%s", self.port, e)
            return False
        self._server.daemon_threads = True
        # port=0 时由系统分配——把真实端口回填，便于测试与排查
        try:
            self.port = int(self._server.server_address[1])
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="tangtang-console",
            daemon=True)
        self._thread.start()
        logger.info("🏠 会客厅已开门：http://%s:%s（主人身份：%s）",
                    self.host, self.port, self.guest_qq or "<未设置>")
        return True

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self.store.close()
        logger.info("🏠 会客厅已关门")

    # ── 跨线程 → 主循环 ───────────────────────────────────────────
    def _run(self, coro, timeout: float = TURN_TIMEOUT):
        if self.loop is None or self.loop.is_closed():
            raise RuntimeError("糖糖本体未运行")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def _turn_lock_get(self) -> asyncio.Lock:
        """锁必须在 bot 的 loop 里创建/使用，所以延迟到回合内初始化。"""
        if self._turn_lock is None:
            self._turn_lock = asyncio.Lock()
        return self._turn_lock

    # ── 端点实现 ──────────────────────────────────────────────────
    def health(self) -> dict:
        return {
            "ok": self.handler is not None,
            "uptime_s": int(time.time() - self.started_at),
            "guest_qq": self.guest_qq,
            "port": self.port,
            "bot_qq": str(getattr(self.handler, "bot_qq", "") or ""),
        }

    def say(self, session_id: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        session = self.store.ensure_session(session_id)
        return self._run(self._say_async(session["id"], text))

    def turn(self, session_id: str) -> dict:
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        session = self.store.ensure_session(session_id)
        return self._run(self._turn_async(session["id"]))

    # ── 真身回合 ──────────────────────────────────────────────────
    async def _identity_turn(self, text: str, *, guest: str | None = None,
                             guard_key: str = "console") -> tuple[str, dict]:
        """跑一轮**真身**（生产路径），把她的输出截获下来，不写任何存档。

        调用方决定要不要落库：
        · `desktop:` 会话 → 写进会客厅库（.会客厅.db）
        · `private:` 镜像 → **不写**，因为 `handle_private_message` 自己已经把
          这条消息和她(在 chat_log 里的)回复都写进主库了，再写一份就重复了。
        """
        h = self.handler
        old_guest = self.guest_qq
        if guest:
            self.guest_qq = str(guest)
        cap = _SendCapture(h.napcat)
        started = time.time()
        msg = self.build_msg(text)
        # 本回合的稳定键：与 `turn_traces` / `chat_log` 用的是同一个值（`v2:…`）。
        # 前端拿它就能把这一轮的思维链全文换回来（`/api/console/trace?event_key=`）。
        # 2026-10-25 第 17 轮：本地会话原先只有 `reasoning_chars`（字数），
        # 没有键 → 「本地发消息只显示消息本身」，看不到她怎么想的。
        try:
            event_key = str(getattr(msg.get("_inbound_event"), "event_key", "") or "")
        except Exception:
            event_key = ""
        try:
            with cap, _turn_guard(guard_key):
                await h.handle_private_message(msg)
        finally:
            self.guest_qq = old_guest
        ms = int((time.time() - started) * 1000)
        reply = cap.text_reply()
        meta = self._turn_meta(ms)
        if event_key:
            meta["event_key"] = event_key
        if cap.group:
            meta["group_attempts"] = [
                {"group_id": g, "text": t} for g, t in cap.group]
        if not reply.strip():
            logger.warning("会客厅回合没有捕获到任何文本（可能只发了媒体）")
            reply = "（这一轮她没有发文字）"
        self.last_turn = meta
        return reply, meta

    async def _say_async(self, sid: str, text: str) -> dict:
        """走**生产路径**的一轮私聊：记忆/驱动力/情绪/关系场全部参与。"""
        async with self._turn_lock_get():
            self.store.append_message(sid, "user", text)
            try:
                reply, meta = await self._identity_turn(text, guard_key=sid)
            except Exception as e:
                logger.error("会客厅回合失败: %s\n%s", e, traceback.format_exc())
                err = f"（糖糖这轮没说话：{e}）"
                self.store.append_message(sid, "system", err)
                raise RuntimeError(err) from e
            self.store.append_message(sid, "tangtang", reply, meta)
            return {"reply": reply, "meta": meta, "session_id": sid,
                    "state": self.state_snapshot()}

    async def _turn_async(self, sid: str) -> dict:
        """自主回合：没有人在跟她说话，让她按自己此刻的状态开口。

        ⚠️ 这条路径**不是** QQ 对话路径的复制品——她没有「对方」这个锚点，
        所以这里显式用「人格 + 当前状态」组装提示词，并在提示里说明这是
        自主开口而不是回复。走 `_call_llm` 保留工具能力（她可能想唱歌/查东西）。
        """
        h = self.handler
        async with self._turn_lock_get():
            started = time.time()
            system_prompt = self._autonomous_system_prompt()
            user_message = self._autonomous_user_message()
            try:
                reply = await h._call_llm(system_prompt, user_message)
            except Exception as e:
                logger.error("会客厅自主回合失败: %s\n%s", e, traceback.format_exc())
                err = f"（自主回合失败：{e}）"
                self.store.append_message(sid, "system", err)
                raise RuntimeError(err) from e
            ms = int((time.time() - started) * 1000)
            reply = (reply or "").strip()
            if not reply:
                reply = "（她没有说话）"
            meta = self._turn_meta(ms)
            meta["autonomous"] = True
            self.store.append_message(sid, "tangtang", reply, meta)
            self.last_turn = meta
            return {"reply": reply, "meta": meta, "session_id": sid,
                    "state": self.state_snapshot()}

    def _turn_meta(self, ms: int) -> dict:
        """本轮的可观测信息。全部 best-effort——统计缺了也不能让回合失败。"""
        model, provider = "", ""
        try:
            llm = (self.handler.config.get("llm") or {})
            active = llm.get("active_model")
            profiles = llm.get("models") or []
            prof = next((p for p in profiles
                         if isinstance(p, dict) and p.get("id") == active), None)
            prof = prof or (profiles[0] if profiles else {}) or {}
            model = prof.get("model") or llm.get("model") or ""
            provider = prof.get("provider") or llm.get("provider") or ""
        except Exception:
            pass

        # token 数是**估算**（handler 的 ContextBuilder 按字符折算），不是 API 回报的
        tokens = 0
        try:
            report = getattr(self.handler, "_last_token_report", None)
            if report is not None:
                tokens = int(getattr(report, "total", 0) or 0)
        except Exception:
            pass

        diag = {}
        try:
            diag = getattr(self.handler, "_last_llm_diagnostic", None) or {}
        except Exception:
            diag = {}

        return {
            "ms": ms,
            "model": model,
            "provider": provider,
            "tokens": tokens,
            "tools": [],
            "finish_reason": str(diag.get("finish_reason") or ""),
            "reasoning_chars": int(diag.get("reasoning_chars") or 0),
        }

    def build_msg(self, text: str) -> dict:
        """构造一条「看起来像 QQ 私聊」的消息。

        **形态必须与 `onebot/ws_client.py::_handle_private` 完全一致**——那是
        入站的唯一规范化边界：`user_id` 是 **str**（不是 int），字段名是
        `type` 而不是 `post_type/message_type`，没有 `sender`/`self_id`。
        早期版本按 OneBot 原始事件猜了一份，结果 `memory._qq_suffix()` 拿到 int
        调 `len()` 直接 TypeError（真实链路早就归一化成 str 了）。

        message_id 用纳秒取余的**负数**：OneBot int32 负 ID 合法，且不会和真实
        消息 ID 撞车——`inbound_message_already_persisted` 靠 (time, message_id)
        判重投，撞车会导致这条消息被当成重放静默丢弃。
        """
        h = self.handler
        guest = str(self.guest_qq or "")
        nick = ""
        try:
            person = h.memory.get_or_create_person(guest) or {}
            nick = str(person.get("nickname") or "")
        except Exception:
            pass
        nick = nick or "主人"
        now = int(time.time())
        mid = -(int(time.time_ns() % 2_000_000_000) + 1)
        msg = {
            "type": "private",
            "user_id": guest,
            "nickname": nick,
            "message": text,
            "raw_message": text,
            "message_id": mid,
            "time": now,
        }
        # 与 ws_client 同一个边界函数：下游靠 _inbound_event 拿证据/判重放
        try:
            from .inbound_event import normalize_inbound_event
            msg["_inbound_event"] = normalize_inbound_event("private", msg)
        except Exception:
            logger.debug("会客厅：normalize_inbound_event 失败", exc_info=True)
        # 来源标记：日志/下游可据此分辨这不是 QQ 真消息
        msg["_console"] = True
        # ⚠️ 必须跳过批处理。`handle_private_message` 里
        # `batcher.enqueue_private(msg)` 命中后会**立即 return**，真正的
        # 回复在批处理定时器里异步发生——那时会客厅的发送截获器早就退出了，
        # 表现为「0.1 秒返回、一个字都没捕获到」。
        # 会客厅是一问一答，本来就不需要合并连续消息。
        msg["_batched"] = True
        return msg

    # ── 自主回合的提示词 ──────────────────────────────────────────
    def _relationship_for_guest(self):
        h = self.handler
        try:
            person = h.memory.get_or_create_person(self.guest_qq) or {}
            return h._determine_relationship(person)
        except Exception:
            from .personality import Relationship
            return Relationship.STRANGER

    def _autonomous_system_prompt(self) -> str:
        h = self.handler
        memories = ""
        intimacy = 0
        try:
            person = h.memory.get_or_create_person(self.guest_qq) or {}
            intimacy = int(person.get("intimacy") or 0)
            memories = h.memory.format_compact_memories(
                self.guest_qq, include_profile=True) or ""
        except Exception:
            logger.debug("自主回合取记忆失败", exc_info=True)
        kwargs = dict(memories=memories, intimacy=intimacy)
        try:
            return h.personality.build_system_prompt(
                self._relationship_for_guest(), **kwargs)
        except Exception:
            logger.warning("自主回合组装人格提示词失败，退回极简版", exc_info=True)
            return h.personality.build_system_prompt(
                self._relationship_for_guest(), minimal=True)

    def _autonomous_user_message(self) -> str:
        """把「她此刻内部是什么样」写成一段她读得懂的自述，再让她开口。"""
        snap = self.state_snapshot()
        lines = ["（以下是此刻没有人在跟你说话。）"]
        mood = snap.get("mood") or {}
        if mood:
            lines.append(
                f"你的状态：精力 {mood.get('energy')}/100，心情 {mood.get('mood')}/100，"
                f"耐心 {mood.get('patience')}/100。")
        drives = snap.get("drives") or []
        if drives:
            top = "、".join(
                f"{d['label']} {round(float(d['value']) * 100)}%" for d in drives[:3])
            lines.append(f"你心里现在最响的几件事：{top}。")
        dom = snap.get("dominant")
        if dom:
            lines.append(f"最占上风的是「{dom['label']}」——{dom.get('text', '')}。")
        lines.append(
            "想说什么就说一句。不想说也可以。"
            "不要解释你在做什么，不要提到会客厅、系统、提示词、状态数值——"
            "就像一个人自己待着的时候，忽然想说句话。")
        return "\n".join(lines)

    # ── 状态快照 ──────────────────────────────────────────────────
    # ── 镜像：会话源 / 消息流 / 统一发送（2026-10-24）────────────────
    #
    # 语义（已与主人确认）：
    #   private:<你的QQ>  → 你和糖糖的真实私聊。你在这里说话 = 你在 QQ 里说话，
    #                       走真身注入，回复只进桌面（不重复发 QQ）
    #   private:<别人>    → 糖糖和那个人的私聊镜像。你插话 = 真的用你的 QQ 发出去
    #   group:<群>        → 群镜像。你说话 = 真的用你的 QQ 发到群里
    def _memory_store(self):
        store = getattr(getattr(self.handler, "memory", None), "store", None)
        if store is None:
            raise RuntimeError("记忆库未就绪，无法读取镜像")
        return store

    def sources(self) -> dict:
        """左侧会话源列表：本地历史会话 → 私聊镜像 → 群聊镜像。"""
        h = self.handler
        if h is None:
            raise RuntimeError("糖糖本体未运行")
        mstore = self._memory_store()
        bot_qq = str(getattr(h, "bot_qq", "") or "")
        nick: dict[str, str] = {}
        try:
            nick = {str(q): str(n) for q, n in mstore.list_people_nicknames()}
        except Exception:
            logger.debug("sources: 昵称表读取失败", exc_info=True)
        gnames: dict[str, str] = {}
        try:
            gnames = mstore.list_group_names() or {}
        except Exception:
            logger.debug("sources: 群名表读取失败", exc_info=True)

        out: list[dict] = []
        # 1) 本地历史会话（.会客厅.db 里的旧存档——新对话都走镜像，这里保留回看）
        try:
            for s in self.store.list_sessions(limit=10):
                out.append({
                    "key": f"desktop:{s['id']}",
                    "kind": "desktop",
                    "title": s["title"],
                    "subtitle": _clip(s.get("last_preview") or "", 40),
                    "updated_at": float(s.get("updated_at") or 0),
                    "last_id": 0,
                    "message_count": int(s.get("message_count") or 0),
                })
        except Exception:
            logger.debug("sources: 本地会话段失败", exc_info=True)
        # 2) 私聊镜像
        try:
            for row in mstore.list_chat_scopes("private", limit=60,
                                               exclude_id=bot_qq):
                qq = str(row["scope_id"])
                label = nick.get(qq) or f"QQ{qq[-4:] if len(qq) > 4 else qq}"
                if qq == str(getattr(h, "owner_qq", "")):
                    label = f"{label}（和你）"
                out.append({
                    "key": f"private:{qq}",
                    "kind": "private",
                    "title": label,
                    "subtitle": _clip(row["last_message"], 40),
                    "updated_at": _ts(row["last_time"]),
                    "last_id": int(row["last_id"]),
                    "message_count": int(row["message_count"]),
                })
        except Exception:
            logger.debug("sources: 私聊段失败", exc_info=True)
        # 3) 群聊镜像
        try:
            for row in mstore.list_chat_scopes("group", limit=60):
                gid = str(row["scope_id"])
                out.append({
                    "key": f"group:{gid}",
                    "kind": "group",
                    "title": gnames.get(gid) or f"群{gid}",
                    "subtitle": _clip(row["last_message"], 40),
                    "updated_at": _ts(row["last_time"]),
                    "last_id": int(row["last_id"]),
                    "message_count": int(row["message_count"]),
                })
        except Exception:
            logger.debug("sources: 群聊段失败", exc_info=True)
        # 4) 独白频道（只读）——她没发出去的内在活动
        #
        # 这是唯一一个**不对应任何 QQ 会话**的源：日记是她自己写的，
        # 内心消化是她有欲望但选择不说时留下的。两者都只存在于本机，
        # 不经过 QQ，所以也就没有「未读」和「发言输入」。见 `_monologue_entries`。
        try:
            entries = self._monologue_entries()
            if entries:
                newest = entries[-1]
                out.append({
                    "key": "monologue:journal",
                    "kind": "monologue",
                    "title": "独白",
                    "subtitle": _clip(newest["content"], 40),
                    "updated_at": newest["ts"],
                    "last_id": 0,
                    "message_count": len(entries),
                })
        except Exception:
            logger.debug("sources: 独白段失败", exc_info=True)
        return {"sources": out}

    # ── 独白频道 ──────────────────────────────────────────────────
    def _monologue_entries(self) -> list[dict]:
        """她的内在活动时间线：日记 + 内心消化，按时间**正序**。

        ⚠️ 合成 id 的稳定性：id 用「排序后的序号」（1..N），所以新条目只会
        追加在**末尾**——只要排序键单调（journal.id / created_at），已有条目的
        id 就不会漂移，前端拿 id 做增量去重才安全。别改成 `ORDER BY 时间倒序`
        再取序号，那样每写一条日记全部 id 都会变。
        """
        h = self.handler
        if h is None:
            raise RuntimeError("糖糖本体未运行")
        mstore = self._memory_store()
        raw: list[dict] = []
        # 1) 日记（永久保留，最纯正的独白）
        try:
            for row in mstore.list_journal(limit=200):
                text = str(row.get("entry") or "").strip()
                if not text:
                    continue
                when = str(row.get("created_at") or "").strip()
                date = str(row.get("date") or "").strip()
                raw.append({
                    "sort": (when or f"{date} 00:00:00", int(row.get("id") or 0)),
                    "ts": _ts(when),
                    "content": text,
                    "entry_kind": "journal",
                    "mood": str(row.get("mood") or ""),
                    "heading": (f"{date[5:]} 的日记" if len(date) >= 10 else "日记"),
                })
        except Exception:
            logger.debug("独白: 日记读取失败", exc_info=True)
        # 2) 内心消化（体验缓冲里的 internal_digest，会被反思循环消费掉）
        try:
            buf = list(getattr(getattr(h, "self_state", None),
                               "_experience_buffer", []) or [])
            for exp in buf:
                if not isinstance(exp, dict) or exp.get("type") != "internal_digest":
                    continue
                diary = str(exp.get("diary") or "").strip()
                if not diary:
                    continue
                when = str(exp.get("time") or "").strip()
                raw.append({
                    "sort": (when or "1970-01-01 00:00:00", 0),
                    "ts": _ts(when),
                    "content": diary,
                    "entry_kind": "digest",
                    "mood": "",
                    "heading": "内心消化 · " + _drive_label(exp.get("drive")),
                })
        except Exception:
            logger.debug("独白: 内心消化读取失败", exc_info=True)
        raw.sort(key=lambda e: e["sort"])
        out = []
        for i, e in enumerate(raw, start=1):
            out.append({
                "id": i,
                "role": "tangtang",
                "speaker": "糖糖",
                "content": e["content"],
                "ts": e["ts"],
                "trace": None,
                "event_key": "",
                "media": [],
                "entry_kind": e["entry_kind"],
                "mood": e["mood"],
                "heading": e["heading"],
            })
        return out

    def _monologue_feed(self, after_id: int = 0, limit: int = 100) -> dict:
        entries = self._monologue_entries()
        try:
            after = max(0, int(after_id or 0))
        except (TypeError, ValueError):
            after = 0
        fresh = [e for e in entries if e["id"] > after]
        limit = max(1, min(int(limit or 100), 500))
        if len(fresh) > limit:
            fresh = fresh[-limit:]
        return {"messages": fresh, "has_more": False}

    # ── 会话搜索（搜正文，不是搜标题）────────────────────────────
    def search(self, keyword: str, limit: int = 40) -> dict:
        """在全部 QQ 镜像的**消息正文**里搜关键词。"""
        h = self.handler
        if h is None:
            raise RuntimeError("糖糖本体未运行")
        text = str(keyword or "").strip()
        if not text:
            return {"hits": [], "total": 0, "truncated": False}
        mstore = self._memory_store()
        rows, total = mstore.search_chat(text, limit)
        nick: dict[str, str] = {}
        try:
            nick = {str(q): str(n) for q, n in mstore.list_people_nicknames()}
        except Exception:
            logger.debug("search: 昵称表读取失败", exc_info=True)
        gnames: dict[str, str] = {}
        try:
            gnames = mstore.list_group_names() or {}
        except Exception:
            logger.debug("search: 群名表读取失败", exc_info=True)
        bot_qq = str(getattr(h, "bot_qq", "") or "")
        owner_qq = str(getattr(h, "owner_qq", "") or "")
        hits: list[dict] = []
        for r in rows:
            msg = _clean_text(str(r.get("message") or ""))
            span = _find_span(msg, text)
            if span is None:
                continue
            gid = str(r.get("group_id") or "")
            qq = str(r.get("qq_id") or "")
            kind = "group" if gid else "private"
            if kind == "group":
                scope = f"group:{gid}"
                title = gnames.get(gid) or f"群{gid}"
            else:
                scope = f"private:{qq}"
                label = nick.get(qq) or f"QQ{qq[-4:] if len(qq) > 4 else qq}"
                title = f"{label}（和你）" if qq == owner_qq else label
            is_bot = bool(r.get("is_bot_reply"))
            if r.get("speaker_qq"):
                speaker = "你（桌面代发）"
            elif is_bot:
                speaker = "糖糖"
            elif qq == bot_qq:
                speaker = "糖糖"
            else:
                speaker = str(r.get("nickname") or "") or f"QQ{qq}"
            start, length = span[1], span[2]
            hits.append({
                "scope": scope,
                "kind": kind,
                "title": title,
                "id": int(r.get("id") or 0),
                "role": "tangtang" if is_bot else "user",
                "speaker": speaker,
                "snippet": span[0],
                "match_start": start,
                "match_len": length,
                "ts": _ts(str(r.get("timestamp") or "")),
            })
        return {
            "hits": hits,
            "total": int(total),
            "truncated": int(total) > len(hits),
        }

    # ── 媒体（图片 / 语音）───────────────────────────────────────
    def _media_item(self, scope: str, msg_id: int, item: dict) -> dict:
        """把一条附件变成前端可直接用的描述（字节由 /media 端点现取）。

        `cached` 是**可用性的真相**：QQ 的临时链接几小时就失效，历史图片基本
        全都取不回来了。前端据此决定「渲染 <img> 去请求」还是「直接显示
        已过期占位」——否则打开一个 100 条的群会话会当场发出几十个注定 410
        的请求（2026-10-24 实测：51 张图只有 1 张能加载）。
        """
        cached = bool(item.get("local"))
        if not cached:
            cached = media_cache.find_cached(int(msg_id), int(item["index"])) is not None
        qs = urlencode({"scope": scope, "id": int(msg_id), "n": int(item["index"])})
        return {
            "type": item["type"],
            "name": item["name"],
            "duration": int(item.get("duration") or 0),
            "local": bool(item.get("local")),
            "cached": cached,
            "path": f"/api/console/media?{qs}",
        }

    def media(self, scope: str, msg_id: int, index: int = 0) -> tuple[bytes, str]:
        """取一条消息的第 index 个附件字节。

        ⚠️ QQ 的 media url 带 rkey，实测几小时后就 `download url has expired`
        （retcode -5503007）。所以这里先看 `media_cache` 里有没有落地的副本
        ——收消息时 `inbound_event` 已经预取过一份。彻底没有就抛
        FileNotFoundError，路由层返回 410，前端显示「媒体已过期」。
        """
        kind, _, sid = str(scope or "").partition(":")
        if kind not in ("private", "group"):
            raise ValueError(f"媒体只支持聊天镜像: {scope}")
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        try:
            mid = int(msg_id)
            idx = max(0, int(index or 0))
        except (TypeError, ValueError):
            raise ValueError("id / n 必须是整数")
        row = self._memory_store().get_chat_row(kind, sid, mid)
        if not row:
            raise ValueError("消息不存在")
        path = media_cache.resolve(
            mid, str(row.get("message") or ""), str(row.get("segments") or ""), idx)
        if path is None:
            raise FileNotFoundError("媒体已过期")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise FileNotFoundError(str(e))
        import mimetypes
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return data, ctype

    def feed(self, scope: str, after_id: int = 0, limit: int = 100) -> dict:
        """某会话的消息流。`after_id>0` 时是增量（轮询用）。"""
        scope = str(scope or "")
        kind, _, sid = scope.partition(":")
        try:
            after = max(0, int(after_id or 0))
        except (TypeError, ValueError):
            after = 0
        limit = max(1, min(int(limit or 100), 500))
        if kind == "monologue":
            return self._monologue_feed(after, limit)
        if kind == "desktop":
            # 本地存档：按会客厅库自己的 id 递增，映射成统一形状
            msgs = self.store.list_messages(sid, limit=limit)
            out = []
            for m in msgs:
                if m["id"] <= after:
                    continue
                role = "tangtang" if m["role"] == "tangtang" else "user"
                # 2026-10-25 第 17 轮：本地会话也要能看她「怎么想的」。
                # 回合元信息里有 `event_key` + `reasoning_chars`，投影成与 QQ 镜像
                # 同形状的 trace 摘要（见 _local_trace_from_meta 的说明）。
                trace = _local_trace_from_meta(m.get("meta"))
                out.append({
                    "id": int(m["id"]),
                    "role": role,
                    "speaker": "糖糖" if role == "tangtang"
                               else ("你" if m["role"] == "user" else "系统"),
                    "content": m["content"],
                    "ts": float(m["ts"] or 0),
                    "trace": trace,
                    "event_key": (trace or {}).get("event_key", ""),
                    # 本地存档没有 chat_log id，媒体端点取不到字节——给空数组，
                    # 免得前端渲染一堆点不开的破图。
                    "media": [],
                })
            return {"messages": out, "has_more": False}
        if kind not in ("private", "group"):
            raise ValueError(f"未知会话: {scope}")
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        msgs, more = self._memory_store().read_chat_feed(kind, sid, after, limit)
        for m in msgs:
            segs = str(m.pop("segments", "") or "")
            items = media_cache.summarize(str(m.get("content") or ""), segs)
            m["media"] = [self._media_item(scope, m["id"], it) for it in items]
        return {"messages": msgs, "has_more": more}

    def send(self, scope: str, text: str) -> dict:
        """统一发送入口——行为取决于会话类型（见本节顶部说明）。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        h = self.handler
        if h is None:
            raise RuntimeError("糖糖本体未运行")
        kind, _, sid = str(scope or "").partition(":")
        if kind == "desktop":
            res = self.say(sid, text)
            return {"ok": True, "mode": "local", "reply": res.get("reply") or ""}
        if kind == "private":
            if sid == str(getattr(h, "owner_qq", "")):
                # 和糖糖本人说话：注入真身，回复只进桌面
                reply, _meta = self._run(
                    self._identity_turn(text, guest=sid, guard_key="mirror"))
                return {"ok": True, "mode": "local", "reply": reply}
            return self._send_qq("private", sid, text)
        if kind == "group":
            return self._send_qq("group", sid, text)
        raise ValueError(f"未知会话: {scope}")

    def _send_qq(self, kind: str, target: str, text: str) -> dict:
        """真的用糖糖的号发出去。发完补一条 chat_log，镜像里才看得到自己说的话。

        ⚠️ 注意这是**以主人身份**在 QQ 里发言，但走的是糖糖的账号——之所以
        能这样：桌面端的会话对象就是糖糖在用的那个 QQ。发出去的内容对群里
        其他人来说来自糖糖，所以前端必须做二次确认（契约里已要求）。
        """
        h = self.handler
        nap = getattr(h, "napcat", None)
        if nap is None:
            raise RuntimeError("QQ 未连接，无法发送")

        async def _do():
            if kind == "private":
                return await nap.send_private_message(target, text)
            return await nap.send_group_message(target, text)

        result = self._run(_do(), timeout=30.0)
        ok = bool(getattr(result, "ok", result))
        if not ok:
            err = getattr(result, "error", "") or "发送失败"
            raise RuntimeError(f"QQ 发送失败: {err}")
        # 补进 chat_log——**必须补**：它是她的记忆来源（记忆提取 / history_query /
        # search_chat_history 全读它）。不补的话她在自己的世界里从没说过这句话，
        # 而主人明确要求「会客厅的对话要写进真实记忆」。
        #
        # 语义（2026-10-24 主人确认）：QQ 上别人看到的是**糖糖的号**在说话，
        # 所以记 `is_bot_reply=1` —— 她该当成自己说过，与外部事实一致；
        # `speaker_qq` 记真正打字的账号，镜像据此显示「你（桌面代发）」，
        # 而不是让界面冒充她本人。
        owner = str(getattr(h, "owner_qq", "") or "")
        token = f"{int(time.time() * 1000)}:{int(time.time_ns() % 1000000)}"
        row_id = 0
        try:
            store = self._memory_store()
            if kind == "private":
                row_id = int(store.insert_chat(
                    target, text, is_bot=True, speaker_qq=owner,
                    event_key=f"console:private:{target}:{token}") or 0)
            else:
                row_id = int(store.insert_chat(
                    str(getattr(h, "bot_qq", "") or ""), text,
                    group_id=target, is_bot=True, speaker_qq=owner,
                    event_key=f"console:group:{target}:{token}") or 0)
        except Exception:
            logger.warning("会客厅：发送成功但 chat_log 补记失败", exc_info=True)
        return {
            "ok": True, "mode": "qq",
            "message_id": int(getattr(result, "message_id", 0) or 0),
            "echo": {
                "id": row_id,
                "role": "tangtang",
                "speaker": "你（桌面代发）",
                "content": text,
                "ts": time.time(),
                "trace": None,
                "event_key": "",
            },
        }

    # ── 回合痕迹：「她这句话是怎么想出来的」────────────────────────
    # 数据在 memory.db 的 turn_traces 表（与 decision_runs 同键），不在会客厅库：
    # 它是 bot 的回合数据，与会客厅的「会话存档」不是一回事。
    def _trace_store(self):
        store = getattr(getattr(self.handler, "memory", None), "store", None)
        if store is None:
            raise RuntimeError("记忆库未就绪，无法读取回合痕迹")
        return store

    def turn_traces(self, scope_id: str = "", limit: int = 50) -> list[dict]:
        """某会话最近的回合痕迹（新→旧）。scope_id 空 = 全部会话。"""
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        return self._trace_store().query_turn_traces(scope_id, limit)

    def turn_trace(self, run_id: str = "", event_key: str = "") -> dict | None:
        """单条痕迹：优先 run_id；挂到 chat_log 那一行时用 event_key。"""
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        store = self._trace_store()
        if run_id:
            return store.get_turn_trace(run_id)
        if event_key:
            return store.get_turn_trace_by_event(event_key)
        return None

    def turn_replay(self, event_key: str = "") -> dict | None:
        """一条回复的完整回合回放：想了什么 → 做了什么 → 决定了什么 → 对方怎么反应。

        这四段数据本来散在三张表（`turn_traces` / `decision_runs` / `feedback`）
        和一个只读反查（`chat_log`），前端点开一条消息时一次拿全，省三次往返。

        **每一段都独立容错**：某一段读不出来时给 None，界面隐藏那一块——
        回放是观察窗口，不该因为一张表的小毛病整页塌掉。返回 None 表示
        「这一回合根本没留痕」（trace 功能 2026-10-24 才上线，更早的回合没有）。
        """
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        store = self._trace_store()
        key = str(event_key or "").strip()
        if not key:
            return None

        # 两个入口共用：前端可能传 bot 回复的 `delivery:<触发id>:<序号>`，
        # 也可能直接传触发消息的 `v2:…`。前者必须先解析出触发消息——因为
        # `turn_traces` 记的是**触发消息**的 event_key（实测 380 条 bot 回复
        # 与 turn_traces 直接 join 命中 0 条，就是这个原因）。
        row = None
        try:
            row = store.get_chat_by_event(key)
        except Exception:
            logger.debug("回放：chat_log 反查失败", exc_info=True)

        trace_key = key
        trigger = row
        if key.startswith("delivery:"):
            parts = key.split(":")
            trigger = None
            if len(parts) > 1 and parts[1].isdigit():
                try:
                    trigger = store.get_chat_by_id(int(parts[1]))
                except Exception:
                    logger.debug("回放：delivery 解析失败", exc_info=True)
            trace_key = str((trigger or {}).get("event_key") or "")

        trace = store.get_turn_trace_by_event(trace_key) if trace_key else None
        if trace is None:
            return None
        # 规范化成前端的 ReplayPayload 契约。store 的内部命名是 `tool_steps` /
        # `prompt_chars`（`/traces` 列表端点也在用），在这里做一层投影比改
        # 那个公共返回结构安全——也顺手把「思维链字符数」按**实际返回的文本**
        # 算，不让前端看到一个和正文对不上的数字。
        reasoning = str(trace.get("reasoning") or "")
        # 七段流程要用的原始字段：`trace` 下面会被覆盖成前端投影（丢掉 digest），
        # 所以先存下来（2026-10-25 第 17 轮 ④）。
        raw_digest = trace.get("prompt_digest")
        raw_prompt_chars = int(trace.get("prompt_chars") or 0)
        raw_steps = trace.get("tool_steps")
        steps: list[dict] = []
        for st in (raw_steps if isinstance(raw_steps, list) else []):
            if not isinstance(st, dict):
                continue
            try:
                ms = int(st.get("ms") or 0)
            except (TypeError, ValueError):
                ms = 0
            steps.append({
                "name": str(st.get("name") or ""),
                "args": str(st.get("args") or ""),
                "result": str(st.get("result") or ""),
                "ms": ms,
            })
        recorded_at = str(trace.get("recorded_at") or "")
        trace = {
            "run_id": str(trace.get("run_id") or ""),
            "model": str(trace.get("model") or ""),
            "reasoning": reasoning,
            "reasoning_chars": len(reasoning),
            "tools": steps,
            "duration_ms": int(trace.get("duration_ms") or 0),
            "recorded_at": recorded_at,
            "suspect": recorded_at < TRACE_TRUST_SINCE,
        }

        decision = None
        run_id = str(trace.get("run_id") or "")
        if run_id:
            try:
                decision = store.get_decision_run(run_id)
            except Exception:
                # `get_decision_run` 对 tool_calls 损坏是**抛错**而不是降级
                # （那是事实契约）。回放只是观察视图，吞掉给 None。
                logger.debug("回放：decision_run 读取失败", exc_info=True)

        feedback = None
        if row:
            # ⚠ 2026-10-25 第 17 轮 ④ 修：原来把**触发消息的正文**（对方说的那句）
            # 当成 `reply_text` 传进去，而 `find_feedback_for_turn` 的匹配条件是
            # 「两边文本有前缀关系」——对方的原话和她发出去的回复前缀几乎不可能
            # 相同，所以 `feedback` 长期匹配不到，回放里「对方怎么反应」和第 7 段
            # 一直是空的（不是"还没反应"，是**永远查不到**）。
            # 正确输入：她这次真正发出去的话（chat_log 里 `delivery:<触发id>:%`）。
            try:
                reply_text = store.get_bot_reply_text_for_trigger(
                    int(row.get("id") or 0))
            except Exception:
                logger.debug("回放：取她的回复正文失败", exc_info=True)
                reply_text = ""
            try:
                feedback = store.find_feedback_for_turn(
                    str((trigger or row).get("qq_id") or ""),
                    str(row.get("timestamp") or ""),
                    reply_text or str(row.get("message") or ""),
                )
            except Exception:
                logger.debug("回放：feedback 匹配失败", exc_info=True)

        message = None
        if row:
            group_id = str(row.get("group_id") or "")
            message = {
                "id": int(row.get("id") or 0),
                "content": str(row.get("message") or ""),
                "timestamp": str(row.get("timestamp") or ""),
                # 三个身份不能混：桌面代发 / 糖糖本人 / 对方。之前只判
                # speaker_qq，导致「直接传触发消息的 v2 键」时把对方说的话
                # 标成「糖糖」——界面会撒谎。
                "speaker": (
                    "你（桌面代发）" if row.get("speaker_qq")
                    else ("糖糖" if row.get("is_bot_reply")
                          else (str(row.get("qq_id") or "") or "对方"))
                ),
                "scope": (f"group:{group_id}" if group_id
                          else f"private:{row.get('qq_id') or ''}"),
            }
        # 2026-10-25 第 17 轮 ④：把这一回合拆成七段流程，给「她怎么想的」
        # 做时间轴。**加在现有四段旁边而不是新端点**——前端已经在调 /replay，
        # 多给一个字段省一次往返，老字段一个不动（向后兼容）。
        flow = _build_turn_flow(
            message=message, digest=raw_digest, prompt_chars=raw_prompt_chars,
            trace=trace, decision=decision, feedback=feedback,
            # 她这一轮真正发出去的话（第 7 段原来只看 feedback，多数回合是空的）
            reply_text=reply_text)
        return {"message": message, "trace": trace, "decision": decision,
                "feedback": feedback, "flow": flow,
                # 整条链的体检结论（前端顶部那条横幅）。
                # 放在返回里而不是让前端数状态值——判据只有一处（第 47 条教训）。
                "flow_summary": _summarize_turn_flow(flow),
                # 2026-10-25 第 47 轮：主人「把必要的参数放到前端，比如各环节的
                # token 预算」——这些数一直在 `prompt_digest` 里躺着，从来没有出口。
                # 现在一次带出去：账本（四段 token / 背景预算 / 每块命运 / 预警）。
                "prompt_ledger": _build_prompt_ledger(
                    digest=raw_digest, prompt_chars=raw_prompt_chars),
                # 「她做了什么」的可读版：工具分类 + 空手判定 + 她的决策理由。
                # 输入是下面规范化过的 `steps`（不是原始 trace）——同一份数据，
                # 与第 6 站显示的是同一批，不会出现两处口径不一致。
                "tool_effects": _build_tool_effects(steps)}

    #: `scenarios/*.yaml` 的目录指纹 → 一个**独立**的 ScenarioManager。
    #: 为什么不直接用 `self.handler.scenarios`：它在 bot 启动时加载一次，
    #: 改完 yaml 不重载就永远是旧值（2026-10-26「群管理页显示旧场景名」的根因）。
    _SCEN_FRESH_CACHE: dict = {"key": None, "mgr": None}

    def _fresh_scenarios(self):
        """返回一个与磁盘当前内容一致的场景管理器（文件没变时复用）。"""
        live = getattr(self.handler, "scenarios", None)
        if live is None:
            return None
        base = getattr(live, "_dir", None)
        if base is None:
            return live
        try:
            files = sorted(Path(base).glob("*.yaml"))
            key = tuple((f.name, f.stat().st_mtime_ns) for f in files)
        except OSError:
            return live
        cache = self._SCEN_FRESH_CACHE
        if cache["key"] == key and cache["mgr"] is not None:
            return cache["mgr"]
        try:
            mgr = type(live)(base)
        except Exception:
            logger.debug("场景清单重建失败，退回进程内快照", exc_info=True)
            return live
        cache["key"] = key
        cache["mgr"] = mgr
        return mgr

    def _norm_scenario(self, value: str) -> str:
        """把配置里存的场景值归一成**场景清单里的 display 全名**。

        历史包袱三件套，少认一个界面上就会显示「未在场景清单中」：
          · 全局 `scenario_targets` 存 display（`亲密模式`，**不带 emoji**）
          · 群级 `groups.<gid>.scenario` 存 name（`seductive`）
          · 而 `scenarios/*.yaml` 的 display 带 emoji 前缀（`❤️ 亲密模式`）
        实测：配置里 `2524950083: 亲密模式` 在界面上被标成「未在场景清单中」，
        而那个场景其实好好的。用 `ScenarioManager.get()` 的匹配能力（name /
        display / 去 emoji 都认）统一换成清单里的 display。

        匹配不上就**原样返回**——那说明配置真的引用了已删除的场景，
        界面上应该看得见，不能悄悄抹掉。
        """
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            scenarios = self._fresh_scenarios()
            sc = scenarios.get(raw) if scenarios is not None else None
        except Exception:
            logger.debug("群管理：场景归一化失败", exc_info=True)
            sc = None
        if sc is not None:
            return str(sc.display or sc.name or raw)
        return raw

    def groups(self) -> dict:
        """群管理页的数据：群列表 + 场景清单 + 现有绑定（**只读**）。

        为什么读放在 bot 进程、写放在 bridge：
          · 读要 `chat_log` 的消息量和最后活跃——DB 访问层在这个进程里；
          · 写只需要碰 `config.yaml`，bridge 早就有「合并式保存 + 备份」的成熟
            路径（`api_config_save`）。
        这个分工让 bot **永远不提供改配置的端点**（模块 docstring 的安全约束）。
        代价是两边读到的配置可能短暂不一致（保存后要重启 bot 才生效）——
        前端据此提示重启，而不是假装已经生效。
        """
        h = self.handler
        if h is None:
            raise RuntimeError("糖糖本体未运行")
        store = self._memory_store()
        cfg = getattr(h, "config", None) or {}

        # 场景清单：`name` 是配置里存的键，`display` 是给人看的名字。
        # 注意历史包袱——全局 scenario_targets 存的是 display（'亲密模式'），
        # 而群级 scenario 存的是 name（'seductive'）。两边都要能显示，所以
        # 前端拿到的场景选项要同时带这两个字段。
        scenarios: list[dict] = []
        try:
            # 现读磁盘：进程内那份是启动时的快照，改完 yaml 不重载就永远是旧的
            _fresh = self._fresh_scenarios()
            for sc in ((_fresh.list_all() if _fresh is not None else []) or []):
                scenarios.append({
                    "name": str(sc.name or ""),
                    "display": str(sc.display or sc.name or ""),
                    "type": str(sc.type or ""),
                    "description": str(sc.description or "")[:200],
                })
        except Exception:
            logger.debug("群管理：场景清单读取失败", exc_info=True)
        scenarios.sort(key=lambda s: s["name"])

        groups_cfg = cfg.get("groups") or {}
        if not isinstance(groups_cfg, dict):
            groups_cfg = {}
        bl = cfg.get("blacklist") or {}
        if not isinstance(bl, dict):
            bl = {}
        black_groups = {str(x) for x in (bl.get("groups") or [])}
        black_users = [str(x) for x in (bl.get("private_users") or [])]
        global_targets = cfg.get("scenario_targets") or {}
        if not isinstance(global_targets, dict):
            global_targets = {}

        try:
            rows = store.list_group_overview()
        except Exception:
            logger.debug("群管理：群总览读取失败", exc_info=True)
            rows = []

        # 所有出现在「场景目标」里的人，一次性取昵称
        wanted: set[str] = set(str(q) for q in global_targets)
        for gcfg in groups_cfg.values():
            if isinstance(gcfg, dict):
                wanted.update(str(q) for q in (gcfg.get("scenario_targets") or {}))
        try:
            names = store.get_people_names(sorted(wanted))
        except Exception:
            logger.debug("群管理：昵称批量查询失败", exc_info=True)
            names = {}

        groups: list[dict] = []
        for row in rows:
            gid = str(row.get("id") or "")
            gcfg = groups_cfg.get(gid) or {}
            if not isinstance(gcfg, dict):
                gcfg = {}
            targets = []
            raw_targets = gcfg.get("scenario_targets") or {}
            if isinstance(raw_targets, dict):
                for qq, sn in raw_targets.items():
                    targets.append({
                        "qq": str(qq),
                        "scenario": self._norm_scenario(sn),
                        "name": names.get(str(qq), ""),
                    })
            targets.sort(key=lambda t: t["qq"])
            groups.append({
                "id": gid,
                "name": str(row.get("name") or ""),
                "member_count": int(row.get("member_count") or 0),
                "max_members": int(row.get("max_members") or 0),
                "messages": int(row.get("messages") or 0),
                "last_active": str(row.get("last_active") or ""),
                "scenario": self._norm_scenario(gcfg.get("scenario") or ""),
                "targets": targets,
                "blacklisted": gid in black_groups,
                "in_config": bool(gcfg),
            })

        gt_list = [{
            "qq": str(qq),
            "scenario": self._norm_scenario(sn),
            "name": names.get(str(qq), ""),
        } for qq, sn in global_targets.items()]
        gt_list.sort(key=lambda t: t["qq"])

        return {
            "groups": groups,
            "scenarios": scenarios,
            "global_targets": gt_list,
            "blacklist": {
                "groups": sorted(black_groups),
                "private_users": black_users,
            },
        }

    def memory_graph(self, center: str = "", limit_people: int = 40) -> dict:
        """记忆图谱：她「脑子里关于大家的认知」长什么样。

        `center` 空 → 全局视图（人 —— 类别）；`center` 是 QQ 号 → 以这个人为
        中心的认知切片（人 → 语义簇 → 事实，外加关于 ta 的记忆）。

        返回**给力导向图直接吃的**结构：`nodes` / `edges` 的 id 都带类型前缀
        （`person:` / `cluster:` / `fact:` / `memory:`），前端不用猜节点是什么。

        规模控制放在这里：全局最多 40 人 + 8 个类别；单人 1 + 簇数 + 事实(≤60)
        + 记忆(≤40)。再多的点画出来只是毛线团。
        """
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        store = self._memory_store()
        center = str(center or "").strip()

        if not center:
            people = store.memory_graph_overview(limit_people)
            stats_rows = store.cluster_category_stats()
            nodes: list[dict] = []
            edges: list[dict] = []
            people_ids: set = set()
            for p in people:
                qq = str(p.get("qq") or "")
                if not qq:
                    continue
                people_ids.add(qq)
                weight = int(p.get("about") or 0) + int(p.get("clusters") or 0) * 3
                nodes.append({
                    "id": "person:" + qq,
                    "kind": "person",
                    "label": str(p.get("nickname") or "") or ("QQ" + qq),
                    "qq": qq,
                    # 面积随「她记得多少」次线性增长。用 0.5 次方实测会顶格
                    # （40 个人里前 5 个全是 26.0，视觉上分不出谁更重要），
                    # 换成 0.25 次方后 9 → 11 → 16 → 21 才拉得开差距。
                    "size": round(6 + min(22.0, (max(weight, 1) ** 0.25) * 3.2), 1),
                    "meta": {
                        "intimacy": int(p.get("intimacy") or 0),
                        "clusters": int(p.get("clusters") or 0),
                        "memories": int(p.get("about") or 0),
                    },
                })
            cat_nodes: dict = {}
            for row in stats_rows:
                qq = str(row.get("qq") or "")
                if qq not in people_ids:
                    continue
                cat = str(row.get("category") or "")
                if not cat:
                    continue
                cid = "cat:" + cat
                if cid not in cat_nodes:
                    cat_nodes[cid] = {
                        "id": cid,
                        "kind": "category",
                        "label": CATEGORY_LABELS.get(cat, cat),
                        "size": 10.0,
                        "meta": {"category": cat, "clusters": 0, "facts": 0},
                    }
                    nodes.append(cat_nodes[cid])
                cat_nodes[cid]["meta"]["clusters"] += int(row.get("clusters") or 0)
                cat_nodes[cid]["meta"]["facts"] += int(row.get("facts") or 0)
                edges.append({
                    "source": "person:" + qq,
                    "target": cid,
                    "kind": "has",
                    "weight": int(row.get("clusters") or 0),
                })
            # 类别节点面积按「被多少人共享」定：人人都有的类别是主线。
            # 这里必须线性——开方会让 40 人里占比 38 和 20 的两个类别几乎一样大。
            for node in cat_nodes.values():
                degree = sum(1 for e in edges if e["target"] == node["id"])
                node["size"] = round(8 + min(16.0, degree * 0.4), 1)
            return {
                "mode": "overview",
                "center": "",
                "nodes": nodes,
                "edges": edges,
                "stats": store.memory_graph_totals(),
                "side": None,
            }

        raw = store.person_memory_graph(center)
        person = raw.get("person") or {}
        nodes = [{
            "id": "person:" + center,
            "kind": "person",
            "label": str(person.get("nickname") or "") or ("QQ" + center),
            "qq": center,
            "size": 22.0,
            "meta": {
                "intimacy": int(person.get("intimacy") or 0),
                "relationship": str(person.get("relationship") or ""),
                "summary": str(person.get("relationship_summary") or "")[:300],
            },
        }]
        edges = []
        for c in raw.get("clusters") or []:
            cid = "cluster:%s" % c.get("id")
            fact_count = int(c.get("fact_count") or 0)
            nodes.append({
                "id": cid,
                "kind": "cluster",
                "label": str(c.get("title") or "") or str(c.get("category") or "簇"),
                "size": round(8 + min(10.0, (max(fact_count, 1) ** 0.5) * 2.6), 1),
                "meta": {
                    "category": str(c.get("category") or ""),
                    "category_label": CATEGORY_LABELS.get(
                        str(c.get("category") or ""), str(c.get("category") or "")),
                    "summary": str(c.get("summary") or "")[:300],
                    "fact_count": fact_count,
                    "permanence": str(c.get("permanence") or ""),
                },
            })
            edges.append({"source": "person:" + center, "target": cid,
                          "kind": "has", "weight": fact_count})
        cluster_ids = {"cluster:%s" % c.get("id") for c in (raw.get("clusters") or [])}
        for f in raw.get("facts") or []:
            fid = "fact:%s" % f.get("id")
            parent = "cluster:%s" % f.get("cluster_id")
            nodes.append({
                "id": fid,
                "kind": "fact",
                "label": str(f.get("fact") or "")[:48],
                "size": 5.0,
                "meta": {
                    "confidence": float(f.get("confidence") or 0),
                    "importance": int(f.get("importance") or 0),
                },
            })
            if parent in cluster_ids:
                edges.append({"source": parent, "target": fid,
                              "kind": "contains", "weight": 1})
        for m in raw.get("memories") or []:
            mid = "memory:%s" % m.get("id")
            nodes.append({
                "id": mid,
                "kind": "memory",
                "label": str(m.get("value") or "")[:40],
                "size": 4.0,
                "meta": {
                    "key": str(m.get("key") or ""),
                    "origin": str(m.get("origin") or ""),
                    "importance": int(m.get("importance") or 0),
                    "event_time": str(m.get("event_time") or ""),
                },
            })
            edges.append({"source": "person:" + center, "target": mid,
                          "kind": "remembers", "weight": 1})
        return {
            "mode": "person",
            "center": center,
            "nodes": nodes,
            "edges": edges,
            "stats": None,
            # 经历和记忆原文放侧栏：它们没有边，当节点画只会更乱
            "side": {
                "person": person,
                "memories": raw.get("memories") or [],
                "episodes": raw.get("episodes") or [],
            },
        }

    def memory_history(self, qq_id: str = "") -> dict:
        """一个人记忆的**演化史**：她对同一个人的理解是怎么一版版变过来的。

        `superseded_by` 实测是 `memories.id` 指针（501/501 全部可解析），
        但它是 **N→1** 的——画像重合成时，多条旧事实会被**同一条**新画像取代
        （实测 `成展` 那条链末端 21249 同时接手了 19982/19983/19984/19985 四条）。
        所以链条不是链表而是**合并树**，这里压平成
        「一条主线 head → leaf，外加被并入的兄弟节点」。

        另外 1261 条 `status='superseded'` 却**没有** `superseded_by` ——
        它们被推翻了但不知道被谁取代。这不是我们能在展示层补的，如实列出
        （这叫「断链」），反而能暴露数据质量问题。
        """
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        qq = str(qq_id or "").strip()
        if not qq:
            raise ValueError("缺少 qq 参数")
        store = self._memory_store()
        rows = store.person_memory_history_rows(qq)
        by_id = {int(r.get("id") or 0): r for r in rows}

        def when(r: dict) -> str:
            return (str(r.get("event_time") or "").strip()
                    or str(r.get("ingested_at") or "").strip())

        def brief(r: dict, extra: dict | None = None) -> dict:
            out = {
                "id": int(r.get("id") or 0),
                "key": str(r.get("key") or ""),
                "value": str(r.get("value") or ""),
                "status": str(r.get("status") or ""),
                "time": when(r),
                "valid_to": str(r.get("valid_to") or ""),
                "origin": str(r.get("origin") or ""),
                "importance": int(r.get("importance") or 0),
            }
            if extra:
                out.update(extra)
            return out

        next_of: dict = {}
        prev_of: dict = {}
        for r in rows:
            ptr = str(r.get("superseded_by") or "").strip()
            if not ptr.isdigit():
                continue
            nid = int(ptr)
            # 只认指向「这个人自己的记忆」的边：跨人的指针要么是数据错，
            # 要么是那一条已经被清理掉了，两种情况都不该拼进这张图。
            if nid not in by_id:
                continue
            next_of[int(r.get("id") or 0)] = nid
            prev_of.setdefault(nid, []).append(int(r.get("id") or 0))

        # 按**叶子**反向聚族，而不是按「链头」正向遍历。
        #
        # 为什么：`superseded_by` 是 N→1 的合并树，多个根会汇进同一节点。
        # 原来的写法从每个根出发各走一遍，导致同一条主链被重复输出 N 次
        # （实测「维维美」40 条「链」其实是同一棵树的 40 个根，完全一样的 10 版）。
        # 现在一个叶子 = 一条链，前驱全部归入这一族。
        leaves = [i for i in by_id if i not in next_of]
        chains: list[dict] = []
        chained_ids: set = set()
        for leaf in leaves:
            family: set = set()
            stack = [leaf]
            while stack:
                cur = stack.pop()
                for p in prev_of.get(cur, []):
                    if p not in family:
                        family.add(p)
                        stack.append(p)
            if not family:
                continue  # 孤立节点（没有版本更迭）不成链

            # 主线：从叶子往回走，每一步取**时间最晚**的前驱（离叶子最近的那版），
            # 剩下的都是被并入的旁支。
            line: list[int] = [leaf]
            cur = leaf
            guard = 0
            while guard < 40:
                cands = [p for p in prev_of.get(cur, []) if p not in line]
                if not cands:
                    break
                cands.sort(key=lambda i: when(by_id.get(i) or {}), reverse=True)
                cur = cands[0]
                line.append(cur)
                guard += 1
            line.reverse()  # 老 → 新

            chained_ids.update(line)
            line_set = set(line)
            merged = []
            for nid in line:
                for p in prev_of.get(nid, []):
                    if p in line_set:
                        continue
                    chained_ids.add(p)
                    merged.append(brief(by_id.get(p) or {}, {"into": nid}))
            leaf_row = by_id.get(leaf) or {}
            chains.append({
                "head_id": line[0],
                "leaf_id": leaf,
                "kind": str(leaf_row.get("key") or ""),
                "span": [when(by_id.get(line[0]) or {}), when(leaf_row)],
                "versions": [brief(by_id.get(i) or {}) for i in line],
                "merged": merged[:40],
            })
        # 新的链条排前面：按末端时间倒序
        chains.sort(key=lambda c: c.get("span", ["", ""])[1], reverse=True)
        chains = chains[:40]

        # 主题演化：按 key 分组的时间序（没有显式链的记忆靠它看累积）
        groups: dict = {}
        for r in rows:
            groups.setdefault(str(r.get("key") or "未分类"), []).append(r)
        by_key = []
        for key, items in groups.items():
            items.sort(key=lambda r: when(r) or "9999")
            by_key.append({
                "key": key,
                "count": len(items),
                "items": [brief(r) for r in items[:30]],
            })
        by_key.sort(key=lambda g: -g["count"])

        superseded = sum(1 for r in rows
                         if str(r.get("status") or "") == "superseded")
        orphans = [brief(r) for r in rows
                   if str(r.get("status") or "") == "superseded"
                   and str(r.get("superseded_by") or "").strip() == ""]

        return {
            "person": store.get_people_names([qq]).get(qq, "") or "",
            "owner": qq,
            "summary": {
                "total": len(rows),
                "superseded": superseded,
                "chained": len(chained_ids),
                "chains": len(chains),
                "orphan_superseded": len(orphans),
                "active": sum(1 for r in rows
                              if str(r.get("status") or "active") == "active"),
            },
            "chains": chains,
            "by_key": by_key,
            "orphans": orphans[:60],
        }

    def memory_revisions(self, qq_id: str = "", limit: int = 200) -> dict:
        """一个人的**记忆变更流水**：每次改记忆的「何时 / 谁 / 从什么 / 改成什么 / 为什么」。

        与 `memory_history` 的分工（两者互补，不能互相替代）：

        - `memory_history` 是**版本链**——从 `superseded_by` / `valid_to` 推出来的
          「这一版被哪一版取代」。它能覆盖历史，但只认得「整行被替代」这一种事件。
        - `memory_revisions` 是**变更流水**——实施留痕之后每一次写操作的事实记录。
          它能表达内容改写（旧 value 被覆盖）、物理删除、importance/trust 调整，
          但**只有 2026-10-24 之后的数据**（此前没记，回填就是编造）。

        所以 UI 上必须分开显示：把「当时记的」和「从指针推断的」混进同一条时间线，
        等于让人分不清哪条是证据、哪条是推论。
        """
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        store = self._memory_store()
        qq = str(qq_id or "").strip()
        try:
            capped = max(20, min(500, int(limit or 200)))
        except (TypeError, ValueError):
            capped = 200
        raw_rows = store.memory_revisions(qq_id=qq, limit=capped)
        stats = store.memory_revision_stats(qq)
        items = [
            {
                "id": int(r.get("id") or 0),
                "entity": str(r.get("entity") or ""),
                "entity_id": str(r.get("entity_id") or ""),
                "qq": str(r.get("qq_id") or ""),
                "op": str(r.get("op") or ""),
                "field": str(r.get("field") or ""),
                "before": str(r.get("before_value") or ""),
                "after": str(r.get("after_value") or ""),
                "reason": str(r.get("reason") or ""),
                "actor": str(r.get("actor") or ""),
                "at": str(r.get("created_at") or ""),
            }
            for r in raw_rows
        ]
        person = store.get_people_names([qq]).get(qq, "") if qq else ""
        return {
            "person": person or "",
            "owner": qq,
            "summary": {
                "total": int(stats.get("total") or 0),
                "returned": len(items),
                "by_actor": stats.get("by_actor") or [],
                "by_op": stats.get("by_op") or [],
                "first_at": items[-1]["at"] if items else "",
                "last_at": items[0]["at"] if items else "",
            },
            "rows": items,
            "note": "变更流水自 2026-10-24 起记录；更早的变更只有版本链（见「演化史」）",
        }

    def memory_profile(self, qq_id: str = "") -> dict:
        """一个人的**记忆剖面**：七维索引 + 六层架构 + 全局存储能力。

        为什么单开一个端点、不塞进 memory_graph：
        图谱要的是「点」（人 / 簇 / 事实，靠边连起来看形状），剖面要的是
        「维度」和「层」（同一批数据的另一种切法）。硬塞在一起两边都变形。

        七维就是 `memories` 表上七个**正交**的索引维度——
        `key`（记忆语法）/ `cognitive`（情景 vs 语义）/ `retention`（保留策略）/
        `trust_level`（可信度）/ `status`（生命周期）/ `origin`（来源）/ `importance`。
        这不是我编的分类，是库表本来就这么存的，只是从来没在界面上摊开过。

        六层是数据从原始到抽象的流向：
        L0 原话 → L1 证据 → L2 记忆 → L3 语义簇 → L4 画像/经历 → L5 长期叙事。
        每一层都能往下钻：簇里有事实，事实有置信度，记忆能指回原话。
        """
        if self.handler is None:
            raise RuntimeError("糖糖本体未运行")
        qq = str(qq_id or "").strip()
        if not qq:
            raise ValueError("缺少 qq 参数")
        store = self._memory_store()

        rows = store.person_memory_rows(qq)
        # 带回尽量多的记忆：筛选在前端做，数据不全就会筛出假的分布。
        # 单人记忆量级实测最大约 1000 条，1000 条 JSON 约 300KB，本地 HTTP 可接受。
        raw = store.person_memory_graph(
            qq, memory_limit=1000, episode_limit=40, fact_limit=120,
            include_inactive=True)
        person = raw.get("person") or {}
        # 2026-10-24 P1：「称呼」放**顶层**，不塞进 person 字典。
        # 原因：前端 `MemoryProfile.person` 的声明是 Record<string, string|number>，
        # 而这里一个字段是 boolean、一个是数组——塞进去会让声明与运行时自相矛盾，
        # 前端只能靠 `as` 断言骗过编译器（那正是"假绿"）。
        # 语义上也更干净：person 代表 people 表的列，称呼是派生视图。
        nickname = str(person.get("nickname") or "")
        try:
            aliases = store.get_alias_records(qq)
        except Exception:
            logger.debug("记忆剖面：别名查询失败", exc_info=True)
            aliases = []

        from collections import Counter

        def tally(field: str, order: list | None = None) -> list[dict]:
            cnt: Counter = Counter()
            for r in rows:
                val = str(r.get(field) or "").strip() or "未标注"
                cnt[val] += 1
            items = [{"name": k, "count": v} for k, v in cnt.items()]
            if order:
                rank = {k: i for i, k in enumerate(order)}
                items.sort(key=lambda x: (rank.get(x["name"], 999), -x["count"]))
            else:
                items.sort(key=lambda x: -x["count"])
            return items

        imp: Counter = Counter()
        for r in rows:
            try:
                lv = int(r.get("importance") or 0)
            except (TypeError, ValueError):
                lv = 0
            imp[max(0, min(10, lv))] += 1
        importance = [{"name": str(lv), "count": imp.get(lv, 0)}
                      for lv in range(1, 11)]

        # 时序：按月聚合；event_time 为空的是「不知道发生在哪天」，
        # 单独计数而不是丢掉——那个数字本身就有信息量（越老的数据越缺）。
        months: dict = {}
        undated = 0
        for r in rows:
            et = str(r.get("event_time") or "").strip()
            by_event = bool(et)
            if not et:
                # 退回写入时间：不给它时间就等于假装「这条记忆不存在于时间轴上」，
                # 而 event_time 实测只有 25% 覆盖，退回写入日才画得出完整曲线。
                et = str(r.get("ingested_at") or "").strip()
            if not et:
                undated += 1
                continue
            key = et[:7]
            slot = months.setdefault(
                key, {"month": key, "count": 0, "superseded": 0, "by_event": 0})
            slot["count"] += 1
            if by_event:
                slot["by_event"] += 1
            # 「被推翻」的判定要走 status，不能只看 superseded_by 字段：
            # 实测这个人 status='superseded' 有 213 条，而 superseded_by 全空——
            # 只看字段会让整条时间线的「被推翻」永远是 0（假绿，教训 #37 同族）。
            if (str(r.get("status") or "").strip() == "superseded"
                    or str(r.get("superseded_by") or "").strip()):
                slot["superseded"] += 1
        timeline = [months[k] for k in sorted(months)]

        status_cnt: Counter = Counter(
            (str(r.get("status") or "").strip() or "active") for r in rows)
        cog_cnt: Counter = Counter(str(r.get("cognitive") or "").strip() for r in rows)
        about = sum(1 for r in rows if str(r.get("target_qq") or "").strip() == qq)
        spoken = sum(1 for r in rows if str(r.get("owner") or "").strip() == qq)
        self_talk = sum(1 for r in rows
                        if str(r.get("owner") or "").strip() == qq
                        and str(r.get("target_qq") or "").strip() == qq)
        # 单人视角的信任门（2026-10-24 立项研究第三期）：与
        # `query_memories(trusted_only=True)` 同一放行集合（store 的常量），
        # 所以剖面页的「可注入」就是这个人在上下文里实际能出现的记忆条数。
        trusted_active = sum(
            1 for r in rows
            if (str(r.get("status") or "").strip() or "active") == "active"
            and str(r.get("trust_level") or "").strip() in TRUSTED_TRUST_LEVELS)

        mem_list = raw.get("memories") or []
        ev_rows = []
        try:
            ev_rows = store.person_evidence_samples([m.get("id") for m in mem_list])
        except Exception:
            logger.debug("记忆剖面：证据采样失败", exc_info=True)
        ev_by_memory: dict = {}
        for ev in ev_rows:
            ev_by_memory.setdefault(int(ev.get("memory_id") or 0), []).append({
                "chat_id": int(ev.get("chat_id") or 0),
                "message": str(ev.get("message") or "")[:600],
                "timestamp": str(ev.get("timestamp") or ""),
                "is_bot_reply": bool(ev.get("is_bot_reply")),
            })

        clusters = []
        facts_by_cluster: dict = {}
        for f in raw.get("facts") or []:
            facts_by_cluster.setdefault(int(f.get("cluster_id") or 0), []).append(f)
        for c in raw.get("clusters") or []:
            cid = int(c.get("id") or 0)
            clusters.append({
                "id": cid,
                "category": str(c.get("category") or ""),
                "category_label": CATEGORY_LABELS.get(
                    str(c.get("category") or ""), str(c.get("category") or "")),
                "title": str(c.get("title") or ""),
                "summary": str(c.get("summary") or ""),
                "fact_count": int(c.get("fact_count") or 0),
                "permanence": str(c.get("permanence") or ""),
                "facts": [{
                    "id": int(f.get("id") or 0),
                    "fact": str(f.get("fact") or ""),
                    "confidence": float(f.get("confidence") or 0),
                    "importance": int(f.get("importance") or 0),
                } for f in facts_by_cluster.get(cid, [])],
            })

        memories = [{
            "id": int(m.get("id") or 0),
            "key": str(m.get("key") or ""),
            "value": str(m.get("value") or ""),
            "importance": int(m.get("importance") or 0),
            "origin": str(m.get("origin") or ""),
            "event_time": str(m.get("event_time") or ""),
            # 这四个维度是 L2 列表里「按维度筛选」的依据——缺了就只能统计不能筛
            "cognitive": str(m.get("cognitive") or ""),
            "retention": str(m.get("retention") or ""),
            "trust": str(m.get("trust_level") or ""),
            "status": str(m.get("status") or ""),
            "evidence": ev_by_memory.get(int(m.get("id") or 0), []),
        } for m in mem_list]

        episodes = [{
            "id": int(e.get("id") or 0),
            "title": str(e.get("title") or ""),
            "summary": str(e.get("summary") or ""),
            "time_start": str(e.get("time_start") or ""),
            "time_end": str(e.get("time_end") or ""),
        } for e in (raw.get("episodes") or [])]

        caps = {}
        try:
            caps = store.memory_capabilities()
        except Exception:
            logger.debug("记忆剖面：存储能力查询失败", exc_info=True)

        return {
            "person": person,
            # 「称呼」独立成块：QQ 昵称（跨群稳定身份，可能为空）+ 自称/外号列表
            # （带来源与证据记忆）。分开放是刻意的——混在一起展示，等于让
            # 「她自称米雪儿」看起来像「她叫米雪儿」。
            "addresses": {
                "nickname": nickname,
                "nickname_missing": not nickname,
                "aliases": aliases,
            },
            "counts": {
                "memories": len(rows),
                "active": status_cnt.get("active", 0),
                # 「记得多少」和「能用上多少」是两个数（2026-10-24）。只显示前者
                # 会让人以为糖糖认识她，其实九成记忆进不了上下文——本轮调查就是
                # 因为这个数字从未出现过，才把它当成「昵称丢了」的表象问题。
                "trusted_injectable": trusted_active,
                "trusted_blocked": max(0, status_cnt.get("active", 0) - trusted_active),
                "retracted": status_cnt.get("retracted", 0),
                "superseded": status_cnt.get("superseded", 0),
                "episodic": cog_cnt.get("episodic", 0),
                "semantic": cog_cnt.get("semantic", 0),
                "about": about,
                "spoken": spoken,
                "clusters": len(clusters),
                "facts": sum(len(c["facts"]) for c in clusters),
                "episodes": len(episodes),
                "returned": len(memories),
                "self_talk": self_talk,
                "evidence": len(ev_rows),
                "undated": undated,
            },
            "dimensions": {
                "key": tally("key"),
                # 认知类型只有两个值，固定顺序：情景 → 语义
                "cognitive": tally("cognitive", ["episodic", "semantic"]),
                # 保留策略按「越久越靠前」排
                "retention": tally("retention",
                                   ["permanent", "durable", "normal", "transient"]),
                "trust": tally("trust_level",
                               ["corrected", "verified", "unverified",
                                "legacy_unverified"]),
                "status": tally("status",
                                ["active", "superseded", "retracted", "fulfilled"]),
                "origin": tally("origin",
                                ["extracted", "self", "summarized", "backfill",
                                 "corrected"]),
                "importance": importance,
            },
            "timeline": timeline,
            "layers": {
                "clusters": clusters,
                "memories": memories,
                "episodes": episodes,
                "evidence": ev_rows[:80],
            },
            "capabilities": caps,
        }

    def state_snapshot(self) -> dict:
        """糖糖此刻的内部状态。

        ⚠️ 方法名**不叫 `snapshot`**：`tests/test_integration_contract.py` 的零调用
        闸门按「方法名」跨类统计属性/字符串证据，叫 `snapshot` 会把既有的
        待归档死代码 `agent/knowledge_index.py::snapshot` 判成「已被接线」，
        让棘轮永远收紧不了（2026-10-24 实测两个闸门同时假红）。

        **每个分区独立 try**：某一个子系统改名/异常时，整页不能塌。
        缺数据的分区给 None，前端据此隐藏该卡片。
        """
        h = self.handler
        out: dict[str, Any] = {
            "bot": {"name": "", "qq": "", "uptime_s": int(time.time() - self.started_at)},
            "mood": None, "drives": [], "dominant": None, "self": None,
            "relationships": None, "memory": None, "model": None,
            "turn": self.last_turn,
        }
        if h is None:
            return out

        try:
            cfg_bot = (h.config.get("bot") or {})
            out["bot"] = {
                "name": cfg_bot.get("name") or "糖糖",
                "qq": str(getattr(h, "bot_qq", "") or ""),
                "uptime_s": out["bot"]["uptime_s"],
            }
        except Exception:
            logger.debug("快照 bot 段失败", exc_info=True)

        # 情绪
        try:
            mood = h.mood
            out["mood"] = {
                "energy": int(mood.energy),
                "mood": int(mood.mood),
                "patience": int(mood.patience),
                "context": (mood.build_context() or "")[:400],
            }
        except Exception:
            logger.debug("快照 mood 段失败", exc_info=True)

        # 驱动力
        try:
            ds = h.self_state.drives
            items = []
            for key, drv in ds.drives.items():
                items.append({
                    "key": key,
                    "label": getattr(drv, "label", key),
                    "value": round(float(drv.value), 3),
                    "urgent": bool(drv.is_urgent),
                    "active": bool(drv.is_active),
                    "intensity": getattr(drv, "intensity_text", ""),
                })
            items.sort(key=lambda d: -d["value"])
            out["drives"] = items
            dom = ds.get_dominant()
            if dom is not None:
                out["dominant"] = {
                    "key": getattr(dom, "name", ""),
                    "label": getattr(dom, "label", ""),
                    "text": getattr(dom, "intensity_text", ""),
                }
        except Exception:
            logger.debug("快照 drives 段失败", exc_info=True)

        # 自我
        # ⚠️ `existence.energy` 是字符串（low/normal/high），不是 0-100 的数字——
        # 早期版本按 int 处理，int('low') 抛 ValueError 把整个 self 段吞成 None。
        # 经验缓冲的键是 message/time（不是 summary/ts），见 get_recent_experiences。
        try:
            ss = h.self_state
            recent = []
            try:
                for exp in (ss.get_recent_experiences(limit=5) or []):
                    if isinstance(exp, dict):
                        recent.append({
                            "summary": str(exp.get("message") or ""),
                            "nickname": str(exp.get("nickname") or ""),
                            "time": str(exp.get("time") or ""),
                        })
            except Exception:
                logger.debug("快照 experience 段失败", exc_info=True)
            out["self"] = {
                "narrative": (ss.self_narrative.summary or "")[:1200],
                "values": (ss.values.to_tendency_text() or "")[:600],
                "energy": str(getattr(ss.existence, "energy", "") or ""),
                "mood_tint": str(getattr(ss.existence, "mood_tint", "") or ""),
                "experiences": recent,
            }
        except Exception:
            logger.debug("快照 self 段失败", exc_info=True)

        # 关系场
        try:
            ss = h.self_state
            rels = list(ss.relationships.values())
            nick_map: dict[str, str] = {}
            try:
                nick_map = {str(q): str(n) for q, n in h.memory.store.list_people_nicknames()}
            except Exception:
                pass
            top = sorted(rels, key=lambda r: -float(getattr(r, "closeness", 0)))[:5]
            out["relationships"] = {
                "total": len(rels),
                "close": sum(1 for r in rels if float(getattr(r, "closeness", 0)) >= 0.6),
                "familiar": sum(1 for r in rels
                                if 0.3 <= float(getattr(r, "closeness", 0)) < 0.6),
                "top": [{
                    "qq": str(getattr(r, "qq_id", "")),
                    "nickname": nick_map.get(str(getattr(r, "qq_id", "")), "")
                                  or str(getattr(r, "qq_id", "")),
                    "closeness": round(float(getattr(r, "closeness", 0)), 3),
                    "tier": getattr(r, "familiarity_level", ""),
                } for r in top],
            }
        except Exception:
            logger.debug("快照 relationships 段失败", exc_info=True)

        # 记忆
        # ⚠️ `query_all_memories()` 只返回 id/importance/timestamp（**不含正文**）——
        # 它是给统计用的。要正文必须走 query_memories()，它 SELECT 了 value。
        try:
            total = 0
            try:
                total = int((h.memory.get_memory_stats() or {}).get("total", 0))
            except Exception:
                pass
            recent_mem: list[dict] = []
            try:
                rows = h.memory.store.query_memories(limit=200) or []
                rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
                for r in rows[:5]:
                    recent_mem.append({
                        "text": str(r.get("value") or "")[:200],
                        "time": str(r.get("timestamp") or ""),
                        "importance": int(r.get("importance") or 0),
                    })
            except Exception:
                logger.debug("快照 recent memories 段失败", exc_info=True)
            out["memory"] = {"total": total, "recent": recent_mem}
        except Exception:
            logger.debug("快照 memory 段失败", exc_info=True)

        # 模型
        try:
            meta = self._turn_meta(0)
            out["model"] = {"provider": meta["provider"], "model": meta["model"]}
        except Exception:
            logger.debug("快照 model 段失败", exc_info=True)

        return out


class _turn_guard:
    """标记「当前处于会客厅回合」，让发送截获器知道该不该动手。"""

    __slots__ = ("sid", "_token")

    def __init__(self, sid: str):
        self.sid = sid
        self._token = None

    def __enter__(self):
        self._token = _TURN_CTX.set({"session": self.sid})
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _TURN_CTX.reset(self._token)
        return False
