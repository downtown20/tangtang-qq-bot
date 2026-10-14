"""
记忆健康监控 — 轻量指标采集器

所有操作 O(1)，不阻塞主流程。指标存在 kv_store（按天分区）。
采集点只做内存 incr，每 5 分钟异步 flush 到数据库。

指标清单：
  基础提取/记忆质量/用户覆盖/系统资源指标，另含提取 job 生命周期扩展计数。
"""

from datetime import datetime
import logging

logger = logging.getLogger("糖糖.Metrics")


class MemoryMetrics:
    """轻量指标收集器。所有写操作只写内存，flush 时才写磁盘。"""

    def __init__(self, store):
        self._store = store
        self._counters: dict[str, int] = {}  # 内存缓冲

    # ═══════════════════════════════════════
    # 采集 API — 在各代码路径中调用
    # ═══════════════════════════════════════

    def incr(self, name: str, delta: int = 1):
        """增加计数器。仅内存操作，不写磁盘。"""
        self._counters[name] = self._counters.get(name, 0) + delta

    def record_extract_attempt(self):
        """M1: LLM 提取被触发（_extract_memories_with_llm 入口）"""
        self.incr("extract_attempts")

    def record_extract_result(self, outcome: str, count: int = 0,
                              rejected: int = 0):
        """记录一次 LLM 子批次结果；协议质量与记忆产出分别计数。"""
        self.incr("extract_outcomes_total")
        if outcome in ("success_with_items", "success_empty", "rejected_all"):
            self.incr("extract_protocol_success")
        if outcome == "success_with_items":
            self.incr("extract_with_items")
            self.incr("extract_memories_total", count)
        elif outcome == "success_empty":
            self.incr("extract_success_empty")
        elif outcome == "rejected_all":
            self.incr("extract_rejected_all")
        elif outcome == "invalid_json":
            self.incr("extract_invalid_json")
        elif outcome == "transport_error":
            self.incr("extract_transport_error")
        if rejected:
            self.incr("extract_rejected", rejected)

    def record_extract_success(self, count: int):
        """M2+M3: 提取成功产生记忆"""
        self.incr("extract_successes")
        self.incr("extract_memories_total", count)

    def record_extract_empty(self):
        """M5: LLM 返回空（无意义内容或调用失败）"""
        self.incr("extract_llm_failures")

    def record_extract_rejected(self, count: int):
        """M4: 被后置校验拦截"""
        self.incr("extract_rejected", count)

    def record_busy_skip(self):
        """M6: 因 _llm_busy 跳过提取"""
        self.incr("extract_busy_skipped")

    def record_self_memory(self):
        """M7: 正则自忆产生"""
        self.incr("self_memories_today")

    def record_dedup_hit(self):
        """M10: BGE 去重命中"""
        self.incr("dedup_hits")

    def record_confidence(self, conf: float):
        """M8 辅助: 记录置信度样本（存区间计数）

        三个区间含义：
          ≥0.8 → confidence_high: LLM 明确看到用户自述（如"我在北大读书"→ 0.95）
          0.5-0.7 → confidence_mid: LLM 暗示/间接推断（如"下周考试真烦"→ 推测是学生, 0.6）
          <0.5 → confidence_low: LLM 自己都不确定，会被校验层直接丢弃不存库

        健康标准：≥0.8 应多于 0.5-0.7。如果大部分集中在 0.5-0.7，
        说明 LLM 在偷懒给默认值（之前数据 98.3% 全是 0.7）"""
        if conf >= 0.8:
            self.incr("confidence_high")
        elif conf >= 0.5:
            self.incr("confidence_mid")
        else:
            self.incr("confidence_low")

    def record_cognitive(self, cognitive: str):
        """M9 辅助: 记录认知类型"""
        if cognitive == "episodic":
            self.incr("cognitive_episodic")
        else:
            self.incr("cognitive_semantic")

    def record_window_decision(self, decision_run, *, faded: bool = False) -> str:
        """由一个已完成 DecisionRun 统一派生窗口决策漏斗计数。

        窗口的 reply/skip 以 LLM 回合的终态事实为唯一分类输入；fade 是 skip
        后由 ConversationTracker 观察到的状态转换，因而是 skip 的子集，不能
        计入互斥的 total 分母。``action`` 延续既有统计语义：本回合要求发送
        动作即算 reply，避免把语音/贴图等已响应回合误归为沉默。
        """
        from .interaction_contract import DecisionRun

        if not isinstance(decision_run, DecisionRun):
            raise TypeError("record_window_decision requires DecisionRun")
        if decision_run.status != "completed":
            raise ValueError("window decision run must be completed")
        if decision_run.decision == "skip":
            outcome = "skip"
        elif decision_run.decision in {"reply", "action"}:
            outcome = "reply"
        else:
            raise ValueError("window decision run has no observable outcome")
        if faded and outcome != "skip":
            raise ValueError("only a skipped window decision can fade")

        self.incr("window_decisions_total")
        self.incr(f"window_decisions_{outcome}")
        if faded:
            self.incr("window_fade_after_silence")
        return outcome

    # ═══════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def flush(self):
        """将内存计数器写入 kv_store，返回本次是否成功。"""
        if not self._counters:
            return True
        today = self._today()
        snapshot = dict(self._counters)
        try:
            self._store.increment_metric_batch(today, snapshot)
            # 只减去本次快照；即使未来有其他线程在 flush 期间递增，
            # 新增部分也不会被 clear 误丢。
            for name, delta in snapshot.items():
                remaining = self._counters.get(name, 0) - delta
                if remaining:
                    self._counters[name] = remaining
                else:
                    self._counters.pop(name, None)
            return True
        except Exception as e:
            logger.warning(f"指标 flush 失败（缓冲保留，将重试）: {e}")
            return False

    # ═══════════════════════════════════════
    # 读取 API — 控制台和 /状态 使用
    # ═══════════════════════════════════════

    def get_today(self, name: str) -> int:
        try:
            return int(self._store.kv_get(f"metric:{self._today()}:{name}") or 0)
        except (ValueError, TypeError):
            return 0

    def get_current(self, name: str) -> int:
        """读取今日已落盘值和尚未 flush 的内存增量。"""
        return self.get_today(name) + int(self._counters.get(name, 0))

    def get_latest(self, name: str) -> int:
        try:
            return int(self._store.kv_get(f"metric:latest:{name}") or 0)
        except (ValueError, TypeError):
            return 0

    def get_trend(self, name: str, days: int = 7) -> list[tuple[str, int]]:
        """获取过去 N 天的指标趋势"""
        from datetime import timedelta
        result = []
        for i in range(days - 1, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                val = int(self._store.kv_get(f"metric:{day}:{name}") or 0)
            except (ValueError, TypeError):
                val = 0
            result.append((day, val))
        return result
