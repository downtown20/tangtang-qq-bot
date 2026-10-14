"""记忆提取任务的生命周期遥测。

该模块只负责把阶段事实投影到内存指标和结构化日志，不改变队列状态、
不触发重试，也不执行任何 LLM/数据库副作用。这样观察器可以把 job 级
吞吐与 LLM 子批次指标分开读取。
"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter


_STAGE_METRICS = {
    "admitted": "extract_jobs_admitted",
    "deferred": "extract_jobs_deferred",
    "created": "extract_jobs_created",
    "lease_acquired": "extract_jobs_lease_acquired",
    "lease_missed": "extract_jobs_lease_missed",
    "llm_started": "extract_job_llm_started",
    "llm_succeeded": "extract_job_llm_succeeded",
    "llm_failed": "extract_job_llm_failed",
    "ready": "extract_jobs_ready",
    "completed": "extract_jobs_completed",
    "failed": "extract_jobs_failed",
    "requeued": "extract_jobs_requeued",
    "dead": "extract_jobs_dead",
    "missing_messages": "extract_jobs_missing_messages",
}


def queue_age_seconds(created_at: object, *, now: datetime | None = None) -> float:
    """计算任务从创建到当前的秒数；时间坏掉时返回 ``0.0``。"""
    try:
        created = datetime.strptime(str(created_at or ""), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0.0
    current = now or datetime.now()
    # Store 时间戳使用本地时区的无时区文本；调用方可能传入 aware
    # datetime（例如观察器/测试）。统一去掉 offset，避免 naive/aware
    # 相减抛 TypeError，遥测失败不应影响主状态机。
    if current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    return max(0.0, (current - created).total_seconds())


def record_extraction_stage(
    metrics,
    logger,
    stage: str,
    job: dict | None,
    *,
    started_at: float | None = None,
    error: str = "",
) -> None:
    """记录一个 job 阶段；遥测失败不能反向影响提取状态机。"""
    metric_name = _STAGE_METRICS.get(str(stage))
    if metric_name:
        try:
            metrics.incr(metric_name)
        except Exception:
            # 观测必须是旁路，测试桩或旧运行时缺少 metrics 时不能打断主链。
            pass

    data = job if isinstance(job, dict) else {}
    age = queue_age_seconds(data.get("created_at"))
    elapsed = 0.0
    if started_at is not None:
        elapsed = max(0.0, (perf_counter() - started_at) * 1000)
    fields = (
        f"stage={stage} job_id={data.get('id', '')} "
        f"direction={data.get('direction', '')} queue_age_s={age:.3f} "
        f"elapsed_ms={elapsed:.1f}"
    )
    if error:
        fields += f" error={str(error)[:120]}"
    try:
        logger.info("🧠 提取生命周期 | %s", fields)
    except Exception:
        pass
