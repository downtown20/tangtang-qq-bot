"""
心情追踪器 — 心理陪伴用户的情绪趋势监控

StructBERT 逐条分析，每日汇总，检测持续走低趋势。
发现异常时通过回调通知——糖糖可以主动关心。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Callable

logger = logging.getLogger("糖糖.MoodTracker")


class MoodTracker:
    """StructBERT 心情追踪——趋势监控，非实时分析"""

    MODEL_DIRS = [
        os.path.expanduser("~/models/iic/nlp_structbert_sentiment-classification_chinese-base"),
        "./models/iic/nlp_structbert_sentiment-classification_chinese-base",
    ]

    def __init__(self, db_path: str = "./memory.db", store=None):
        self._db = db_path
        self._store = store  # 2026-08-10 收口：mood_log 表归 Store 管理，直连 SQL 全部改走 Store
        self._model = None
        self._tokenizer = None
        self._ready = False
        self._on_alert: Optional[Callable] = None  # (qq_id, nickname, trend_desc) -> None

    # ═══ 模型加载 ═══

    def load(self):
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch
        except ImportError:
            logger.warning("⚠ transformers 未安装，心情追踪不可用")
            return

        local = None
        for d in self.MODEL_DIRS:
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "config.json")):
                local = d
                break
        if not local:
            logger.warning("⚠ 未找到 StructBERT 模型")
            return

        try:
            # 2026-08-15 23:55 事故：并发 from_pretrained 竞态出 meta 空权重——
            # 统一持锁串行（本模块是 torch.load 手动装载，本身安全，持锁防未来漂移）
            from .model_lock import MODEL_LOAD_LOCK
            with MODEL_LOAD_LOCK:
                self._tokenizer = AutoTokenizer.from_pretrained(local)
                # 模型是 EasyNLP 导出格式：key 带 encoder. 前缀、config 缺 model_type——
                # HF 的 AutoModel 直接加载会报误导性的 "state dictionary corrupted"。
                # 手动重映射 encoder.* → bert.*（丢弃非持久化 buffer position_ids）。
                import torch
                from transformers import BertConfig, BertForSequenceClassification
                state = torch.load(os.path.join(local, "pytorch_model.bin"), map_location="cpu")
                mapped = {}
                for k, v in state.items():
                    if k == "encoder.embeddings.position_ids":
                        continue
                    mapped["bert." + k[len("encoder."):] if k.startswith("encoder.") else k] = v
                config = BertConfig.from_pretrained(local)
                self._model = BertForSequenceClassification(config)
                self._model.load_state_dict(mapped, strict=True)
            self._ready = True
            logger.info("📊 StructBERT 心情追踪就绪")
        except Exception as e:
            logger.warning(f"⚠ StructBERT 加载失败: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    def on_alert(self, callback: Callable):
        """设置告警回调——检测到情绪走低时调用"""
        self._on_alert = callback

    # ═══ 情感分析 ═══

    def analyze(self, text: str) -> Optional[float]:
        """分析单条消息的情感分——0=负面，1=正面"""
        if not self._ready or not text:
            return None
        try:
            import torch
            encoded = self._tokenizer(text, padding=True, truncation=True,
                                      return_tensors='pt', max_length=256)
            with torch.no_grad():
                outputs = self._model(**encoded)
                probs = torch.softmax(outputs.logits, dim=-1).squeeze().tolist()
            # StructBERT 输出 [neg, pos] 或 [neg, neutral, pos]
            if len(probs) == 2:
                return probs[1]  # positive probability
            elif len(probs) >= 3:
                return probs[2]  # positive probability in 3-class
            return probs[0] if probs else None
        except Exception:
            return None

    # ═══ 数据存储 ═══

    def record(self, qq_id: str, score: float):
        """记录一条心情分——当天汇总到日平均（2026-08-10 收口：走 Store）"""
        today = datetime.now().strftime("%Y-%m-%d")
        self._store.upsert_mood_log(qq_id, today, score)

    def get_trend(self, qq_id: str, days: int = 7) -> list[tuple[str, float, int]]:
        """获取最近 N 天的心情趋势 [(date, avg_score, message_count), ...]"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        # 2026-08-10 收口：走 Store 方法
        return self._store.get_mood_trend(qq_id, since)

    # ═══ 趋势检测 ═══

    def check_alert(self, qq_id: str) -> Optional[str]:
        """检查是否需要告警——连续 3 天走低，或单日骤降"""
        trend = self.get_trend(qq_id, days=7)
        if len(trend) < 3:
            return None

        scores = [s for _, s, _ in trend[-5:]]

        # 检测 1：最近 3 天持续下降
        if len(scores) >= 3 and scores[-3] > scores[-2] > scores[-1]:
            drop = scores[-3] - scores[-1]
            if drop > 0.2:
                return f"连续 3 天心情走低（降幅 {drop:.2f}），建议主动关心"

        # 检测 2：今天较昨日骤降
        if len(scores) >= 2:
            today, yesterday = scores[-1], scores[-2]
            if yesterday - today > 0.3 and today < 0.4:
                return f"今天心情骤降（{yesterday:.2f}→{today:.2f}），需要关注"

        # 检测 3：整体低迷（近 3 天平均 < 0.3）
        recent = scores[-3:]
        if sum(recent) / len(recent) < 0.3:
            return f"近 3 天持续低迷（均分 {sum(recent)/len(recent):.2f}），可能处于低谷"

        return None
