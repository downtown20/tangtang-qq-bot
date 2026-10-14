"""统一发送动作（P0-D1 2026-08-28）——所有外部发送的唯一 helper。

LLM 工具 send_message、旧执行别名（group_say/send_private_message/
relay_message）、命令行 /传话 全部走这里，保证：
  - verbatim：actual 与 requested 完全相同
  - relay：正文显式标明主人归因，并完整保留 requested
  - natural：LLM 改写为糖糖口吻，但回执记录实际正文
  - 统一结构化 receipt（JSON）：requested/actual/target/channel/mode/
    attribution/message_id/status（confirmed/uncertain/failed/draft）
  - 发送异常一律 uncertain——外部 POST 后响应可能丢失，禁止伪装 failed 触发重放

禁止再按长度/关键词系统猜 mode（历史反模式 #9/#24）。
"""

import json
import logging

from .action_contract import ActionReceipt, derive_action_id
from onebot.ws_client import send_delivery_state

logger = logging.getLogger("糖糖.SendActions")

VALID_MODES = ("verbatim", "natural", "relay")
VALID_ATTRIBUTIONS = ("owner", "self", "none")


def build_receipt(*, requested: str, actual: str, channel: str, target: str,
                  mode: str, attribution: str, status: str,
                  message_id: int = 0) -> str:
    """统一结构化回执（JSON 字符串，供工具循环直接返回）。"""
    payload = {
        "requested": requested,
        "text": actual,
        "mode": mode,
        "attribution": attribution,
    }
    receipt = ActionReceipt(
        action_id=derive_action_id(
            source_id="legacy-text-receipt", kind="text", channel=channel,
            target=target, payload=payload,
        ),
        kind="text", channel=channel, target=target,
        status=status, message_ids=(message_id,),
        actual=payload,
    )
    return json.dumps(receipt.to_legacy_text_dict(), ensure_ascii=False)


def parse_receipt(receipt_json: str) -> dict:
    try:
        data = json.loads(receipt_json)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def format_human_receipt(receipt_json: str, target_label: str = "") -> str:
    """把结构化回执格式化成面向命令行/人类可读的回报。"""
    r = parse_receipt(receipt_json)
    who = target_label or r.get("target") or "对方"
    actual = (r.get("actual") or "")[:120]
    status = r.get("status", "")
    if status == "confirmed":
        return (f"✅ 已送达{who}～\n📨 内容：{actual}"
                f"{'…' if len(r.get('actual') or '') > 120 else ''}")
    if status == "uncertain":
        return f"⚠️ 已提交给{who}，但未确认送达——请勿重复发送"
    if status == "draft":
        return f"📝 草稿已生成（还没发）——\n\n目标：{who}\n\n---内容---\n{actual}\n---"
    return f"❌ 发送失败。{who}可能不是好友，或屏蔽了临时会话。"


async def _natural_rewrite(llm_call, message: str, bot_name: str) -> str:
    """natural 模式：LLM 把请求改写成糖糖口吻（保留原意，材料外不编）。
    失败/空回复 → 返回空串，调用方原样兜底。"""
    if not llm_call:
        return ""
    try:
        reply = await llm_call(
            f"你是{bot_name}。把下面这段话用你自己的口吻说出来——"
            "保留原意，不要添加原文没有的信息，不要改变请求内容本身。",
            f"要转述的话：{message}",
        )
        return str(reply or "").strip()
    except Exception as e:
        logger.warning(f"natural 改写失败（原样兜底）: {e}")
        return ""


async def execute_send_action(napcat, *, channel: str, target: str,
                              message: str, mode: str = "verbatim",
                              attribution: str = "none", review: bool = False,
                              llm_call=None, bot_name: str = "糖糖") -> str:
    """执行统一发送动作，返回结构化 receipt JSON 字符串。

    mode:
      verbatim — actual 与 requested 完全相同（绝不改写）
      relay    — 转达语义：attribution 强制 owner，actual 显式标明主人归因，
                   requested 原文完整保留（不再 LLM 隐藏主人或自由改写意图）
      natural  — LLM 改写为糖糖口吻（保留语义）；失败/空 → 原样兜底
    attribution: owner/self/none——写入回执；relay 时强制 owner 且正文可见归因
    review=True → status=draft，不发送（草稿回执，等确认后重发）
    参数无效（空 message/target、非法 channel）= failed（未发起外部副作用）；
    发送异常一律 uncertain——无法证明未送达，禁止自动重放。
    """
    requested = str(message or "").strip()
    mode = mode if mode in VALID_MODES else "verbatim"
    attribution = attribution if attribution in VALID_ATTRIBUTIONS else "none"
    if not requested or not str(target or "").strip() or channel not in ("private", "group"):
        return build_receipt(requested=requested, actual=requested,
                             channel=channel, target=str(target or ""),
                             mode=mode, attribution=attribution,
                             status="failed")
    target = str(target).strip()
    if mode == "relay":
        attribution = "owner"  # 转达语义：归因强制主人
        actual = f"主人让我转告你：{requested}"  # 接收者可见归因，原话完整保留
    elif mode == "natural":
        actual = await _natural_rewrite(llm_call, requested, bot_name) or requested
    else:  # verbatim
        actual = requested
    if review:
        return build_receipt(requested=requested, actual=actual,
                             channel=channel, target=target,
                             mode=mode, attribution=attribution,
                             status="draft")
    try:
        if channel == "group":
            result = await napcat.send_group_message(target, actual)
        else:
            result = await napcat.send_private_message(target, actual)
    except Exception as e:
        # 外部 POST 后响应可能丢失；只能 uncertain，禁止自动重放。
        logger.warning(f"发送动作异常（按 uncertain 处理，禁止重放）: {e}")
        return build_receipt(requested=requested, actual=actual,
                             channel=channel, target=target,
                             mode=mode, attribution=attribution,
                             status="uncertain")
    state = send_delivery_state(result)
    mid = int(getattr(result, "message_id", 0) or 0)
    return build_receipt(requested=requested, actual=actual,
                         channel=channel, target=target,
                         mode=mode, attribution=attribution,
                         status=state, message_id=mid)
