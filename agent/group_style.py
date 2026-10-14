"""
群风学习器 —— 统计 + 周期性 LLM 话题总结
低成本逐渐融入群聊氛围
"""

import re
from collections import Counter, deque
from datetime import datetime
from typing import Optional, Callable


class GroupStyle:
    """单个群的风格档案"""

    def __init__(self, group_id: str, group_name: str = ""):
        self.group_id = group_id
        self.group_name = group_name

        # 统计
        self.total_messages = 0
        self.word_counter = Counter()
        self.msg_lengths = []
        self.face_usage_count = 0
        self.image_count = 0
        self.last_summary_at = 0
        self.summary_cache = ""

        # LLM 话题总结
        self._topic_buffer: deque = deque(maxlen=200)
        self._topic_last_extract = 0
        self._topic_cache = ""
        self._llm_caller: Optional[Callable] = None  # 由 handler 注入

    def feed(self, raw_message: str, text: str, nickname: str = ""):
        """喂一条新消息"""
        self.total_messages += 1

        # 统计
        self.face_usage_count += raw_message.count("[CQ:face,")
        self.image_count += raw_message.count("[CQ:image,")

        words = re.split(r'[\s,，。！？!?、…\./]+', text)
        for w in words:
            w = w.strip()
            if len(w) >= 2:
                self.word_counter[w] += 1

        self.msg_lengths.append(len(text))
        if len(self.msg_lengths) > 200:
            self.msg_lengths.pop(0)

        # 话题总结缓冲区（存纯文本，定期让LLM总结）
        clean = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
        if clean:
            self._topic_buffer.append(f"{nickname}: {clean}" if nickname else clean)

    def summary(self) -> str:
        """生成群风描述（统计 + 话题总结）"""
        if self.total_messages < 20:
            return "（刚开始观察这个群，还不太了解风格）"

        stopwords = {"什么", "怎么", "为什么", "这个", "那个", "就是", "一个",
                     "可以", "觉得", "不是", "已经", "还是", "没有", "知道",
                     "这样", "如果", "因为", "所以", "但是", "然后", "不过",
                     "真的", "应该", "可能", "其实", "比较", "特别", "非常",
                     "现在", "今天", "明天", "昨天", "一下", "有点", "哈哈"}
        top_words = [(w, c) for w, c in self.word_counter.most_common(50)
                     if w not in stopwords][:15]

        avg_len = sum(self.msg_lengths) / len(self.msg_lengths) if self.msg_lengths else 0
        face_rate = self.face_usage_count / self.total_messages if self.total_messages else 0
        img_rate = self.image_count / self.total_messages if self.total_messages else 0

        parts = [f"已观察{self.total_messages}条消息。"]

        if avg_len < 8:
            parts.append("说话很简短。")
        elif avg_len < 20:
            parts.append("聊天节奏正常。")
        else:
            parts.append("喜欢说比较长的话。")

        if face_rate > 0.3:
            parts.append("超爱用QQ表情。")
        if img_rate > 0.2:
            parts.append("表情包满天飞。")

        if top_words:
            words_str = "、".join(w for w, _ in top_words[:8])
            parts.append(f"常见词：{words_str}。")

        # 附加 LLM 话题总结
        if self._topic_cache:
            parts.append(f"\n近期话题：{self._topic_cache}")

        self.summary_cache = " ".join(parts)
        self.last_summary_at = self.total_messages
        return self.summary_cache

    async def maybe_update_topics(self):
        """每 200 条消息让 LLM 总结一次近期话题"""
        if not self._llm_caller:
            return
        since_last = self.total_messages - self._topic_last_extract
        if since_last < 200 or len(self._topic_buffer) < 30:
            return

        recent = "\n".join(list(self._topic_buffer)[-60:])
        prompt = (
            f"以下是群聊最近的一些消息。请用一两句话总结：这群人最近在聊什么话题？氛围怎么样？\n\n"
            f"{recent}\n\n"
            f"直接输出总结，不要加前缀："
        )
        try:
            result = await self._llm_caller(
                system_prompt="你是一个群聊观察员，用简短的中文总结群聊话题和氛围。",
                user_message=prompt,
            )
            if result and len(result) > 5:
                self._topic_cache = result.strip()
                self._topic_last_extract = self.total_messages
        except Exception:
            pass  # 静默失败，不影响主流程


class GroupStyleManager:
    """管理所有群的风格档案"""

    def __init__(self):
        self.groups: dict[str, GroupStyle] = {}
        self._llm_caller: Optional[Callable] = None

    def set_llm_caller(self, caller: Callable):
        """注入 LLM 调用函数，用于话题总结"""
        self._llm_caller = caller
        for gs in self.groups.values():
            gs._llm_caller = caller

    def get_or_create(self, group_id: str, group_name: str = "") -> GroupStyle:
        if group_id not in self.groups:
            gs = GroupStyle(group_id, group_name)
            if self._llm_caller:
                gs._llm_caller = self._llm_caller
            self.groups[group_id] = gs
        return self.groups[group_id]

    def feed(self, group_id: str, raw_message: str, text: str, group_name: str = "", nickname: str = ""):
        gs = self.get_or_create(group_id, group_name)
        if group_name:
            gs.group_name = group_name
        gs.feed(raw_message, text, nickname)

    def get_context(self, group_id: str) -> str:
        """获取群风描述，用于注入系统提示词"""
        gs = self.groups.get(group_id)
        if not gs or gs.total_messages < 20:
            return ""
        if gs.total_messages - gs.last_summary_at >= 50 or not gs.summary_cache:
            return gs.summary()
        return gs.summary_cache

    async def maybe_update_topics(self, group_id: str):
        """检查是否需要更新话题总结"""
        gs = self.groups.get(group_id)
        if gs:
            await gs.maybe_update_topics()

    def get_group_name(self, group_id: str) -> str:
        gs = self.groups.get(group_id)
        return gs.group_name if gs else ""
