"""
小糖糖 统一数据访问层 🗄️
所有 SQLite 操作集中在此，是项目唯一的数据库访问入口。

用法：
    store = Store("memory.db")
    person = store.get_or_create_person("123456", "小明")
    store.add_intimacy("123456", 5)
"""

import hashlib
import json
import re
import sqlite3
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .extraction_policy import summarize_extraction_backlog

logger = logging.getLogger("糖糖.Store")


def _revision_text(value) -> str:
    """把留痕字段值归一成 TEXT（memory_revisions 所有列都是 NOT NULL）。

    为什么单独一个函数而不是各处 str()：删除前快照是整行 dict，
    直接 str(dict) 会写出 Python repr（单引号、True/None），
    既不可读也不能再解析回来。JSON 至少是机器可复原的。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


# ── 信任模型：哪些 trust_level 能进入「可信召回 / 可信注入」──
# 这不是信任等级的枚举，而是**唯一一道门的放行集合**：记忆召回
# `query_memories(trusted_only=True)` 与画像注入 `Memory.active_notes()` 必须
# 放行同一批等级。历史上这两处各写一遍字面集合，加可观测性时又要写第三遍——
# 三处口径一旦漂移，界面上说「可注入 N 条」和实际召回的数字就是两个数，
# 而这种不一致在界面上看不出来（教训 #35：文档/展示与代码分家）。
# 2026-10-24 收敛到这一处：Python 判断用它，SQL 用下面生成的片段。
# ⚠ 改这个集合 = 改信任模型。`agent/health_check.py` 里 6 处只读观测 SQL
#   仍写字面量，改这里时必须同步（那里不参与放行判定，只报数）。
TRUSTED_TRUST_LEVELS = frozenset({"verified", "manual", "corrected"})
_TRUSTED_SQL_IN = "(" + ",".join(
    "'%s'" % level for level in sorted(TRUSTED_TRUST_LEVELS)) + ")"

#: legacy → verified 重验证的**置信度门槛**（2026-10-25 第五轮）。
#:
#: 为什么需要第二个判据：`update_memory_evidence` 原来只要求「有被原文逐字支持的
#: quote」，这只证明**这句话说过**，不证明**这条记忆是关于 ta 本人的事实**。
#: 小说情节、玩笑、转述、引用、假设都能通过引文校验——抽样实证：
#: `mem#25510`「小说开头设定主角…猝死（小说情节内容）」、`mem#19337`
#: 「自称是Ezfic大神」都被升成了 verified 并进入注入。
#:
#: 与 `insert_memory` 新提取路径的 `confidence >= 0.75` 是同一件事的两条路：
#: 那条路有门槛，这条路**一直没有**。取 0.7 比 0.75 略宽，因为重验证额外拿到了
#: 原文证据、证据强度更高；卡 0.75 会把大量默认置信度（0.7）的正常记忆挡在门外。
LEGACY_PROMOTION_MIN_CONFIDENCE = 0.7

# ── 注入顺序的信任优先级（2026-10-25 主人决策「corrected 优先」）──
# `TRUSTED_TRUST_LEVELS` 回答的是「**能不能**进注入」，它是**集合**——平级、无顺序。
# 但同一件事可能同时存在两条都能注入的记忆：一条是 ta 亲口纠正的结论
# （corrected），一条是提取器当时以为对的事实（verified）。集合放行了两个，
# 注入文本却把它们写成同一个样子（都是 💭），于是「信谁」变成她的自由发挥。
#
# 现场（2026-10-25 查库）：`mem#17589`（corrected「成展是大学大二学生
# （不是高中生）」）与 `mem#18121`（verified「今年7月刚靠指标生名额考上一中，
# 是高一新生」）属于同一个人、同时 active、同时注入，无任何仲裁——而后者是
# 第三人称转述被记成本人事实的产物（`chat#199048` 原文明指「穷到吃外卖」，
# 纠正流程当时只撤掉了 17475/17476，没碰到还处于 legacy 的 18121，
# 10-24 的证据回挂又把它升成了 verified）。
#
# 主人 2026-10-25 拍板：**corrected 优先**。它不替 LLM 判断哪条是真的（LLM 是
# 唯一决策者），只把「这条是 ta 亲口纠正过的」这个事实摆到台面上——注入排序
# 在前、行前带标记、块尾给一条「冲突时以它为准」的契约。判定仍归 LLM，
# 依据由系统提供（与「不让她更小心，而是给她信息」同一条纪律）。
#
# ⚠ 这是**注入呈现顺序**，不是真假判定。不要拿它去过滤/丢弃记忆——
#   那是替 LLM 做判断，且会让「她为什么想不起这条」变成无法解释的黑箱。
TRUST_INJECT_PRIORITY = {
    "corrected": 3,   # ta 亲口否定了别的说法 → 最高
    "manual": 2,      # 人工录入
    "verified": 1,    # 提取器 + 原话佐证
}


# ── 自述名字的窄句式（P1，2026-10-24）──
# 只认「名字/叫」句式，**刻意不认「我是X」**：后者绝大多数是身份词
# （"我是中国人"/"我是大二学生"/"我是欧泊搜查官"），把身份词当名字是这个
# 功能最容易犯的错（实测库里 `自称欧泊第1人`、`我是米雪儿·李呀！…` 都会命中）。
# 允许的字符集也刻意收窄到中日文/拉丁字母/间隔号：**宁可漏认，不可错认**——
# 漏认只是少一个待确认的称呼，错认会直接污染人物档案。
_NAME_CHARS = r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z·・．.]{1,12}"
_SELF_NAME_PATTERNS = (
    re.compile(r"^(?:我的)?名字(?:是|叫)\s*(" + _NAME_CHARS + r")$"),
    re.compile(r"^我叫\s*(" + _NAME_CHARS + r")$"),
    re.compile(r"^(?:请|可以)?叫我\s*(" + _NAME_CHARS + r")$"),
    re.compile(r"^(" + _NAME_CHARS + r")是我的名字$"),
)
# 代词/泛称不是名字。只列明确无歧义的几个，不做穷举。
_SELF_NAME_STOPWORDS = frozenset(
    {"我", "你", "他", "她", "它", "ta", "自己", "谁", "名字"}
)
# 名字里不会出现的虚词/语气字。用于挡住「叫我尾大狐酱就没问题」这类把
# 语气尾巴吞进名字的误判——实测原文如此，抽出来是 `尾大狐酱就没问题`。
# 宁可漏认（少一个待确认的称呼），不可错认（污染档案）。
_SELF_NAME_BLOCK_CHARS = frozenset("就没不是了吗呢吧啊哦呀嘛的你我他她它们")


_TASK_ACTION_INVARIANT_COUNT_SQL = """
    SELECT COUNT(DISTINCT a.id)
    FROM task_action_attempts a
    JOIN tasks t ON t.id=a.task_id
    LEFT JOIN send_outbox o ON o.task_attempt_id=a.id
    WHERE (
      t.current_attempt_id=a.id AND NOT (
        (t.status='sending' AND a.state IN ('persisted','outbox_pending','sending'))
        OR (t.status='done' AND (
          a.state='confirmed'
          OR NOT EXISTS (
            SELECT 1 FROM task_action_items i
            WHERE i.task_id=t.id
              AND NOT EXISTS (
                SELECT 1 FROM task_action_confirmations c
                WHERE c.task_id=i.task_id AND c.ordinal=i.ordinal
              )
          )
        ))
        OR (t.status='uncertain' AND a.state='uncertain')
        OR (t.status='failed' AND a.state IN ('failed','dead'))
        OR (t.status='partial' AND a.state='partial')
      )
    ) OR (
      o.action_id IS NOT NULL AND NOT (
        (o.status='pending' AND a.state='outbox_pending')
        OR (o.status='pending' AND a.state='sending' AND EXISTS (
          SELECT 1
          FROM task_action_children confirmed_child
          JOIN task_action_confirmations confirmed
            ON confirmed.attempt_id=confirmed_child.attempt_id
           AND confirmed.ordinal=confirmed_child.ordinal
           AND confirmed.action_id=confirmed_child.action_id
           AND confirmed.outbox_id=confirmed_child.outbox_id
          JOIN confirmed_action_facts fact
            ON fact.domain_action_id=confirmed.action_id
           AND fact.outbox_id=confirmed.outbox_id
          WHERE confirmed_child.attempt_id=a.id
            AND confirmed_child.state='confirmed'
        ))
        OR (o.status='sending' AND a.state='sending')
        OR (o.status='uncertain' AND a.state='uncertain')
        OR (o.status='dead' AND a.state='dead')
        OR (o.status='confirmed_unaccounted' AND a.state='confirmed'
            AND a.accounting_state='pending_repair')
        OR (o.status='confirmed_conflict' AND a.state='confirmed'
            AND a.accounting_state='conflict')
        OR (o.status='cancelled' AND a.state='cancelled')
      )
    ) OR (
      o.status IN ('pending','sending')
      AND (t.current_attempt_id IS NOT a.id OR t.status!='sending')
    ) OR (
      o.action_id IS NULL
      AND a.state IN ('persisted','outbox_pending','sending')
    ) OR EXISTS (
      SELECT 1 FROM task_action_children ch
      WHERE ch.attempt_id=a.id AND (
        ch.outbox_id='' OR (
          NOT EXISTS (
            SELECT 1 FROM send_outbox linked
            WHERE linked.action_id=ch.outbox_id
              AND linked.task_attempt_id=ch.attempt_id
              AND linked.ordinal=ch.ordinal
          ) AND NOT EXISTS (
            SELECT 1 FROM task_action_confirmations confirmed
            WHERE confirmed.attempt_id=ch.attempt_id
              AND confirmed.ordinal=ch.ordinal
              AND confirmed.action_id=ch.action_id
              AND confirmed.outbox_id=ch.outbox_id
          )
        )
      )
    ) OR EXISTS (
      SELECT 1 FROM task_action_children ch
      WHERE ch.attempt_id=a.id AND NOT (
        EXISTS (
          SELECT 1 FROM send_outbox linked
          WHERE linked.action_id=ch.outbox_id
            AND linked.task_attempt_id=ch.attempt_id
            AND linked.ordinal=ch.ordinal
            AND linked.status=ch.state
        ) OR (
          ch.state='confirmed' AND EXISTS (
            SELECT 1 FROM task_action_confirmations confirmed
            WHERE confirmed.attempt_id=ch.attempt_id
              AND confirmed.ordinal=ch.ordinal
              AND confirmed.action_id=ch.action_id
              AND confirmed.outbox_id=ch.outbox_id
          )
        )
      )
    ) OR EXISTS (
      SELECT 1 FROM json_each(
         CASE WHEN json_valid(a.plan_json) THEN a.plan_json
              ELSE '{"children":[]}' END, '$.children'
       ) planned
      WHERE COALESCE(json_extract(planned.value,'$.schema_version'),1)>=2
        AND (NOT EXISTS (
          SELECT 1 FROM json_each(CASE WHEN json_valid(a.plan_json)
            THEN a.plan_json ELSE '{"children":[]}' END,'$.children') legacy
          WHERE COALESCE(json_extract(legacy.value,'$.schema_version'),1)<2
        ) OR EXISTS (
          SELECT 1 FROM task_action_children projected
          WHERE projected.attempt_id=a.id
        ))
        AND NOT EXISTS (
        SELECT 1 FROM task_action_children ch
        WHERE ch.attempt_id=a.id
          AND ch.ordinal=json_extract(planned.value,'$.ordinal')
          AND ch.action_id=json_extract(planned.value,'$.action_id')
      )
    ) OR EXISTS (
      SELECT 1 FROM task_action_confirmations c
      WHERE c.attempt_id=a.id AND NOT EXISTS (
        SELECT 1 FROM send_outbox linked
        WHERE linked.action_id=c.outbox_id
          AND linked.task_attempt_id=c.attempt_id
          AND linked.ordinal=c.ordinal AND (
            (c.evidence_source='outbox_settle' AND linked.status IN (
              'sending','confirmed_unaccounted','confirmed_conflict'))
            OR (c.evidence_source='late_settle' AND linked.status IN (
              'uncertain','failed','dead','confirmed_unaccounted',
              'confirmed_conflict'))
            OR (c.evidence_source='known_confirmed_backfill' AND linked.status IN (
              'confirmed_unaccounted','confirmed_conflict'))
          )
      ) AND NOT EXISTS (
        SELECT 1 FROM confirmed_action_facts fact
        WHERE fact.domain_action_id=c.action_id
          AND fact.outbox_id=c.outbox_id
      )
    ) OR EXISTS (
      SELECT 1 FROM task_action_confirmations invalid
      WHERE json_valid(invalid.message_ids_json)=0
         OR json_type(invalid.message_ids_json,'$')!='array'
         OR CASE WHEN json_valid(invalid.message_ids_json) THEN
              json_array_length(invalid.message_ids_json)=0 ELSE 0 END
         OR EXISTS (
              SELECT 1 FROM json_each(
                  CASE WHEN json_valid(invalid.message_ids_json)
                       THEN invalid.message_ids_json ELSE '[]' END
              ) m
              WHERE m.type!='integer' OR m.value=0
                OR m.value<-(2147483648) OR m.value>2147483647
         )
    )
"""

_CONFIRMED_FACT_INTEGRITY_COUNT_SQL = """
    SELECT COUNT(*)
    FROM confirmed_action_facts f
    WHERE json_valid(f.immutable_json)=0
       OR json_type(f.immutable_json,'$')!='object'
       OR json_valid(f.actual_json)=0
       OR json_type(f.actual_json,'$')!='object'
       OR json_valid(f.message_ids_json)=0
       OR json_type(f.message_ids_json,'$')!='array'
       OR CASE WHEN json_valid(f.message_ids_json) THEN
            CASE WHEN json_array_length(f.message_ids_json)=0
                   OR EXISTS (
                       SELECT 1 FROM json_each(f.message_ids_json) m
                       WHERE m.type!='integer' OR m.value=0
                         OR m.value<-(2147483648) OR m.value>2147483647
                   )
                 THEN 1 ELSE 0 END
          ELSE 1 END
       OR CASE WHEN json_valid(f.immutable_json) THEN
            CASE WHEN json_extract(f.immutable_json,'$.action_id') IS NOT f.domain_action_id
                   OR json_extract(f.immutable_json,'$.schema_version') IS NOT f.schema_version
                   OR json_extract(f.immutable_json,'$.identity_version') IS NOT f.identity_version
                   OR json_extract(f.immutable_json,'$.source_id') IS NOT f.source_id
                   OR json_extract(f.immutable_json,'$.scope_id') IS NOT f.scope_id
                   OR json_extract(f.immutable_json,'$.kind') IS NOT f.kind
                   OR json_extract(f.immutable_json,'$.channel') IS NOT f.channel
                   OR json_extract(f.immutable_json,'$.target') IS NOT f.target
                   OR json_extract(f.immutable_json,'$.conversation_ref.projection_kind') IS NOT f.projection_kind
                   OR json_extract(f.immutable_json,'$.conversation_ref.conversation_user_id') IS NOT f.conversation_user_id
                   OR json_extract(f.immutable_json,'$.conversation_ref.group_id') IS NOT f.group_id
                   OR json_extract(f.immutable_json,'$.conversation_ref.source_chat_id') IS NOT f.source_chat_id
                   OR json_extract(f.immutable_json,'$.conversation_ref.self_memory_eligible') IS NOT f.self_memory_eligible
                   OR (json_type(f.immutable_json,'$.outbox_id') IS NOT NULL
                       AND json_extract(f.immutable_json,'$.outbox_id') IS NOT f.outbox_id)
                   OR (json_type(f.immutable_json,'$.confirmed_at') IS NOT NULL
                       AND json_extract(f.immutable_json,'$.confirmed_at') IS NOT f.confirmed_at)
                 THEN 1 ELSE 0 END
          ELSE 1 END
       OR CASE WHEN json_valid(f.immutable_json) AND json_valid(f.actual_json) THEN
            CASE WHEN json(json_extract(f.immutable_json,'$.actual')) IS NOT json(f.actual_json)
                 THEN 1 ELSE 0 END
          ELSE 1 END
       OR CASE WHEN json_valid(f.immutable_json) AND json_valid(f.message_ids_json) THEN
            CASE WHEN json(json_extract(f.immutable_json,'$.message_ids')) IS NOT json(f.message_ids_json)
                 THEN 1 ELSE 0 END
          ELSE 1 END
       OR NOT (
            EXISTS (
                SELECT 1 FROM send_outbox o
                WHERE o.action_id=f.outbox_id
                  AND o.domain_action_id=f.domain_action_id
                  AND o.domain_action_id!=''
                  AND o.status IN (
                      'confirmed','confirmed_unaccounted','confirmed_conflict'
                  )
                  AND json_valid(o.receipt_template)
                  AND json_extract(o.receipt_template,'$.action_id')
                      =f.domain_action_id
            )
            OR EXISTS (
                SELECT 1 FROM action_receipt_mailbox m
                WHERE m.scope_id=f.scope_id
                  AND m.action_id=f.domain_action_id
                  AND m.action_status='confirmed'
                  AND json_valid(m.receipt_json)
                  AND json_extract(m.receipt_json,'$.action_id')
                      =f.domain_action_id
                  AND json_extract(m.receipt_json,'$.status')='confirmed'
            )
       )
"""

_TASK_BARE_SENDING_COUNT_SQL = """
    SELECT COUNT(*)
    FROM tasks
    WHERE status='sending' AND current_attempt_id IS NULL
"""

_TASK_ACTION_PHASE2A_VERSION = "20260829_task_action_phase2a_v1"
_TASK_ACTION_PHASE2A_RECEIPT_V1_VERSION = (
    "20260829_task_action_phase2a_receipt_v1"
)
_TASK_ACTION_PHASE2A_RECEIPT_VERSION = (
    "20260829_task_action_phase2a_receipt_v2"
)
_TASK_ACTION_PHASE2A_INDEXES = {
    "idx_task_action_attempts_retry_request",
    "idx_task_action_retry_requests_accepted_expected",
    "idx_task_action_retry_requests_task_time",
}
_TASK_ACTION_PHASE2A_TRIGGERS = {
    "trg_task_action_attempt_retry_business_guard",
    "trg_task_action_attempt_retry_link_guard",
    "trg_task_action_attempt_retry_link_immutable",
    "trg_task_action_retry_request_immutable_delete",
    "trg_task_action_retry_request_immutable_update",
    "trg_task_action_retry_request_insert_guard",
}
_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS = {
    "trg_task_action_phase2a_receipt_outbox_guard",
    "trg_task_action_phase2a_receipt_request_guard",
}
_TASK_ACTION_PHASE2B_VERSION = "20260829_task_action_phase2b_v1"
# Phase 2b v1 的 child 表没有保存 schema floor；该独立小迁移为每个已
# 投影 child 固定当时的 plan schema_version，兼容旧 v1，同时阻止重启后
# 将已投影 v2 同步降级为自洽的 v1。
_TASK_ACTION_PHASE2B_FLOOR_VERSION = "20260829_task_action_phase2b_floor_v1"
_TASK_ACTION_PHASE2B_FLOOR_INDEXES = {
    "idx_task_action_projection_anchors_attempt",
}
_TASK_ACTION_PHASE2B_FLOOR_TRIGGERS = {
    "trg_task_action_projection_anchor_immutable_update",
    "trg_task_action_projection_anchor_immutable_delete",
    "trg_task_action_projection_anchor_insert_guard",
}
# confirmed_action_facts 的严格锚点独立于 Task Action 版本管理：生产库在
# Phase 2b 之前就可能已有该触发器，必须能从已知旧合同原子升级。
_CONFIRMED_FACT_ANCHOR_VERSION = "20260829_confirmed_fact_anchor_v2"
_CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM = (
    "02d5223d2a03181210a13ed9b9a2005e5256aadae5c8221b5c69c167040877e1"
)
# v2 strict-anchor 只校验 delivery identity/status；v3 还要求永久事实的
# message_ids 非空。保留 v2 指纹，允许已知合同原子升级，未知漂移仍拒绝启动。
_CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V2 = (
    "b2adc983d97a5cd52640071486d41bfc373215af00a22cae7d63ae9ea6eae71a"
)
# 2026-08-29 本轮空 evidence 护栏上线前的安全 JSON-guard 合同；生产库
# 可能已写入此 marker，必须先识别再原子升级，不能误报未知漂移。
_CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V3 = (
    "9217ea034869806e48401a992a787aec854c28412df670ec2fd6da700b61179d"
)
# Phase 2b v1 已经可能写入生产库；保留其冻结指纹，只允许从这一个
# 已知旧合同升级确认投递状态护栏。其它 checksum 漂移仍 fail-closed。
_TASK_ACTION_PHASE2B_LEGACY_V1_CHECKSUM = (
    "38ac6e6c4b7effc1f4f32987500036d5d3962561219d10cc101eea16eff3ee38"
)
_TASK_ACTION_PHASE2B_LEGACY_V1_STATUS_GUARD_CHECKSUM = (
    "dabb3e3fe35ffcaf5d2afaacdc7e2eacb3c4917cf7356bf243e390a74227de3a"
)
# 2026-08-29 曾在生产副本出现的早期已发布合同：确认 identity/evidence
# 护栏尚未收紧，但其余 Phase 2b 对象与数据可由同一受控升级补齐。
_TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM = (
    "667f733f2c9fe130bcf285f36f1ad61ffdecd00a9406a3b25188093b1e551a29"
)
_TASK_ACTION_PHASE2B_INDEXES = {
    "idx_task_action_items_task_ordinal",
    "idx_task_action_children_attempt_ordinal",
    "idx_task_action_children_item_generation",
    "idx_task_action_confirmations_task_ordinal",
    "idx_task_action_confirmations_attempt",
}
_TASK_ACTION_PHASE2B_TRIGGERS = {
    "trg_task_action_item_immutable_update",
    "trg_task_action_item_immutable_delete",
    "trg_task_action_child_identity_immutable",
    "trg_task_action_child_outbox_insert_guard",
    "trg_task_action_child_outbox_update_guard",
    "trg_task_action_child_state_guard",
    "trg_task_action_child_immutable_delete",
    "trg_task_action_confirmation_identity_guard",
    "trg_task_action_confirmation_evidence_guard",
    "trg_task_action_confirmation_immutable_update",
    "trg_task_action_confirmation_immutable_delete",
    "trg_task_action_confirmation_duplicate_event",
    "trg_task_action_outbox_delete_requires_confirmation",
}

# 迟到确认只能由已验证的平台回调进入。公开 Store API 不应成为命令层
# 可伪造“已发送”事实的入口；能力对象仅由适配层显式传递。
_LATE_CONFIRMATION_CAPABILITY = object()


def _normalize_delivery_message_ids(values) -> list[int]:
    """归一化 OneBot message_id，统一接受 signed int32 且去重。"""
    normalized: list[int] = []
    for raw in values or ():
        if isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if -(2**31) <= value <= 2**31 - 1 and value != 0 and value not in normalized:
            normalized.append(value)
    return normalized


class ConfirmedProjectionError(RuntimeError):
    """平台已确认、但本地永久事实不满足可提交条件。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code or "PROJECTION_ERROR")[:128]


class ConfirmedProjectionConflict(ConfirmedProjectionError):
    """同一 domain action 出现不同 immutable fact。"""

    def __init__(self, existing: str, incoming: str):
        super().__init__("PROJECTION_CONFLICT")
        self.existing = str(existing)
        self.incoming = str(incoming)


def _sanitize_display_name(name: str, limit: int = 40) -> str:
    """显示名净化（2026-08-15 Codex Minor 9）：昵称/外号是用户可控文本，
    流进所有提示词（插话简报/记忆块/交叉上下文/群关系图）。数据层一次净化，
    覆盖全部注入点——剥换行与控制字符（结构性提示注入的载体），截断超长。
    2026-08-15 整体审查安全 M1 补强：剥 bidi 覆盖符/零宽字符（U+202A-202E、
    U+2066-2069、U+200B、U+FEFF 等）——日志和控制台里名字可倒序伪装他人。"""
    if not name:
        return ""
    cleaned = "".join(
        ch for ch in str(name)
        if ch >= " " and ch != "\x7f"
        and not (0x202A <= ord(ch) <= 0x202E)      # bidi 覆盖
        and not (0x2066 <= ord(ch) <= 0x2069)      # bidi 隔离
        and ord(ch) not in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060)  # 零宽/词连符/格式
    )
    return cleaned.strip()[:limit]


def _parse_chat_ts(text: str) -> float:
    """`chat_log.timestamp`（'YYYY-MM-DD HH:MM:SS' 本地时间）→ unix 秒。

    会客厅镜像需要把消息时间交给前端排序/显示。解析失败返回 0，不抛异常——
    历史行里有空时间戳，一条坏数据不该让整个会话流读不出来。
    """
    if not text:
        return 0.0
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, OSError):
        return 0.0


class Store:
    """SQLite 数据访问层 —— 唯一持有数据库连接的地方"""

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        """统一连接工厂（2026-08-10）：每个连接都开启外键和超时——
        否则外键只在个别连接生效，批量删除会留下孤儿 embedding 记录"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        # SQLite 默认 recursive_triggers=OFF 时，INSERT OR REPLACE 可能
        # 通过隐式 DELETE 绕过 append-only 护栏；统一打开，确保所有连接
        # 的替换写都经过同一组不可变事实触发器。
        conn.execute("PRAGMA recursive_triggers=ON")
        return conn

    # ═══════════════════════════════════════
    # 数据库初始化
    # ═══════════════════════════════════════

    def _init_db(self):
        """初始化所有表和索引（幂等）"""
        with self._connect() as conn:
            # WAL 模式：允许并发读 + 一个写，大幅减少 SQLITE_BUSY
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            chat_log_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_log'"
            ).fetchone() is not None
            conn.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    qq_id TEXT PRIMARY KEY,
                    nickname TEXT DEFAULT '',
                    intimacy INTEGER DEFAULT 0,
                    relationship TEXT DEFAULT 'stranger',
                    notes TEXT DEFAULT '',
                    first_met TEXT DEFAULT '',
                    last_chat TEXT DEFAULT '',
                    total_chats INTEGER DEFAULT 0,
                    sex TEXT DEFAULT '',
                    age INTEGER DEFAULT 0
                )
            """)
            # 迁移：为旧版 people 表添加缺失列
            for col, typ in [("sex", "TEXT DEFAULT ''"), ("age", "INTEGER DEFAULT 0")]:
                try:
                    conn.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.execute("""
                CREATE TABLE IF NOT EXISTS group_members (
                    group_id    TEXT NOT NULL,
                    qq_id       TEXT NOT NULL,
                    card        TEXT DEFAULT '',
                    role        TEXT DEFAULT 'member',
                    title       TEXT DEFAULT '',
                    join_time   INTEGER DEFAULT 0,
                    last_sent   INTEGER DEFAULT 0,
                    updated     TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS group_info (
                    group_id     TEXT PRIMARY KEY,
                    group_name   TEXT DEFAULT '',
                    member_count INTEGER DEFAULT 0,
                    max_members  INTEGER DEFAULT 0,
                    updated      TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    importance INTEGER DEFAULT 3
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    group_id TEXT DEFAULT '',
                    is_bot_reply INTEGER DEFAULT 0,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            # 2026-08-28 任务A：原始事件字段 + 幂等键 + quarantine（可回滚标记，
            # 禁止删除唯一历史——synthetic 行只标记，不物理删除）
            for col, col_type in [
                ("event_key", "TEXT DEFAULT ''"),      # scope:platform_message_id 幂等键
                ("raw_message", "TEXT DEFAULT ''"),    # 原始 CQ 文本（合并视图不得覆盖）
                ("segments", "TEXT DEFAULT ''"),       # typed segments JSON（Task B 预生成）
                ("is_synthetic", "INTEGER DEFAULT 0"), # 批处理合成行标记
                ("quarantined_at", "TEXT DEFAULT ''"), # 非空=已 quarantine（可回滚）
                # 真正的发言人账号（2026-10-24）：桌面端「以主人身份代发」到 QQ 时，
                # `is_bot_reply=1`（QQ 上别人看到的是糖糖的号在说）但打字的是主人。
                # 空 = 按 is_bot_reply 推断（历史行全部为空，行为不变）。
                ("speaker_qq", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE chat_log ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            # 幂等唯一索引：同一 (scope, platform_message_id) 只落一次
            # （NapCat 重投/重连重放不重复记账；message_id=0 不设 event_key 不强去重）
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_log_event_key
                ON chat_log(event_key) WHERE event_key != ''
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    source TEXT DEFAULT 'auto',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_qq TEXT NOT NULL,
                    description TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)
            # 2026-08-15 群提醒支持：旧库迁移补 group_id 列
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN group_id TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # P0-C（2026-08-28）：typed action payload + 幂等 key——旧行默认空串，
            # 读取安全（无 payload = text-only 老行为）
            for col, col_type in [
                ("action_payload", "TEXT DEFAULT ''"),   # JSON: {text,sticker_emotion,voice_text}
                ("idempotency_key", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
                ON tasks(idempotency_key) WHERE idempotency_key != ''
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_unique
                ON aliases(alias, qq_id)
            """)
            # 2026-10-24 P1：自称名字要能追溯「依据的是哪条记忆」。没有这个指针，
            # 档案里凭空多出一个外号，事后没人知道它从哪来、该不该删。
            try:
                conn.execute("ALTER TABLE aliases ADD COLUMN memory_id INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 遗忘曲线列（兼容旧库：列不存在时添加）
            for col, col_type in [
                ("last_recalled", "TEXT DEFAULT ''"),
                ("recall_count", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # 认知类型 + 置信度 + 来源（R1-1：记忆认知类型体系）
            for col, col_type in [
                ("cognitive", "TEXT DEFAULT 'semantic'"),
                ("confidence", "REAL DEFAULT 0.7"),
                ("origin", "TEXT DEFAULT 'extracted'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # 真值状态（2026-08-16 批 2 纠正闭环）：active=有效 / retracted=被否定 /
            # superseded=被纠正替代。撤销不改 importance（importance 是相关性不是真值，
            # 且 set_memory_importance 钳最小值 1），统一走 status 过滤。
            # 注：cluster_facts 的 status 在表创建处 ALTER（表在下方才建）
            try:
                conn.execute("ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'")
            except sqlite3.OperationalError:
                pass
            # 自我记忆来源隔离：旧记录为空表示历史上未绑定对象，读侧不再
            # 将其当作当前用户的证据；新记录必须写入目标用户/群组。
            for col, col_type in [
                ("target_qq", "TEXT DEFAULT ''"),
                ("source_group_id", "TEXT DEFAULT ''"),
                ("evidence_ids", "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # 千人长期记忆真值模型（2026-08-26）：旧行默认不可信，只有经过
            # 证据校验、人工写入或纠正闭环的记忆才能进入自动对话。
            truth_columns = {
                "trust_level": "TEXT DEFAULT 'legacy_unverified'",
                "retention": "TEXT DEFAULT 'normal'",
                "event_time": "TEXT DEFAULT ''",
                "ingested_at": "TEXT DEFAULT ''",
                "valid_from": "TEXT DEFAULT ''",
                "valid_to": "TEXT DEFAULT ''",
                "superseded_by": "INTEGER",
                "idempotency_key": "TEXT DEFAULT ''",
            }
            existing_memory_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            for col, col_type in truth_columns.items():
                if col not in existing_memory_columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {col_type}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL DEFAULT '',
                    applied_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    memory_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'supports',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (memory_id, chat_id, relation),
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                    FOREIGN KEY (chat_id) REFERENCES chat_log(id) ON DELETE RESTRICT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_evidence_chat "
                "ON memory_evidence(chat_id)"
            )
            # legacy 记忆的可信升级必须留下可回溯的人工复核记录；不能只靠
            # memories.trust_level 的最终值判断是谁、基于什么证据做的升级。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_reverification_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    previous_trust_level TEXT NOT NULL,
                    new_trust_level TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL,
                    evidence_quote TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE RESTRICT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_reverification_memory "
                "ON memory_reverification_events(memory_id, id)"
            )
            # 拒绝/延后不改变 trust_level，因此不能伪装成上面的信任迁移事件。
            # 独立保存 disposition，供审计队列按“最近一次人工决定”过滤。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_reverification_dispositions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('rejected','deferred')),
                    evidence_ids TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE RESTRICT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_reverification_disposition "
                "ON memory_reverification_dispositions(memory_id, id)"
            )
            # 记忆变更流水（2026-10-24）：append-only，只 INSERT，永不 UPDATE/DELETE。
            #
            # 为什么需要它：现有 status/valid_to/superseded_by 只能表达「整行被替代」
            # 这一种事件。而「value 被就地改写」「行被物理删除」这两类事件事后完全
            # 不可复原——语义去重命中（相似度>0.85）时旧表述是永久消失的。
            #
            # 为什么不加外键：这张表的职责之一就是记录「行已被删除」，加
            # ON DELETE CASCADE 会让删除时流水跟着消失（自相矛盾），加 RESTRICT
            # 又会禁止删除。所以 entity_id 是裸值，不对 memories.id 设约束。
            #
            # entity: memories / cluster_facts / fact_clusters / people
            # op:     update / status / supersede / retract / fulfill / delete / migrate
            # field:  被改的列名（value/importance/trust_level/notes/summary/...）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL DEFAULT '',
                    op TEXT NOT NULL,
                    field TEXT NOT NULL DEFAULT '',
                    before_value TEXT NOT NULL DEFAULT '',
                    after_value TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_revisions_entity "
                "ON memory_revisions(entity, entity_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_revisions_qq "
                "ON memory_revisions(qq_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_revisions_actor "
                "ON memory_revisions(actor, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_subject_truth "
                "ON memories(qq_id, status, trust_level, importance DESC, event_time DESC, id DESC)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_idempotency "
                "ON memories(idempotency_key) WHERE idempotency_key != ''"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS extraction_cursors (
                    pipeline TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    cursor_chat_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (pipeline, qq_id, direction)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS extraction_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL UNIQUE,
                    pipeline TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL DEFAULT '',
                    range_start INTEGER NOT NULL,
                    range_end INTEGER NOT NULL,
                    message_ids TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    protocol_version TEXT NOT NULL DEFAULT 'memory-v1',
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            extraction_job_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(extraction_jobs)"
                ).fetchall()
            }
            if "message_ids" not in extraction_job_columns:
                conn.execute(
                    "ALTER TABLE extraction_jobs ADD COLUMN "
                    "message_ids TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_extraction_jobs_resume "
                "ON extraction_jobs(qq_id, direction, status, range_start, range_end)"
            )

            # ADR-002：只交付已发生动作的事实，不拥有发送、重试或重放。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_receipt_mailbox (
                    scope_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    source_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target TEXT NOT NULL,
                    action_status TEXT NOT NULL,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    receipt_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'deliverable',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (scope_id, action_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_receipt_delivery "
                "ON action_receipt_mailbox(scope_id,state,created_at,ordinal)"
            )

            # ADR-003：mailbox 是可消费投递箱；以下三表才是不可清理的
            # known-confirmed 业务事实、窗口真值和冲突审计。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS confirmed_action_facts (
                    domain_action_id TEXT PRIMARY KEY,
                    outbox_id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    identity_version INTEGER NOT NULL DEFAULT 1,
                    source_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('group','private')),
                    target TEXT NOT NULL,
                    actor_kind TEXT NOT NULL DEFAULT 'bot' CHECK(actor_kind='bot'),
                    projection_kind TEXT NOT NULL
                        CHECK(projection_kind IN ('conversation_reply','none')),
                    conversation_user_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    source_chat_id INTEGER,
                    self_memory_eligible INTEGER NOT NULL DEFAULT 0
                        CHECK(self_memory_eligible IN (0,1)),
                    actual_json TEXT NOT NULL,
                    message_ids_json TEXT NOT NULL,
                    immutable_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    chat_log_id INTEGER UNIQUE,
                    CHECK(
                        (projection_kind='none' AND conversation_user_id=''
                         AND group_id='' AND chat_log_id IS NULL
                         AND self_memory_eligible=0)
                        OR
                        (projection_kind='conversation_reply'
                         AND conversation_user_id!='' AND chat_log_id IS NOT NULL)
                    ),
                    FOREIGN KEY(chat_log_id) REFERENCES chat_log(id)
                )
            """)
            # known-confirmed 是永久事实，只能由统一发送账本写入；任何
            # UPDATE/DELETE 都会破坏记忆、窗口投影和审计链，必须硬拒绝。
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_confirmed_action_fact_immutable_update
                BEFORE UPDATE ON confirmed_action_facts
                BEGIN SELECT RAISE(ABORT,'confirmed action fact is append-only'); END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_confirmed_action_fact_immutable_delete
                BEFORE DELETE ON confirmed_action_facts
                BEGIN SELECT RAISE(ABORT,'confirmed action fact is append-only'); END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_confirmed_action_fact_insert_anchor
                BEFORE INSERT ON confirmed_action_facts
                WHEN NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND (o.domain_action_id='' OR o.domain_action_id=NEW.domain_action_id)
                ) AND NOT EXISTS (
                    SELECT 1 FROM action_receipt_mailbox m
                    WHERE m.scope_id=NEW.scope_id AND m.action_id=NEW.domain_action_id
                )
                BEGIN SELECT RAISE(ABORT,'confirmed action fact lacks delivery anchor'); END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_confirmed_action_fact_strict_anchor
                BEFORE INSERT ON confirmed_action_facts
                WHEN NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.domain_action_id=NEW.domain_action_id
                      AND o.domain_action_id!=''
                      AND o.status IN (
                          'sending','confirmed','confirmed_unaccounted',
                          'confirmed_conflict'
                      )
                      AND json_valid(o.receipt_template)
                      AND json_extract(o.receipt_template,'$.action_id')
                          =NEW.domain_action_id
                ) AND NOT EXISTS (
                    SELECT 1 FROM action_receipt_mailbox m
                    WHERE m.scope_id=NEW.scope_id
                      AND m.action_id=NEW.domain_action_id
                      AND m.action_status='confirmed'
                      AND json_valid(m.receipt_json)
                      AND json_extract(m.receipt_json,'$.action_id')
                          =NEW.domain_action_id
                      AND json_extract(m.receipt_json,'$.status')='confirmed'
                )
                BEGIN SELECT RAISE(ABORT,'confirmed action fact lacks strict anchor'); END
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_window_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    domain_action_id TEXT NOT NULL UNIQUE,
                    scope_id TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('group','private')),
                    actor_kind TEXT NOT NULL CHECK(actor_kind='bot'),
                    conversation_user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    chat_log_id INTEGER NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY(domain_action_id)
                        REFERENCES confirmed_action_facts(domain_action_id),
                    FOREIGN KEY(chat_log_id) REFERENCES chat_log(id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_window_scope_time "
                "ON conversation_window_events("
                "scope_id,conversation_user_id,occurred_at,id)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_projection_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain_action_id TEXT NOT NULL,
                    outbox_id TEXT NOT NULL UNIQUE,
                    existing_json TEXT NOT NULL,
                    incoming_json TEXT NOT NULL,
                    detected_at TEXT NOT NULL
                )
            """)

            # 明确人工入口可从历史 origin 安全恢复；其余旧行保持 legacy。
            conn.execute(
                "UPDATE memories SET trust_level='manual' "
                "WHERE origin='manual' AND COALESCE(trust_level,'') IN ('','legacy_unverified')"
            )
            conn.execute(
                "UPDATE memories SET trust_level='corrected' "
                "WHERE origin='corrected' AND COALESCE(trust_level,'') IN ('','legacy_unverified')"
            )
            conn.execute(
                "UPDATE memories SET ingested_at=timestamp "
                "WHERE COALESCE(ingested_at,'')=''"
            )

            # 仅首次验证并迁移历史 CSV 证据。校验失败的行保留为 legacy 线索，
            # 不猜测来源、不用相似度自动认领原文。
            truth_migration = "20260826_memory_truth_v1"
            migrated = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (truth_migration,)
            ).fetchone()
            if not migrated:
                legacy_rows = conn.execute(
                    "SELECT id, qq_id, origin, target_qq, source_group_id, evidence_ids "
                    "FROM memories WHERE COALESCE(evidence_ids,'') != '' "
                    "AND COALESCE(trust_level,'legacy_unverified')='legacy_unverified'"
                ).fetchall()
                promoted = 0
                for mem_id, mem_qq, mem_origin, target_qq, source_group, evidence in legacy_rows:
                    evidence_rows = self._validate_memory_evidence(
                        conn, mem_qq, mem_origin, target_qq, source_group, evidence,
                    )
                    if not evidence_rows:
                        continue
                    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.executemany(
                        "INSERT OR IGNORE INTO memory_evidence "
                        "(memory_id, chat_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                        [(mem_id, row[0], created) for row in evidence_rows],
                    )
                    event_time = max(str(row[4] or "") for row in evidence_rows)
                    conn.execute(
                        "UPDATE memories SET trust_level='verified', event_time=? WHERE id=?",
                        (event_time, mem_id),
                    )
                    promoted += 1
                conn.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                    (truth_migration, "memory-truth-v1", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                if promoted:
                    logger.warning(f"🧠 已校验并迁移 {promoted} 条历史记忆证据")

            # 摘要只能继承可追溯的原始证据；旧版曾仅凭“来源记忆行可信”
            # 就把 profile/fact synthesis 标成 verified，导致无 evidence 的
            # 合成事实进入 trusted_only 召回。保留原行作审计，但降为线索级，
            # 让它退出自动记忆注入（幂等，下一次启动不会重复改变已降级行）。
            unanchored_ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT id FROM memories "
                    "WHERE origin='summarized' "
                    "AND COALESCE(status,'active')='active' "
                    "AND COALESCE(trust_level,'legacy_unverified')='verified' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM memory_evidence e WHERE e.memory_id=memories.id"
                    ") "
                    "ORDER BY id"
                ).fetchall()
            ]
            if unanchored_ids:
                conn.execute(
                    "UPDATE memories SET trust_level='legacy_unverified' "
                    "WHERE id IN (%s)" % ",".join("?" * len(unanchored_ids)),
                    unanchored_ids,
                )
                for mem_id in unanchored_ids:
                    owner = conn.execute(
                        "SELECT qq_id FROM memories WHERE id=?", (mem_id,)
                    ).fetchone()
                    self._record_revision(
                        conn, entity="memories", entity_id=mem_id,
                        qq_id=str(owner[0] or "") if owner else "",
                        op="migrate", field="trust_level",
                        before="verified", after="legacy_unverified",
                        reason=("迁移：无原始证据的摘要不得继承 verified"
                                "（保留审计行，但退出自动注入）"),
                        actor="migration:unanchored_summary",
                    )
            unanchored_summaries = len(unanchored_ids)
            if unanchored_summaries:
                logger.warning(
                    "🧹 已隔离 %s 条无原始证据的摘要记忆（保留审计）",
                    unanchored_summaries,
                )
            # 旧版 origin=self 是 bot 全局池，没有目标用户与原始消息，已造成
            # 跨用户串承诺。保留审计记录但退出全部读路径；幂等迁移。
            retired_ids = [
                (int(row[0]), str(row[1] or "")) for row in conn.execute(
                    "SELECT id, qq_id FROM memories "
                    "WHERE origin='self' AND COALESCE(target_qq,'')='' "
                    "AND COALESCE(status,'active')='active' ORDER BY id"
                ).fetchall()
            ]
            if retired_ids:
                conn.execute(
                    "UPDATE memories SET status='retracted' "
                    "WHERE id IN (%s)" % ",".join("?" * len(retired_ids)),
                    [item[0] for item in retired_ids],
                )
                for mem_id, mem_qq in retired_ids:
                    self._record_revision(
                        conn, entity="memories", entity_id=mem_id,
                        qq_id=mem_qq, op="retract", field="status",
                        before="active", after="retracted",
                        reason=("迁移：旧版 origin=self 无目标用户，已造成跨用户串承诺，"
                                "退出全部读路径"),
                        actor="migration:self_memory_scope",
                    )
            retired = len(retired_ids)
            if retired:
                logger.warning(f"🧹 已隔离 {retired} 条无目标的旧自我记忆（保留审计）")

            # 旧版 action_completed 只有聊天原文锚点，没有 confirmed action
            # 回执，无法证明外部动作真的发生。保留原行供审计，但一次性降为
            # legacy_unverified，使 trusted_only 自忆召回 fail-closed；新版提取
            # 会同时保存 confirmed_action_id，不受该迁移影响。
            action_receipt_migration = "20260831_self_action_receipt_v1"
            if not conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    (action_receipt_migration,),
            ).fetchone():
                action_ids = [
                    (int(row[0]), str(row[1] or "")) for row in conn.execute(
                        "SELECT id, qq_id FROM memories "
                        "WHERE origin='self' AND key='action_completed' "
                        "AND COALESCE(status,'active')='active' ORDER BY id"
                    ).fetchall()
                ]
                if action_ids:
                    conn.execute(
                        "UPDATE memories SET trust_level='legacy_unverified' "
                        "WHERE id IN (%s)" % ",".join("?" * len(action_ids)),
                        [item[0] for item in action_ids],
                    )
                    for mem_id, mem_qq in action_ids:
                        self._record_revision(
                            conn, entity="memories", entity_id=mem_id,
                            qq_id=mem_qq, op="migrate", field="trust_level",
                            before="verified", after="legacy_unverified",
                            reason=("迁移：旧完成事实只有聊天原文锚点、无 confirmed action "
                                    "回执，无法证明外部动作真的发生"),
                            actor="migration:self_action_receipt",
                        )
                unverified_actions = len(action_ids)
                conn.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) "
                    "VALUES (?, ?, ?)",
                    (action_receipt_migration, "self-action-receipt-v1",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                if unverified_actions:
                    logger.warning(
                        "🧹 已隔离 %s 条缺少 confirmed action 回执的旧完成事实",
                        unverified_actions,
                    )
            # 画像脏标（2026-08-16 批 2）：纠正后置 1，禁止注入旧 notes；
            # 后台重合成成功后才原子换入并清零
            try:
                conn.execute("ALTER TABLE people ADD COLUMN notes_dirty INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            people_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()
            }
            if "notes_trust_level" not in people_columns:
                conn.execute(
                    "ALTER TABLE people ADD COLUMN notes_trust_level "
                    "TEXT DEFAULT 'legacy_unverified'"
                )
            if "notes_source_ids" not in people_columns:
                conn.execute(
                    "ALTER TABLE people ADD COLUMN notes_source_ids TEXT DEFAULT ''"
                )
            # relationship_summary 列（关系档案）
            try:
                conn.execute("ALTER TABLE people ADD COLUMN relationship_summary TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE people ADD COLUMN relationship_updated INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # birthday 列（Feature 3: 生日系统）
            try:
                conn.execute("ALTER TABLE people ADD COLUMN birthday TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # 每日首次互动加分标记（2026-08-10：与 last_chat 分离——last_chat 每天更新，
            # 用它判断"是否加过分"会导致每日 +5 永不触发）
            try:
                conn.execute("ALTER TABLE people ADD COLUMN last_bonus_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # 🆕 向量嵌入表——语义记忆检索
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id INTEGER PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_embeddings_id ON memory_embeddings(memory_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_embedding_failures (
                    memory_id INTEGER PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    last_attempt_at TEXT NOT NULL,
                    retry_after TEXT NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_embedding_failures_retry "
                "ON memory_embedding_failures(retry_after)"
            )
            # 🆕 R3-1: Episode 聚合表——将零散 episodic 记忆聚合为结构化事件
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    source_group_id TEXT DEFAULT '__legacy_unscoped__',
                    title TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    time_start TEXT NOT NULL,
                    time_end TEXT NOT NULL,
                    paragraph_ids TEXT DEFAULT '',
                    feeling TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_qq ON episodes(qq_id)")
            episode_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()
            }
            if "source_group_id" not in episode_columns:
                # 旧 episode 可能已经混合群/私，不能把它们误认成私聊。
                conn.execute(
                    "ALTER TABLE episodes ADD COLUMN source_group_id TEXT "
                    "DEFAULT '__legacy_unscoped__'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_scope_time "
                "ON episodes(qq_id, source_group_id, time_end)"
            )
            # 🧠 心情日志（2026-08-10 收口：表从 mood_tracker.py 移入 Store 统一管理）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mood_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    score REAL NOT NULL,
                    message_count INTEGER DEFAULT 1
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mood_log_qq_date ON mood_log(qq_id, date)")
            # 🆕 聊天索引——私聊消息清洗后向量化，供 tool calling 语义搜索
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_index (
                    chat_id INTEGER PRIMARY KEY,
                    qq_id TEXT NOT NULL,
                    clean_text TEXT NOT NULL,
                    embedding BLOB,
                    timestamp TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_index_qq ON chat_index(qq_id)")
            # 🆕 结构化记忆层——事实簇 + 原子事实
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fact_clusters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_qq TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    fact_count INTEGER DEFAULT 0,
                    permanence TEXT DEFAULT 'normal',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    embedding BLOB
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_clusters_subject ON fact_clusters(subject_qq)")
            # UNIQUE 索引——防止并发 INSERT 产生重复簇（TOCTOU 修复）
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_clusters_subject_title ON fact_clusters(subject_qq, title)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cluster_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster_id INTEGER NOT NULL,
                    subject_qq TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    source_qq TEXT DEFAULT '',
                    evidence_ids TEXT DEFAULT '',
                    confidence REAL DEFAULT 1.0,
                    importance INTEGER DEFAULT 5,
                    created_at TEXT NOT NULL,
                    embedding BLOB,
                    FOREIGN KEY (cluster_id) REFERENCES fact_clusters(id) ON DELETE CASCADE
                )
            """)
            # 真值状态列（2026-08-16 批 2）：表定义在前、ALTER 在后——
            # 建表前的 ALTER 会报 no such table 被静默吞掉
            try:
                conn.execute("ALTER TABLE cluster_facts ADD COLUMN status TEXT DEFAULT 'active'")
            except sqlite3.OperationalError:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_facts_cluster ON cluster_facts(cluster_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_facts_subject ON cluster_facts(subject_qq)")
            # UNIQUE 约束——防止并发 INSERT 产生重复事实
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cluster_facts_fact ON cluster_facts(cluster_id, fact)")
            # 通用键值存储——替代零散的 JSON 文件（.mood_state.json 等）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated TEXT NOT NULL
                )
            """)
            # 群友反馈——感知糖糖的回复是否被喜欢
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_reply TEXT NOT NULL,
                    user_reaction TEXT,
                    user_qq TEXT NOT NULL,
                    group_id TEXT DEFAULT '',
                    sentiment TEXT DEFAULT 'neutral',
                    confidence REAL DEFAULT 0.5,
                    reply_ms INTEGER DEFAULT 0,
                    direction_verified INTEGER DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            """)
            # 新库直接建索引；旧大库必须通过 tools/迁移_chat_log_索引.py 显式
            # 迁移，避免首次启动在事件循环所在主线程静默阻塞数秒。
            chat_log_indexes = {
                "idx_chat_log_qq_user_id":
                    "CREATE INDEX idx_chat_log_qq_user_id "
                    "ON chat_log(qq_id, is_bot_reply, id)",
                # 覆盖自治积压快照的过滤、游标和 oldest_at 读取；旧大库由
                # tools/迁移_chat_log_索引.py 在 bot 停止时显式构建，避免启动阻塞。
                "idx_chat_log_extraction_backlog":
                    "CREATE INDEX idx_chat_log_extraction_backlog "
                    "ON chat_log(qq_id, is_bot_reply, quarantined_at, id, timestamp)",
                "idx_chat_log_group_id":
                    "CREATE INDEX idx_chat_log_group_id ON chat_log(group_id, id)",
                # 覆盖群/私聊时间线查询，旧大库由显式迁移脚本构建，避免启动阻塞。
                "idx_chat_log_group_time":
                    "CREATE INDEX idx_chat_log_group_time "
                    "ON chat_log(group_id, timestamp DESC, id DESC)",
                "idx_chat_log_group_user_time":
                    "CREATE INDEX idx_chat_log_group_user_time "
                    "ON chat_log(group_id, qq_id, timestamp DESC, id DESC)",
            }
            existing_chat_indexes = {
                row[1] for row in conn.execute("PRAGMA index_list('chat_log')")
            }
            if not chat_log_existed:
                for index_sql in chat_log_indexes.values():
                    conn.execute(index_sql)
            else:
                missing_chat_indexes = set(chat_log_indexes) - existing_chat_indexes
                if missing_chat_indexes:
                    logger.warning(
                        "⚠️ chat_log 缺少性能索引 %s；请在启动外运行 "
                        "python tools/迁移_chat_log_索引.py",
                        ", ".join(sorted(missing_chat_indexes)),
                    )
            # 旧反馈没有“这条反应确实指向糖糖回复”的证据。旧私聊按会话天然
            # 一对一，可迁移为可信；旧群聊保持 0，退出 LLM 反思读路径。
            feedback_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(feedback)")
            }
            if "direction_verified" not in feedback_columns:
                conn.execute(
                    "ALTER TABLE feedback ADD COLUMN direction_verified INTEGER DEFAULT 0"
                )
                conn.execute(
                    "UPDATE feedback SET direction_verified=1 "
                    "WHERE COALESCE(group_id,'')=''"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_qq)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_sentiment ON feedback(sentiment)")

            # 🧘 每日摘要（情节记忆——每个群每天一条）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    message_count INTEGER DEFAULT 0,
                    topics TEXT DEFAULT '[]',
                    mentions_of_bot INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT ''
                )
            """)
            # 2026-08-10：去重 + 唯一约束——重启后重复反思不再插入重复摘要
            conn.execute(
                "DELETE FROM daily_digests WHERE id NOT IN "
                "(SELECT MAX(id) FROM daily_digests GROUP BY date, group_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_digests_date_group "
                "ON daily_digests(date, group_id)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_digests_date ON daily_digests(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_digests_group ON daily_digests(group_id)")

            # 🧘 糖糖日记（有情感分量的自我记录）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tangtang_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    mood TEXT DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_date ON tangtang_journal(date)")

            # 📋 意见征集（2026-08-16）——主人发起、糖糖私聊发布、窗口收集
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opinion_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',   -- open/closed
                    created_at TEXT NOT NULL DEFAULT '',
                    closed_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opinion_participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    qq_id TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',  -- pending/participating/done/refused/no_reply
                    last_msg_ts TEXT DEFAULT '',
                    claim_token TEXT DEFAULT '',
                    claim_ts TEXT DEFAULT '',
                    UNIQUE(campaign_id, qq_id)
                )
            """)
            participant_columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(opinion_participants)"
                ).fetchall()
            }
            if "claim_token" not in participant_columns:
                conn.execute(
                    "ALTER TABLE opinion_participants ADD COLUMN claim_token TEXT DEFAULT ''"
                )
            if "claim_ts" not in participant_columns:
                conn.execute(
                    "ALTER TABLE opinion_participants ADD COLUMN claim_ts TEXT DEFAULT ''"
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_opinion_parts_campaign ON opinion_participants(campaign_id)")
            # 旧版本没有数据库级的单 open 约束；先把历史上可能遗留的多个
            # open 活动收敛为最新一条，再创建唯一索引，避免升级时直接被
            # ``UNIQUE constraint failed`` 卡死。关闭旧活动只修复非法状态，
            # 不删除参与者/消息，且保留一条可审计的启动告警。
            open_rows = conn.execute(
                "SELECT id FROM opinion_campaigns WHERE status='open' ORDER BY id DESC"
            ).fetchall()
            if len(open_rows) > 1:
                closed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                stale_ids = [int(row[0]) for row in open_rows[1:]]
                conn.executemany(
                    "UPDATE opinion_campaigns SET status='closed', closed_at=? "
                    "WHERE id=? AND status='open'",
                    [(closed_at, campaign_id) for campaign_id in stale_ids],
                )
                logger.warning(
                    "📋 意见征集迁移：发现 %d 个重复 open 活动，已关闭旧活动，保留 campaign_id=%s",
                    len(stale_ids), open_rows[0][0],
                )
            # 跨进程/重复实例也只能存在一个 open 活动；应用层先查再写不是并发安全边界。
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_opinion_one_open "
                "ON opinion_campaigns(status) WHERE status='open'"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS opinion_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    qq_id TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    message TEXT NOT NULL,
                    is_bot INTEGER DEFAULT 0,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_opinion_msgs_campaign ON opinion_messages(campaign_id)")

            # 2026-08-17 对话质量遥测：每条已发送回复一行聚合指标（无正文，
            # 只有可计数特征——隐私安全；盲测/A-B 的观测基础，Codex P2 精简版。
            # 终审修复：target_key 为稳定哈希会话键，不落原始 QQ/群号；
            # model/scenario 字段无法在统一发送点拿到——恒空字段不如不建）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reply_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT DEFAULT (datetime('now','localtime')),
                    target_key TEXT,
                    target_type TEXT,
                    reply_len INTEGER,
                    meow INTEGER,
                    bracket_action INTEGER,
                    question_end INTEGER,
                    assistant_tell INTEGER,
                    hard_turn INTEGER,
                    bubble_count INTEGER
                )
            """)
            # 2026-10-14 台账 T22：`CREATE TABLE IF NOT EXISTS` 对**已存在**的表
            # 不会补列——老库必须显式迁移，否则新 INSERT 会因列数不符而整条失败。
            # 沿用本文件既有写法（PRAGMA 读列名 + ALTER ADD COLUMN，见 send_outbox）。
            _rm_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(reply_metrics)")
            }
            if "bubble_count" not in _rm_columns:
                conn.execute(
                    "ALTER TABLE reply_metrics ADD COLUMN bubble_count INTEGER"
                )

            # P2 发送 outbox：仅保存可重试的网络/网关失败。sending 状态说明
            # 请求可能已经离开本进程；重启时转 uncertain，禁止盲目重放。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS send_outbox (
                    action_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    receipt_template TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            outbox_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")
            }
            if "receipt_template" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE send_outbox ADD COLUMN "
                    "receipt_template TEXT NOT NULL DEFAULT ''"
                )
            outbox_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")
            }
            for column, declaration in (
                ("domain_action_id", "TEXT NOT NULL DEFAULT ''"),
                ("confirmed_message_ids", "TEXT NOT NULL DEFAULT ''"),
                ("confirmed_at", "TEXT NOT NULL DEFAULT ''"),
                ("projection_error", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in outbox_columns:
                    conn.execute(
                        f"ALTER TABLE send_outbox ADD COLUMN {column} {declaration}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_send_outbox_due "
                "ON send_outbox(status,next_retry_at,created_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_send_outbox_domain_action "
                "ON send_outbox(domain_action_id) WHERE domain_action_id != ''"
            )

            # ADR-005 Phase 0：只建立 durable attempt/plan/outbox 关联契约；
            # 尚不启用 TaskManager 的 task-linked 发送。迁移必须整体成功，
            # 不能给旧库留下半截列或失去历史 outbox。
            self._migrate_task_action_schema(conn)
            self._migrate_confirmed_fact_anchor_schema(conn)
            fact_integrity_violations = self._confirmed_fact_integrity_count_conn(conn)
            if fact_integrity_violations:
                raise sqlite3.OperationalError(
                    "confirmed action fact integrity violations: "
                    f"{fact_integrity_violations}"
                )

            # P1 持久 inbox：适配层先登记再执行。claimed 可安全恢复为 received；
            # executing 在崩溃后只能标 uncertain（回调可能已产生外部副作用），
            # 禁止为了“不丢消息”而盲目重放导致重复回复/命令。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbound_events_status "
                "ON inbound_events(status,updated_at)"
            )

            # P1-4a：主动事件先落不可变来源，再由独立状态行承载后续
            # claim/决策/执行生命周期。来源表不写入“已发送”等推断事实，
            # 幂等键和 payload 快照一旦落盘不可被同事件覆盖。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('group','private')),
                    target TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK(
                        json_valid(payload_json)=1
                        AND json_type(payload_json,'$')='object'
                    ),
                    created_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_event_state (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                        'pending','claimed','decided','executing',
                        'confirmed','failed','uncertain','skipped'
                    )),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    decision_run_id TEXT NOT NULL DEFAULT '',
                    action_plan_id TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES proactive_events(event_id)
                        ON DELETE RESTRICT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_proactive_event_state_status "
                "ON proactive_event_state(status,updated_at)"
            )

            # P0-1c：LLM 回合终态事实。只保存 completed/failed，避免把
            # 尚未结束的 running 状态误当成可核验决策；run_id 幂等且内容漂移拒绝。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decision_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    event_key TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('completed','failed')),
                    decision TEXT NOT NULL DEFAULT '',
                    tool_calls_json TEXT NOT NULL CHECK(
                        json_valid(tool_calls_json)=1
                        AND json_type(tool_calls_json,'$')='array'
                    ),
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_runs_scope_time "
                "ON decision_runs(scope_id,recorded_at)"
            )

            # ── 回合痕迹（2026-10-24 会客厅「看到她在想什么」）─────────────
            # 为什么不塞进 decision_runs：那张表是**决策事实契约**——frozen
            # dataclass、status/decision 的 CHECK 约束、还有接线闸门盯着它。
            # 思维链是**观察数据**：可能几千字、可能含内部策略、要能按天清理。
            # 混进审计事实表会让「契约」跟着 UI 需求变形，也让清理无从下手。
            #
            # run_id 与 decision_runs 同键（一回合一条），event_key 可回挂
            # chat_log —— 三者构成「这句回复 ← 这次决策 ← 她当时怎么想的」链条。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    event_key TEXT NOT NULL DEFAULT '',
                    scope_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    reasoning_text TEXT NOT NULL DEFAULT '',
                    tool_steps_json TEXT NOT NULL DEFAULT '[]',
                    prompt_chars INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_traces_scope "
                "ON turn_traces(scope_id,id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_traces_event "
                "ON turn_traces(event_key)"
            )
            # 2026-10-25 第三轮自查：加 `prompt_digest`——原表只记「她怎么想的」
            # （reasoning）和「她查了什么」（tool_steps），**没有「她收到了什么」**。
            # 于是「她记错了」这类问题只能靠本地复刻 prompt + 日志推断（第 1、2 轮
            # 都卡在这里）。存摘要不存全文：要回溯的是结构（哪些段、哪些背景块、
            # 历史多少条），不是全文。老库走 ALTER 迁移。
            _tt_cols = {_r[1] for _r in conn.execute(
                "PRAGMA table_info(turn_traces)").fetchall()}
            if "prompt_digest" not in _tt_cols:
                conn.execute(
                    "ALTER TABLE turn_traces "
                    "ADD COLUMN prompt_digest TEXT NOT NULL DEFAULT ''"
                )

            # ── 未闭合事件（open_loops，2026-10-24 设计一）──
            # 「她说过要去做 X」不是一条记忆，是一个**有状态、会被改写**的对象：
            # 问了几次、有没有结果、什么时候该放弃。所以独立成表而不是
            # `memories.key='open_loop'`——记忆是只读历史，硬塞进去会让
            # memory_revisions 被高频状态改动淹没（store 里明写的「不记纯计数」边界）。
            # 方案：docs/开发规划/未闭合事件_技术方案_20261024.md
            conn.execute("""
                CREATE TABLE IF NOT EXISTS open_loops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'plan',
                    expected_at TEXT NOT NULL DEFAULT '',
                    source_memory_id INTEGER NOT NULL DEFAULT 0,
                    source_group_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN (
                        'open','resolved','dropped','expired'
                    )),
                    asked_count INTEGER NOT NULL DEFAULT 0 CHECK(asked_count >= 0),
                    last_asked_at TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_open_loops_open "
                "ON open_loops(qq_id, status, expected_at)"
            )

            # 事实簇历史证据迁移必须在所有表建好后执行；只做一次、可审计，
            # 不删除原始行。无效锚点降为 retracted，无法追溯的旧摘要清空，
            # 避免历史合成文本继续作为 LLM 的事实依据。
            self._migrate_cluster_fact_evidence(conn)

            conn.commit()

    def _migrate_cluster_fact_evidence(self, conn) -> None:
        """隔离历史事实簇中错误/缺失的聊天证据（幂等、可回滚）。

        ``cluster_facts.evidence_ids`` 是早期逗号字符串，没有外键约束；
        历史批处理曾把糖糖回复、其它用户消息甚至不存在的 id 当作证据。
        这些行保留作审计，但不能继续支撑事实簇摘要：有非空且不完整锚点的
        原子事实标 ``retracted``，没有任何可追溯 active 原子事实的簇清空摘要。
        新版提取路径会在写入前校验用户消息证据，因此该迁移只需运行一次。
        """
        migration_version = "20260830_cluster_fact_evidence_v1"
        if conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (migration_version,),
        ).fetchone():
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        invalid_fact_ids: list[int] = []
        invalid_fact_subjects: dict[int, str] = {}
        affected_cluster_ids: set[int] = set()
        rows = conn.execute(
            "SELECT id,cluster_id,subject_qq,evidence_ids FROM cluster_facts "
            "WHERE COALESCE(status,'active')='active' "
            "AND COALESCE(evidence_ids,'')!=''"
        ).fetchall()

        for fact_id, cluster_id, subject_qq, raw_evidence in rows:
            tokens = [part.strip() for part in str(raw_evidence).split(",")]
            evidence_ids: list[int] = []
            malformed = not tokens
            for token in tokens:
                if not token.isdigit() or int(token) <= 0:
                    malformed = True
                    break
                evidence_ids.append(int(token))
            if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
                malformed = True

            if not malformed:
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence_rows = conn.execute(
                    "SELECT id FROM chat_log "
                    f"WHERE id IN ({placeholders}) "
                    "AND qq_id=? AND is_bot_reply=0 "
                    "AND COALESCE(quarantined_at,'')=''",
                    [*evidence_ids, str(subject_qq or "")],
                ).fetchall()
                malformed = len(evidence_rows) != len(evidence_ids)

            if malformed:
                invalid_fact_ids.append(int(fact_id))
                invalid_fact_subjects[int(fact_id)] = str(subject_qq or "")
                affected_cluster_ids.add(int(cluster_id))

        if invalid_fact_ids:
            conn.executemany(
                "UPDATE cluster_facts SET status='retracted' "
                "WHERE id=? AND COALESCE(status,'active')='active'",
                [(fact_id,) for fact_id in invalid_fact_ids],
            )
            for fact_id in invalid_fact_ids:
                self._record_revision(
                    conn, entity="cluster_facts", entity_id=fact_id,
                    qq_id=invalid_fact_subjects.get(fact_id, ""),
                    op="migrate", field="status",
                    before="active", after="retracted",
                    reason=("迁移：历史 evidence_ids 含无效/外部锚点，"
                            "保留原子行作审计但退出摘要支撑"),
                    actor="migration:cluster_fact_evidence",
                )

        # 无 evidence 的历史事实同样不能留下可供 LLM 读取的摘要；原子行仍
        # 保留，方便之后人工复核/重建，不把不可追溯内容伪装成已删除。
        orphan_clusters = conn.execute(
            "SELECT fc.id FROM fact_clusters fc "
            "WHERE COALESCE(fc.summary,'')!='' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM cluster_facts cf "
            "WHERE cf.cluster_id=fc.id "
            "AND COALESCE(cf.status,'active')='active' "
            "AND COALESCE(cf.evidence_ids,'')!=''"
            ")"
        ).fetchall()
        affected_cluster_ids.update(int(row[0]) for row in orphan_clusters)
        if affected_cluster_ids:
            ordered_clusters = sorted(affected_cluster_ids)
            cluster_summaries = {
                int(row[0]): str(row[1] or "") for row in conn.execute(
                    "SELECT id, COALESCE(summary,'') FROM fact_clusters "
                    "WHERE id IN (%s)" % ",".join("?" * len(ordered_clusters)),
                    ordered_clusters,
                ).fetchall()
            }
            conn.executemany(
                "UPDATE fact_clusters SET summary='', "
                "fact_count=(SELECT COUNT(*) FROM cluster_facts cf "
                "WHERE cf.cluster_id=fact_clusters.id "
                "AND COALESCE(cf.status,'active')='active'), updated_at=? "
                "WHERE id=?",
                [(now, cluster_id) for cluster_id in ordered_clusters],
            )
            for cluster_id in ordered_clusters:
                self._record_revision(
                    conn, entity="fact_clusters", entity_id=cluster_id, qq_id="",
                    op="update", field="summary",
                    before=cluster_summaries.get(cluster_id, ""), after="",
                    reason="迁移：簇内已无可追溯 active 事实，摘要不得继续注入",
                    actor="migration:cluster_fact_evidence",
                )

        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) "
            "VALUES (?,?,?)",
            (migration_version, "cluster-fact-evidence-v1", now),
        )
        if invalid_fact_ids or orphan_clusters:
            logger.warning(
                "🧠 事实簇证据迁移：隔离 %d 条无效锚点，清空 %d 个不可追溯摘要",
                len(invalid_fact_ids), len(orphan_clusters),
            )

    @staticmethod
    def _confirmed_fact_anchor_trigger_sql() -> str:
        """返回 confirmed fact 严格锚点的唯一 SQL 合同。"""
        return """
            CREATE TRIGGER trg_confirmed_action_fact_strict_anchor
            BEFORE INSERT ON confirmed_action_facts
            WHEN json_valid(NEW.message_ids_json)=0
              OR json_type(NEW.message_ids_json,'$')!='array'
              OR CASE WHEN json_valid(NEW.message_ids_json)
                      THEN json_array_length(NEW.message_ids_json)=0
                      ELSE 0 END
              OR EXISTS (
                  SELECT 1 FROM json_each(
                      CASE WHEN json_valid(NEW.message_ids_json)
                           THEN NEW.message_ids_json ELSE '[]' END
                  ) m
                  WHERE m.type!='integer' OR m.value=0
                    OR m.value<-(2147483648) OR m.value>2147483647
              )
              OR NOT EXISTS (
                SELECT 1 FROM send_outbox o
                WHERE o.action_id=NEW.outbox_id
                  AND o.domain_action_id=NEW.domain_action_id
                  AND o.domain_action_id!=''
                  AND o.status IN (
                      'sending','confirmed','confirmed_unaccounted',
                      'confirmed_conflict'
                  )
                  AND json_valid(o.receipt_template)
                  AND json_extract(o.receipt_template,'$.action_id')
                      =NEW.domain_action_id
            ) AND NOT EXISTS (
                SELECT 1 FROM action_receipt_mailbox m
                WHERE m.scope_id=NEW.scope_id
                  AND m.action_id=NEW.domain_action_id
                  AND m.action_status='confirmed'
                  AND json_valid(m.receipt_json)
                  AND json_extract(m.receipt_json,'$.action_id')
                      =NEW.domain_action_id
                  AND json_extract(m.receipt_json,'$.status')='confirmed'
            )
            BEGIN SELECT RAISE(ABORT,'confirmed action fact lacks strict anchor'); END
        """

    @staticmethod
    def _confirmed_fact_anchor_checksum(conn) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_confirmed_action_fact_strict_anchor'"
        ).fetchone()
        if not row or not row[0]:
            return ""
        compact = "".join(str(row[0]).lower().split())
        return hashlib.sha256(compact.encode("utf-8")).hexdigest()

    @classmethod
    def _confirmed_fact_anchor_expected_checksum(cls) -> str:
        compact = "".join(cls._confirmed_fact_anchor_trigger_sql().lower().split())
        return hashlib.sha256(compact.encode("utf-8")).hexdigest()

    @classmethod
    def _validate_confirmed_fact_anchor_schema(cls, conn) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_confirmed_action_fact_strict_anchor'"
        ).fetchone()
        if not row or not row[0]:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor trigger is missing"
            )
        compact = "".join(str(row[0]).lower().split())
        required_fragments = (
            "beforeinsertonconfirmed_action_facts",
            "json_array_length(new.message_ids_json)",
            "m.type!='integer'",
            "m.action_status='confirmed'",
            "json_extract(m.receipt_json,'$.action_id')=new.domain_action_id",
            "json_extract(m.receipt_json,'$.status')='confirmed'",
        )
        missing = [fragment for fragment in required_fragments
                   if fragment not in compact]
        if missing:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor contract missing: "
                + ", ".join(missing)
            )
        checksum = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        expected = cls._confirmed_fact_anchor_expected_checksum()
        if checksum != expected:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor schema manifest differs"
            )
        return checksum

    @classmethod
    def _migrate_confirmed_fact_anchor_schema(cls, conn) -> None:
        """原子升级旧 strict-anchor，禁止失败时留下半迁移触发器/标记。"""
        marker_row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_CONFIRMED_FACT_ANCHOR_VERSION,),
        ).fetchone()
        marker = str(marker_row[0]) if marker_row else ""
        expected = cls._confirmed_fact_anchor_expected_checksum()
        current = cls._confirmed_fact_anchor_checksum(conn)
        legacy_checksums = {
            _CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM,
            _CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V2,
            _CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V3,
        }
        if marker and marker not in {expected, *legacy_checksums}:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor migration marker differs"
            )
        if marker == expected and current != expected:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor trigger drifted"
            )
        if current not in {"", expected, *legacy_checksums}:
            raise sqlite3.OperationalError(
                "confirmed fact strict anchor trigger is unversioned"
            )

        conn.execute("SAVEPOINT confirmed_fact_anchor")
        try:
            if current != expected:
                conn.execute(
                    "DROP TRIGGER IF EXISTS "
                    "trg_confirmed_action_fact_strict_anchor"
                )
                conn.execute(cls._confirmed_fact_anchor_trigger_sql())
            checksum = cls._validate_confirmed_fact_anchor_schema(conn)
            applied_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if marker_row:
                if marker != checksum:
                    conn.execute(
                        "UPDATE schema_migrations SET checksum=?,applied_at=? "
                        "WHERE version=?",
                        (checksum, applied_at, _CONFIRMED_FACT_ANCHOR_VERSION),
                    )
            else:
                conn.execute(
                    "INSERT INTO schema_migrations(version,checksum,applied_at) "
                    "VALUES (?,?,?)",
                    ( _CONFIRMED_FACT_ANCHOR_VERSION, checksum, applied_at),
                )
            conn.execute("RELEASE SAVEPOINT confirmed_fact_anchor")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT confirmed_fact_anchor")
            conn.execute("RELEASE SAVEPOINT confirmed_fact_anchor")
            raise

    def _migrate_task_action_schema(self, conn) -> None:
        """增量建立定时动作 attempt 契约；缺版本或契约漂移时拒绝启动。"""
        migration_version = "20260828_task_action_phase0_v1"
        expected_indexes = {
            "idx_task_action_attempts_task_state",
            "idx_task_action_events_attempt_time",
            "idx_send_outbox_task_ordinal",
            "idx_send_outbox_task_status",
            "idx_send_outbox_owner_due",
        }
        expected_triggers = {
            "trg_task_action_accounting_event",
            "trg_task_action_attempt_accounting_transition",
            "trg_task_action_attempt_cancel_guard",
            "trg_task_action_attempt_created",
            "trg_task_action_attempt_generation",
            "trg_task_action_attempt_identity_immutable",
            "trg_task_action_attempt_insert_state",
            "trg_task_action_attempt_plan_children",
            "trg_task_action_attempt_state_transition",
            "trg_task_action_attempt_terminal_guard",
            "trg_task_action_current_attempt_insert",
            "trg_task_action_current_attempt_owner",
            "trg_task_action_current_attempt_transition",
            "trg_task_action_delivery_event",
            "trg_task_action_event_immutable_delete",
            "trg_task_action_event_immutable_update",
            "trg_task_action_outbox_insert_guard",
            "trg_task_action_outbox_link_immutable",
            "trg_task_action_outbox_open_guard",
            "trg_task_action_outbox_status_guard",
            "trg_task_action_task_pending_guard",
            "trg_task_action_task_status_guard",
        }

        def _compact_sql(table: str) -> str:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return "".join(str(row[0] if row else "").lower().split())

        def _phase0_artifacts_exist() -> bool:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables & {"task_action_attempts", "task_action_events"}:
                return True
            task_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)")
            }
            outbox_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")
            }
            if "current_attempt_id" in task_columns:
                return True
            if outbox_columns & {"task_attempt_id", "ordinal", "retry_owner"}:
                return True
            objects = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
                )
            }
            return bool(objects & (expected_indexes | expected_triggers))

        def _validate_phase0_schema(
            *,
            index_manifest: set[str] | None = None,
            trigger_manifest: set[str] | None = None,
        ) -> None:
            errors: list[str] = []
            required_indexes = index_manifest or expected_indexes
            required_triggers = trigger_manifest or expected_triggers
            required_columns = {
                "task_action_attempts": {
                    "id", "task_id", "generation", "plan_id", "plan_json",
                    "state", "accounting_state", "last_error", "created_at",
                    "updated_at", "finalized_at",
                },
                "task_action_events": {
                    "id", "attempt_id", "event_type", "from_state", "to_state",
                    "outbox_id", "domain_action_id", "actor", "reason",
                    "metadata_json", "created_at",
                },
                "tasks": {"current_attempt_id"},
                "send_outbox": {"task_attempt_id", "ordinal", "retry_owner"},
            }
            for table, required in required_columns.items():
                actual = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                missing = sorted(required - actual)
                if missing:
                    errors.append(f"{table} missing columns {missing}")

            expected_fks = {
                "task_action_attempts": {
                    ("tasks", "task_id", "id", "RESTRICT")
                },
                "task_action_events": {
                    ("task_action_attempts", "attempt_id", "id", "RESTRICT")
                },
                "tasks": {
                    ("task_action_attempts", "current_attempt_id", "id", "RESTRICT")
                },
                "send_outbox": {
                    ("task_action_attempts", "task_attempt_id", "id", "RESTRICT")
                },
            }
            for table, expected in expected_fks.items():
                actual = {
                    (row[2], row[3], row[4], row[6])
                    for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                }
                if not expected <= actual:
                    errors.append(f"{table} foreign keys differ")

            excluded_triggers = tuple(sorted(
                _TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS
                | _TASK_ACTION_PHASE2B_TRIGGERS
            ))
            actual_triggers = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name IN "
                    "('task_action_attempts','task_action_events','tasks','send_outbox',"
                    "'task_action_retry_requests') "
                    "AND name NOT IN (" + ",".join("?" for _ in excluded_triggers)
                    + ")",
                    excluded_triggers,
                )
            }
            if actual_triggers != required_triggers:
                errors.append("task action trigger manifest differs")
            excluded_indexes = tuple(sorted(
                _TASK_ACTION_PHASE2B_INDEXES
                | _TASK_ACTION_PHASE2B_FLOOR_INDEXES
            ))
            actual_indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND ("
                    "name LIKE 'idx_task_action_%' "
                    "OR name LIKE 'idx_send_outbox_task_%' "
                    "OR name='idx_send_outbox_owner_due') AND name NOT IN ("
                    + ",".join("?" for _ in excluded_indexes) + ")",
                    excluded_indexes,
                )
            }
            if actual_indexes != required_indexes:
                errors.append("task action index manifest differs")

            attempt_sql = _compact_sql("task_action_attempts")
            for fragment in (
                "json_valid(plan_json)",
                "json_type(plan_json,'$')='object'",
                "json_type(plan_json,'$.plan_id')='text'",
                "json_extract(plan_json,'$.plan_id')=plan_id",
                "json_type(plan_json,'$.source_id')='text'",
                "json_extract(plan_json,'$.source_id')="
                "'task:'||task_id||':attempt:'||generation",
                "json_type(plan_json,'$.scope_id')='text'",
                "json_type(plan_json,'$.channel')='text'",
                "json_type(plan_json,'$.target')='text'",
                "json_extract(plan_json,'$.scope_id')=case"
                "whenjson_extract(plan_json,'$.channel')='group'"
                "thenjson_extract(plan_json,'$.target')"
                "else'_private_'||json_extract(plan_json,'$.target')end",
                "json_type(plan_json,'$.schema_version')='integer'",
                "json_type(plan_json,'$.children')='array'",
                "json_array_length(plan_json,'$.children')>0",
                "json_type(plan_json,'$.plan_id')isnotnull",
                "json_type(plan_json,'$.source_id')isnotnull",
                "json_type(plan_json,'$.scope_id')isnotnull",
                "json_type(plan_json,'$.channel')isnotnull",
                "json_type(plan_json,'$.target')isnotnull",
                "json_type(plan_json,'$.schema_version')isnotnull",
                "json_type(plan_json,'$.children')isnotnull",
            ):
                if fragment not in attempt_sql:
                    errors.append(f"attempt plan constraint missing {fragment}")

            event_sql = _compact_sql("task_action_events")
            for fragment in (
                "check(length(trim(event_type))>0)",
                "check(length(trim(actor))>0)",
                "check(length(trim(reason))>0)",
                "json_valid(metadata_json)",
                "json_type(metadata_json,'$')='object'",
            ):
                if fragment not in event_sql:
                    errors.append(f"event constraint missing {fragment}")

            task_sql = _compact_sql("tasks")
            if ("current_attempt_idintegerdefaultnullreferences"
                    "task_action_attempts(id)ondeleterestrict") not in task_sql:
                errors.append("tasks current_attempt_id contract differs")
            outbox_sql = _compact_sql("send_outbox")
            for fragment in (
                "task_attempt_idintegerdefaultnullreferences"
                "task_action_attempts(id)ondeleterestrict",
                "ordinalintegernotnulldefault0check(ordinal>=0)",
                "retry_ownertextnotnulldefault'outbox'check(retry_owner='outbox')",
            ):
                if fragment not in outbox_sql:
                    errors.append(f"send_outbox contract missing {fragment}")

            invalid_current = conn.execute(
                "SELECT COUNT(*) FROM tasks t JOIN task_action_attempts a "
                "ON a.id=t.current_attempt_id WHERE a.task_id!=t.id"
            ).fetchone()[0]
            invalid_links = conn.execute(
                "SELECT COUNT(*) FROM send_outbox o "
                "WHERE o.task_attempt_id IS NOT NULL AND ("
                "trim(COALESCE(o.domain_action_id,''))='' "
                "OR o.retry_owner!='outbox')"
            ).fetchone()[0]
            double_owners = conn.execute(
                "SELECT COUNT(*) FROM send_outbox o "
                "JOIN task_action_attempts a ON a.id=o.task_attempt_id "
                "LEFT JOIN tasks t ON t.id=a.task_id "
                "WHERE o.status IN ('pending','sending') AND ("
                "t.current_attempt_id IS NOT a.id OR t.status!='sending')"
            ).fetchone()[0]
            if invalid_current:
                errors.append("cross-task current attempt rows exist")
            if invalid_links:
                errors.append("invalid task outbox links exist")
            if double_owners:
                errors.append("task and outbox retry ownership conflicts")
            if errors:
                raise sqlite3.OperationalError(
                    "task action schema invalid: " + "; ".join(errors[:6])
                )

        def _schema_checksum() -> str:
            object_names = (
                {"task_action_attempts", "task_action_events"}
                | expected_indexes | expected_triggers
            )
            placeholders = ",".join("?" for _ in object_names)
            objects = conn.execute(
                "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
                f"WHERE name IN ({placeholders}) ORDER BY type,name",
                tuple(sorted(object_names)),
            ).fetchall()
            column_contract = {}
            for table, names in (
                ("tasks", {"current_attempt_id"}),
                ("send_outbox", {"task_attempt_id", "ordinal", "retry_owner"}),
            ):
                column_contract[table] = [
                    tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")
                    if row[1] in names
                ]
                column_contract[f"{table}:fk"] = [
                    tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                    if row[3] in names
                ]
            payload = json.dumps(
                {"objects": objects, "columns": column_contract},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        def _install_phase2a() -> None:
            self._install_task_action_phase2a_schema(conn)
            _validate_phase0_schema(
                index_manifest=expected_indexes | _TASK_ACTION_PHASE2A_INDEXES,
                trigger_manifest=expected_triggers | _TASK_ACTION_PHASE2A_TRIGGERS,
            )
            self._validate_task_action_phase2a_schema(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version,checksum,applied_at) "
                "VALUES (?,?,?)",
                (_TASK_ACTION_PHASE2A_VERSION,
                 self._task_action_phase2a_checksum(conn),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._migrate_task_action_phase2a_receipt_schema(conn)
            self._migrate_task_action_phase2b_schema(conn)
            self._migrate_task_action_phase2b_floor_schema(conn)

        conn.execute("SAVEPOINT task_action_schema")
        try:
            phase2a_marker = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?",
                (_TASK_ACTION_PHASE2A_VERSION,),
            ).fetchone()
            if phase2a_marker:
                phase0_marker = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    (migration_version,),
                ).fetchone()
                if not phase0_marker:
                    raise sqlite3.OperationalError(
                        "task action Phase 2a is missing its Phase 0 lineage"
                    )
                # Phase 2b 扩展前会校验两个冻结标记；receipt 合同的数据
                # 校验仍在 Phase 2a 主校验之后执行，以保持启动失败的诊断顺序。
                self._migrate_task_action_phase2b_schema(conn)
                self._migrate_task_action_phase2b_floor_schema(conn)
                phase2a_marker = conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=?",
                    (_TASK_ACTION_PHASE2A_VERSION,),
                ).fetchone()
                _validate_phase0_schema(
                    index_manifest=expected_indexes | _TASK_ACTION_PHASE2A_INDEXES,
                    trigger_manifest=expected_triggers | _TASK_ACTION_PHASE2A_TRIGGERS,
                )
                self._validate_task_action_phase2a_schema(conn)
                if str(phase2a_marker[0]) != self._task_action_phase2a_checksum(conn):
                    raise sqlite3.OperationalError(
                        "task action Phase 2a schema manifest checksum differs"
                    )
                self._migrate_task_action_phase2a_receipt_schema(conn)
                conn.execute("RELEASE SAVEPOINT task_action_schema")
                return
            marker = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?",
                (migration_version,),
            ).fetchone()
            if marker:
                _validate_phase0_schema()
                if str(marker[0]) != _schema_checksum():
                    raise sqlite3.OperationalError(
                        "task action schema manifest checksum differs"
                    )
                _install_phase2a()
                conn.execute("RELEASE SAVEPOINT task_action_schema")
                return
            if _phase0_artifacts_exist():
                raise sqlite3.OperationalError(
                    "task action schema is unversioned or partially installed"
                )

            conn.execute("""
                CREATE TABLE task_action_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 0),
                    plan_id TEXT NOT NULL UNIQUE CHECK(length(trim(plan_id)) > 0),
                    plan_json TEXT NOT NULL CHECK(
                        json_valid(plan_json)
                        AND json_type(plan_json,'$') = 'object'
                        AND json_type(plan_json,'$.plan_id') = 'text'
                        AND json_extract(plan_json,'$.plan_id') = plan_id
                        AND json_type(plan_json,'$.source_id') = 'text'
                        AND length(trim(json_extract(plan_json,'$.source_id'))) > 0
                        AND json_extract(plan_json,'$.source_id') =
                            'task:' || task_id || ':attempt:' || generation
                        AND json_type(plan_json,'$.scope_id') = 'text'
                        AND length(trim(json_extract(plan_json,'$.scope_id'))) > 0
                        AND json_type(plan_json,'$.channel') = 'text'
                        AND json_extract(plan_json,'$.channel') IN ('group','private')
                        AND json_type(plan_json,'$.target') = 'text'
                        AND length(trim(json_extract(plan_json,'$.target'))) > 0
                        AND json_extract(plan_json,'$.scope_id') = CASE
                            WHEN json_extract(plan_json,'$.channel') = 'group'
                            THEN json_extract(plan_json,'$.target')
                            ELSE '_private_' || json_extract(plan_json,'$.target')
                        END
                        AND json_type(plan_json,'$.schema_version') = 'integer'
                        AND json_extract(plan_json,'$.schema_version') >= 1
                        AND json_type(plan_json,'$.children') = 'array'
                        AND json_array_length(plan_json,'$.children') > 0
                        AND json_type(plan_json,'$.plan_id') IS NOT NULL
                        AND json_type(plan_json,'$.source_id') IS NOT NULL
                        AND json_type(plan_json,'$.scope_id') IS NOT NULL
                        AND json_type(plan_json,'$.channel') IS NOT NULL
                        AND json_type(plan_json,'$.target') IS NOT NULL
                        AND json_type(plan_json,'$.schema_version') IS NOT NULL
                        AND json_type(plan_json,'$.children') IS NOT NULL
                    ),
                    state TEXT NOT NULL DEFAULT 'persisted' CHECK(state IN (
                        'persisted','outbox_pending','sending','confirmed',
                        'uncertain','failed','dead','partial','cancelled'
                    )),
                    accounting_state TEXT NOT NULL DEFAULT 'none' CHECK(
                        accounting_state IN ('none','clean','pending_repair','conflict')
                    ),
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finalized_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, generation),
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT
                )
            """)

            task_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)")
            }
            if "current_attempt_id" not in task_columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN current_attempt_id INTEGER "
                    "DEFAULT NULL REFERENCES task_action_attempts(id) ON DELETE RESTRICT"
                )

            conn.execute("""
                CREATE TABLE task_action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(length(trim(event_type)) > 0),
                    from_state TEXT NOT NULL DEFAULT '',
                    to_state TEXT NOT NULL DEFAULT '',
                    outbox_id TEXT NOT NULL DEFAULT '',
                    domain_action_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'system'
                        CHECK(length(trim(actor)) > 0),
                    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
                    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(
                        json_valid(metadata_json)
                        AND json_type(metadata_json,'$') = 'object'
                    ),
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                        CHECK(length(trim(created_at)) > 0),
                    FOREIGN KEY(attempt_id) REFERENCES task_action_attempts(id)
                        ON DELETE RESTRICT
                )
            """)

            outbox_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")
            }
            for column, declaration in (
                (
                    "task_attempt_id",
                    "INTEGER DEFAULT NULL REFERENCES task_action_attempts(id) "
                    "ON DELETE RESTRICT",
                ),
                ("ordinal", "INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0)"),
                (
                    "retry_owner",
                    "TEXT NOT NULL DEFAULT 'outbox' CHECK(retry_owner = 'outbox')",
                ),
            ):
                if column not in outbox_columns:
                    conn.execute(
                        f"ALTER TABLE send_outbox ADD COLUMN {column} {declaration}"
                    )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_action_attempts_task_state "
                "ON task_action_attempts(task_id,state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_action_events_attempt_time "
                "ON task_action_events(attempt_id,created_at,id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_send_outbox_task_ordinal "
                "ON send_outbox(task_attempt_id,ordinal) "
                "WHERE task_attempt_id IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_send_outbox_task_status "
                "ON send_outbox(task_attempt_id,status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_send_outbox_owner_due "
                "ON send_outbox(retry_owner,status,next_retry_at,created_at)"
            )

            # generation、状态机和 current 指针必须由数据库守最后一道防线；
            # Phase 1 的 Store API 仍会使用 expected-state CAS 提供并发语义。
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_generation
                BEFORE INSERT ON task_action_attempts
                WHEN NOT EXISTS (SELECT 1 FROM tasks WHERE id=NEW.task_id)
                  OR COALESCE((
                    SELECT status FROM tasks WHERE id=NEW.task_id
                  ), '')!='sending'
                  OR (
                    (SELECT current_attempt_id FROM tasks WHERE id=NEW.task_id) IS NULL
                    AND NOT (
                      (NEW.generation=0 AND NOT EXISTS (
                        SELECT 1 FROM task_action_attempts WHERE task_id=NEW.task_id
                      ))
                      OR (
                        NEW.generation=(SELECT COALESCE(MAX(generation),-1)+1
                                        FROM task_action_attempts
                                        WHERE task_id=NEW.task_id)
                        AND EXISTS (
                          SELECT 1 FROM task_action_attempts previous
                          WHERE previous.task_id=NEW.task_id
                            AND previous.generation=NEW.generation-1
                            AND previous.state='cancelled'
                            AND previous.accounting_state IN ('none','clean')
                            AND NOT EXISTS (
                              SELECT 1 FROM send_outbox o
                              WHERE o.task_attempt_id=previous.id
                                AND (o.status!='cancelled' OR o.attempts!=0)
                            )
                        )
                      )
                    )
                  )
                  OR (
                    (SELECT current_attempt_id FROM tasks WHERE id=NEW.task_id) IS NOT NULL
                    AND NOT EXISTS (
                      SELECT 1 FROM tasks t
                      JOIN task_action_attempts current
                        ON current.id=t.current_attempt_id
                      WHERE t.id=NEW.task_id
                        AND current.task_id=NEW.task_id
                        AND NEW.generation=current.generation+1
                        AND current.state IN (
                          'uncertain','failed','dead','partial','cancelled'
                        )
                        AND current.accounting_state IN ('none','clean')
                        AND NOT EXISTS (
                          SELECT 1 FROM send_outbox o
                          WHERE o.task_attempt_id=current.id
                            AND o.status IN ('pending','sending')
                        )
                    )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'task action generation is not contiguous');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_insert_state
                BEFORE INSERT ON task_action_attempts
                WHEN NEW.state!='persisted' OR NEW.accounting_state!='none'
                BEGIN
                    SELECT RAISE(ABORT, 'task action attempt must start persisted');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_plan_children
                BEFORE INSERT ON task_action_attempts
                WHEN EXISTS (
                  SELECT 1 FROM json_each(
                    CASE WHEN json_valid(NEW.plan_json)
                         THEN NEW.plan_json ELSE '{"children":[]}' END,
                    '$.children'
                  ) child
                  WHERE child.type!='object'
                     OR COALESCE(
                          json_type(child.value,'$.action_id')='text'
                          AND length(trim(json_extract(
                            child.value,'$.action_id'
                          )))>0,
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.kind')='text'
                          AND json_extract(child.value,'$.kind') IN (
                            'text','voice','sticker','image','sing'
                          ),
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.channel')='text'
                          AND json_extract(child.value,'$.channel')=
                              json_extract(NEW.plan_json,'$.channel'),
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.target')='text'
                          AND json_extract(child.value,'$.target')=
                              json_extract(NEW.plan_json,'$.target'),
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.payload')='object', 0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.source_id')='text'
                          AND json_extract(child.value,'$.source_id')=
                              json_extract(NEW.plan_json,'$.source_id'),
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.scope_id')='text'
                          AND json_extract(child.value,'$.scope_id')=
                              json_extract(NEW.plan_json,'$.scope_id'),
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.ordinal')='integer'
                          AND json_extract(child.value,'$.ordinal')>=0,
                          0
                        )=0
                     OR COALESCE(
                          json_type(child.value,'$.schema_version')='integer'
                          AND json_extract(child.value,'$.schema_version')>=1,
                          0
                        )=0
                ) OR EXISTS (
                  SELECT 1 FROM json_each(
                    CASE WHEN json_valid(NEW.plan_json)
                         THEN NEW.plan_json ELSE '{"children":[]}' END,
                    '$.children'
                  ) child
                  GROUP BY json_extract(child.value,'$.action_id')
                  HAVING COUNT(*)>1
                ) OR EXISTS (
                  SELECT 1 FROM json_each(
                    CASE WHEN json_valid(NEW.plan_json)
                         THEN NEW.plan_json ELSE '{"children":[]}' END,
                    '$.children'
                  ) child
                  GROUP BY json_extract(child.value,'$.ordinal')
                  HAVING COUNT(*)>1
                )
                BEGIN
                    SELECT RAISE(ABORT, 'task action plan children are invalid');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_identity_immutable
                BEFORE UPDATE OF task_id,generation,plan_id,plan_json
                ON task_action_attempts
                WHEN NEW.task_id IS NOT OLD.task_id
                  OR NEW.generation IS NOT OLD.generation
                  OR NEW.plan_id IS NOT OLD.plan_id
                  OR NEW.plan_json IS NOT OLD.plan_json
                BEGIN
                    SELECT RAISE(ABORT, 'task action attempt identity is immutable');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_state_transition
                BEFORE UPDATE OF state ON task_action_attempts
                WHEN NEW.state!=OLD.state AND NOT (
                  (OLD.state='persisted' AND NEW.state IN (
                    'outbox_pending','failed','uncertain','cancelled'
                  ))
                  OR (OLD.state='outbox_pending' AND NEW.state IN (
                    'sending','failed','dead','uncertain','cancelled'
                  ))
                  OR (OLD.state='sending' AND NEW.state IN (
                    'outbox_pending','confirmed','uncertain','failed','dead','partial'
                  ))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task action delivery transition');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_cancel_guard
                BEFORE UPDATE OF state ON task_action_attempts
                WHEN NEW.state='cancelled' AND OLD.state!='cancelled'
                  AND EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.task_attempt_id=OLD.id
                      AND (o.status!='cancelled' OR o.attempts!=0)
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'attempt has non-cancellable outbox');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_terminal_guard
                BEFORE UPDATE OF state ON task_action_attempts
                WHEN NEW.state!=OLD.state
                  AND NEW.state IN (
                    'confirmed','uncertain','failed','dead','partial','cancelled'
                  )
                  AND EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.task_attempt_id=OLD.id
                      AND o.status IN ('pending','sending')
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'attempt cannot settle with open outbox');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_accounting_transition
                BEFORE UPDATE OF accounting_state ON task_action_attempts
                WHEN NEW.accounting_state!=OLD.accounting_state AND NOT (
                  (OLD.accounting_state='none' AND NEW.accounting_state IN (
                    'clean','pending_repair','conflict'
                  ))
                  OR (OLD.accounting_state='pending_repair'
                      AND NEW.accounting_state IN ('clean','conflict'))
                  OR (OLD.accounting_state='conflict'
                      AND NEW.accounting_state='clean')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task action accounting transition');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_attempt_created
                AFTER INSERT ON task_action_attempts
                BEGIN
                    INSERT INTO task_action_events
                    (attempt_id,event_type,to_state,actor,reason,metadata_json)
                    VALUES (NEW.id,'attempt_created',NEW.state,'system',
                            'attempt_persisted',
                            json_object('accounting_state',NEW.accounting_state));
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_delivery_event
                AFTER UPDATE OF state ON task_action_attempts
                WHEN NEW.state != OLD.state
                BEGIN
                    INSERT INTO task_action_events
                    (attempt_id,event_type,from_state,to_state,actor,reason,metadata_json)
                    VALUES (NEW.id,'delivery_state_changed',OLD.state,NEW.state,
                            'system','delivery_state_transition',
                            json_object('updated_at',NEW.updated_at,
                                        'finalized_at',NEW.finalized_at,
                                        'last_error',NEW.last_error));
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_accounting_event
                AFTER UPDATE OF accounting_state ON task_action_attempts
                WHEN NEW.accounting_state != OLD.accounting_state
                BEGIN
                    INSERT INTO task_action_events
                    (attempt_id,event_type,from_state,to_state,actor,reason,metadata_json)
                    VALUES (NEW.id,'accounting_state_changed',
                            OLD.accounting_state,NEW.accounting_state,
                            'system','accounting_state_transition',
                            json_object('updated_at',NEW.updated_at,
                                        'last_error',NEW.last_error));
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_event_immutable_update
                BEFORE UPDATE ON task_action_events
                BEGIN
                    SELECT RAISE(ABORT, 'task action events are append-only');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_event_immutable_delete
                BEFORE DELETE ON task_action_events
                BEGIN
                    SELECT RAISE(ABORT, 'task action events are append-only');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_current_attempt_insert
                BEFORE INSERT ON tasks
                WHEN NEW.current_attempt_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'task must exist before its first attempt');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_current_attempt_owner
                BEFORE UPDATE OF current_attempt_id ON tasks
                WHEN NEW.current_attempt_id IS NOT NULL
                 AND NOT EXISTS (
                    SELECT 1 FROM task_action_attempts
                    WHERE id=NEW.current_attempt_id AND task_id=NEW.id
                 )
                BEGIN
                    SELECT RAISE(ABORT, 'current attempt belongs to another task');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_current_attempt_transition
                BEFORE UPDATE OF current_attempt_id ON tasks
                WHEN NEW.current_attempt_id IS NOT OLD.current_attempt_id
                  AND NOT (
                    (OLD.current_attempt_id IS NULL
                     AND NEW.current_attempt_id IS NOT NULL
                     AND NEW.status='sending'
                     AND EXISTS (
                       SELECT 1 FROM task_action_attempts candidate
                       WHERE candidate.id=NEW.current_attempt_id
                         AND candidate.task_id=NEW.id
                         AND candidate.state='persisted'
                         AND candidate.generation=(
                           SELECT MAX(generation) FROM task_action_attempts
                           WHERE task_id=NEW.id
                         )
                         AND (
                           (candidate.generation=0 AND (
                             SELECT COUNT(*) FROM task_action_attempts
                             WHERE task_id=NEW.id
                           )=1)
                           OR EXISTS (
                             SELECT 1 FROM task_action_attempts previous
                             WHERE previous.task_id=NEW.id
                               AND previous.generation=candidate.generation-1
                               AND previous.state='cancelled'
                               AND previous.accounting_state IN ('none','clean')
                               AND NOT EXISTS (
                                 SELECT 1 FROM send_outbox o
                                 WHERE o.task_attempt_id=previous.id
                                   AND (o.status!='cancelled' OR o.attempts!=0)
                               )
                           )
                         )
                     ))
                    OR (OLD.current_attempt_id IS NOT NULL
                        AND NEW.current_attempt_id IS NOT NULL
                        AND NEW.status='sending'
                        AND EXISTS (
                          SELECT 1 FROM task_action_attempts previous
                          JOIN task_action_attempts candidate
                            ON candidate.id=NEW.current_attempt_id
                          WHERE previous.id=OLD.current_attempt_id
                            AND previous.task_id=NEW.id
                            AND candidate.task_id=NEW.id
                            AND candidate.generation=previous.generation+1
                            AND candidate.state='persisted'
                            AND previous.state IN (
                              'uncertain','failed','dead','partial','cancelled'
                            )
                            AND previous.accounting_state IN ('none','clean')
                            AND NOT EXISTS (
                              SELECT 1 FROM send_outbox o
                              WHERE o.task_attempt_id=previous.id
                                AND o.status IN ('pending','sending')
                            )
                        ))
                    OR (OLD.current_attempt_id IS NOT NULL
                        AND NEW.current_attempt_id IS NULL
                        AND NEW.status='pending'
                        AND EXISTS (
                          SELECT 1 FROM task_action_attempts previous
                          WHERE previous.id=OLD.current_attempt_id
                            AND previous.task_id=NEW.id
                            AND previous.state='cancelled'
                            AND previous.accounting_state IN ('none','clean')
                            AND NOT EXISTS (
                              SELECT 1 FROM send_outbox o
                              WHERE o.task_attempt_id=previous.id
                                AND (o.status!='cancelled' OR o.attempts!=0)
                            )
                        ))
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid current attempt transition');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_task_status_guard
                BEFORE UPDATE OF status,current_attempt_id ON tasks
                WHEN NEW.current_attempt_id IS NOT NULL AND (
                  NEW.status NOT IN (
                    'sending','done','uncertain','failed','partial'
                  )
                  OR (
                    NEW.status!='sending'
                    AND EXISTS (
                      SELECT 1 FROM task_action_attempts a
                      JOIN send_outbox o ON o.task_attempt_id=a.id
                      WHERE a.task_id=NEW.id
                        AND o.status IN ('pending','sending')
                    )
                  )
                  OR (
                    NEW.status='done'
                    AND NOT EXISTS (
                      SELECT 1 FROM task_action_attempts a
                      WHERE a.id=NEW.current_attempt_id
                        AND a.task_id=NEW.id AND a.state='confirmed'
                    )
                  )
                  OR (
                    NEW.status='uncertain'
                    AND NOT EXISTS (
                      SELECT 1 FROM task_action_attempts a
                      WHERE a.id=NEW.current_attempt_id
                        AND a.task_id=NEW.id AND a.state='uncertain'
                    )
                  )
                  OR (
                    NEW.status='failed'
                    AND NOT EXISTS (
                      SELECT 1 FROM task_action_attempts a
                      WHERE a.id=NEW.current_attempt_id
                        AND a.task_id=NEW.id
                        AND a.state IN ('failed','dead')
                    )
                  )
                  OR (
                    NEW.status='partial'
                    AND NOT EXISTS (
                      SELECT 1 FROM task_action_attempts a
                      WHERE a.id=NEW.current_attempt_id
                        AND a.task_id=NEW.id AND a.state='partial'
                    )
                  )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'task status disagrees with current attempt');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_task_pending_guard
                BEFORE UPDATE OF status,current_attempt_id ON tasks
                WHEN NEW.status='pending' AND (
                  NEW.current_attempt_id IS NOT NULL
                  OR EXISTS (
                    SELECT 1 FROM task_action_attempts a
                    JOIN send_outbox o ON o.task_attempt_id=a.id
                    WHERE a.task_id=NEW.id AND o.status IN ('pending','sending')
                  )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'pending task would create a second retry owner');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_outbox_link_immutable
                BEFORE UPDATE OF action_id,target_type,target_id,group_id,message,
                    receipt_template,domain_action_id,task_attempt_id,ordinal,retry_owner
                ON send_outbox
                WHEN (OLD.task_attempt_id IS NOT NULL OR NEW.task_attempt_id IS NOT NULL)
                  AND (
                    NEW.action_id IS NOT OLD.action_id
                    OR NEW.target_type IS NOT OLD.target_type
                    OR NEW.target_id IS NOT OLD.target_id
                    OR NEW.group_id IS NOT OLD.group_id
                    OR NEW.message IS NOT OLD.message
                    OR NEW.receipt_template IS NOT OLD.receipt_template
                    OR NEW.domain_action_id IS NOT OLD.domain_action_id
                    OR NEW.task_attempt_id IS NOT OLD.task_attempt_id
                    OR NEW.ordinal IS NOT OLD.ordinal
                    OR NEW.retry_owner IS NOT OLD.retry_owner
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'task outbox identity is immutable');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_outbox_insert_guard
                BEFORE INSERT ON send_outbox
                WHEN NEW.task_attempt_id IS NOT NULL AND (
                  trim(COALESCE(NEW.domain_action_id,''))=''
                  OR NEW.retry_owner!='outbox'
                  OR NEW.status!='pending'
                  OR NEW.attempts!=0
                  OR NOT EXISTS (
                    SELECT 1 FROM task_action_attempts a
                    JOIN tasks t ON t.id=a.task_id
                    WHERE a.id=NEW.task_attempt_id
                      AND t.current_attempt_id=a.id
                      AND t.status='sending'
                      AND a.state IN ('persisted','outbox_pending')
                  )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'task outbox has no current sending owner');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_outbox_open_guard
                BEFORE UPDATE OF status ON send_outbox
                WHEN NEW.task_attempt_id IS NOT NULL
                  AND NEW.status IN ('pending','sending')
                  AND NOT EXISTS (
                    SELECT 1 FROM task_action_attempts a
                    JOIN tasks t ON t.id=a.task_id
                    WHERE a.id=NEW.task_attempt_id
                      AND t.current_attempt_id=a.id
                      AND t.status='sending'
                      AND a.state IN ('outbox_pending','sending')
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'task outbox cannot reopen without its owner');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_action_outbox_status_guard
                BEFORE UPDATE OF status ON send_outbox
                WHEN NEW.task_attempt_id IS NOT NULL
                  AND NEW.status NOT IN (
                    'pending','sending','uncertain','dead',
                    'confirmed_unaccounted','confirmed_conflict','cancelled'
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task outbox status');
                END
            """)

            _validate_phase0_schema()
            conn.execute(
                "INSERT INTO schema_migrations(version,checksum,applied_at) "
                "VALUES (?,?,?)",
                (migration_version, _schema_checksum(),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            _install_phase2a()
            conn.execute("RELEASE SAVEPOINT task_action_schema")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT task_action_schema")
            conn.execute("RELEASE SAVEPOINT task_action_schema")
            raise

    @staticmethod
    def _task_action_phase2a_checksum(conn) -> str:
        objects = conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            "WHERE name IN ('task_action_attempts','task_action_events',"
            "'task_action_retry_requests','idx_send_outbox_task_ordinal',"
            "'idx_send_outbox_task_status','idx_send_outbox_owner_due') "
            "OR (name LIKE 'idx_task_action_%' AND name NOT IN ("
            + ",".join("?" for _ in (
                _TASK_ACTION_PHASE2B_INDEXES
                | _TASK_ACTION_PHASE2B_FLOOR_INDEXES
            )) + ")) "
            "OR (name LIKE 'trg_task_action_%' AND name NOT IN ("
            "'trg_task_action_phase2a_receipt_outbox_guard',"
            "'trg_task_action_phase2a_receipt_request_guard',"
            + ",".join("?" for _ in (
                _TASK_ACTION_PHASE2B_TRIGGERS
                | _TASK_ACTION_PHASE2B_FLOOR_TRIGGERS
            )) + ")) "
            "ORDER BY type,name"
            , tuple(sorted(
                _TASK_ACTION_PHASE2B_INDEXES
                | _TASK_ACTION_PHASE2B_FLOOR_INDEXES
            )) + tuple(sorted(
                _TASK_ACTION_PHASE2B_TRIGGERS
                | _TASK_ACTION_PHASE2B_FLOOR_TRIGGERS
            ))
        ).fetchall()
        column_contract = {}
        for table, names in (
            ("tasks", {"current_attempt_id"}),
            ("send_outbox", {"task_attempt_id", "ordinal", "retry_owner"}),
            ("task_action_attempts", {"retry_request_id"}),
        ):
            column_contract[table] = [
                tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")
                if row[1] in names
            ]
            column_contract[f"{table}:fk"] = [
                tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                if row[3] in names
            ]
        payload = json.dumps(
            {"objects": objects, "columns": column_contract},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _validate_task_action_phase2a_schema(self, conn) -> None:
        errors: list[str] = []
        # 启动时必须复现 ActionEnvelope/ConversationRef 的语义边界；
        # 仅检查 JSON 是 object 会允许同步篡改后的跨用户归账继续启动。
        from .action_contract import ConversationRef
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(task_action_attempts)")
        }
        request_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(task_action_retry_requests)"
            )
        }
        expected_request_columns = {
            "request_id", "requested_task_id", "requested_attempt_id", "task_id",
            "expected_attempt_id", "expected_generation", "new_attempt_id",
            "new_generation", "actor", "verification_result", "force_resend_ack",
            "selected_ordinals_json", "skipped_ordinals_json", "result",
            "reason_code", "evidence_json", "created_at",
        }
        if "retry_request_id" not in attempt_columns:
            errors.append("task_action_attempts missing retry_request_id")
        if request_columns != expected_request_columns:
            errors.append("task_action_retry_requests columns differ")

        expected_fks = {
            "task_action_attempts": {
                ("task_action_retry_requests", "retry_request_id", "request_id", "RESTRICT"),
            },
            "task_action_retry_requests": {
                ("tasks", "task_id", "id", "RESTRICT"),
                ("task_action_attempts", "expected_attempt_id", "id", "RESTRICT"),
                ("task_action_attempts", "new_attempt_id", "id", "RESTRICT"),
            },
        }
        for table, expected in expected_fks.items():
            actual = {
                (row[2], row[3], row[4], row[6])
                for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            }
            if not expected <= actual:
                errors.append(f"{table} Phase 2a foreign keys differ")

        actual_indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND ("
                "name LIKE 'idx_task_action_retry_%' "
                "OR name LIKE 'idx_task_action_attempts_retry_%')"
            )
        }
        if actual_indexes != _TASK_ACTION_PHASE2A_INDEXES:
            errors.append("task action Phase 2a index manifest differs")
        actual_triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND ("
                "name LIKE 'trg_task_action_retry_%' "
                "OR name LIKE 'trg_task_action_attempt_retry_%') "
                "AND name NOT IN (?,?)",
                tuple(sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS)),
            )
        }
        if actual_triggers != _TASK_ACTION_PHASE2A_TRIGGERS:
            errors.append("task action Phase 2a trigger manifest differs")
        # 名称集合与 checksum 仍不能证明 trigger 保留了护栏：同名弱
        # trigger 配合自洽 checksum 会绕过仅结构性的检查。这里固定检查
        # retry lineage/业务克隆/审计请求的不可替代 SQL 片段。
        semantic_trigger_fragments = {
            "trg_task_action_attempt_retry_link_guard": (
                "new.generation=0andnew.retry_request_idisnotnull",
                "new.generation>0",
                "new.retry_request_id",
            ),
            "trg_task_action_attempt_retry_link_immutable": (
                "beforeupdateofretry_request_id",
                "new.retry_request_idisnotold.retry_request_id",
            ),
            "trg_task_action_attempt_retry_business_guard": (
                "new.generation>0",
                "json_array_length",
                "task_action_attemptsroot",
                "taskretrybusinessplanisnotasafeclone",
            ),
            "trg_task_action_retry_request_insert_guard": (
                "beforeinsertontask_action_retry_requests",
                "new.result='accepted'",
                "new.expected_attempt_id",
                "new.selected_ordinals_json",
                "created.retry_request_id",
            ),
            "trg_task_action_retry_request_immutable_update": (
                "beforeupdateontask_action_retry_requests",
                "taskretryrequestsareappend-only",
            ),
            "trg_task_action_retry_request_immutable_delete": (
                "beforedeleteontask_action_retry_requests",
                "taskretryrequestsareappend-only",
            ),
        }
        for trigger_name, fragments in semantic_trigger_fragments.items():
            trigger_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            trigger_sql = "".join(
                str(trigger_row[0] if trigger_row else "").lower().split()
            )
            for fragment in fragments:
                if fragment not in trigger_sql:
                    errors.append(
                        f"{trigger_name} semantic contract missing {fragment}"
                    )

        table_sql = "".join(str((conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='task_action_retry_requests'"
        ).fetchone() or ("",))[0]).lower().split())
        attempt_sql = "".join(str((conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='task_action_attempts'"
        ).fetchone() or ("",))[0]).lower().split())
        for fragment in (
            "json_valid(selected_ordinals_json)",
            "json_type(selected_ordinals_json,'$')='array'",
            "json_valid(skipped_ordinals_json)",
            "json_type(skipped_ordinals_json,'$')='array'",
            "json_valid(evidence_json)",
            "json_type(evidence_json,'$')='object'",
            "deferrableinitiallydeferred",
        ):
            if fragment not in table_sql:
                errors.append(f"retry request contract missing {fragment}")
        if "deferrableinitiallydeferred" not in attempt_sql:
            errors.append("attempt retry foreign key is not deferred")

        invalid_links = conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts a "
            "LEFT JOIN task_action_retry_requests r "
            "ON r.request_id=a.retry_request_id "
            "WHERE (a.generation=0 AND a.retry_request_id IS NOT NULL) "
            "OR (a.generation>0 AND (r.request_id IS NULL "
            "OR r.result!='accepted' OR r.new_attempt_id!=a.id))"
        ).fetchone()[0]
        invalid_requests = conn.execute(
            "SELECT COUNT(*) FROM task_action_retry_requests r "
            "LEFT JOIN task_action_attempts a ON a.id=r.new_attempt_id "
            "WHERE (r.result='accepted' AND (a.id IS NULL "
            "OR a.retry_request_id!=r.request_id OR a.task_id!=r.task_id "
            "OR a.generation!=r.new_generation)) "
            "OR (r.result='rejected' AND r.new_attempt_id IS NOT NULL)"
        ).fetchone()[0]
        if invalid_links:
            errors.append("task retry attempt links are invalid")
        if invalid_requests:
            errors.append("task retry request links are invalid")
        safe_plan = (
            "CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END"
        )
        safe_receipt = (
            "CASE WHEN json_valid(o.receipt_template) THEN o.receipt_template "
            "ELSE '{}' END"
        )
        child_path = "'$.children[' || o.ordinal || ']'"
        invalid_outbox_identity = conn.execute(
            "SELECT COUNT(*) FROM send_outbox o "
            "JOIN task_action_attempts a ON a.id=o.task_attempt_id "
            "WHERE o.task_attempt_id IS NOT NULL AND ("
            "NOT json_valid(a.plan_json) "
            f"OR json_type({safe_plan},'$.children')!='array' "
            f"OR json_type({safe_plan},{child_path})!='object' "
            f"OR a.plan_id IS NOT json_extract({safe_plan},'$.plan_id') "
            f"OR o.domain_action_id IS NOT json_extract({safe_plan},"
            f"{child_path} || '.action_id') "
            f"OR o.ordinal IS NOT json_extract({safe_plan},"
            f"{child_path} || '.ordinal') "
            f"OR o.target_type IS NOT json_extract({safe_plan},"
            f"{child_path} || '.channel') "
            f"OR o.target_id IS NOT json_extract({safe_plan},"
            f"{child_path} || '.target') "
            f"OR o.group_id IS NOT CASE WHEN json_extract({safe_plan},"
            f"{child_path} || '.channel')='group' THEN json_extract({safe_plan},"
            f"{child_path} || '.target') ELSE '' END "
            f"OR (json_extract({safe_plan},{child_path} || '.kind')='text' "
            f"AND o.message IS NOT json_extract({safe_plan},"
            f"{child_path} || '.payload.text')) "
            f"OR (CAST(COALESCE(json_extract({safe_plan},"
            f"{child_path} || '.schema_version'),0) "
            "AS INTEGER)>=2 AND ("
            "NOT json_valid(o.receipt_template) "
            f"OR json_extract({safe_receipt},'$.action_id') "
            "IS NOT o.domain_action_id "
            f"OR json_extract({safe_receipt},'$.kind') IS NOT "
            f"json_extract({safe_plan},{child_path} || '.kind') "
            f"OR json_extract({safe_receipt},'$.channel') IS NOT o.target_type "
            f"OR json_extract({safe_receipt},'$.target') IS NOT o.target_id "
            f"OR json_extract({safe_receipt},'$.source_id') IS NOT "
            f"json_extract({safe_plan},'$.source_id') "
            f"OR json_extract({safe_receipt},'$.scope_id') IS NOT "
            f"json_extract({safe_plan},'$.scope_id') "
            f"OR json_extract({safe_receipt},'$.ordinal') IS NOT o.ordinal "
             f"OR json_extract({safe_receipt},'$.identity_payload') IS NOT "
             f"json_extract({safe_plan},{child_path} || '.payload') "
             f"OR json_extract({safe_receipt},'$.conversation_ref') IS NOT "
             f"json_extract({safe_plan},{child_path} || '.conversation_ref') "
             f"OR json_type({safe_receipt},'$.actual')!='object' "
             f"OR (json_extract({safe_plan},{child_path} || '.kind')='text' AND ("
             f"json_type({safe_receipt},'$.actual.text')!='text' "
             f"OR json_extract({safe_receipt},'$.actual.text') IS NOT o.message))"
             "))"
             ")"
         ).fetchone()[0]
        # 触发器只在写入时约束 children；启动时还必须重新验证冻结
        # plan 中每个 child 的范围/来源，防止停用触发器后篡改 plan_json，
        # 再恢复触发器并用旧 checksum 伪装成完整 schema。
        invalid_plan_children = conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts a "
            "JOIN json_each(CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END,'$.children') child "
            "WHERE child.type!='object' "
            "OR COALESCE(json_type(child.value,'$.action_id')='text' "
            "AND length(trim(json_extract(child.value,'$.action_id')))>0,0)=0 "
            "OR COALESCE(json_type(child.value,'$.kind')='text' "
            "AND json_extract(child.value,'$.kind') IN "
            "('text','voice','sticker','image','sing'),0)=0 "
            "OR COALESCE(json_type(child.value,'$.channel')='text' "
            "AND json_extract(child.value,'$.channel')="
            "json_extract(a.plan_json,'$.channel'),0)=0 "
            "OR COALESCE(json_type(child.value,'$.target')='text' "
            "AND json_extract(child.value,'$.target')="
            "json_extract(a.plan_json,'$.target'),0)=0 "
            "OR COALESCE(json_type(child.value,'$.payload')='object',0)=0 "
            "OR COALESCE(json_type(child.value,'$.source_id')='text' "
            "AND json_extract(child.value,'$.source_id')="
            "json_extract(a.plan_json,'$.source_id'),0)=0 "
            "OR COALESCE(json_type(child.value,'$.scope_id')='text' "
            "AND json_extract(child.value,'$.scope_id')="
            "json_extract(a.plan_json,'$.scope_id'),0)=0 "
            "OR COALESCE(json_type(child.value,'$.ordinal')='integer' "
            "AND json_extract(child.value,'$.ordinal')>=0,0)=0 "
            "OR COALESCE(json_type(child.value,'$.schema_version')='integer' "
            "AND json_extract(child.value,'$.schema_version')>=1,0)=0 "
            "OR (CAST(COALESCE(json_extract(child.value,'$.schema_version'),0) "
            "AS INTEGER)>=2 AND ("
            "json_type(child.value,'$.identity_version')!='integer' "
            "OR json_extract(child.value,'$.identity_version')!=1 "
            "OR json_type(child.value,'$.conversation_ref')!='object'"
            "))"
        ).fetchone()[0]
        invalid_plan_action_ids = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT a.id,json_extract(child.value,'$.action_id') AS action_id "
            "FROM task_action_attempts a JOIN json_each("
            "CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END,'$.children') child "
            "GROUP BY a.id,json_extract(child.value,'$.action_id') "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
        invalid_plan_ordinals = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT a.id,json_extract(child.value,'$.ordinal') AS ordinal "
            "FROM task_action_attempts a JOIN json_each("
            "CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END,'$.children') child "
            "GROUP BY a.id,json_extract(child.value,'$.ordinal') "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
        conversation_ref_errors = []
        plan_rows = conn.execute(
            "SELECT id,plan_json FROM task_action_attempts ORDER BY id"
        ).fetchall()
        for attempt_id, raw_plan in plan_rows:
            try:
                plan = json.loads(str(raw_plan or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(plan, dict) or not isinstance(plan.get("children"), list):
                continue
            for child in plan["children"]:
                schema_version = (
                    child.get("schema_version") if isinstance(child, dict) else None
                )
                if (not isinstance(child, dict) or isinstance(schema_version, bool)
                        or not isinstance(schema_version, int)
                        or schema_version < 2):
                    continue
                try:
                    conversation = ConversationRef.from_value(
                        child.get("conversation_ref")
                    )
                    conversation.validate_for_action(
                        child.get("channel", ""), child.get("target", ""),
                        child.get("scope_id", ""),
                    )
                except (TypeError, ValueError) as exc:
                    conversation_ref_errors.append(
                        f"attempt {int(attempt_id)}: {str(exc)[:120]}"
                    )
        if conversation_ref_errors:
            errors.append(
                "task action conversation_ref contract invalid: "
                + "; ".join(conversation_ref_errors[:4])
            )

        # Phase 2b 的逻辑 item 是跨 generation 的冻结业务身份；若表已
        # 存在，启动时逐 child 比较 canonical 内容，防止 plan 与 item
        # 分叉后仍被当作同一动作继续归账。Phase 2a 初装时该表尚不存在。
        has_phase2b_items = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_action_items'"
        ).fetchone() is not None
        if has_phase2b_items:
            canonical_drift = []
            rows = conn.execute(
                "SELECT a.id,child.value,i.canonical_json "
                "FROM task_action_attempts a "
                "JOIN json_each(CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
                "ELSE '{\"children\":[]}' END,'$.children') child "
                "JOIN task_action_items i ON i.task_id=a.task_id "
                "AND i.ordinal=json_extract(child.value,'$.ordinal') "
                "WHERE child.type='object'"
            ).fetchall()
            for attempt_id, child_value, canonical_json in rows:
                try:
                    child = json.loads(str(child_value))
                    expected = dict(child)
                    expected.pop("action_id", None)
                    expected.pop("source_id", None)
                    actual = json.loads(str(canonical_json or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    canonical_drift.append(f"attempt {int(attempt_id)}")
                    continue
                if actual != expected:
                    canonical_drift.append(f"attempt {int(attempt_id)}")
            if canonical_drift:
                errors.append(
                    "task action logical item canonical drift: "
                    + ", ".join(canonical_drift[:8])
                )

            # Phase 2b 投影一旦落地，物理 child 就是该 attempt 的版本锚。
            # 不能把 plan/item/receipt 同步改成 v1 来绕过 v2 的身份校验；
            # 终态 attempt 也不能在重启时凭空失去全部 child 投影。
            projection_gaps = []
            terminal_states = {
                "confirmed", "uncertain", "failed", "dead", "partial",
                "cancelled",
            }
            attempt_rows = conn.execute(
                "SELECT id,state,plan_json FROM task_action_attempts ORDER BY id"
            ).fetchall()
            for attempt_id, attempt_state, raw_plan in attempt_rows:
                try:
                    plan = json.loads(str(raw_plan or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                children = plan.get("children") if isinstance(plan, dict) else None
                if not isinstance(children, list):
                    continue
                projected = {
                    (int(row[0]), str(row[1])): int(row[2])
                    for row in conn.execute(
                        "SELECT ordinal,action_id,item_id "
                        "FROM task_action_children WHERE attempt_id=?",
                        (int(attempt_id),),
                    ).fetchall()
                }
                if (str(attempt_state) in terminal_states and children
                        and not projected):
                    projection_gaps.append(f"attempt {int(attempt_id)}")
                if not projected:
                    continue
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    key = (int(child.get("ordinal", -1)),
                           str(child.get("action_id", "")))
                    if key not in projected:
                        projection_gaps.append(f"attempt {int(attempt_id)}")
                        continue
                    # schema floor 由独立 anchor 迁移固定；这里不按当前
                    # plan 版本硬拒绝 legacy v1，保持旧库兼容。
            if projection_gaps:
                errors.append(
                    "task action child coverage is incomplete after projection: "
                    + ", ".join(sorted(set(projection_gaps))[:8])
                )
        if invalid_outbox_identity:
            errors.append("task linked outbox identity differs from frozen plan")
        if invalid_plan_children or invalid_plan_action_ids or invalid_plan_ordinals:
            errors.append("task action plan child identity drift exists")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("task action Phase 2a foreign key check failed")
        if errors:
            raise sqlite3.OperationalError(
                "task action Phase 2a schema invalid: " + "; ".join(errors[:8])
            )

    @staticmethod
    def _task_action_phase2a_receipt_checksum(
        conn, version: str | None = None,
    ) -> str:
        """计算回执 actual 收紧迁移自身的结构指纹。

        Phase 2a 主迁移已经可能在生产库执行，不能改写其 checksum；回执
        guard 作为后续、可审计的小迁移独立指纹，避免静默改变已部署 DDL。
        """
        names = tuple(sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS))
        placeholders = ",".join("?" for _ in names)
        triggers = conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') "
            "FROM sqlite_master WHERE type='trigger' AND name IN ("
            + placeholders + ") ORDER BY name",
            names,
        ).fetchall()
        payload = json.dumps(
            {"version": version or _TASK_ACTION_PHASE2A_RECEIPT_VERSION,
             "triggers": [tuple(row) for row in triggers]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _validate_task_action_phase2a_receipt_schema(self, conn) -> None:
        """验证 task-linked outbox 的 actual 回执与冻结正文一致。"""
        errors: list[str] = []
        safe_plan = (
            "CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END"
        )
        child_path = "'$.children[' || o.ordinal || ']'"
        trigger_sql = {
            row[0]: str(row[1] or "") for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?)",
                tuple(sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS)),
            )
        }
        actual_triggers = set(trigger_sql)
        if actual_triggers != _TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS:
            errors.append("receipt guard trigger manifest differs")
        else:
            outbox_sql = "".join(trigger_sql[
                "trg_task_action_phase2a_receipt_outbox_guard"
            ].split()).lower()
            request_sql = "".join(trigger_sql[
                "trg_task_action_phase2a_receipt_request_guard"
            ].split()).lower()
            if (
                "'$.children['||new.ordinal||'].schema_version'"
                not in outbox_sql
                or "'$.children['||new.ordinal||'].kind'" not in outbox_sql
            ):
                errors.append("receipt outbox guard ordinal contract differs")
            if (
                "'$.children['||linked.ordinal||'].schema_version'"
                not in request_sql
                or "'$.children['||linked.ordinal||'].payload.text'"
                not in request_sql
            ):
                errors.append("receipt request guard ordinal contract differs")

        invalid_actual = conn.execute(
            "SELECT COUNT(*) FROM send_outbox o "
            "JOIN task_action_attempts a ON a.id=o.task_attempt_id "
            "WHERE o.task_attempt_id IS NOT NULL "
            f"AND CAST(COALESCE(json_extract({safe_plan},"
            f"{child_path} || '.schema_version'),0) "
            "AS INTEGER)>=2 AND ("
            "NOT json_valid(o.receipt_template) "
            "OR CASE WHEN json_valid(o.receipt_template) THEN "
            "json_type(o.receipt_template,'$.actual') ELSE '' END!='object' "
            f"OR (json_extract({safe_plan},{child_path} || '.kind')='text' AND ("
            "CASE WHEN json_valid(o.receipt_template) THEN "
            "json_type(o.receipt_template,'$.actual.text') ELSE '' END!='text' "
            "OR CASE WHEN json_valid(o.receipt_template) THEN "
            "json_extract(o.receipt_template,'$.actual.text') ELSE NULL END "
            "IS NOT o.message))"
            ")"
        ).fetchone()[0]
        if invalid_actual:
            errors.append("task linked outbox receipt actual differs from frozen text")
        if errors:
            raise sqlite3.OperationalError(
                "task action Phase 2a receipt schema invalid: "
                + "; ".join(errors[:4])
            )

    def _install_task_action_phase2a_receipt_schema(self, conn) -> None:
        """安装回执 actual guard；独立于已发布的 Phase 2a DDL。"""
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?)",
                tuple(sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS)),
            )
        }
        if existing and existing != _TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS:
            raise sqlite3.OperationalError(
                "task action Phase 2a receipt schema is partially installed"
            )
        if existing:
            return

        conn.execute("""
            CREATE TRIGGER trg_task_action_phase2a_receipt_outbox_guard
            BEFORE INSERT ON send_outbox
            WHEN NEW.task_attempt_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM task_action_attempts a
                WHERE a.id=NEW.task_attempt_id
                  AND CAST(COALESCE(json_extract(
                        a.plan_json,
                        '$.children[' || NEW.ordinal || '].schema_version'
                      ),0)
                           AS INTEGER)>=2
              ) AND (
              NOT json_valid(NEW.receipt_template)
              OR CASE WHEN json_valid(NEW.receipt_template) THEN
                   json_type(NEW.receipt_template,'$.actual') ELSE '' END!='object'
              OR (
                EXISTS (
                  SELECT 1 FROM task_action_attempts a
                  WHERE a.id=NEW.task_attempt_id
                    AND json_extract(
                          a.plan_json,
                          '$.children[' || NEW.ordinal || '].kind'
                        )='text'
                )
                AND (
                  CASE WHEN json_valid(NEW.receipt_template) THEN
                       json_type(NEW.receipt_template,'$.actual.text') ELSE '' END!='text'
                  OR CASE WHEN json_valid(NEW.receipt_template) THEN
                       json_extract(NEW.receipt_template,'$.actual.text')
                       ELSE NULL END IS NOT NEW.message
                )
              )
            )
            BEGIN
                SELECT RAISE(ABORT, 'task outbox receipt actual is invalid');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_phase2a_receipt_request_guard
            BEFORE INSERT ON task_action_retry_requests
            WHEN NEW.result='accepted'
              AND EXISTS (
                SELECT 1
                FROM task_action_attempts created
                JOIN send_outbox linked ON linked.task_attempt_id=created.id
                WHERE created.id=NEW.new_attempt_id
                  AND linked.ordinal=0
                  AND CAST(COALESCE(json_extract(
                        created.plan_json,
                        '$.children[' || linked.ordinal || '].schema_version'
                      ),0)
                           AS INTEGER)>=2
              ) AND NOT EXISTS (
              SELECT 1
              FROM task_action_attempts created
              JOIN send_outbox linked ON linked.task_attempt_id=created.id
              WHERE created.id=NEW.new_attempt_id
                AND created.task_id=NEW.task_id
                AND linked.ordinal=0
                AND json_valid(linked.receipt_template)
                AND json_type(linked.receipt_template,'$.actual')='object'
                AND json_type(linked.receipt_template,'$.actual.text')='text'
                AND json_extract(linked.receipt_template,'$.actual.text')
                    IS linked.message
                AND json_extract(linked.receipt_template,'$.actual.text')
                    IS json_extract(
                         created.plan_json,
                         '$.children[' || linked.ordinal || '].payload.text'
                       )
            )
            BEGIN
                SELECT RAISE(ABORT, 'task retry receipt actual is invalid');
            END
        """)

    def _migrate_task_action_phase2a_receipt_schema(self, conn) -> None:
        """在 Phase 2a 之后原子安装并校验 actual 回执收紧迁移。"""
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2A_RECEIPT_VERSION,),
        ).fetchone()
        if marker:
            self._validate_task_action_phase2a_receipt_schema(conn)
            if str(marker[0]) != self._task_action_phase2a_receipt_checksum(conn):
                raise sqlite3.OperationalError(
                    "task action Phase 2a receipt schema manifest checksum differs"
                )
            return
        v1_marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2A_RECEIPT_V1_VERSION,),
        ).fetchone()
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?)",
                tuple(sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS)),
            )
        }
        if v1_marker:
            if existing != _TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS:
                raise sqlite3.OperationalError(
                    "task action Phase 2a receipt v1 schema is partially installed"
                )
            if str(v1_marker[0]) != self._task_action_phase2a_receipt_checksum(
                conn, _TASK_ACTION_PHASE2A_RECEIPT_V1_VERSION,
            ):
                raise sqlite3.OperationalError(
                    "task action Phase 2a receipt v1 schema manifest checksum differs"
                )
            for name in sorted(_TASK_ACTION_PHASE2A_RECEIPT_TRIGGERS):
                conn.execute(f'DROP TRIGGER "{name}"')
        elif existing:
            raise sqlite3.OperationalError(
                "task action Phase 2a receipt schema is unversioned"
            )
        self._install_task_action_phase2a_receipt_schema(conn)
        self._validate_task_action_phase2a_receipt_schema(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) "
            "VALUES (?,?,?)",
            (
                _TASK_ACTION_PHASE2A_RECEIPT_VERSION,
                self._task_action_phase2a_receipt_checksum(conn),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
            )

    @staticmethod
    def _task_action_phase2b_checksum(conn) -> str:
        """Phase 2b 持久化合同的独立结构指纹。"""
        names = {
            "task_action_items", "task_action_children",
            "task_action_confirmations",
        } | _TASK_ACTION_PHASE2B_INDEXES | _TASK_ACTION_PHASE2B_TRIGGERS
        placeholders = ",".join("?" for _ in names)
        objects = conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            f"WHERE name IN ({placeholders}) ORDER BY type,name",
            tuple(sorted(names)),
        ).fetchall()
        contracts = {}
        for table in ("task_action_items", "task_action_children",
                      "task_action_confirmations"):
            contracts[table] = [tuple(row) for row in conn.execute(
                f"PRAGMA table_info({table})")]
            contracts[f"{table}:fk"] = [tuple(row) for row in conn.execute(
                f"PRAGMA foreign_key_list({table})")]
        payload = json.dumps(
            {"objects": [tuple(row) for row in objects],
             "contracts": contracts},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _task_action_phase2b_floor_checksum(conn) -> str:
        """计算已投影 child 的 schema floor 合同指纹。"""
        names = ({"task_action_projection_anchors"}
                 | _TASK_ACTION_PHASE2B_FLOOR_INDEXES
                 | _TASK_ACTION_PHASE2B_FLOOR_TRIGGERS)
        placeholders = ",".join("?" for _ in names)
        objects = conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            f"WHERE name IN ({placeholders}) ORDER BY type,name",
            tuple(sorted(names)),
        ).fetchall()
        contracts = {
            "table": [tuple(row) for row in conn.execute(
                "PRAGMA table_info(task_action_projection_anchors)")],
            "fk": [tuple(row) for row in conn.execute(
                "PRAGMA foreign_key_list(task_action_projection_anchors)")],
        }
        payload = json.dumps(
            {"objects": [tuple(row) for row in objects],
             "contracts": contracts},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _validate_task_action_phase2b_floor_schema(self, conn) -> None:
        """验证每个已投影 child 的不可降级 schema floor。"""
        errors: list[str] = []
        required = {
            "id", "attempt_id", "ordinal", "action_id", "schema_version",
            "created_at",
        }
        actual = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(task_action_projection_anchors)")
        }
        missing = sorted(required - actual)
        if missing:
            errors.append(f"projection anchor missing columns {missing}")
        actual_indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_task_action_projection_anchors_attempt",),
            )
        }
        if actual_indexes != _TASK_ACTION_PHASE2B_FLOOR_INDEXES:
            errors.append("projection anchor index manifest differs")
        actual_triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
                tuple(sorted(_TASK_ACTION_PHASE2B_FLOOR_TRIGGERS)),
            )
        }
        if actual_triggers != _TASK_ACTION_PHASE2B_FLOOR_TRIGGERS:
            errors.append("projection anchor trigger manifest differs")
        table_sql = "".join(str((conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='task_action_projection_anchors'"
        ).fetchone() or ("",))[0]).lower().split())
        for fragment in (
            "unique(attempt_id,ordinal)", "unique(action_id)",
            "action_idtextnotnull", "schema_versionintegernotnull",
            "check(schema_version>=1)",
        ):
            if fragment not in table_sql:
                errors.append(f"projection anchor contract missing {fragment}")
        trigger_fragments = {
            "trg_task_action_projection_anchor_insert_guard": (
                "new.attempt_id", "new.ordinal", "new.action_id",
                "task_action_attempts", "task_action_children",
            ),
            "trg_task_action_projection_anchor_immutable_update": (
                "new.attempt_idisnotold.attempt_id",
                "new.ordinalisnotold.ordinal",
                "new.action_idisnotold.action_id",
                "new.schema_versionisnotold.schema_version",
            ),
            "trg_task_action_projection_anchor_immutable_delete": (
                "append-only",),
        }
        for name, fragments in trigger_fragments.items():
            sql = "".join(str((conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone() or ("",))[0]).lower().split())
            for fragment in fragments:
                if fragment not in sql:
                    errors.append(f"{name} semantic contract missing {fragment}")

        # anchor 必须逐一对应不可变 plan child 和现存物理 child；任何一边
        # 被删除、换 action 或降低版本，都在启动时 fail-closed。
        for anchor in conn.execute(
            "SELECT attempt_id,ordinal,action_id,schema_version "
            "FROM task_action_projection_anchors ORDER BY id"
        ).fetchall():
            attempt_id, ordinal, action_id, floor = anchor
            child_row = conn.execute(
                "SELECT 1 FROM task_action_children "
                "WHERE attempt_id=? AND ordinal=? AND action_id=?",
                (int(attempt_id), int(ordinal), str(action_id)),
            ).fetchone()
            plan_row = conn.execute(
                "SELECT plan_json FROM task_action_attempts WHERE id=?",
                (int(attempt_id),),
            ).fetchone()
            if not child_row or not plan_row:
                errors.append(f"projection anchor {int(attempt_id)}:{int(ordinal)} orphaned")
                continue
            try:
                plan = json.loads(str(plan_row[0] or ""))
                planned = next(
                    child for child in plan.get("children", [])
                    if isinstance(child, dict)
                    and int(child.get("ordinal", -1)) == int(ordinal)
                    and str(child.get("action_id", "")) == str(action_id)
                )
                version = planned.get("schema_version")
            except (TypeError, ValueError, KeyError, StopIteration,
                    json.JSONDecodeError):
                errors.append(f"projection anchor {int(attempt_id)}:{int(ordinal)} plan mismatch")
                continue
            if (isinstance(version, bool) or not isinstance(version, int)
                    or int(floor) != version):
                errors.append(
                    f"projection anchor {int(attempt_id)}:{int(ordinal)} "
                    "schema downgrade or drift"
                )
        missing_anchor = conn.execute(
            "SELECT COUNT(*) FROM task_action_children c "
            "WHERE NOT EXISTS (SELECT 1 FROM task_action_projection_anchors a "
            "WHERE a.attempt_id=c.attempt_id AND a.ordinal=c.ordinal "
            "AND a.action_id=c.action_id)"
        ).fetchone()[0]
        if missing_anchor:
            errors.append("projected child schema floor is missing")
        if errors:
            raise sqlite3.OperationalError(
                "task action Phase 2b floor schema invalid: "
                + "; ".join(errors[:8])
            )

    def _migrate_task_action_phase2b_floor_schema(self, conn) -> None:
        """安装/校验 child schema floor；旧 v1 projection 按原版本锚定。"""
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_FLOOR_VERSION,),
        ).fetchone()
        names = ({"task_action_projection_anchors"}
                 | _TASK_ACTION_PHASE2B_FLOOR_INDEXES
                 | _TASK_ACTION_PHASE2B_FLOOR_TRIGGERS)
        placeholders = ",".join("?" for _ in names)
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN (" + placeholders + ")",
                tuple(sorted(names)),
            )
        }
        if marker:
            if existing != names:
                raise sqlite3.OperationalError(
                    "task action Phase 2b floor schema is partial"
                )
            self._validate_task_action_phase2b_floor_schema(conn)
            checksum = self._task_action_phase2b_floor_checksum(conn)
            if str(marker[0]) != checksum:
                raise sqlite3.OperationalError(
                    "task action Phase 2b floor schema manifest checksum differs"
                )
            return
        if existing:
            raise sqlite3.OperationalError(
                "task action Phase 2b floor schema is unversioned or partial"
            )
        conn.execute("""
            CREATE TABLE task_action_projection_anchors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                action_id TEXT NOT NULL CHECK(length(trim(action_id)) > 0),
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                created_at TEXT NOT NULL,
                UNIQUE(attempt_id,ordinal),
                UNIQUE(action_id),
                FOREIGN KEY(attempt_id) REFERENCES task_action_attempts(id)
                    ON DELETE RESTRICT
            )
        """)
        conn.execute(
            "CREATE INDEX idx_task_action_projection_anchors_attempt "
            "ON task_action_projection_anchors(attempt_id,ordinal)"
        )
        conn.execute("""
            CREATE TRIGGER trg_task_action_projection_anchor_insert_guard
            BEFORE INSERT ON task_action_projection_anchors
            WHEN NOT EXISTS (
                SELECT 1 FROM task_action_attempts a
                JOIN task_action_children c ON c.attempt_id=a.id
                    AND c.ordinal=NEW.ordinal AND c.action_id=NEW.action_id
                WHERE a.id=NEW.attempt_id
                  AND json_valid(a.plan_json)
                  AND CAST(json_extract(a.plan_json,'$.children[' || NEW.ordinal
                      || '].schema_version') AS INTEGER)=NEW.schema_version
            )
            BEGIN SELECT RAISE(ABORT,'projection anchor identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_projection_anchor_immutable_update
            BEFORE UPDATE OF attempt_id,ordinal,action_id,schema_version
                ON task_action_projection_anchors
            WHEN NEW.attempt_id IS NOT OLD.attempt_id
              OR NEW.ordinal IS NOT OLD.ordinal
              OR NEW.action_id IS NOT OLD.action_id
              OR NEW.schema_version IS NOT OLD.schema_version
            BEGIN SELECT RAISE(ABORT,'projection anchor is immutable'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_projection_anchor_immutable_delete
            BEFORE DELETE ON task_action_projection_anchors
            BEGIN SELECT RAISE(ABORT,'projection anchor is append-only'); END
        """)
        # 迁移期间由不可变的当前 plan/child 建立一次 floor；旧 v1 仍保留
        # v1，不将兼容性数据伪装成 v2。
        rows = conn.execute(
            "SELECT c.attempt_id,c.ordinal,c.action_id,a.plan_json "
            "FROM task_action_children c JOIN task_action_attempts a "
            "ON a.id=c.attempt_id ORDER BY c.id"
        ).fetchall()
        for attempt_id, ordinal, action_id, raw_plan in rows:
            try:
                plan = json.loads(str(raw_plan or ""))
                child = next(
                    child for child in plan["children"]
                    if isinstance(child, dict)
                    and int(child["ordinal"]) == int(ordinal)
                    and str(child["action_id"]) == str(action_id)
                )
                version = child["schema_version"]
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise ValueError("invalid schema_version")
            except (TypeError, ValueError, KeyError, StopIteration,
                    json.JSONDecodeError) as exc:
                raise sqlite3.OperationalError(
                    f"projection anchor backfill failed for {attempt_id}:{ordinal}: {exc}"
                ) from exc
            conn.execute(
                "INSERT INTO task_action_projection_anchors "
                "(attempt_id,ordinal,action_id,schema_version,created_at) "
                "VALUES (?,?,?,?,?)",
                (int(attempt_id), int(ordinal), str(action_id), int(version),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        self._validate_task_action_phase2b_floor_schema(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) "
            "VALUES (?,?,?)",
            (_TASK_ACTION_PHASE2B_FLOOR_VERSION,
             self._task_action_phase2b_floor_checksum(conn),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def _validate_task_action_phase2b_schema(self, conn) -> None:
        """验证逻辑 item、物理 child 和确认事实的完整性。"""
        errors: list[str] = []
        required_columns = {
            "task_action_items": {
                "id", "task_id", "ordinal", "canonical_json", "created_at",
            },
            "task_action_children": {
                "id", "item_id", "attempt_id", "generation", "ordinal",
                "action_id", "outbox_id", "state", "created_at", "updated_at",
            },
            "task_action_confirmations": {
                "id", "task_id", "attempt_id", "item_id", "generation",
                "ordinal", "action_id", "outbox_id", "evidence_source",
                "message_ids_json", "evidence_json", "confirmed_at",
                "recorded_at", "actor",
            },
        }
        for table, required in required_columns.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(required - actual)
            if missing:
                errors.append(f"{table} missing columns {missing}")

        expected_fks = {
            "task_action_items": {("tasks", "task_id", "id", "RESTRICT")},
            "task_action_children": {
                ("task_action_items", "item_id", "id", "RESTRICT"),
                ("task_action_attempts", "attempt_id", "id", "RESTRICT"),
            },
            "task_action_confirmations": {
                ("tasks", "task_id", "id", "RESTRICT"),
                ("task_action_attempts", "attempt_id", "id", "RESTRICT"),
                ("task_action_items", "item_id", "id", "RESTRICT"),
            },
        }
        for table, expected in expected_fks.items():
            actual = {
                (row[2], row[3], row[4], row[6])
                for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            }
            if not expected <= actual:
                errors.append(f"{table} Phase 2b foreign keys differ")

        actual_indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN (" + ",".join("?" for _ in _TASK_ACTION_PHASE2B_INDEXES)
                + ")", tuple(sorted(_TASK_ACTION_PHASE2B_INDEXES)))
        }
        if actual_indexes != _TASK_ACTION_PHASE2B_INDEXES:
            errors.append("task action Phase 2b index manifest differs")
        actual_triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name IN (" + ",".join("?" for _ in _TASK_ACTION_PHASE2B_TRIGGERS)
                + ")", tuple(sorted(_TASK_ACTION_PHASE2B_TRIGGERS)))
        }
        if actual_triggers != _TASK_ACTION_PHASE2B_TRIGGERS:
            errors.append("task action Phase 2b trigger manifest differs")

        # 名称与 checksum 不能单独证明触发器仍保留关键护栏：攻击者若
        # 同步替换 DDL 和 marker，必须仍被代码侧的语义片段校验拦截。
        semantic_trigger_fragments = {
            "trg_task_action_child_outbox_insert_guard": (
                "new.outbox_id!=''", "send_outbox", "new.attempt_id",
                "new.ordinal", "confirmed_action_facts",
            ),
            "trg_task_action_child_outbox_update_guard": (
                "new.outbox_idisnotold.outbox_id", "send_outbox",
                "new.attempt_id", "new.ordinal", "confirmed_action_facts",
            ),
            "trg_task_action_child_state_guard": (
                "new.stateisnotold.state", "send_outbox",
                "o.status=new.state", "task_action_confirmations",
            ),
            "trg_task_action_confirmation_evidence_guard": (
                "json_each", "message_ids_json", "value=0",
                "2147483648", "2147483647",
            ),
            "trg_task_action_outbox_delete_requires_confirmation": (
                "old.task_attempt_idisnotnull",
                "task_action_confirmations",
                "c.outbox_id=old.action_id",
            ),
            "trg_task_action_attempt_state_transition": (
                "task_action_children",
                "task_action_confirmations",
                "c.action_id=ch.action_id",
            ),
            "trg_task_action_confirmation_identity_guard": (
                "task_action_children", "new.ordinal", "new.action_id",
                "new.outbox_id", "new.evidence_source", "send_outbox",
                "status='sending'", "statusin('uncertain','failed','dead')",
                "confirmed_action_facts",
            ),
            "trg_task_action_task_status_guard": (
                "new.status='done'",
                "task_action_items",
                "task_action_confirmations",
            ),
        }
        for trigger_name, fragments in semantic_trigger_fragments.items():
            trigger_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            trigger_sql = "".join(str(trigger_row[0] if trigger_row else "").lower().split())
            for fragment in fragments:
                if fragment not in trigger_sql:
                    errors.append(
                        f"{trigger_name} semantic contract missing {fragment}"
                    )

        for table, fragments in {
            "task_action_items": (
                "unique(task_id,ordinal)", "json_valid(canonical_json)",
                "json_type(canonical_json,'$')='object'",
            ),
            "task_action_children": (
                "unique(attempt_id,ordinal)", "action_idtextnotnullunique",
            ),
            "task_action_confirmations": (
                "action_idtextnotnullunique", "json_valid(message_ids_json)",
                "json_type(message_ids_json,'$')='array'",
                "json_valid(evidence_json)",
                "json_type(evidence_json,'$')='object'",
            ),
        }.items():
            sql = "".join(str((conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() or ("",))[0]).lower().split())
            for fragment in fragments:
                if fragment not in sql:
                    errors.append(f"{table} contract missing {fragment}")

        invalid_children = conn.execute(
            "SELECT COUNT(*) FROM task_action_children c "
            "JOIN task_action_attempts a ON a.id=c.attempt_id "
            "JOIN task_action_items i ON i.id=c.item_id "
            "WHERE c.generation!=a.generation OR c.ordinal!=i.ordinal "
            "OR i.task_id!=a.task_id OR trim(c.action_id)=''"
        ).fetchone()[0]
        invalid_child_outboxes = conn.execute(
            "SELECT COUNT(*) FROM task_action_children c "
            "WHERE c.outbox_id='' OR (NOT EXISTS ("
            "  SELECT 1 FROM send_outbox o "
            "  WHERE o.action_id=c.outbox_id AND o.task_attempt_id=c.attempt_id "
            "    AND o.ordinal=c.ordinal"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM task_action_confirmations f "
            "  WHERE f.attempt_id=c.attempt_id AND f.ordinal=c.ordinal "
            "    AND f.outbox_id=c.outbox_id AND f.action_id=c.action_id"
            "))"
        ).fetchone()[0]
        invalid_child_states = conn.execute(
            "SELECT COUNT(*) FROM task_action_children c "
            "WHERE NOT (EXISTS ("
            "  SELECT 1 FROM send_outbox o "
            "  WHERE o.action_id=c.outbox_id AND o.task_attempt_id=c.attempt_id "
            "    AND o.ordinal=c.ordinal AND o.status=c.state"
            ") OR (c.state='confirmed' AND EXISTS ("
            "  SELECT 1 FROM task_action_confirmations f "
            "  WHERE f.attempt_id=c.attempt_id AND f.ordinal=c.ordinal "
            "    AND f.outbox_id=c.outbox_id AND f.action_id=c.action_id"
            ")))"
        ).fetchone()[0]
        missing_children = conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts a "
            "JOIN json_each(CASE WHEN json_valid(a.plan_json) THEN a.plan_json "
            "ELSE '{\"children\":[]}' END,'$.children') planned "
            "WHERE COALESCE(json_extract(planned.value,'$.schema_version'),1)>=2 "
            "AND (NOT EXISTS ("
            "  SELECT 1 FROM json_each(CASE WHEN json_valid(a.plan_json) "
            "  THEN a.plan_json ELSE '{\"children\":[]}' END,'$.children') legacy "
            "  WHERE COALESCE(json_extract(legacy.value,'$.schema_version'),1)<2"
            ") OR EXISTS ("
            "  SELECT 1 FROM task_action_children projected "
            "  WHERE projected.attempt_id=a.id"
            ")) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_action_children c "
            "  WHERE c.attempt_id=a.id "
            "    AND c.ordinal=json_extract(planned.value,'$.ordinal') "
            "    AND c.action_id=json_extract(planned.value,'$.action_id')"
            ")"
        ).fetchone()[0]
        invalid_confirmations = conn.execute(
            "SELECT COUNT(*) FROM task_action_confirmations c "
            "JOIN task_action_attempts a ON a.id=c.attempt_id "
            "JOIN task_action_items i ON i.id=c.item_id "
            "JOIN task_action_children ch ON ch.attempt_id=c.attempt_id "
            " AND ch.ordinal=c.ordinal "
            "WHERE c.task_id!=a.task_id OR c.generation!=a.generation "
            "OR c.ordinal!=i.ordinal OR ch.action_id!=c.action_id "
            "OR ch.outbox_id!=c.outbox_id"
        ).fetchone()[0]
        invalid_confirmation_delivery = conn.execute(
            "SELECT COUNT(*) FROM task_action_confirmations c "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM send_outbox o "
            "  WHERE o.action_id=c.outbox_id AND o.task_attempt_id=c.attempt_id "
            "    AND o.ordinal=c.ordinal AND ("
            "      (c.evidence_source='outbox_settle' AND o.status IN "
            "          ('sending','confirmed_unaccounted','confirmed_conflict'))"
            "      OR (c.evidence_source='late_settle' AND o.status IN "
            "          ('uncertain','failed','dead','confirmed_unaccounted',"
            "           'confirmed_conflict'))"
            "      OR (c.evidence_source='known_confirmed_backfill' AND o.status IN "
            "          ('confirmed_unaccounted','confirmed_conflict'))"
            "    )"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM confirmed_action_facts f "
            "  WHERE f.domain_action_id=c.action_id AND f.outbox_id=c.outbox_id"
            ")"
        ).fetchone()[0]
        invalid_confirmation_evidence = conn.execute(
            "SELECT COUNT(*) FROM task_action_confirmations c "
            "WHERE json_valid(c.message_ids_json)=0 "
            "OR json_type(c.message_ids_json,'$')!='array' "
            "OR CASE WHEN json_valid(c.message_ids_json) THEN "
            "json_array_length(c.message_ids_json)=0 ELSE 0 END "
            "OR EXISTS (SELECT 1 FROM json_each(CASE WHEN "
            "json_valid(c.message_ids_json) THEN c.message_ids_json ELSE '[]' END) m "
            "WHERE m.type!='integer' OR m.value=0 "
            "OR m.value<-(2147483648) OR m.value>2147483647)"
        ).fetchone()[0]
        if invalid_children:
            errors.append("task action child identity drift exists")
        if invalid_child_outboxes:
            errors.append("task action child outbox identity drift exists")
        if invalid_child_states:
            errors.append("task action child state drift exists")
        if missing_children:
            errors.append("task action child coverage is incomplete")
        if invalid_confirmations:
            errors.append("task action confirmation identity drift exists")
        if invalid_confirmation_delivery:
            errors.append("task action confirmation delivery evidence is invalid")
        if invalid_confirmation_evidence:
            errors.append("task action confirmation evidence is invalid")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("task action Phase 2b foreign key check failed")
        if errors:
            raise sqlite3.OperationalError(
                "task action Phase 2b schema invalid: " + "; ".join(errors[:8])
            )

    @staticmethod
    def _phase2b_canonical_child(child: dict) -> dict:
        canonical = json.loads(json.dumps(child, ensure_ascii=False, sort_keys=True))
        canonical.pop("action_id", None)
        canonical.pop("source_id", None)
        return canonical

    def _backfill_task_action_phase2b(self, conn) -> None:
        """从不可变 plan 和已存在的正向事实建立 Phase 2b 投影。"""
        affected_attempt_ids: set[int] = set()
        affected_task_ids: set[int] = set()
        attempts = conn.execute(
            "SELECT id,task_id,generation,plan_json,state FROM task_action_attempts "
            "ORDER BY task_id,generation,id"
        ).fetchall()
        for attempt_id, task_id, generation, plan_json, attempt_state in attempts:
            affected_attempt_ids.add(int(attempt_id))
            affected_task_ids.add(int(task_id))
            try:
                plan = json.loads(str(plan_json or ""))
                children = plan["children"]
                if not isinstance(children, list) or not children:
                    raise ValueError("children")
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise sqlite3.OperationalError(
                    f"task action Phase 2b cannot backfill attempt {attempt_id}: {exc}"
                ) from exc
            if str(attempt_state) == "confirmed":
                # 旧库的 attempt.state 不是确认事实本身：若 outbox 已删，
                # 必须还能在永久 facts 找到同一 action；否则宁可拒绝启动，
                # 也不能把未经平台证实的“confirmed”扩散到新投影。
                for child in children:
                    proven = conn.execute(
                        "SELECT 1 FROM send_outbox "
                        "WHERE task_attempt_id=? AND ordinal=? "
                        "AND status IN ('confirmed','confirmed_unaccounted',"
                        "'confirmed_conflict') "
                        "UNION ALL SELECT 1 FROM confirmed_action_facts "
                        "WHERE domain_action_id=? LIMIT 1",
                        (int(attempt_id), int(child["ordinal"]),
                         str(child["action_id"])),
                    ).fetchone()
                    if not proven:
                        raise sqlite3.OperationalError(
                            f"task action attempt {attempt_id} confirmed without evidence"
                        )
            for child in children:
                ordinal = int(child["ordinal"])
                canonical_json = json.dumps(
                    self._phase2b_canonical_child(child), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"),
                )
                item = conn.execute(
                    "SELECT id,canonical_json FROM task_action_items "
                    "WHERE task_id=? AND ordinal=?", (int(task_id), ordinal),
                ).fetchone()
                if item:
                    if str(item[1]) != canonical_json:
                        raise sqlite3.OperationalError(
                            "task action logical item payload drift"
                        )
                    item_id = int(item[0])
                else:
                    item_id = int(conn.execute(
                        "INSERT INTO task_action_items "
                        "(task_id,ordinal,canonical_json,created_at) VALUES (?,?,?,?) "
                        "RETURNING id",
                        (int(task_id), ordinal, canonical_json,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    ).fetchone()[0])
                outbox = conn.execute(
                    "SELECT action_id,status FROM send_outbox "
                    "WHERE task_attempt_id=? AND ordinal=?",
                    (int(attempt_id), ordinal),
                ).fetchone()
                fact = conn.execute(
                    "SELECT outbox_id FROM confirmed_action_facts "
                    "WHERE domain_action_id=?", (str(child["action_id"]),),
                ).fetchone()
                if outbox and fact and str(outbox[0]) != str(fact[0]):
                    raise sqlite3.OperationalError(
                        "task action confirmed delivery anchor drift"
                    )
                child_outbox_id = str(outbox[0]) if outbox else (
                    str(fact[0]) if fact else ""
                )
                child_state = str(outbox[1]) if outbox else (
                    "confirmed" if fact else str(attempt_state)
                )
                conn.execute(
                    "INSERT INTO task_action_children "
                    "(item_id,attempt_id,generation,ordinal,action_id,outbox_id,state,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (item_id, int(attempt_id), int(generation), ordinal,
                     str(child["action_id"]), child_outbox_id,
                     child_state, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )

        # confirmed_action_facts 已经是投影后的不可变正向事实；只有能精确
        # 对应 attempt/source/action 的行才回填，其他普通动作事实不碰。
        facts = conn.execute(
            "SELECT f.domain_action_id,f.outbox_id,f.source_id,f.actual_json,"
            "f.message_ids_json,f.confirmed_at "
            "FROM confirmed_action_facts f "
            "WHERE f.source_id LIKE 'task:%'"
        ).fetchall()
        for action_id, outbox_id, source_id, actual_json, message_ids, confirmed_at in facts:
            match = conn.execute(
                "SELECT a.id,a.task_id,a.generation,i.id,i.ordinal "
                "FROM task_action_attempts a "
                "JOIN task_action_children ch ON ch.attempt_id=a.id "
                "JOIN task_action_items i ON i.id=ch.item_id AND i.ordinal=ch.ordinal "
                "WHERE a.plan_json LIKE ? AND ch.action_id=?",
                (f'%"source_id":"{source_id}"%', str(action_id)),
            ).fetchone()
            if not match:
                continue
            attempt_id, task_id, generation, item_id, ordinal = match
            conn.execute(
                "INSERT OR IGNORE INTO task_action_confirmations "
                "(task_id,attempt_id,item_id,generation,ordinal,action_id,outbox_id,"
                "evidence_source,message_ids_json,evidence_json,confirmed_at,recorded_at,actor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(task_id), int(attempt_id), int(item_id), int(generation),
                 int(ordinal), str(action_id), str(outbox_id or ""),
                 "known_confirmed_backfill", str(message_ids or "[]"),
                 json.dumps({"actual_json": str(actual_json or "")},
                            ensure_ascii=False, separators=(",", ":")),
                 str(confirmed_at or ""), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "migration"),
            )
        # 迁移时保留的 known-confirmed outbox 也必须先进入事实面。
        rows = conn.execute(
            "SELECT o.action_id,o.domain_action_id,o.task_attempt_id,o.ordinal,"
            "o.confirmed_message_ids,o.confirmed_at "
            "FROM send_outbox o WHERE o.task_attempt_id IS NOT NULL "
            "AND o.status IN ('confirmed_unaccounted','confirmed_conflict')"
        ).fetchall()
        for outbox_id, action_id, attempt_id, ordinal, message_ids, confirmed_at in rows:
            child = conn.execute(
                "SELECT a.task_id,a.generation,ch.item_id,ch.action_id "
                "FROM task_action_attempts a JOIN task_action_children ch "
                "ON ch.attempt_id=a.id AND ch.ordinal=? WHERE a.id=?",
                (int(ordinal), int(attempt_id)),
            ).fetchone()
            if not child or str(child[3]) != str(action_id):
                raise sqlite3.OperationalError(
                    "known-confirmed outbox has no matching Phase 2b child"
                )
            conn.execute(
                "INSERT OR IGNORE INTO task_action_confirmations "
                "(task_id,attempt_id,item_id,generation,ordinal,action_id,outbox_id,"
                "evidence_source,message_ids_json,evidence_json,confirmed_at,recorded_at,actor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(child[0]), int(attempt_id), int(child[2]), int(child[1]),
                 int(ordinal), str(action_id), str(outbox_id),
                 "known_confirmed_backfill", str(message_ids or "[]"),
                 json.dumps({"source": "known_confirmed_outbox"}, separators=(",", ":")),
                 str(confirmed_at or ""), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "migration"),
            )
            conn.execute(
                "UPDATE task_action_attempts SET accounting_state='pending_repair',"
                "last_error=COALESCE(NULLIF(last_error,''),'KNOWN_CONFIRMED_BACKFILL'),"
                "updated_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(attempt_id)),
            )
            affected_attempt_ids.add(int(attempt_id))

        # 回填不仅建投影，还必须在同一 savepoint 内完成 reducer；否则旧库
        # 会留下 sending/none 的假开放状态，重启后既不重发也不显示 done。
        for attempt_id in sorted(affected_attempt_ids):
            self._reduce_task_action_attempt_conn(conn, attempt_id)
            task_row = conn.execute(
                "SELECT task_id FROM task_action_attempts WHERE id=?",
                (int(attempt_id),),
            ).fetchone()
            if task_row:
                affected_task_ids.add(int(task_row[0]))
        for task_id in sorted(affected_task_ids):
            self._reduce_task_action_task_conn(conn, task_id)

    def _install_task_action_phase2b_schema(self, conn) -> None:
        """安装 Phase 2b 正向事实面和 outbox 删除护栏。"""
        conn.execute("""
            CREATE TABLE task_action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                canonical_json TEXT NOT NULL CHECK(
                    json_valid(canonical_json)
                    AND json_type(canonical_json,'$')='object'
                ),
                created_at TEXT NOT NULL,
                UNIQUE(task_id,ordinal),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT
            )
        """)
        conn.execute("""
            CREATE TABLE task_action_children (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                attempt_id INTEGER NOT NULL,
                generation INTEGER NOT NULL CHECK(generation >= 0),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                action_id TEXT NOT NULL UNIQUE CHECK(length(trim(action_id)) > 0),
                outbox_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK(state IN (
                    'pending','sending','confirmed','uncertain','failed','dead',
                    'partial','cancelled','confirmed_unaccounted','confirmed_conflict'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(attempt_id,ordinal),
                FOREIGN KEY(item_id) REFERENCES task_action_items(id) ON DELETE RESTRICT,
                FOREIGN KEY(attempt_id) REFERENCES task_action_attempts(id) ON DELETE RESTRICT
            )
        """)
        conn.execute("""
            CREATE TABLE task_action_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                attempt_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                generation INTEGER NOT NULL CHECK(generation >= 0),
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                action_id TEXT NOT NULL UNIQUE CHECK(length(trim(action_id)) > 0),
                outbox_id TEXT NOT NULL DEFAULT '',
                evidence_source TEXT NOT NULL CHECK(evidence_source IN (
                    'outbox_settle','late_settle','known_confirmed_backfill'
                )),
                message_ids_json TEXT NOT NULL CHECK(
                    json_valid(message_ids_json)
                    AND json_type(message_ids_json,'$')='array'
                ),
                evidence_json TEXT NOT NULL CHECK(
                    json_valid(evidence_json)
                    AND json_type(evidence_json,'$')='object'
                ),
                confirmed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY(attempt_id) REFERENCES task_action_attempts(id) ON DELETE RESTRICT,
                FOREIGN KEY(item_id) REFERENCES task_action_items(id) ON DELETE RESTRICT
            )
        """)
        conn.execute("CREATE INDEX idx_task_action_items_task_ordinal ON task_action_items(task_id,ordinal)")
        conn.execute("CREATE INDEX idx_task_action_children_attempt_ordinal ON task_action_children(attempt_id,ordinal)")
        conn.execute("CREATE INDEX idx_task_action_children_item_generation ON task_action_children(item_id,generation)")
        conn.execute("CREATE INDEX idx_task_action_confirmations_task_ordinal ON task_action_confirmations(task_id,ordinal,id)")
        conn.execute("CREATE INDEX idx_task_action_confirmations_attempt ON task_action_confirmations(attempt_id,id)")
        conn.execute("""
            CREATE TRIGGER trg_task_action_item_immutable_update
            BEFORE UPDATE OF task_id,ordinal,canonical_json ON task_action_items
            BEGIN SELECT RAISE(ABORT,'task action item identity is immutable'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_item_immutable_delete
            BEFORE DELETE ON task_action_items
            BEGIN SELECT RAISE(ABORT,'task action item is append-only'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_child_identity_immutable
            BEFORE UPDATE OF item_id,attempt_id,generation,ordinal,action_id ON task_action_children
            WHEN NEW.item_id IS NOT OLD.item_id OR NEW.attempt_id IS NOT OLD.attempt_id
              OR NEW.generation IS NOT OLD.generation OR NEW.ordinal IS NOT OLD.ordinal
              OR NEW.action_id IS NOT OLD.action_id
            BEGIN SELECT RAISE(ABORT,'task action child identity is immutable'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_child_outbox_insert_guard
            BEFORE INSERT ON task_action_children
            WHEN NEW.outbox_id!=''
              AND NOT EXISTS (
                  SELECT 1 FROM send_outbox o
                  WHERE o.action_id=NEW.outbox_id
                    AND o.task_attempt_id=NEW.attempt_id
                    AND o.ordinal=NEW.ordinal
              )
              AND NOT EXISTS (
                  SELECT 1 FROM confirmed_action_facts f
                  WHERE f.outbox_id=NEW.outbox_id
                    AND f.domain_action_id=NEW.action_id
              )
            BEGIN SELECT RAISE(ABORT,'task action child outbox identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_child_outbox_update_guard
            BEFORE UPDATE OF outbox_id ON task_action_children
            WHEN NEW.outbox_id IS NOT OLD.outbox_id AND (
                OLD.outbox_id!='' OR (
                    NOT EXISTS (
                        SELECT 1 FROM send_outbox o
                        WHERE o.action_id=NEW.outbox_id
                          AND o.task_attempt_id=NEW.attempt_id
                          AND o.ordinal=NEW.ordinal
                    ) AND NOT EXISTS (
                        SELECT 1 FROM confirmed_action_facts f
                        WHERE f.outbox_id=NEW.outbox_id
                          AND f.domain_action_id=NEW.action_id
                    )
                )
            )
            BEGIN SELECT RAISE(ABORT,'task action child outbox identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_child_state_guard
            BEFORE UPDATE OF state ON task_action_children
            WHEN NEW.state IS NOT OLD.state AND NOT (
                EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.task_attempt_id=NEW.attempt_id
                      AND o.ordinal=NEW.ordinal
                      AND o.status=NEW.state
                ) OR (
                    NEW.state='confirmed' AND EXISTS (
                        SELECT 1 FROM task_action_confirmations c
                        WHERE c.attempt_id=NEW.attempt_id
                          AND c.ordinal=NEW.ordinal
                          AND c.action_id=NEW.action_id
                          AND c.outbox_id=NEW.outbox_id
                    )
                )
            )
            BEGIN SELECT RAISE(ABORT,'task action child state disagrees with delivery'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_child_immutable_delete
            BEFORE DELETE ON task_action_children
            BEGIN SELECT RAISE(ABORT,'task action child is append-only'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_identity_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN NOT EXISTS (
                SELECT 1 FROM task_action_attempts a
                JOIN task_action_children ch ON ch.attempt_id=a.id
                    AND ch.ordinal=NEW.ordinal AND ch.action_id=NEW.action_id
                    AND ch.outbox_id=NEW.outbox_id
                JOIN task_action_items i ON i.id=ch.item_id
                WHERE a.id=NEW.attempt_id AND a.task_id=NEW.task_id
                  AND a.generation=NEW.generation AND i.id=NEW.item_id
            ) OR (
                NEW.evidence_source='outbox_settle' AND NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.task_attempt_id=NEW.attempt_id
                      AND o.ordinal=NEW.ordinal AND o.status='sending'
                )
            ) OR (
                NEW.evidence_source='late_settle' AND NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.task_attempt_id=NEW.attempt_id
                      AND o.ordinal=NEW.ordinal
                      AND o.status IN ('uncertain','failed','dead')
                )
            ) OR (
                NEW.evidence_source='known_confirmed_backfill' AND NOT (
                    EXISTS (
                        SELECT 1 FROM send_outbox o
                        WHERE o.action_id=NEW.outbox_id
                          AND o.task_attempt_id=NEW.attempt_id
                          AND o.ordinal=NEW.ordinal
                          AND o.status IN ('confirmed_unaccounted',
                                           'confirmed_conflict')
                    ) OR EXISTS (
                        SELECT 1 FROM confirmed_action_facts f
                        WHERE f.domain_action_id=NEW.action_id
                          AND f.outbox_id=NEW.outbox_id
                    )
                )
            )
            BEGIN SELECT RAISE(ABORT,'task action confirmation identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_evidence_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN CASE WHEN json_valid(NEW.message_ids_json)
                      THEN json_array_length(NEW.message_ids_json)=0
                      ELSE 0 END
              OR EXISTS (
                  SELECT 1 FROM json_each(NEW.message_ids_json) m
                  WHERE m.type!='integer' OR m.value=0
                    OR m.value<-(2147483648) OR m.value>2147483647
              )
            BEGIN SELECT RAISE(ABORT,'task action confirmation evidence is invalid'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_immutable_update
            BEFORE UPDATE ON task_action_confirmations
            BEGIN SELECT RAISE(ABORT,'task action confirmation is append-only'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_immutable_delete
            BEFORE DELETE ON task_action_confirmations
            BEGIN SELECT RAISE(ABORT,'task action confirmation is append-only'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_duplicate_event
            AFTER INSERT ON task_action_confirmations
            WHEN EXISTS (
                SELECT 1 FROM task_action_confirmations old
                WHERE old.task_id=NEW.task_id AND old.ordinal=NEW.ordinal
                  AND old.id<NEW.id AND old.action_id!=NEW.action_id
            )
            BEGIN
                INSERT INTO task_action_events
                    (attempt_id,event_type,from_state,to_state,outbox_id,
                     domain_action_id,actor,reason,metadata_json)
                SELECT NEW.attempt_id,'duplicate_delivery_detected','','',
                       NEW.outbox_id,NEW.action_id,NEW.actor,
                       'same logical item confirmed by multiple physical actions',
                       json_object('task_id',NEW.task_id,'ordinal',NEW.ordinal,
                                   'action_id',NEW.action_id);
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_outbox_delete_requires_confirmation
            BEFORE DELETE ON send_outbox
            WHEN OLD.task_attempt_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM task_action_confirmations c
                WHERE c.attempt_id=OLD.task_attempt_id
                  AND c.ordinal=OLD.ordinal AND c.outbox_id=OLD.action_id
            )
            BEGIN SELECT RAISE(ABORT,'linked outbox requires confirmation before delete'); END
        """)

    @staticmethod
    def _upgrade_task_action_phase2b_confirmation_guard(conn) -> None:
        """从已发布的 Phase 2b v1 升级确认事实的投递状态护栏。"""
        conn.execute(
            "DROP TRIGGER trg_task_action_confirmation_identity_guard"
        )
        conn.execute(
            "DROP TRIGGER trg_task_action_confirmation_evidence_guard"
        )
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_identity_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN NOT EXISTS (
                SELECT 1 FROM task_action_attempts a
                JOIN task_action_children ch ON ch.attempt_id=a.id
                    AND ch.ordinal=NEW.ordinal AND ch.action_id=NEW.action_id
                    AND ch.outbox_id=NEW.outbox_id
                JOIN task_action_items i ON i.id=ch.item_id
                WHERE a.id=NEW.attempt_id AND a.task_id=NEW.task_id
                  AND a.generation=NEW.generation AND i.id=NEW.item_id
            ) OR (
                NEW.evidence_source='outbox_settle' AND NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.task_attempt_id=NEW.attempt_id
                      AND o.ordinal=NEW.ordinal AND o.status='sending'
                )
            ) OR (
                NEW.evidence_source='late_settle' AND NOT EXISTS (
                    SELECT 1 FROM send_outbox o
                    WHERE o.action_id=NEW.outbox_id
                      AND o.task_attempt_id=NEW.attempt_id
                      AND o.ordinal=NEW.ordinal
                      AND o.status IN ('uncertain','failed','dead')
                )
            ) OR (
                NEW.evidence_source='known_confirmed_backfill' AND NOT (
                    EXISTS (
                        SELECT 1 FROM send_outbox o
                        WHERE o.action_id=NEW.outbox_id
                          AND o.task_attempt_id=NEW.attempt_id
                          AND o.ordinal=NEW.ordinal
                          AND o.status IN ('confirmed_unaccounted',
                                           'confirmed_conflict')
                    ) OR EXISTS (
                        SELECT 1 FROM confirmed_action_facts f
                        WHERE f.domain_action_id=NEW.action_id
                          AND f.outbox_id=NEW.outbox_id
                    )
                )
            )
            BEGIN SELECT RAISE(ABORT,'task action confirmation identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_evidence_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN CASE WHEN json_valid(NEW.message_ids_json)
                      THEN json_array_length(NEW.message_ids_json)=0
                      ELSE 0 END
              OR EXISTS (
                  SELECT 1 FROM json_each(NEW.message_ids_json) m
                  WHERE m.type!='integer' OR m.value=0
                    OR m.value<-(2147483648) OR m.value>2147483647
              )
            BEGIN SELECT RAISE(ABORT,'task action confirmation evidence is invalid'); END
        """)

    def _migrate_task_action_phase2b_schema(self, conn) -> None:
        """Phase 2b 独立、幂等且 fail-closed 的结构迁移。"""
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        ).fetchone()
        if marker:
            checksum = self._task_action_phase2b_checksum(conn)
            if str(marker[0]) in {
                _TASK_ACTION_PHASE2B_LEGACY_V1_CHECKSUM,
                _TASK_ACTION_PHASE2B_LEGACY_V1_STATUS_GUARD_CHECKSUM,
                _TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM,
            }:
                # 白名单只表示“已知可升级的发布版本”，不表示可以信任
                # 当前数据库对象。先比较冻结指纹，防止 marker 保留但
                # trigger/index 被篡改时被迁移逻辑静默覆盖。
                if checksum != str(marker[0]):
                    raise sqlite3.OperationalError(
                        "task action Phase 2b legacy schema manifest differs "
                        "before guard upgrade"
                    )
                self._upgrade_task_action_phase2b_confirmation_guard(conn)
                self._validate_task_action_phase2b_schema(conn)
                checksum = self._task_action_phase2b_checksum(conn)
                conn.execute(
                    "UPDATE schema_migrations SET checksum=? WHERE version=?",
                    (checksum, _TASK_ACTION_PHASE2B_VERSION),
                )
                return
            self._validate_task_action_phase2b_schema(conn)
            if str(marker[0]) != checksum:
                raise sqlite3.OperationalError(
                    "task action Phase 2b schema manifest checksum differs"
                )
            return
        # Phase 2b 会扩展 Phase 0 的状态触发器，并在完成后刷新 Phase 2a
        # checksum。扩展前必须先验证被冻结的旧合同；否则篡改过的旧标记
        # 会被下面的 UPDATE 静默覆盖，破坏 fail-closed 漂移检测。
        phase2a_marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2A_VERSION,),
        ).fetchone()
        if not phase2a_marker:
            raise sqlite3.OperationalError(
                "task action Phase 2a marker missing before Phase 2b migration"
            )
        self._validate_task_action_phase2a_schema(conn)
        if str(phase2a_marker[0]) != self._task_action_phase2a_checksum(conn):
            raise sqlite3.OperationalError(
                "task action Phase 2a schema manifest checksum differs before Phase 2b"
            )
        receipt_marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2A_RECEIPT_VERSION,),
        ).fetchone()
        if not receipt_marker:
            # 只有 receipt 迁移尚未写入标记时才在这里补装；已有标记的
            # 数据校验留给外层 Phase 2a receipt validator，避免掩盖更
            # 具体的启动诊断。
            self._migrate_task_action_phase2a_receipt_schema(conn)
            receipt_marker = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?",
                (_TASK_ACTION_PHASE2A_RECEIPT_VERSION,),
            ).fetchone()
            if not receipt_marker:
                raise sqlite3.OperationalError(
                    "task action Phase 2a receipt marker missing before Phase 2b migration"
                )
        if str(receipt_marker[0]) != self._task_action_phase2a_receipt_checksum(conn):
            raise sqlite3.OperationalError(
                "task action Phase 2a receipt checksum differs before Phase 2b"
            )
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger') "
                "AND name IN (" + ",".join("?" for _ in (
                    {"task_action_items", "task_action_children", "task_action_confirmations"}
                    | _TASK_ACTION_PHASE2B_INDEXES | _TASK_ACTION_PHASE2B_TRIGGERS
                )) + ")",
                tuple(sorted(
                    {"task_action_items", "task_action_children", "task_action_confirmations"}
                    | _TASK_ACTION_PHASE2B_INDEXES | _TASK_ACTION_PHASE2B_TRIGGERS
                )),
            )
        }
        if existing:
            raise sqlite3.OperationalError(
                "task action Phase 2b schema is unversioned or partially installed"
            )
        # Late evidence can legitimately move an uncertain physical attempt
        # to confirmed.  This is the only Phase 0 trigger whose transition
        # contract is extended; update its Phase 2a marker atomically below.
        conn.execute("DROP TRIGGER trg_task_action_attempt_state_transition")
        conn.execute("""
            CREATE TRIGGER trg_task_action_attempt_state_transition
            BEFORE UPDATE OF state ON task_action_attempts
            WHEN NEW.state!=OLD.state AND NOT (
              (OLD.state='persisted' AND NEW.state IN (
                'outbox_pending','failed','uncertain','cancelled'
              ))
              OR (OLD.state='outbox_pending' AND NEW.state IN (
                'sending','failed','dead','uncertain','cancelled'
              ))
              OR (OLD.state='sending' AND NEW.state IN (
                'outbox_pending','confirmed','uncertain','failed','dead','partial'
              ))
              OR (OLD.state IN ('uncertain','failed','dead','partial')
                  AND NEW.state='confirmed'
                  AND EXISTS (
                    SELECT 1 FROM task_action_children ch
                    WHERE ch.attempt_id=OLD.id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM task_action_children ch
                    WHERE ch.attempt_id=OLD.id
                      AND NOT EXISTS (
                        SELECT 1 FROM task_action_confirmations c
                        WHERE c.attempt_id=OLD.id
                          AND c.ordinal=ch.ordinal
                          AND c.action_id=ch.action_id
                      )
                  ))
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid task action delivery transition');
            END
        """)
        self._install_task_action_phase2b_schema(conn)
        # task.status=done 代表所有逻辑 ordinal 已有正向 confirmation，
        # 不再错误地要求 current generation 的物理 attempt 也必须 confirmed。
        # current_attempt_id 仍保留最新 generation，便于审计且绝不回拨。
        conn.execute("DROP TRIGGER trg_task_action_task_status_guard")
        conn.execute("""
            CREATE TRIGGER trg_task_action_task_status_guard
            BEFORE UPDATE OF status,current_attempt_id ON tasks
            WHEN NEW.current_attempt_id IS NOT NULL AND (
              NEW.status NOT IN ('sending','done','uncertain','failed','partial')
              OR (
                NEW.status!='sending'
                AND EXISTS (
                  SELECT 1 FROM task_action_attempts a
                  JOIN send_outbox o ON o.task_attempt_id=a.id
                  WHERE a.task_id=NEW.id
                    AND o.status IN ('pending','sending')
                )
              )
              OR (
                NEW.status='done' AND NOT (
                  EXISTS (
                    SELECT 1 FROM task_action_attempts a
                    WHERE a.id=NEW.current_attempt_id
                      AND a.task_id=NEW.id AND a.state='confirmed'
                  )
                  OR NOT EXISTS (
                    SELECT 1 FROM task_action_items i
                    WHERE i.task_id=NEW.id
                      AND NOT EXISTS (
                        SELECT 1 FROM task_action_confirmations c
                        WHERE c.task_id=i.task_id AND c.ordinal=i.ordinal
                      )
                  )
                )
              )
              OR (
                NEW.status='uncertain' AND NOT EXISTS (
                  SELECT 1 FROM task_action_attempts a
                  WHERE a.id=NEW.current_attempt_id
                    AND a.task_id=NEW.id AND a.state='uncertain'
                )
              )
              OR (
                NEW.status='failed' AND NOT EXISTS (
                  SELECT 1 FROM task_action_attempts a
                  WHERE a.id=NEW.current_attempt_id
                    AND a.task_id=NEW.id AND a.state IN ('failed','dead')
                )
              )
              OR (
                NEW.status='partial' AND NOT EXISTS (
                  SELECT 1 FROM task_action_attempts a
                  WHERE a.id=NEW.current_attempt_id
                    AND a.task_id=NEW.id AND a.state='partial'
                )
              )
            )
            BEGIN
                SELECT RAISE(ABORT, 'task status disagrees with current attempt');
            END
        """)
        self._backfill_task_action_phase2b(conn)
        self._validate_task_action_phase2b_schema(conn)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (self._task_action_phase2a_checksum(conn), _TASK_ACTION_PHASE2A_VERSION),
        )
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) VALUES (?,?,?)",
            (_TASK_ACTION_PHASE2B_VERSION, self._task_action_phase2b_checksum(conn),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def _install_task_action_phase2a_schema(self, conn) -> None:
        """从已验证的 Phase 0 原子升级；新行为仍由后续 feature gate 控制。"""
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(task_action_attempts)")
        }
        objects = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
            )
        }
        if ("task_action_retry_requests" in tables
                or "retry_request_id" in attempt_columns
                or objects & (_TASK_ACTION_PHASE2A_INDEXES | _TASK_ACTION_PHASE2A_TRIGGERS)):
            raise sqlite3.OperationalError(
                "task action Phase 2a schema is unversioned or partially installed"
            )
        if conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE generation>0"
        ).fetchone()[0]:
            raise sqlite3.OperationalError(
                "task action Phase 2a migration requires audited retry lineage"
            )

        conn.execute("""
            CREATE TABLE task_action_retry_requests (
                request_id TEXT PRIMARY KEY CHECK(length(trim(request_id)) > 0),
                requested_task_id TEXT NOT NULL CHECK(
                    length(trim(requested_task_id)) BETWEEN 1 AND 64
                ),
                requested_attempt_id TEXT NOT NULL CHECK(
                    length(trim(requested_attempt_id)) BETWEEN 1 AND 64
                ),
                task_id INTEGER DEFAULT NULL,
                expected_attempt_id INTEGER DEFAULT NULL,
                expected_generation INTEGER DEFAULT NULL CHECK(
                    expected_generation IS NULL OR expected_generation >= 0
                ),
                new_attempt_id INTEGER DEFAULT NULL UNIQUE,
                new_generation INTEGER DEFAULT NULL CHECK(
                    new_generation IS NULL OR new_generation > 0
                ),
                actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
                verification_result TEXT NOT NULL CHECK(verification_result IN (
                    'NOT_REQUIRED','VERIFIED_NOT_DELIVERED','DELIVERY_UNKNOWN'
                )),
                force_resend_ack INTEGER NOT NULL DEFAULT 0
                    CHECK(force_resend_ack IN (0,1)),
                selected_ordinals_json TEXT NOT NULL DEFAULT '[]' CHECK(
                    json_valid(selected_ordinals_json)
                    AND json_type(selected_ordinals_json,'$') = 'array'
                ),
                skipped_ordinals_json TEXT NOT NULL DEFAULT '[]' CHECK(
                    json_valid(skipped_ordinals_json)
                    AND json_type(skipped_ordinals_json,'$') = 'array'
                ),
                result TEXT NOT NULL CHECK(result IN ('accepted','rejected')),
                reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
                evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(
                    json_valid(evidence_json)
                    AND json_type(evidence_json,'$') = 'object'
                ),
                created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
                CHECK(
                    (result='accepted'
                     AND task_id IS NOT NULL
                     AND expected_attempt_id IS NOT NULL
                     AND expected_generation IS NOT NULL
                     AND requested_task_id=CAST(task_id AS TEXT)
                     AND requested_attempt_id=CAST(expected_attempt_id AS TEXT)
                     AND new_attempt_id IS NOT NULL
                     AND new_generation=expected_generation+1)
                    OR
                    (result='rejected'
                     AND new_attempt_id IS NULL
                     AND new_generation IS NULL
                     AND (
                       (task_id IS NULL AND expected_attempt_id IS NULL
                        AND expected_generation IS NULL)
                       OR
                       (task_id IS NOT NULL AND expected_attempt_id IS NULL
                        AND expected_generation IS NULL
                        AND requested_task_id=CAST(task_id AS TEXT))
                       OR
                       (task_id IS NOT NULL AND expected_attempt_id IS NOT NULL
                        AND expected_generation IS NOT NULL
                        AND requested_task_id=CAST(task_id AS TEXT)
                        AND requested_attempt_id=CAST(expected_attempt_id AS TEXT))
                     ))
                ),
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY(expected_attempt_id) REFERENCES task_action_attempts(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY(new_attempt_id) REFERENCES task_action_attempts(id)
                    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
            )
        """)
        conn.execute(
            "ALTER TABLE task_action_attempts ADD COLUMN retry_request_id TEXT "
            "DEFAULT NULL REFERENCES task_action_retry_requests(request_id) "
            "ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_task_action_attempts_retry_request "
            "ON task_action_attempts(retry_request_id) "
            "WHERE retry_request_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_task_action_retry_requests_accepted_expected "
            "ON task_action_retry_requests(expected_attempt_id) "
            "WHERE result='accepted'"
        )
        conn.execute(
            "CREATE INDEX idx_task_action_retry_requests_task_time "
            "ON task_action_retry_requests(task_id,created_at,request_id)"
        )
        conn.execute("""
            CREATE TRIGGER trg_task_action_attempt_retry_link_guard
            BEFORE INSERT ON task_action_attempts
            WHEN (NEW.generation=0 AND NEW.retry_request_id IS NOT NULL)
              OR (NEW.generation>0
                  AND trim(COALESCE(NEW.retry_request_id,''))='')
              OR (NEW.generation>0 AND EXISTS (
                    SELECT 1 FROM task_action_retry_requests r
                    WHERE r.request_id=NEW.retry_request_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'task retry generation requires request lineage');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_attempt_retry_link_immutable
            BEFORE UPDATE OF retry_request_id ON task_action_attempts
            WHEN NEW.retry_request_id IS NOT OLD.retry_request_id
            BEGIN
                SELECT RAISE(ABORT, 'task retry request link is immutable');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_attempt_retry_business_guard
            BEFORE INSERT ON task_action_attempts
            WHEN NEW.generation>0 AND (
              COALESCE(json_array_length(
                CASE WHEN json_valid(NEW.plan_json)
                     THEN NEW.plan_json ELSE '{"children":[]}' END,
                '$.children'
              ),-1)!=1
              OR COALESCE(json_extract(
                   CASE WHEN json_valid(NEW.plan_json)
                        THEN NEW.plan_json ELSE '{"children":[]}' END,
                   '$.children[0].kind'
                 ),'')!='text'
              OR COALESCE(json_extract(
                   CASE WHEN json_valid(NEW.plan_json)
                        THEN NEW.plan_json ELSE '{"children":[]}' END,
                   '$.children[0].ordinal'
                 ),-1)!=0
              OR NOT EXISTS (
                SELECT 1
                FROM task_action_attempts root
                JOIN task_action_attempts previous
                  ON previous.task_id=root.task_id
                 AND previous.generation=NEW.generation-1
                WHERE root.task_id=NEW.task_id
                  AND root.generation=0
                  AND root.retry_request_id IS NULL
                  AND previous.state IN ('failed','dead')
                  AND previous.accounting_state IN ('none','clean')
                  AND (
                    SELECT COUNT(*) FROM send_outbox prior
                    WHERE prior.task_attempt_id=previous.id
                  )=1
                  AND EXISTS (
                    SELECT 1
                    FROM send_outbox prior
                    JOIN action_receipt_mailbox receipt
                      ON receipt.scope_id=json_extract(
                           previous.plan_json,'$.scope_id'
                         )
                     AND receipt.action_id=prior.domain_action_id
                    WHERE prior.task_attempt_id=previous.id
                      AND prior.status='dead'
                      AND prior.attempts>0
                      AND length(trim(COALESCE(prior.last_error,'')))>0
                      AND prior.ordinal=0
                      AND prior.retry_owner='outbox'
                      AND prior.domain_action_id=json_extract(
                        previous.plan_json,'$.children[0].action_id'
                      )
                      AND prior.message=json_extract(
                        previous.plan_json,'$.children[0].payload.text'
                      )
                      AND receipt.action_status='failed'
                      AND receipt.kind='text'
                      AND receipt.ordinal=0
                      AND receipt.source_id=json_extract(
                        previous.plan_json,'$.source_id'
                      )
                      AND json_extract(
                        CASE WHEN json_valid(receipt.receipt_json)
                             THEN receipt.receipt_json ELSE '{}' END,
                        '$.action_id'
                      )=prior.domain_action_id
                  )
                  AND COALESCE(json_array_length(
                        root.plan_json,'$.children'
                      ),-1)=1
                  AND COALESCE(json_extract(
                        root.plan_json,'$.children[0].kind'
                      ),'')='text'
                  AND COALESCE(json_extract(
                        root.plan_json,'$.children[0].ordinal'
                      ),-1)=0
                  AND json(json_remove(
                        CASE WHEN json_valid(NEW.plan_json)
                             THEN NEW.plan_json ELSE '{"children":[]}' END,
                        '$.plan_id','$.source_id','$.created_at',
                        '$.children[0].action_id',
                        '$.children[0].source_id'
                      ))=json(json_remove(
                        root.plan_json,
                        '$.plan_id','$.source_id','$.created_at',
                        '$.children[0].action_id',
                        '$.children[0].source_id'
                      ))
              )
              OR EXISTS (
                SELECT 1 FROM task_action_attempts historical
                JOIN send_outbox o ON o.task_attempt_id=historical.id
                WHERE historical.task_id=NEW.task_id
                  AND o.status IN ('pending','sending')
              )
              OR EXISTS (
                SELECT 1 FROM task_action_attempts historical
                WHERE historical.task_id=NEW.task_id
                  AND historical.state='confirmed'
              )
              OR EXISTS (
                SELECT 1 FROM task_action_attempts historical
                JOIN send_outbox o ON o.task_attempt_id=historical.id
                WHERE historical.task_id=NEW.task_id
                  AND o.status IN (
                    'confirmed_unaccounted','confirmed_conflict'
                  )
              )
              OR EXISTS (
                SELECT 1 FROM task_action_attempts historical
                JOIN confirmed_action_facts f
                  ON f.source_id=json_extract(
                       historical.plan_json,'$.source_id'
                     )
                WHERE historical.task_id=NEW.task_id
              )
            )
            BEGIN
                SELECT RAISE(ABORT, 'task retry business plan is not a safe clone');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_retry_request_insert_guard
            BEFORE INSERT ON task_action_retry_requests
            WHEN EXISTS (
              SELECT 1 FROM task_action_retry_requests existing
              WHERE existing.request_id=NEW.request_id
            )
            OR (NEW.new_attempt_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM task_action_retry_requests existing
              WHERE existing.new_attempt_id=NEW.new_attempt_id
            ))
            OR (NEW.result='accepted' AND EXISTS (
              SELECT 1 FROM task_action_retry_requests existing
              WHERE existing.expected_attempt_id=NEW.expected_attempt_id
                AND existing.result='accepted'
            ))
            OR (
              NEW.result='accepted' AND (
                NOT EXISTS (
                  SELECT 1 FROM task_action_attempts expected
                  WHERE expected.id=NEW.expected_attempt_id
                    AND expected.task_id=NEW.task_id
                    AND expected.generation=NEW.expected_generation
                )
                OR NEW.verification_result!='NOT_REQUIRED'
                OR NEW.force_resend_ack!=0
                OR NEW.reason_code!='RETRY_QUEUED'
                OR COALESCE(json_array_length(
                     NEW.selected_ordinals_json
                   ),-1)!=1
                OR json_type(NEW.selected_ordinals_json,'$[0]')!='integer'
                OR json_extract(NEW.selected_ordinals_json,'$[0]')!=0
                OR COALESCE(json_array_length(
                     NEW.skipped_ordinals_json
                   ),-1)!=0
                OR NOT EXISTS (
                  SELECT 1
                  FROM task_action_attempts expected
                  JOIN task_action_attempts created
                    ON created.id=NEW.new_attempt_id
                  JOIN tasks t ON t.id=NEW.task_id
                  WHERE expected.id=NEW.expected_attempt_id
                    AND expected.task_id=NEW.task_id
                    AND expected.generation=NEW.expected_generation
                    AND expected.state IN ('failed','dead')
                    AND expected.accounting_state IN ('none','clean')
                    AND (
                      SELECT COUNT(*) FROM send_outbox prior
                      WHERE prior.task_attempt_id=expected.id
                    )=1
                    AND EXISTS (
                      SELECT 1
                      FROM send_outbox prior
                      JOIN action_receipt_mailbox receipt
                        ON receipt.scope_id=json_extract(
                             expected.plan_json,'$.scope_id'
                           )
                       AND receipt.action_id=prior.domain_action_id
                      WHERE prior.task_attempt_id=expected.id
                        AND prior.status='dead'
                        AND prior.attempts>0
                        AND length(trim(COALESCE(prior.last_error,'')))>0
                        AND prior.ordinal=0
                        AND prior.retry_owner='outbox'
                        AND prior.domain_action_id=json_extract(
                          expected.plan_json,'$.children[0].action_id'
                        )
                        AND prior.message=json_extract(
                          expected.plan_json,'$.children[0].payload.text'
                        )
                        AND receipt.action_status='failed'
                        AND receipt.kind='text'
                        AND receipt.ordinal=0
                        AND receipt.source_id=json_extract(
                          expected.plan_json,'$.source_id'
                        )
                        AND json_extract(
                          CASE WHEN json_valid(receipt.receipt_json)
                               THEN receipt.receipt_json ELSE '{}' END,
                          '$.action_id'
                        )=prior.domain_action_id
                    )
                    AND created.task_id=NEW.task_id
                    AND created.generation=NEW.new_generation
                    AND created.retry_request_id=NEW.request_id
                    AND created.state='outbox_pending'
                    AND created.accounting_state='none'
                    AND COALESCE(json_array_length(
                          created.plan_json,'$.children'
                        ),-1)=1
                    AND COALESCE(json_extract(
                          created.plan_json,'$.children[0].ordinal'
                        ),-1)=0
                    AND t.current_attempt_id=created.id
                    AND t.status='sending'
                    AND (
                      SELECT COUNT(*) FROM send_outbox linked
                      WHERE linked.task_attempt_id=created.id
                    )=1
                    AND EXISTS (
                      SELECT 1 FROM send_outbox linked
                      WHERE linked.task_attempt_id=created.id
                        AND linked.status='pending'
                        AND linked.attempts=0
                        AND linked.ordinal=0
                        AND linked.retry_owner='outbox'
                        AND linked.domain_action_id=json_extract(
                          created.plan_json,'$.children[0].action_id'
                        )
                        AND linked.target_type=json_extract(
                          created.plan_json,'$.channel'
                        )
                        AND linked.target_id=json_extract(
                          created.plan_json,'$.target'
                        )
                        AND linked.group_id=CASE
                          WHEN json_extract(created.plan_json,'$.channel')='group'
                          THEN json_extract(created.plan_json,'$.target')
                          ELSE ''
                        END
                        AND linked.message=json_extract(
                          created.plan_json,'$.children[0].payload.text'
                        )
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.action_id'
                        )=linked.domain_action_id
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.kind'
                        )='text'
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.source_id'
                        )=json_extract(created.plan_json,'$.source_id')
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.scope_id'
                        )=json_extract(created.plan_json,'$.scope_id')
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.identity_payload'
                        ) IS json_extract(
                          created.plan_json,'$.children[0].payload'
                        )
                        AND json_extract(
                          CASE WHEN json_valid(linked.receipt_template)
                               THEN linked.receipt_template ELSE '{}' END,
                          '$.conversation_ref'
                        ) IS json_extract(
                          created.plan_json,'$.children[0].conversation_ref'
                        )
                    )
                )
                OR EXISTS (
                  SELECT 1 FROM task_action_attempts historical
                  JOIN send_outbox o ON o.task_attempt_id=historical.id
                  WHERE historical.task_id=NEW.task_id
                    AND historical.id!=NEW.new_attempt_id
                    AND o.status IN ('pending','sending')
                )
                OR EXISTS (
                  SELECT 1 FROM task_action_attempts historical
                  WHERE historical.task_id=NEW.task_id
                    AND historical.id!=NEW.new_attempt_id
                    AND historical.state='confirmed'
                )
                OR EXISTS (
                  SELECT 1 FROM task_action_attempts historical
                  JOIN send_outbox o ON o.task_attempt_id=historical.id
                  WHERE historical.task_id=NEW.task_id
                    AND o.status IN (
                      'confirmed_unaccounted','confirmed_conflict'
                    )
                )
                OR EXISTS (
                  SELECT 1 FROM task_action_attempts historical
                  JOIN confirmed_action_facts f
                    ON f.source_id=json_extract(
                         historical.plan_json,'$.source_id'
                       )
                  WHERE historical.task_id=NEW.task_id
                )
              )
            )
            OR (NEW.result='rejected' AND (
              NEW.reason_code='RETRY_QUEUED'
              OR (NEW.reason_code='NOT_FOUND_OR_NOT_OWNER'
                  AND NEW.task_id IS NOT NULL)
              OR (NEW.reason_code='STALE_ATTEMPT'
                  AND NEW.task_id IS NULL)
              OR (NEW.task_id IS NOT NULL
                  AND NEW.expected_attempt_id IS NULL
                  AND NEW.reason_code!='STALE_ATTEMPT')
              OR (NEW.expected_attempt_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM task_action_attempts expected
                WHERE expected.id=NEW.expected_attempt_id
                  AND expected.task_id=NEW.task_id
                  AND expected.generation=NEW.expected_generation
              ))
              OR EXISTS (
                SELECT 1 FROM task_action_attempts created
                WHERE created.retry_request_id=NEW.request_id
              )
            ))
            BEGIN
                SELECT RAISE(ABORT, 'task retry request is inconsistent');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_retry_request_immutable_update
            BEFORE UPDATE ON task_action_retry_requests
            BEGIN
                SELECT RAISE(ABORT, 'task retry requests are append-only');
            END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_retry_request_immutable_delete
            BEFORE DELETE ON task_action_retry_requests
            BEGIN
                SELECT RAISE(ABORT, 'task retry requests are append-only');
            END
        """)

    # ═══════════════════════════════════════
    # 键值存储（替代 JSON 文件）
    # ═══════════════════════════════════════

    def kv_get(self, key: str) -> str | None:
        """读取键值。不存在返回 None。"""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def record_reply_metric(self, *, target_key: str, target_type: str, reply_len: int,
                            meow: int = 0, bracket_action: int = 0, question_end: int = 0,
                            assistant_tell: int = 0, hard_turn: int = 0,
                            bubble_count: int = 0) -> None:
        """对话质量遥测（2026-08-17，Codex P2 精简版）——只存可计数特征，
        不存正文；target_key 是稳定哈希会话键，不落原始 QQ/群号（终审隐私
        修复）。反馈关联走 feedback_stats 既有链路。

        `bubble_count`（2026-10-14 台账 T22）：**实际发出去的气泡条数**。
        加它的理由：用户点名的三个问题里，「消息密度不合理」此前**完全无法测量**
        ——遥测只记了 `reply_len`（正文长度），而密度恰恰是「同样多的字，发几条」。
        数据本来就有（`ReplyPipeline.last_delivered_parts`），只是没人写下来。
        默认 0 保留旧调用点可用（不破坏既有测试）。
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reply_metrics (target_key, target_type, reply_len, meow, "
                "bracket_action, question_end, assistant_tell, hard_turn, bubble_count) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (target_key, target_type, reply_len, meow, bracket_action, question_end,
                 assistant_tell, hard_turn, bubble_count),
            )
            conn.commit()

    def kv_set(self, key: str, value: str):
        """写入键值（UPSERT）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated) VALUES (?, ?, ?)",
                (key, value, now)
            )
            conn.commit()

    def increment_metric_batch(self, day: str, deltas: dict[str, int]) -> None:
        """在一个 SQLite 事务中累加整批日指标并同步 latest。"""
        if not deltas:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for name, raw_delta in deltas.items():
                delta = int(raw_delta)
                daily_key = f"metric:{day}:{name}"
                row = conn.execute(
                    "SELECT value FROM kv_store WHERE key=?", (daily_key,),
                ).fetchone()
                current = int(row[0]) if row else 0
                new_value = str(current + delta)
                conn.execute(
                    "INSERT OR REPLACE INTO kv_store (key,value,updated) VALUES (?,?,?)",
                    (daily_key, new_value, now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO kv_store (key,value,updated) VALUES (?,?,?)",
                    (f"metric:latest:{name}", new_value, now),
                )
            conn.commit()

    def record_feedback(self, bot_reply: str, user_reaction: str | None,
                        user_qq: str, group_id: str = "",
                        sentiment: str = "neutral", confidence: float = 0.5,
                        reply_ms: int = 0, direction_verified: bool = False):
        """记录一条群友对糖糖回复的反应"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO feedback (bot_reply, user_reaction, user_qq, group_id, "
                "sentiment, confidence, reply_ms, direction_verified, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (bot_reply[:200], (user_reaction or "")[:200], user_qq, group_id,
                 sentiment, confidence, reply_ms, int(direction_verified), now)
            )
            conn.commit()

    def get_feedback_stats(self, days: int = 7,
                           verified_only: bool = False) -> dict:
        """获取最近 N 天的反馈统计"""
        with self._connect() as conn:
            cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d")
            all_total = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE timestamp >= ?", (cutoff,)
            ).fetchone()[0]
            verified_total = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE timestamp >= ? "
                "AND direction_verified=1", (cutoff,)
            ).fetchone()[0]
            verified_sql = " AND direction_verified=1" if verified_only else ""
            total = verified_total if verified_only else all_total
            if not total:
                return {
                    "total": 0, "all_total": all_total,
                    "verified_total": verified_total,
                    "verification_rate": (
                        round(verified_total / all_total, 3) if all_total else 0
                    ),
                    "positive_rate": 0, "negative_rate": 0,
                }
            pos = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE timestamp >= ? "
                "AND sentiment='positive'" + verified_sql, (cutoff,)
            ).fetchone()[0]
            neg = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE timestamp >= ? "
                "AND sentiment='negative'" + verified_sql, (cutoff,)
            ).fetchone()[0]
            return {
                "total": total,
                "all_total": all_total,
                "verified_total": verified_total,
                "verification_rate": round(verified_total / all_total, 3),
                "positive_rate": round(pos / total, 3),
                "negative_rate": round(neg / total, 3),
            }

    def get_recent_feedback_reflections(self, user_qq: str, limit: int = 3,
                                        days: int = 30, after_id: int = 0) -> list[dict]:
        """取当前用户最近的互动原文对，供 LLM 作为有来源的反思材料。"""
        cutoff = (datetime.now() - __import__('datetime').timedelta(
            days=days
        )).strftime("%Y-%m-%d")
        try:
            after_id = max(0, int(after_id))
        except (TypeError, ValueError):
            after_id = 0
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, bot_reply, user_reaction, user_qq, group_id, timestamp"
                " FROM feedback WHERE user_qq=? AND timestamp>=?"
                " AND id>?"
                " AND direction_verified=1"
                " AND TRIM(COALESCE(user_reaction,''))!=''"
                " ORDER BY id DESC LIMIT ?",
                (str(user_qq), cutoff, after_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ═══════════════════════════════════════
    # people 表
    # ═══════════════════════════════════════

    def get_or_create_person(self, qq_id: str, nickname: str = "") -> dict:
        """获取或创建人物档案"""
        nickname = _sanitize_display_name(nickname)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM people WHERE qq_id = ?", (qq_id,)
            ).fetchone()

            if row is None:
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                conn.execute(
                    "INSERT INTO people (qq_id, nickname, first_met, last_chat) VALUES (?, ?, ?, ?)",
                    (qq_id, nickname, now, now)
                )
                conn.commit()
                return {
                    "qq_id": qq_id, "nickname": nickname, "intimacy": 0,
                    "relationship": "stranger", "notes": "",
                    "first_met": now, "last_chat": now, "total_chats": 0,
                    "sex": "", "age": 0, "aliases": [],
                    # 2026-08-16 批 1a：字段读写成对——缺失时关系档案游标永远读 0
                    # （relationship_updated），每轮私聊都重合成
                    "relationship_updated": 0, "last_bonus_date": "",
                    "notes_dirty": 0,
                }

            # 在当前连接内读取外号，避免在持有 people 连接时再次打开
            # get_aliases() 的嵌套连接；SQLite 写锁竞争时两个 busy_timeout
            # 会串行叠加，造成一次人物读取长达约 10 秒。
            alias_rows = conn.execute(
                "SELECT alias FROM aliases WHERE qq_id = ? ORDER BY created_at DESC",
                (qq_id,),
            ).fetchall()
            aliases = [_sanitize_display_name(alias_row[0]) for alias_row in alias_rows]
            # 2026-10-24 P0：**库内昵称为空时用传入值补写**（只补空，不覆盖）。
            #
            # 旧行为是「行已存在就彻底无视传入昵称」——而 17 个创建点里只有群关系图
            # 那一个真的传昵称，于是「谁先被不传昵称的路径创建，谁就永久空白」。
            # 实测 27 人昵称为空，其中一位聊了 1886 次、有 252 字画像，档案里却
            # 连名字都没有；群关系图每天都能拿到真实昵称，传进来又被静默丢弃。
            #
            # 为什么**不覆盖**已有值：`people.nickname` 的语义是「跨群稳定身份」
            # （见 handler._handle_group_message 的注释：people 用 QQ 昵称而非群名片），
            # 而群名片/QQ 昵称会随时改。自动覆盖会让身份跟着群名片漂移。
            # 历史版本由 `memory_revisions` 承载，需要时可回溯。
            stored_nick = _sanitize_display_name(row["nickname"] or "")
            if not stored_nick and nickname:
                try:
                    updated = conn.execute(
                        "UPDATE people SET nickname=? WHERE qq_id=? "
                        "AND COALESCE(nickname,'')=''",
                        (nickname, qq_id),
                    ).rowcount
                    if updated:
                        self._record_revision(
                            conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                            op="update", field="nickname", before="", after=nickname,
                            reason=("昵称补写：档案创建时来源路径未带昵称"
                                    "（只补空，不覆盖已有值）"),
                            actor="sync:person_create",
                        )
                    conn.commit()
                    stored_nick = nickname
                except sqlite3.Error:
                    logger.warning("昵称补写失败（不影响档案读取）: %s", qq_id,
                                   exc_info=True)
            return {
                # 2026-08-15 整体审查安全 I4：读路径同样净化——净化器上线前
                # 入库的旧昵称（含控制字符）读出来仍会流进提示词
                "qq_id": row["qq_id"],
                "nickname": stored_nick or _sanitize_display_name(nickname),
                "intimacy": row["intimacy"], "relationship": row["relationship"],
                "notes": row["notes"] or "", "first_met": row["first_met"],
                "last_chat": row["last_chat"], "total_chats": row["total_chats"],
                "sex": row["sex"] if "sex" in row.keys() else "",
                "age": row["age"] if "age" in row.keys() else 0,
                # 2026-08-16 批 1a：与 _update_relationship_timestamp /
                # _check_daily_bonus 的写入成对（此前缺失 → 关系档案每轮重合成）
                "relationship_updated": row["relationship_updated"] if "relationship_updated" in row.keys() else 0,
                "last_bonus_date": row["last_bonus_date"] if "last_bonus_date" in row.keys() else "",
                # 2026-08-16 批 2：画像脏标——dirty 时禁止注入 notes，重合成后清零
                "notes_dirty": row["notes_dirty"] if "notes_dirty" in row.keys() else 0,
                "notes_trust_level": (
                    row["notes_trust_level"] if "notes_trust_level" in row.keys()
                    else "legacy_unverified"
                ),
                "notes_source_ids": (
                    row["notes_source_ids"] if "notes_source_ids" in row.keys() else ""
                ),
                "aliases": aliases
            }

    def person_exists(self, qq_id: str) -> bool:
        """只查存在性，不创建档案（2026-08-15 交叉上下文接线——
        随机数字不能污染 people 表）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM people WHERE qq_id = ?", (str(qq_id),)
            ).fetchone()
        return row is not None

    def touch_person_nickname(self, qq_id: str, nickname: str, *,
                              actor: str = "sync:message_entry") -> bool:
        """消息入口的昵称同步：**昵称为空才补**，已有值一律不动。

        为什么单独一个方法、而不是让消息入口直接调 `get_or_create_person`：
        入口是**每条消息**都走的高频路径，而 `get_or_create_person` 还要查外号、
        组装十几个字段；这里只做一件事，并用进程内缓存挡掉重复查询（同一个人的
        昵称不变时，从第二条消息起就是纯内存判断）。

        为什么**不创建档案**：随机数字/没接触过的 qq 不该因为一条消息就造出
        people 行（交叉上下文那边早有同一条教训：随机数字会污染 people 表）。
        建档是 `get_or_create_person` 的职责，这里只补「已存在档案的空昵称」。

        返回是否真的写入了。
        """
        qq = str(qq_id or "").strip()
        clean = _sanitize_display_name(nickname or "")
        if not qq or not clean:
            return False
        cache = getattr(self, "_nickname_touch_cache", None)
        if cache is None:
            cache = {}
            self._nickname_touch_cache = cache
        if cache.get(qq) == clean:
            return False
        if len(cache) > 4000:
            cache.clear()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(nickname,'') FROM people WHERE qq_id = ?", (qq,)
                ).fetchone()
                if row is None or str(row[0] or "").strip():
                    # 档案不存在（不在这里创建）或已有昵称（不覆盖）——
                    # 记住这次判断，后续消息不必再查库。
                    cache[qq] = clean
                    return False
                changed = conn.execute(
                    "UPDATE people SET nickname=? WHERE qq_id=? "
                    "AND COALESCE(nickname,'')=''",
                    (clean, qq),
                ).rowcount
                if changed:
                    self._record_revision(
                        conn, entity="people", entity_id=qq, qq_id=qq,
                        op="update", field="nickname", before="", after=clean,
                        reason="昵称补写：消息入口拿到 QQ 昵称，档案里此前为空",
                        actor=actor,
                    )
                conn.commit()
                cache[qq] = clean
                return bool(changed)
        except sqlite3.Error:
            # 昵称同步是补充动作，失败不能影响消息处理本身；但必须留日志，
            # 否则「昵称一直是空」会静默到没人发现。
            cache.pop(qq, None)
            logger.warning("昵称同步失败（消息处理继续）: %s", qq, exc_info=True)
            return False

    def update_person(self, qq_id: str, *, actor: str = "unknown",
                      reason: str = "", **kwargs):
        """更新人物档案。特殊参数：total_chats=True 自增聊天计数。

        2026-10-24：这是 `people` 表的通用写入口，按字段分流留痕——
        **真值字段**（notes / notes_trust_level / notes_source_ids / relationship /
        sex / age / nickname）变了就写流水；**统计字段**（intimacy / total_chats /
        last_chat / last_bonus_date / notes_dirty）不写，理由与 reinforce 相同：
        统计漂移与流程标志不是对「她认识这个人是谁」的更正。
        `notes` 是糖糖对一个人的认知本身，被就地覆盖，必须留旧版。
        """
        allowed = {
            "nickname", "intimacy", "relationship", "notes", "last_chat",
            "total_chats", "sex", "age", "last_bonus_date", "notes_dirty",
            "notes_trust_level", "notes_source_ids",
        }
        truth_fields = {
            "nickname", "relationship", "notes", "sex", "age",
            "notes_trust_level", "notes_source_ids",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if "notes" in updates and "notes_trust_level" not in updates:
            # 直接写画像只用于人工/维护入口；自动合成走 replace_profile_snapshot。
            updates["notes_trust_level"] = "manual"
            updates.setdefault("notes_source_ids", "")
        if not updates:
            return

        set_parts = []
        values = []
        for k, v in updates.items():
            if k == "total_chats":
                set_parts.append("total_chats = total_chats + 1")
            else:
                if k == "nickname" and isinstance(v, str):
                    v = _sanitize_display_name(v)
                    updates[k] = v
                set_parts.append(f"{k} = ?")
                values.append(v)
        values.append(qq_id)

        with self._connect() as conn:
            watched = [k for k in updates if k in truth_fields]
            previous = {}
            if watched:
                row = conn.execute(
                    f"SELECT {', '.join(watched)} FROM people WHERE qq_id=?",
                    (qq_id,),
                ).fetchone()
                if row:
                    previous = dict(zip(watched, row))
            conn.execute(
                f"UPDATE people SET {', '.join(set_parts)} WHERE qq_id = ?",
                values
            )
            for field_name in watched:
                before = _revision_text(previous.get(field_name))
                after = _revision_text(updates.get(field_name))
                if before != after:
                    self._record_revision(
                        conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                        op="update", field=field_name,
                        before=before, after=after,
                        reason=reason, actor=actor,
                    )
            conn.commit()

    def set_intimacy(self, qq_id: str, value: int):
        """直接设置亲密度值（0-100）"""
        value = max(0, min(100, value))
        with self._connect() as conn:
            conn.execute(
                "UPDATE people SET intimacy = ? WHERE qq_id = ?",
                (value, qq_id)
            )
            conn.commit()

    def add_intimacy(self, qq_id: str, amount: int) -> int:
        """增加亲密度（上限100），返回新值"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE people SET intimacy = MIN(100, intimacy + ?) WHERE qq_id = ?",
                (amount, qq_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT intimacy FROM people WHERE qq_id = ?", (qq_id,)
            ).fetchone()
            return row[0] if row else 0

    def find_qq_by_nickname(self, nickname: str, fuzzy: bool = True) -> Optional[str]:
        """通过昵称查找 QQ 号（精确 → 模糊，最近活跃优先）。
        fuzzy=False 只做精确匹配——纠正类破坏性操作用（2026-08-17 事故：
        模糊匹配可能把纠正落到相似昵称的人头上）。
        忽略昵称开头的 @（QQ昵称常带 @ 前缀，与消息里的写法不一致）。
        2026-08-17 Codex 全天审查：精确匹配遇到重名时返回 None——
        破坏性操作不静默任选一人（真实库存在 4 组重复昵称）。"""
        q = (nickname or "").strip().lstrip("@")
        if not q:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id FROM people WHERE TRIM(nickname, '@') = ? LIMIT 2",
                (q,)
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]
            if len(rows) > 1:
                if not fuzzy:
                    return None  # 精确档重名不猜（Codex 全天审查——破坏性操作）
                # 模糊档是查询类用途：重名按最近活跃取一
                row = conn.execute(
                    "SELECT qq_id FROM people WHERE TRIM(nickname, '@') = ? "
                    "ORDER BY last_chat DESC LIMIT 1", (q,)
                ).fetchone()
                if row:
                    return row[0]
            if not fuzzy:
                return None
            # 模糊匹配时优先最近活跃的人，避免同名/相似名误匹配
            row = conn.execute(
                "SELECT qq_id FROM people WHERE TRIM(nickname, '@') LIKE ? ORDER BY last_chat DESC LIMIT 1",
                (f"%{q}%",)
            ).fetchone()
            if row:
                return row[0]
        return None

    def find_qq_by_alias(self, alias: str, fuzzy: bool = True) -> Optional[str]:
        """通过外号查找 QQ 号（精确 → 模糊，新创建的外号优先）。
        fuzzy=False 只做精确匹配——纠正类破坏性操作用。
        2026-08-17 Codex 全天审查：精确匹配遇到重名返回 None（同 nickname）。"""
        with self._connect() as conn:
            if fuzzy:
                row = conn.execute(
                    "SELECT qq_id FROM aliases WHERE alias = ? OR alias LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (alias, f"%{alias}%")
                ).fetchone()
            else:
                rows = conn.execute(
                    "SELECT qq_id FROM aliases WHERE alias = ? LIMIT 2",
                    (alias,)
                ).fetchall()
                if len(rows) == 1:
                    return rows[0][0]
                return None  # 重名或不存在——不猜
            if row:
                return row[0]
        return None

    def list_people_nicknames(self) -> list[tuple[str, str]]:
        """全部人物的 (qq_id, nickname)——私聊昵称提及解析用（2026-08-17）。
        量级 = people 行数（百级），每条私聊消息遍历一次无压力。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, nickname FROM people "
                "WHERE nickname IS NOT NULL AND nickname != ''"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    def find_admin_qqs(self, target_qqs: set) -> list[tuple]:
        """批量查 people 表——单条 IN 查询替代 N+1"""
        if not target_qqs:
            return []
        result = []
        placeholders = ",".join("?" * len(target_qqs))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT qq_id, nickname FROM people WHERE qq_id IN ({placeholders})",
                tuple(target_qqs)
            ).fetchall()
            found = {r[0] for r in rows}
            result.extend(rows)
            # 兜底：没查到的用 QQ 号作为昵称
            for qq in target_qqs:
                if qq not in found:
                    result.append((qq, f"QQ{qq}"))
        return result

    def get_messages_by_date(self, subject_qq: str, date: str, limit: int = 50,
                             include_bot_replies: bool = False, group_id: str | None = None,
                             bot_qq: str = "") -> list[dict]:
        """按日期查询聊天记录——替代 handler 中裸 sqlite3.connect"""
        with self._connect() as conn:
            bot_clause = "" if include_bot_replies else " AND is_bot_reply=0"
            group_clause = " AND group_id=?" if group_id is not None else ""
            if subject_qq and subject_qq != "*":
                rows = conn.execute(
                    "SELECT message, timestamp, group_id, is_bot_reply, qq_id FROM chat_log "
                    "WHERE qq_id=?"
                    f"{bot_clause if not include_bot_replies else ''}{group_clause} AND timestamp LIKE ?"
                    " AND COALESCE(quarantined_at,'')='' "
                    "ORDER BY id ASC LIMIT ?",
                    tuple([subject_qq]
                          + ([group_id] if group_id is not None else []) + [date + "%", limit])
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT qq_id, message, timestamp, group_id, is_bot_reply FROM chat_log "
                    f"WHERE 1=1{bot_clause}{group_clause} AND timestamp LIKE ?"
                    " AND COALESCE(quarantined_at,'')='' "
                    "ORDER BY id ASC LIMIT ?",
                    tuple(([group_id] if group_id is not None else []) + [date + "%", limit])
                ).fetchall()
        if subject_qq and subject_qq != "*":
            return [{"message": r[0], "timestamp": r[1], "group_id": r[2] or "",
                     "qq_id": r[4] or subject_qq, "is_bot_reply": bool(r[3])} for r in rows]
        else:
            return [{"qq_id": r[0], "message": r[1], "timestamp": r[2],
                     "group_id": r[3] or "", "is_bot_reply": bool(r[4])} for r in rows]

    # ═══════════════════════════════════════════════════════════
    # 对话窗口回灌（2026-10-25 第 4 轮）
    # ═══════════════════════════════════════════════════════════
    # `memory.short_term` 是纯内存 deque——bot 一重启，所有对话窗口归零，
    # 她下一句话就「不记得刚才聊到哪」。而 `chat_log` 里这些消息全在。
    # 这几条只读查询就是「从唯一事实源重建窗口」的取数口。
    #
    # **刻意不持久化 buffer**：那等于把同一份数据存两处，两份不一致时没人
    # 知道该信谁。chat_log 本来就是唯一事实源，buffer 只是它的内存投影——
    # 投影可以重算，不必存档。

    def list_recent_chat_scopes(self, *, hours: int = 24, limit: int = 8) -> list[dict]:
        """最近 N 小时有过消息的会话，按最后一条时间倒序。

        返回 `[{"kind": "group"|"private", "id": ..., "last": <ts>}]`。
        私聊在 `chat_log` 里 `group_id` 为空，按 `qq_id`（对话对象）拆开。
        排除 quarantine 行与框架合成行（14k+ 条「【同时有 N 个人找你…】」
        是提示词不是聊天，回灌进来会让她把系统提示当成自己说过的话）。
        """
        limit = max(1, min(int(limit), 32))
        cutoff = (datetime.now() - timedelta(hours=max(1, int(hours)))).strftime(
            "%Y-%m-%d %H:%M:%S")
        common = ("timestamp >= ? AND COALESCE(quarantined_at,'')='' "
                  "AND COALESCE(is_synthetic,0)=0")
        out: list[dict] = []
        with self._connect() as conn:
            for row in conn.execute(
                "SELECT group_id, MAX(timestamp) AS last FROM chat_log "
                f"WHERE COALESCE(group_id,'')!='' AND {common} "
                "GROUP BY group_id ORDER BY last DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall():
                out.append({"kind": "group", "id": str(row[0] or ""), "last": row[1]})
            for row in conn.execute(
                "SELECT qq_id, MAX(timestamp) AS last FROM chat_log "
                f"WHERE COALESCE(group_id,'')='' AND {common} "
                "GROUP BY qq_id ORDER BY last DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall():
                out.append({"kind": "private", "id": str(row[0] or ""), "last": row[1]})
        out.sort(key=lambda x: str(x.get("last") or ""), reverse=True)
        return out[:limit]

    def recent_chat_rows_for_scope(self, *, kind: str, scope_id: str,
                                   limit: int = 30) -> list[dict]:
        """某会话最近 N 条聊天，**旧→新**（回灌 buffer 要按时间序塞 deque）。"""
        limit = max(1, min(int(limit), 200))
        if str(kind) == "group":
            where, params = "COALESCE(group_id,'')=?", (str(scope_id),)
        else:
            where, params = "COALESCE(group_id,'')='' AND qq_id=?", (str(scope_id),)
        sql = ("SELECT qq_id, is_bot_reply, message, timestamp FROM chat_log "
               f"WHERE {where} AND COALESCE(quarantined_at,'')='' "
               "AND COALESCE(is_synthetic,0)=0 ORDER BY id DESC LIMIT ?")
        with self._connect() as conn:
            rows = conn.execute(sql, params + (limit,)).fetchall()
        return [{"qq_id": str(r[0] or ""), "is_bot_reply": bool(r[1]),
                 "message": str(r[2] or ""), "timestamp": str(r[3] or "")}
                for r in reversed(rows)]

    def get_nicknames(self, qq_ids: list[str]) -> dict[str, str]:
        """批量取昵称——回灌的历史里得有人名，否则她会重新掉进「一串数字」
        （第 1 轮那个 `@QQ2693914170` 认不出自己的坑就是这个味道）。"""
        ids = [str(q) for q in (qq_ids or []) if str(q or "").strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        out: dict[str, str] = {}
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    f"SELECT qq_id, nickname FROM people WHERE qq_id IN ({placeholders})",
                    tuple(ids),
                ).fetchall()
            except Exception:
                return {}
        for row in rows:
            nick = str(row[1] or "").strip()
            if nick:
                out[str(row[0])] = nick
        return out

    def query_chat_history(self, *, chat_type: str, chat_id: str,
                           speaker: str = "", from_ts: str = "", to_ts: str = "",
                           order: str = "desc", limit: int = 20) -> list[dict]:
        """严格时间线查询（P0-D2 2026-08-28）：按原始 chat_log 时间戳/id
        过滤与排序（数据层 SQL 保证），不用 LIKE 模糊关键词替代。

        chat_type: group=群（chat_id=群号）/ private=私聊（chat_id=qq）。
        speaker 为空=不限说话人；from/to 为 'YYYY-MM-DD[ HH:MM[:SS]]' 前缀比较
        （chat_log.timestamp 同格式字符串比较即时间序）。order 只允许
        asc/desc（查「第一句」用 asc）。limit 上限 50。quarantine 行排除。
        返回含 source chat_log row id 与 platform message_id（从 event_key 解析，
        无 event_key 时为 0）。
        """
        order = "ASC" if order == "asc" else "DESC"
        limit = max(1, min(int(limit or 20), 50))
        conds: list[str] = ["COALESCE(quarantined_at,'')=''"]
        params: list = []
        if chat_type == "group":
            conds.append("group_id=?")
            params.append(str(chat_id))
        else:
            conds.append("group_id='' AND qq_id=?")
            params.append(str(chat_id))
        if speaker:
            conds.append("qq_id=?")
            params.append(str(speaker))
        if from_ts:
            from_ts = str(from_ts).strip()
            if len(from_ts) == 10:  # 仅日期 → 当天 00:00:00 起（P0-D2 收口）
                from_ts += " 00:00:00"
            conds.append("timestamp>=?")
            params.append(from_ts)
        if to_ts:
            to_ts = str(to_ts).strip()
            if len(to_ts) == 10:  # 仅日期 → 包含整天（23:59:59 止）——字符串
                # 前缀比较会把当天消息排外（"2026-08-28 20:00:00" > "2026-08-28"）
                to_ts += " 23:59:59"
            conds.append("timestamp<=?")
            params.append(to_ts)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, qq_id, group_id, is_bot_reply, message, timestamp, "
                "COALESCE(event_key,'') FROM chat_log "
                f"WHERE {' AND '.join(conds)} "
                f"ORDER BY timestamp {order}, id {order} LIMIT ?",
                params + [limit],
            ).fetchall()
        out = []
        for r in rows:
            mid = 0
            key = r[6]
            if key:
                parts = key.split(":")
                if len(parts) >= 3 and parts[-1].isdigit():
                    mid = int(parts[-1])
            out.append({
                "chat_log_id": r[0],
                "qq_id": r[1],
                "group_id": r[2] or "",
                "is_bot": bool(r[3]),
                "message": r[4],
                "timestamp": r[5],
                "message_id": mid,
            })
        return out

    def has_chat_event(self, event_key: str) -> bool:
        """判断平台入站事件是否已落实为 chat_log 事实行。"""
        event_key = str(event_key or "")
        if not event_key:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM chat_log WHERE event_key=? LIMIT 1",
                (event_key,),
            ).fetchone()
        return row is not None

    def get_recent_chats(self, qq_id: str, limit: int = 5) -> list[str]:
        """获取某人最近聊天记录——替代 handler 中裸 sqlite3.connect"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT clean_text FROM chat_index WHERE qq_id=? ORDER BY chat_id DESC LIMIT ?",
                (qq_id, limit)
            ).fetchall()
        return [r[0] for r in rows] if rows else []

    def get_latest_chat_id(self, qq_id: str) -> int | None:
        """获取某人最近的 chat_log ID——用于私聊消息索引"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(id) FROM chat_log WHERE qq_id=? AND group_id=''", (qq_id,)
            ).fetchone()
        return row[0] if row and row[0] else None

    def search_people(self, query: str, limit: int = 5) -> list[dict]:
        """搜索 people 表——按昵称和 notes 模糊匹配。
        2026-08-16 Codex C2：dirty 画像在数据层投影为空——dirty=已知错误，
        任何下游（含 _search_people 工具）都不该再看到旧画像。"""
        safe_query = query.replace("%", "\\%").replace("_", "\\_")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, nickname, notes, COALESCE(notes_dirty,0) as notes_dirty, "
                "COALESCE(notes_trust_level,'legacy_unverified') as notes_trust_level "
                "FROM people WHERE nickname LIKE ? ESCAPE '\\' OR "
                "(COALESCE(notes_trust_level,'legacy_unverified') IN "
                "('verified','manual','corrected') AND notes LIKE ? ESCAPE '\\') LIMIT ?",
                (f"%{safe_query}%", f"%{safe_query}%", limit)
            ).fetchall()
        return [{"qq_id": r[0], "nickname": r[1] or "(无昵称)",
                 "notes": ("" if r[3] or r[4] not in {"verified", "manual", "corrected"}
                           else (r[2] or "")[:80])} for r in rows]

    def batch_get_embeddings(self, memory_ids: list[int]) -> dict[int, object]:
        """分块读取向量并保留 NumPy buffer，避免转 Python float 列表放大内存。"""
        if not memory_ids:
            return {}
        import numpy as np
        unique_ids = list(dict.fromkeys(int(item) for item in memory_ids))
        result = {}
        with self._connect() as conn:
            for start in range(0, len(unique_ids), 800):
                chunk = unique_ids[start:start + 800]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT memory_id, embedding FROM memory_embeddings "
                    f"WHERE memory_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for mem_id, blob in rows:
                    if blob and len(blob) >= 4:
                        try:
                            arr = np.frombuffer(blob, dtype=np.float32)
                            if len(arr) > 0:
                                result[int(mem_id)] = arr
                        except Exception:
                            pass
        return result

    def find_all_people_sorted(self, limit: int = 200) -> list[tuple]:
        """按亲密度降序获取所有人（qq_id, nickname）"""
        with self._connect() as conn:
            return conn.execute(
                "SELECT qq_id, nickname FROM people ORDER BY intimacy DESC LIMIT ?",
                (limit,)
            ).fetchall()

    def find_qq_for_at(self, nickname: str, bot_qq: str) -> Optional[str]:
        """为 @ 解析查 QQ 号：先 people 表，再 aliases 表"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id FROM people WHERE nickname = ? AND qq_id != ? LIMIT 1",
                (nickname, bot_qq)
            ).fetchone()
            if row:
                return row[0]
            row = conn.execute(
                "SELECT qq_id FROM aliases WHERE alias = ? AND qq_id != ? LIMIT 1",
                (nickname, bot_qq)
            ).fetchone()
            if row:
                return row[0]
        return None

    # ═══ group_members 表 ═══

    def upsert_group_member(self, group_id: str, qq_id: str, **kwargs):
        """插入或更新群成员信息（card/role/title/last_sent）。

        ``last_sent`` 只是群成员排序提示，不要求每条消息都落盘。相同
        元数据在 30 秒内直接跳过写事务，避免高流量群聊为同一成员反复
        ``INSERT ... ON CONFLICT`` 并争用 SQLite 唯一写锁。
        """
        allowed = {"card", "role", "title", "join_time", "last_sent"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        updates["updated"] = now
        columns = ["group_id", "qq_id"] + list(updates.keys())
        values = [group_id, qq_id] + list(updates.values())
        placeholders = ", ".join("?" * len(columns))
        assignments = []
        for column in updates:
            if column == "last_sent":
                assignments.append(
                    "last_sent=MAX(group_members.last_sent, excluded.last_sent)"
                )
            else:
                assignments.append(f"{column}=excluded.{column}")
        set_clause = ", ".join(assignments)
        changed = [
            f"group_members.{column} IS NOT excluded.{column}"
            for column in ("card", "role", "title", "join_time")
            if column in updates
        ]
        if "last_sent" in updates:
            changed.append(
                "excluded.last_sent > group_members.last_sent + 30"
            )
        conflict_clause = (
            f"ON CONFLICT(group_id, qq_id) DO UPDATE SET {set_clause}"
        )
        if changed:
            conflict_clause += " WHERE " + " OR ".join(changed)

        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO group_members ({', '.join(columns)}) VALUES ({placeholders}) "
                f"{conflict_clause}",
                values
            )
            conn.commit()

    def get_group_member(self, group_id: str, qq_id: str) -> dict | None:
        """获取某群某个成员的信息"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM group_members WHERE group_id=? AND qq_id=?", (group_id, qq_id)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def get_group_members(self, group_id: str) -> list[dict]:
        """获取某群所有已知成员"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM group_members WHERE group_id=? ORDER BY last_sent DESC", (group_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_member_groups(self, qq_id: str) -> list[dict]:
        """获取某人在哪些群出现过，以及各群身份"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM group_members WHERE qq_id=?", (qq_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ═══ group_info 表 ═══

    def upsert_group_info(self, group_id: str, group_name: str = "",
                          member_count: int = 0, max_members: int = 0):
        """插入或更新群信息"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO group_info (group_id, group_name, member_count, max_members, updated) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                "group_name=excluded.group_name, member_count=excluded.member_count, "
                "max_members=excluded.max_members, updated=excluded.updated",
                (group_id, group_name, member_count, max_members, now)
            )
            conn.commit()

    def get_group_info(self, group_id: str) -> dict | None:
        """获取某个群的信息"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM group_info WHERE group_id=?", (group_id,)).fetchone()
            return dict(row) if row else None

    def list_group_overview(self) -> list[dict]:
        """所有「有痕迹的群」总览：名字、人数、消息量、最后活跃。

        数据源是**三处的并集**，只看一处都会漏：
          · `chat_log.group_id` —— 真聊过的群（最权威的「她在不在这个群」）
          · `group_info` —— 同步过群资料的群（可能还没聊过）
        配置侧（场景/黑名单）不在这里读——那是 config.yaml 的事，
        由调用方合并，避免这一层偷偷依赖配置格式。

        排序按消息量倒序：管理页面最该先看到的是「她真正在活跃的群」。
        """
        sql = (
            "WITH agg AS ("
            "  SELECT group_id, COUNT(*) AS n, MAX(timestamp) AS last_active "
            "  FROM chat_log WHERE COALESCE(group_id,'')<>'' GROUP BY group_id"
            ") "
            "SELECT g.group_id AS id, "
            "       COALESCE(gi.group_name,'') AS name, "
            "       COALESCE(gi.member_count,0) AS member_count, "
            "       COALESCE(gi.max_members,0) AS max_members, "
            "       COALESCE(agg.n,0) AS messages, "
            "       COALESCE(agg.last_active,'') AS last_active "
            "FROM ("
            "  SELECT DISTINCT group_id FROM chat_log WHERE COALESCE(group_id,'')<>'' "
            "  UNION SELECT group_id FROM group_info WHERE COALESCE(group_id,'')<>''"
            ") g "
            "LEFT JOIN group_info gi ON gi.group_id = g.group_id "
            "LEFT JOIN agg ON agg.group_id = g.group_id "
            "ORDER BY messages DESC, id ASC"
        )
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def get_people_names(self, qq_ids: list[str]) -> dict[str, str]:
        """批量取 QQ → 昵称，给「场景目标」列表显示人名用。

        一次查完而不是逐个查：目标表可能有几十行，N+1 查询在 SQLite 上
        虽然不慢，但没必要。查不到的 qq 不出现在结果里，调用方自己回退成 QQ 号。
        """
        ids = [str(q or "").strip() for q in (qq_ids or [])]
        ids = [q for q in ids if q]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, COALESCE(nickname,'') FROM people "
                "WHERE qq_id IN (%s)" % marks, tuple(ids)
            ).fetchall()
        return {str(r[0]): str(r[1] or "") for r in rows if r[0]}

    # ═══════════════════════════════════════════════════════════
    # 记忆图谱（2026-10-24 P2）
    #
    # 「她脑子里关于大家的认知长什么样」：人 → 语义簇 → 事实，外加记忆与经历。
    # 只读；这里只返回原始行，节点/边的组装交给 console_api（那是展示逻辑）。
    # ═══════════════════════════════════════════════════════════

    def memory_graph_overview(self, limit_people: int = 40) -> list[dict]:
        """全局图谱原料：人 + 各自的簇数/记忆数，按「她记得多少」排序。

        为什么不用相关子查询：`people` 538 行 × `memories` 22459 条，逐行 COUNT
        会退化成 538 次全表扫。改成三个聚合 CTE 各扫一次再 LEFT JOIN。
        """
        limit_people = max(1, min(int(limit_people), 300))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "WITH spoke AS ( SELECT qq_id AS qq, COUNT(*) AS n FROM memories "
                "                WHERE COALESCE(qq_id,'')<>'' GROUP BY qq_id ), "
                "     about AS ( SELECT target_qq AS qq, COUNT(*) AS n FROM memories "
                "                WHERE COALESCE(target_qq,'')<>'' GROUP BY target_qq ), "
                "     clus AS ( SELECT subject_qq AS qq, COUNT(*) AS n FROM fact_clusters "
                "               WHERE COALESCE(subject_qq,'')<>'' GROUP BY subject_qq ) "
                "SELECT p.qq_id AS qq, COALESCE(p.nickname,'') AS nickname, "
                "       COALESCE(p.intimacy,0) AS intimacy, "
                "       COALESCE(clus.n,0) AS clusters, "
                "       COALESCE(spoke.n,0) AS spoke, "
                "       COALESCE(about.n,0) AS about "
                "FROM people p "
                "LEFT JOIN spoke ON spoke.qq = p.qq_id "
                "LEFT JOIN about ON about.qq = p.qq_id "
                "LEFT JOIN clus ON clus.qq = p.qq_id "
                "WHERE COALESCE(clus.n,0) + COALESCE(spoke.n,0) + COALESCE(about.n,0) > 0 "
                "ORDER BY (COALESCE(about.n,0) + COALESCE(clus.n,0) * 3) DESC, "
                "         COALESCE(p.intimacy,0) DESC LIMIT ?",
                (limit_people,),
            ).fetchall()
        return [dict(r) for r in rows]

    def cluster_category_stats(self) -> list[dict]:
        """按「人 × 类别」聚合的簇数——全局图上人连到类别的那条边。

        不返回逐簇行：2538 行拼成节点会把全局图淹掉，而人连的其实是
        「我对他形成了 3 个关于习惯的理解」这种**聚合关系**。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT subject_qq AS qq, COALESCE(category,'') AS category, "
                "       COUNT(*) AS clusters, COALESCE(SUM(fact_count),0) AS facts "
                "FROM fact_clusters WHERE COALESCE(subject_qq,'')<>'' "
                "GROUP BY subject_qq, category"
            ).fetchall()
        return [dict(r) for r in rows]

    def memory_graph_totals(self) -> dict:
        """图谱页顶部的总量统计（一次查完，别在前端拼）。"""
        with self._connect() as conn:
            one = lambda sql: int(conn.execute(sql).fetchone()[0] or 0)  # noqa: E731
            return {
                "people": one("SELECT COUNT(*) FROM people"),
                "people_known": one(
                    "SELECT COUNT(*) FROM people WHERE COALESCE(nickname,'')<>''"),
                "clusters": one("SELECT COUNT(*) FROM fact_clusters"),
                "facts": one("SELECT COUNT(*) FROM cluster_facts"),
                "memories": one("SELECT COUNT(*) FROM memories"),
                "memories_active": one(
                    "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active')='active'"),
                "evidence": one("SELECT COUNT(*) FROM memory_evidence"),
                "episodes": one("SELECT COUNT(*) FROM episodes"),
            }

    def person_memory_history_rows(self, qq_id: str) -> list[dict]:
        """某个人的全部记忆（**含正文**与版本指针），给演化史用。

        和 `person_memory_rows` 的区别：那个只取维度列（做聚合统计），
        这个要 `value` 正文和 `valid_to`——演化史看的是「她对他的理解具体
        改成了什么」，没有正文就没意义。

        `superseded_by` 实测是 `memories.id`（501/501 全部能解析），
        但注意它可以是 **N→1**：画像重合成时，多条旧事实会被同一条新画像取代。
        """
        qq = str(qq_id or "").strip()
        if not qq:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, COALESCE(key,'') AS key, COALESCE(value,'') AS value, "
                "       COALESCE(status,'active') AS status, "
                "       COALESCE(origin,'') AS origin, "
                "       COALESCE(importance,0) AS importance, "
                "       COALESCE(event_time,'') AS event_time, "
                "       COALESCE(ingested_at,'') AS ingested_at, "
                "       COALESCE(valid_to,'') AS valid_to, "
                "       COALESCE(superseded_by,'') AS superseded_by, "
                "       COALESCE(target_qq,'') AS target_qq, "
                "       COALESCE(qq_id,'') AS owner "
                "FROM memories WHERE qq_id=? OR target_qq=? "
                "ORDER BY id ASC", (qq, qq)
            ).fetchall()
        return [dict(r) for r in rows]

    def person_memory_rows(self, qq_id: str) -> list[dict]:
        """某个人的全部记忆的**维度列**（不含正文）。

        给记忆剖面做多维聚合用。为什么一次全捞再在 Python 里 Counter：
        7 个维度各发一条 GROUP BY 要扫同一个小集合 7 遍，而一个人的记忆通常
        只有几十到几百条——全捞出来在内存聚合更快，也更好读。
        """
        qq = str(qq_id or "").strip()
        if not qq:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, COALESCE(key,'') AS key, "
                "       COALESCE(cognitive,'') AS cognitive, "
                "       COALESCE(retention,'') AS retention, "
                "       COALESCE(trust_level,'') AS trust_level, "
                "       COALESCE(status,'') AS status, "
                "       COALESCE(origin,'') AS origin, "
                "       COALESCE(importance,0) AS importance, "
                "       COALESCE(event_time,'') AS event_time, "
                "       COALESCE(ingested_at,'') AS ingested_at, "
                "       COALESCE(superseded_by,'') AS superseded_by, "
                "       COALESCE(target_qq,'') AS target_qq, "
                "       COALESCE(qq_id,'') AS owner, "
                "       COALESCE(source_group_id,'') AS source_group_id "
                "FROM memories WHERE qq_id=? OR target_qq=?", (qq, qq)
            ).fetchall()
        return [dict(r) for r in rows]

    def person_evidence_samples(self, memory_ids: list, limit: int = 60) -> list[dict]:
        """把记忆指回原话：`memory_evidence` → `chat_log`，取证据样本。

        这是整张剖面图里唯一能「验真」的一层——上面所有抽象（簇、事实、画像）
        都只能证明「她认为」，只有这一层能证明「原话是什么」。
        """
        ids = [int(m) for m in (memory_ids or []) if str(m).strip().isdigit()]
        ids = ids[:400]
        if not ids:
            return []
        limit = max(1, min(int(limit), 200))
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT e.memory_id, e.chat_id, COALESCE(c.message,'') AS message, "
                "       COALESCE(c.timestamp,'') AS timestamp, c.is_bot_reply "
                "FROM memory_evidence e LEFT JOIN chat_log c ON c.id = e.chat_id "
                "WHERE e.memory_id IN (%s) "
                "ORDER BY e.memory_id DESC, e.chat_id DESC LIMIT ?" % marks,
                tuple(ids) + (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def memory_capabilities(self) -> dict:
        """记忆系统的**存储能力总览**（全局，不分人）。

        这是「她到底能记住什么」的硬件规格：每层的容量、向量覆盖率、提取流水线
        进度。数字直接查不缓存——剖面页要的就是此刻的真实值。
        """
        with self._connect() as conn:
            def one(sql, p=()):
                try:
                    return int(conn.execute(sql, p).fetchone()[0] or 0)
                except Exception:
                    return 0

            mem_total = one("SELECT COUNT(*) FROM memories")
            mem_active = one(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(status,'active')='active'")
            # 信任门的两侧（2026-10-24 立项研究第三期）：门一直在安静地工作，而
            # 「它挡掉了多少」此前从未出现在任何界面上——查昵称时才偶然发现全库
            # 74% 的记忆被挡在可信召回之外。口径必须与真实召回门一致，所以集合
            # 取自 TRUSTED_TRUST_LEVELS，不另写一份字面量。
            trusted_injectable = one(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(status,'active')='active' "
                f"AND COALESCE(trust_level,'legacy_unverified') IN {_TRUSTED_SQL_IN}")
            memories_with_evidence = one(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(status,'active')='active' "
                "AND TRIM(COALESCE(evidence_ids,''))<>''")
            profiled_total = one(
                "SELECT COUNT(*) FROM people WHERE COALESCE(notes,'')<>''")
            # dirty 的画像同样不注入（active_notes 先看 notes_dirty），所以它也在
            # 「被挡住」一侧——把 dirty 圈进来才对得上注入门的实际行为。
            profiled_injectable = one(
                "SELECT COUNT(*) FROM people WHERE COALESCE(notes,'')<>'' "
                "AND COALESCE(notes_dirty,0)=0 "
                "AND COALESCE(notes_trust_level,'legacy_unverified') "
                f"IN {_TRUSTED_SQL_IN}")
            emb = one("SELECT COUNT(*) FROM memory_embeddings")
            try:
                revision_since = str(conn.execute(
                    "SELECT COALESCE(MIN(created_at),'') FROM memory_revisions"
                ).fetchone()[0] or "")
            except sqlite3.Error:
                revision_since = ""
            return {
                "chat_log": one("SELECT COUNT(*) FROM chat_log"),
                "inbound_events": one("SELECT COUNT(*) FROM inbound_events"),
                "evidence": one("SELECT COUNT(*) FROM memory_evidence"),
                "memories": mem_total,
                "memories_active": mem_active,
                # 可注入 / 被信任门挡住——「她认识但用不上」的那一部分必须可见，
                # 否则没人会为它排优先级（这正是本轮调查暴露的可观测性缺口）。
                "trusted_injectable": trusted_injectable,
                "trusted_blocked": max(0, mem_active - trusted_injectable),
                "trusted_cover": (round(trusted_injectable / mem_active, 4)
                                  if mem_active else 0.0),
                # 证据覆盖：补证据这件事的靶子有多大——没有证据就永远升不了级，
                # 所以这个比例决定了「补一天能涨多少」的上限。
                "memories_with_evidence": memories_with_evidence,
                "evidence_cover": (round(memories_with_evidence / mem_active, 4)
                                   if mem_active else 0.0),
                "memories_episodic": one(
                    "SELECT COUNT(*) FROM memories WHERE cognitive='episodic'"),
                "memories_semantic": one(
                    "SELECT COUNT(*) FROM memories WHERE cognitive='semantic'"),
                "embeddings": emb,
                "embedding_dims": 512,
                "embedding_cover": (round(emb / mem_total, 4) if mem_total else 0.0),
                "embedding_failures": one(
                    "SELECT COUNT(*) FROM memory_embedding_failures"),
                "clusters": one("SELECT COUNT(*) FROM fact_clusters"),
                "facts": one("SELECT COUNT(*) FROM cluster_facts"),
                "people": one("SELECT COUNT(*) FROM people"),
                "people_profiled": profiled_total,
                # 画像的同一条门：有画像 ≠ 能注入（legacy/dirty 一律不注入）
                "people_profiled_injectable": profiled_injectable,
                "people_profiled_blocked": max(0, profiled_total - profiled_injectable),
                "episodes": one("SELECT COUNT(*) FROM episodes"),
                "journal": one("SELECT COUNT(*) FROM tangtang_journal"),
                "digests": one("SELECT COUNT(*) FROM daily_digests"),
                "extraction_jobs": one("SELECT COUNT(*) FROM extraction_jobs"),
                "extraction_cursors": one("SELECT COUNT(*) FROM extraction_cursors"),
                # 变更留痕能力（2026-10-24）：不是容量指标，而是「她的记忆可被追溯
                # 到什么程度」——剖面页要能回答「改过几次、谁改的」，所以给出
                # 总条数 + 最早记录时刻（= 能力的生效起点，不是数据创造时间）。
                "revisions": one("SELECT COUNT(*) FROM memory_revisions"),
                "revision_since": revision_since,
            }

    def person_memory_graph(self, qq_id: str, fact_limit: int = 60,
                            memory_limit: int = 40,
                            episode_limit: int = 20,
                            cluster_limit: int = 40,
                            include_inactive: bool = False) -> dict:
        """单个人的完整认知切片：语义簇 + 簇内事实 + 关于 ta 的记忆 + 共同经历。

        `include_inactive=True` 会一并带回 superseded / retracted 的记忆——
        记忆剖面要它：被推翻的记忆也是她记忆史的一部分，而且不看它就没法解释
        「为什么现在记得的是这个版本」。图谱视图保持 False（图上只该画当前有效的）。
        """
        qq = str(qq_id or "").strip()
        if not qq:
            return {}
        fact_limit = max(1, min(int(fact_limit), 300))
        # 记忆上限放到 1000：记忆剖面要「按维度筛选」，而筛选是前端做的——
        # 只带 40 条的话，按 key=event 筛出来的分布是假的（实测某人 1069 条记忆，
        # 40 条里 event 只有 12 条，而真实是 382 条）。1000 条约 300KB，本地可接受。
        memory_limit = max(1, min(int(memory_limit), 1000))
        episode_limit = max(1, min(int(episode_limit), 100))
        # 簇也要限：实测有人被归纳出 145 个簇，全画出来是一团毛线（力导向
        # O(n²) 在 246 节点上每帧 6 万次计算，还得靠它转几十秒才收敛）。
        # 只取事实最多的前 cluster_limit 个——那才是「她对这个人的主要理解」。
        cluster_limit = max(1, min(int(cluster_limit), 200))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            person = conn.execute(
                "SELECT qq_id, COALESCE(nickname,'') AS nickname, "
                "       COALESCE(intimacy,0) AS intimacy, "
                "       COALESCE(relationship,'') AS relationship, "
                "       COALESCE(relationship_summary,'') AS relationship_summary, "
                "       COALESCE(notes,'') AS notes, "
                "       COALESCE(notes_trust_level,'') AS notes_trust_level, "
                "       COALESCE(first_met,'') AS first_met, "
                "       COALESCE(last_chat,'') AS last_chat, "
                "       COALESCE(total_chats,0) AS total_chats, "
                "       COALESCE(birthday,'') AS birthday, "
                "       COALESCE(sex,'') AS sex, "
                "       COALESCE(age,'') AS age "
                "FROM people WHERE qq_id=?", (qq,)).fetchone()
            clusters = [dict(r) for r in conn.execute(
                "SELECT id, COALESCE(category,'') AS category, "
                "       COALESCE(title,'') AS title, COALESCE(summary,'') AS summary, "
                "       COALESCE(fact_count,0) AS fact_count, "
                "       COALESCE(permanence,'') AS permanence "
                "FROM fact_clusters WHERE subject_qq=? "
                "ORDER BY fact_count DESC, id ASC LIMIT ?", (qq, cluster_limit)).fetchall()]
            facts = [dict(r) for r in conn.execute(
                "SELECT id, cluster_id, COALESCE(fact,'') AS fact, "
                "       COALESCE(confidence,0) AS confidence, "
                "       COALESCE(importance,0) AS importance "
                "FROM cluster_facts WHERE subject_qq=? "
                "  AND COALESCE(status,'active')='active' "
                "ORDER BY importance DESC, confidence DESC, id DESC LIMIT ?",
                (qq, fact_limit)).fetchall()]
            mem_sql = (
                "SELECT id, COALESCE(key,'') AS key, COALESCE(value,'') AS value, "
                "       COALESCE(importance,0) AS importance, "
                "       COALESCE(origin,'') AS origin, "
                "       COALESCE(event_time,'') AS event_time, "
                "       COALESCE(cognitive,'') AS cognitive, "
                "       COALESCE(retention,'') AS retention, "
                "       COALESCE(trust_level,'') AS trust_level, "
                "       COALESCE(status,'') AS status "
                "FROM memories WHERE (qq_id=? OR target_qq=?) "
            )
            if not include_inactive:
                mem_sql += "  AND COALESCE(status,'active')='active' "
            mem_sql += "ORDER BY importance DESC, id DESC LIMIT ?"
            memories = [dict(r) for r in conn.execute(
                mem_sql, (qq, qq, memory_limit)).fetchall()]
            episodes = [dict(r) for r in conn.execute(
                "SELECT id, COALESCE(title,'') AS title, "
                "       COALESCE(summary,'') AS summary, "
                "       COALESCE(time_start,'') AS time_start, "
                "       COALESCE(time_end,'') AS time_end "
                "FROM episodes WHERE qq_id=? ORDER BY id DESC LIMIT ?",
                (qq, episode_limit)).fetchall()]
        return {
            "person": dict(person) if person else {"qq_id": qq},
            "clusters": clusters,
            "facts": facts,
            "memories": memories,
            "episodes": episodes,
        }

    def count_bot_replies_between(self, since: str, until: str) -> int:
        """检查在指定时间窗口内是否有糖糖的回复。用于判断 @ 消息是否已被处理。"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM chat_log "
                    "WHERE is_bot_reply = 1 AND timestamp >= ? AND timestamp <= ?",
                    (since, until)
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    def cleanup_stale_groups(self, current_ids: set[str]) -> int:
        """删除不在当前群列表中的过期群记录。返回删除数量。"""
        if not current_ids:
            return 0
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in current_ids)
            cur = conn.execute(
                f"DELETE FROM group_info WHERE group_id NOT IN ({placeholders})",
                list(current_ids)
            )
            conn.commit()
            return cur.rowcount

    def get_all_groups_info(self) -> list[dict]:
        """获取所有已知群的信息"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM group_info ORDER BY member_count DESC").fetchall()
            return [dict(r) for r in rows]

    def count_people(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM people").fetchone()
            return row[0] if row else 0

    def get_person_intimacy(self, qq_id: str) -> int:
        """只查亲密度值"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT intimacy FROM people WHERE qq_id = ?", (qq_id,)
            ).fetchone()
            return row[0] if row else 0

    # ═══════════════════════════════════════
    # aliases 表
    # ═══════════════════════════════════════

    def add_alias(self, qq_id: str, alias: str, source: str = "auto",
                  memory_id: int = 0):
        """记录外号，自动去重。

        `memory_id`（2026-10-24 P1）＝ 这个外号**依据哪条记忆**。没它的话，
        档案里凭空多出一个称呼，事后没人知道它从哪来、该不该删。
        """
        alias = _sanitize_display_name(alias)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO aliases (alias, qq_id, source, created_at, memory_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (alias, qq_id, source, now, int(memory_id or 0))
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass

    def _self_name_from_text(self, value: str) -> str:
        """从一句记忆正文里抽「自述名字」，抽不到返回空串。

        纯函数、无 IO——被提取流程和回填脚本共用。规则刻意窄（见文件顶部的
        `_SELF_NAME_PATTERNS` 注释）：只认「名字是X / 我叫X / 叫我X / X是我的名字」。
        """
        text = str(value or "").strip()
        if not text or len(text) > 24:
            return ""
        for pattern in _SELF_NAME_PATTERNS:
            match = pattern.match(text)
            if not match:
                continue
            candidate = _sanitize_display_name(match.group(1)).strip()
            if not candidate or candidate in _SELF_NAME_STOPWORDS:
                return ""
            if any(ch in _SELF_NAME_BLOCK_CHARS for ch in candidate):
                return ""
            return candidate
        return ""

    def _promote_self_declared_names(self, conn, qq_id: str) -> list[str]:
        """把「名字是X」这类**自述**归档成自称（aliases），返回新归档的名字。

        三条不能让步的边界（写在这里防误改）：

        1. **绝不写 `people.nickname`**。自述可能是角色扮演——实测那位用户
           自称「米雪儿·李」是《卡拉彼丘》的角色设定，还被旁人点明"其实是
           自称米雪儿的人"。系统自动把它写进身份字段，等于替她认定一个假名字，
           比没有名字更糟。`nickname` 只由 QQ 昵称补写（`touch_person_nickname`）。
        2. **只认「名字/叫」句式，不认「我是X」**——后者绝大多数是身份词。
        3. 只归档 `active` 记忆，每条都带 `memory_id` 证据指针，可回溯、可撤销。

        调用方负责 commit（本方法在调用方的事务里跑）。
        """
        owner = str(qq_id or "").strip()
        if not owner:
            return []
        rows = conn.execute(
            "SELECT id, COALESCE(value,'') FROM memories "
            "WHERE qq_id=? AND COALESCE(status,'active')='active' "
            "AND (value LIKE '%名字%' OR value LIKE '%叫我%' OR value LIKE '%我叫%' "
            "     OR value LIKE '%名字叫%') "
            "ORDER BY id DESC LIMIT 200",
            (owner,),
        ).fetchall()
        if not rows:
            return []
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        found: list[str] = []
        for memory_id, value in rows:
            candidate = self._self_name_from_text(value)
            if not candidate:
                continue
            if conn.execute(
                "SELECT 1 FROM aliases WHERE qq_id=? AND alias=?",
                (owner, candidate),
            ).fetchone():
                continue
            try:
                conn.execute(
                    "INSERT INTO aliases (alias, qq_id, source, created_at, memory_id) "
                    "VALUES (?, ?, 'self_declared', ?, ?)",
                    (candidate, owner, now, int(memory_id)),
                )
            except sqlite3.IntegrityError:
                continue
            self._record_revision(
                conn, entity="people", entity_id=owner, qq_id=owner,
                op="update", field="alias", before="", after=candidate,
                reason=f"自述名字归档（依据记忆 #{int(memory_id)}，未写入 nickname）",
                actor="extract:self_declared_name",
            )
            found.append(candidate)
        return found

    def get_aliases(self, qq_id: str) -> list[str]:
        """获取某人的所有外号（2026-08-15 整体审查安全 I4：读路径同样净化——
        净化器上线前入库的旧外号读出来仍会流进提示词）"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias FROM aliases WHERE qq_id = ? ORDER BY created_at DESC",
                (qq_id,)
            ).fetchall()
        return [_sanitize_display_name(r[0]) for r in rows]

    def get_alias_records(self, qq_id: str) -> list[dict]:
        """带来源与证据的别名记录（P1，2026-10-24）。

        为什么要和 `get_aliases`（只返回字符串列表）分开：档案页必须能区分
        「这个人怎么自称」（source='self_declared'，可能是角色扮演，附证据记忆）
        和「别人怎么叫他/系统怎么记的」（auto/llm）。把两种混在一个字符串列表里，
        展示层就没法告诉主人"这个名字可信到什么程度"。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias, COALESCE(source,''), COALESCE(memory_id,0), "
                "       COALESCE(created_at,'') FROM aliases WHERE qq_id=? "
                "ORDER BY created_at DESC, id DESC",
                (str(qq_id),),
            ).fetchall()
        return [{
            "alias": _sanitize_display_name(row[0]),
            "source": str(row[1] or ""),
            "memory_id": int(row[2] or 0),
            "created_at": str(row[3] or ""),
        } for row in rows]

    # ═══════════════════════════════════════
    # memories 表
    # ═══════════════════════════════════════

    @staticmethod
    def _parse_evidence_ids(evidence_ids: str) -> list[int]:
        """严格解析证据 ID；任何脏 token 都使整组证据失效。"""
        raw = str(evidence_ids or "").strip()
        if not raw:
            return []
        parts = [part.strip() for part in raw.split(",")]
        if any(not part.isdigit() or int(part) <= 0 for part in parts):
            return []
        # 稳定去重，保留原始顺序。
        return list(dict.fromkeys(int(part) for part in parts))

    def _validate_memory_evidence(self, conn, qq_id: str, origin: str,
                                  target_qq: str, source_group_id: str,
                                  evidence_ids: str,
                                  evidence_quote: str = "") -> list[tuple]:
        """校验证据确属记忆主体和来源会话，返回 chat_log 行。

        evidence_quote（P0-D2 2026-08-28，审查 Critical 3）：非空时校验
        记忆 claim 必须被证据原文支持——quote 规范化（剥 CQ/空白）后是
        至少一条证据消息的规范化子串；不支持的 claim 返回 []（拒绝写
        verified）。空串 = 旧行为（只校验来源归属，不校验内容支持）。
        """
        ids = self._parse_evidence_ids(evidence_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, qq_id, group_id, is_bot_reply, timestamp, message FROM chat_log "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        by_id = {int(row[0]): row for row in rows}
        if len(by_id) != len(ids):
            return []

        is_self_memory = str(origin or "") == "self"
        expected_qq = str(target_qq or "") if is_self_memory else str(qq_id or "")
        if not expected_qq:
            return []
        expected_bot = 1 if is_self_memory else 0
        expected_group = str(source_group_id or "")
        ordered = [by_id[evidence_id] for evidence_id in ids]
        for row in ordered:
            if str(row[1] or "") != expected_qq:
                return []
            if int(row[3] or 0) != expected_bot:
                return []
            if str(row[2] or "") != expected_group:
                return []
        if evidence_quote:
            import re as _re_q
            norm_quote = _re_q.sub(r'\[CQ:[^\]]*\]', '', evidence_quote)
            norm_quote = " ".join(norm_quote.split())
            if not norm_quote:
                return []  # 空 quote 无法支持任何 claim
            for row in ordered:
                text = _re_q.sub(r'\[CQ:[^\]]*\]', '', str(row[5] or ""))
                if norm_quote in " ".join(text.split()):
                    return ordered  # 至少一条证据原文支持 quote
            return []  # 无证据原文支持——拒绝
        return ordered

    def validate_self_memory_source(self, target_qq: str,
                                    source_group_id: str,
                                    evidence_ids: str) -> bool:
        """验证自忆证据存在且确实是目标对象收到的糖糖回复。

        ``insert_memory`` 对普通历史导入允许降级为 ``legacy_unverified``；
        自忆没有这个降级空间，因为它会被当作糖糖亲口说过的话注入上下文。
        该只读门禁复用同一套来源归属校验，避免先写入再撤回的崩溃窗口。
        """
        with self._connect() as conn:
            return bool(self._validate_memory_evidence(
                conn, "", "self", str(target_qq or ""),
                str(source_group_id or ""), str(evidence_ids or ""),
            ))

    def validate_self_memory_action_anchor(self, action_id: str,
                                           target_qq: str,
                                           source_group_id: str) -> bool:
        """验证 action_completed 是否有同作用域的 confirmed 动作回执。

        自忆中的“已完成”不能只依赖糖糖自己的文字声明；必须能在不可变的
        ``confirmed_action_facts`` 投影中找到同一对象/群的已确认对话动作，
        且该动作显式标记为可进入自忆。普通文字回复没有这个锚点，因此不会
        被误记为外部动作已经完成。
        """
        action = str(action_id or "").strip()
        target = str(target_qq or "").strip()
        group = str(source_group_id or "")
        if not action or not target:
            return False
        if group:
            channel, transport_target, expected_group = "group", group, group
        else:
            channel, transport_target, expected_group = "private", target, ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM confirmed_action_facts "
                "WHERE domain_action_id=? AND projection_kind='conversation_reply' "
                "AND self_memory_eligible=1 AND channel=? AND target=? "
                "AND conversation_user_id=? AND group_id=?",
                (action, channel, transport_target, target, expected_group),
            ).fetchone()
        return bool(row)

    def insert_memory(self, qq_id: str, key: str, value: str,
                      importance: int = 3, timestamp: str = "",
                      cognitive: str = "semantic", confidence: float = 0.7,
                      origin: str = "extracted", status: str = "active",
                      target_qq: str = "", source_group_id: str = "",
                      evidence_ids: str = "", trust_level: str = "",
                      retention: str = "normal", event_time: str = "",
                      valid_from: str = "", valid_to: str = "",
                      superseded_by: int | None = None,
                      idempotency_key: str = "",
                      evidence_quote: str = "",
                      claim_type: str = "stated") -> int:
        """插入一条记忆，返回自增 id。

        P0-D2 收口（Codex 审查）：evidence_quote/claim_type 为普通提取路径的
        证据校验参数（旧调用零影响，默认空串/stated 保持旧签名行为）——
        quote 缺失或 claim_type=inferred 的 extracted 记忆不得因 evidence_ids
        存在而写 verified（降级 legacy_unverified）。origin='self' 是糖糖
        已发送原话（发送链 confirmed 后才提取），证据确定性由
        source_message_id 保证，不受 quote 门槛约束（有原则的例外，勿改）。
        """
        if not timestamp:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        ingested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        retention = retention if retention in {
            "transient", "normal", "durable", "permanent"
        } else "normal"
        idempotency_key = str(idempotency_key or "").strip()
        with self._connect() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return int(existing[0])

            evidence_rows = self._validate_memory_evidence(
                conn, qq_id, origin, target_qq, source_group_id, evidence_ids,
                evidence_quote=evidence_quote,
            )
            if origin == "manual":
                derived_trust = "manual"
            elif origin == "corrected":
                derived_trust = "corrected"
            elif evidence_rows:
                # P0-D2 收口：普通提取必须 evidence_quote 被原文支持（且
                # claim_type=stated）才 verified；quote 缺失的旧项即使有
                # evidence_ids 也降级 legacy_unverified——来源存在性 ≠ claim
                # 被支持（审查 Critical 3）。origin='self' 为有原则的例外
                # （发送链确认的糖糖原话，见 docstring）。
                if origin == "self" or (evidence_quote and claim_type == "stated"):
                    derived_trust = "verified"
                else:
                    derived_trust = "legacy_unverified"
            else:
                # 外部传入 trust_level 不能越过证据校验自行升级。
                derived_trust = "legacy_unverified"

            derived_event_time = (
                max(str(row[4] or "") for row in evidence_rows)
                if evidence_rows else str(event_time or "")
                if derived_trust in {"manual", "corrected"} else ""
            )
            cur = conn.execute(
                "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
                "cognitive, confidence, origin, status, target_qq, source_group_id, evidence_ids, "
                "trust_level, retention, event_time, ingested_at, valid_from, valid_to, "
                "superseded_by, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (qq_id, key, value, timestamp, importance, cognitive, confidence, origin,
                 status, target_qq, source_group_id, evidence_ids, derived_trust,
                 retention, derived_event_time, ingested_at, valid_from, valid_to,
                 superseded_by, idempotency_key)
            )
            memory_id = int(cur.lastrowid)
            if evidence_rows:
                conn.executemany(
                    "INSERT INTO memory_evidence "
                    "(memory_id, chat_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                    [(memory_id, row[0], ingested_at) for row in evidence_rows],
                )
            conn.commit()
        return memory_id

    @staticmethod
    def _append_task_action_event_conn(
            conn, *, attempt_id: int, event_type: str, reason: str,
            from_state: str = "", to_state: str = "", outbox_id: str = "",
            domain_action_id: str = "", metadata: dict | None = None,
            actor: str = "system") -> None:
        """在调用方事务内追加 task action 审计事件。"""
        conn.execute(
            "INSERT INTO task_action_events "
            "(attempt_id,event_type,from_state,to_state,outbox_id,"
            "domain_action_id,actor,reason,metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                int(attempt_id), str(event_type), str(from_state or ""),
                str(to_state or ""), str(outbox_id or ""),
                str(domain_action_id or ""), str(actor or "system"), str(reason),
                json.dumps(
                    dict(metadata or {}), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    def _settle_linked_task_attempt_conn(
            self, conn, *, attempt_id: int, state: str,
            accounting_state: str | None = None, error: str = "",
            outbox_id: str = "", domain_action_id: str = "",
            event_type: str = "outbox_settled", reason: str = "outbox_settled") -> None:
        """记录一次物理动作结果，再分别归约 attempt 与跨代 task。"""
        row = conn.execute(
            "SELECT a.state,a.accounting_state,a.task_id,t.current_attempt_id,"
            "t.status,a.finalized_at FROM task_action_attempts a "
            "JOIN tasks t ON t.id=a.task_id "
            "WHERE a.id=?",
            (int(attempt_id),),
        ).fetchone()
        if not row:
            raise sqlite3.IntegrityError("linked task attempt is missing")
        (old_state, old_accounting, task_id, current_attempt_id,
         task_status, finalized_at) = row
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = ["last_error=?", "updated_at=?"]
        values: list = [str(error or "")[:500], now]
        if accounting_state is not None:
            # accounting_state 是整个 attempt 的投影状态，不是某一条
            # outbox 的瞬时结果：多 child 中任一 confirmed_unaccounted/
            # 缺少 confirmed fact 都必须继续保留 pending_repair；冲突优先。
            projection_conflict = conn.execute(
                "SELECT 1 FROM send_outbox WHERE task_attempt_id=? "
                "AND status='confirmed_conflict' LIMIT 1",
                (int(attempt_id),),
            ).fetchone()
            projection_pending = conn.execute(
                "SELECT 1 WHERE EXISTS ("
                "  SELECT 1 FROM send_outbox WHERE task_attempt_id=? "
                "  AND status='confirmed_unaccounted'"
                ") OR EXISTS ("
                "  SELECT 1 FROM task_action_confirmations c "
                "  WHERE c.attempt_id=? AND NOT EXISTS ("
                "    SELECT 1 FROM confirmed_action_facts fact "
                "    WHERE fact.domain_action_id=c.action_id "
                "      AND fact.outbox_id=c.outbox_id"
                "  )"
                ")",
                (int(attempt_id), int(attempt_id)),
            ).fetchone()
            if projection_conflict or accounting_state == "conflict":
                accounting_state = "conflict"
            elif projection_pending or accounting_state == "pending_repair":
                accounting_state = "pending_repair"
        if accounting_state is not None:
            updates.append("accounting_state=?")
            values.append(str(accounting_state))
        values.append(int(attempt_id))
        conn.execute(
            f"UPDATE task_action_attempts SET {','.join(updates)} WHERE id=?",
            values,
        )

        new_state = self._reduce_task_action_attempt_conn(
            conn, int(attempt_id), event_type=event_type, reason=reason,
        )
        self._reduce_task_action_task_conn(conn, int(task_id))

        self._append_task_action_event_conn(
            conn, attempt_id=int(attempt_id), event_type=event_type,
            reason=reason, from_state=str(old_state), to_state=str(new_state),
            outbox_id=outbox_id, domain_action_id=domain_action_id,
            metadata={
                "accounting_from": str(old_accounting),
                "accounting_to": str(accounting_state or old_accounting),
                "task_current": int(current_attempt_id or 0) == int(attempt_id),
                "requested_state": str(state),
            },
        )

    def _insert_task_text_action_conn(
            self, conn, *, task, text: str, generation: int,
            expected_current_attempt_id: int | None, now: str) -> dict:
        """在调用方事务内冻结一个新的纯文本 generation。"""
        from .action_contract import (
            ActionEnvelope, ConversationRef, build_action_receipt_template,
            derive_action_id,
        )
        from .action_plan import ActionPlan
        import uuid

        task_id = int(task["id"])
        owner = str(task["owner_qq"])
        group_id = str(task["group_id"] or "")
        channel = "group" if group_id else "private"
        target = group_id or owner
        scope_id = group_id or f"_private_{owner}"
        source_id = f"task:{task_id}:attempt:{int(generation)}"
        payload = {"text": text}
        action_id = derive_action_id(
            source_id=source_id, scope_id=scope_id, kind="text",
            channel=channel, target=target, payload=payload, ordinal=0,
            schema_version=2,
        )
        envelope = ActionEnvelope(
            action_id=action_id, kind="text", channel=channel,
            target=target, payload=payload, schema_version=2,
            source_id=source_id, scope_id=scope_id, ordinal=0,
            conversation_ref=ConversationRef(
                projection_kind="conversation_reply",
                conversation_user_id=owner, group_id=group_id,
                source_chat_id=None, self_memory_eligible=False,
            ),
        )
        plan = ActionPlan.create(
            source_id=source_id, scope_id=scope_id, channel=channel,
            target=target, children=(envelope,), created_at=now,
        )
        plan_json = json.dumps(
            plan.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        receipt_template = build_action_receipt_template(envelope, {
            "requested": text, "text": text, "delivery_kind": "text",
            "mode": "verbatim", "attribution": "none",
        })
        template_json = json.dumps(
            receipt_template, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )

        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,state,accounting_state,"
            "last_error,created_at,updated_at,finalized_at) "
            "VALUES (?,?,?,?,'persisted','none','',?,?,'')",
            (task_id, int(generation), plan.plan_id, plan_json, now, now),
        )
        attempt_id = int(cur.lastrowid)
        current = conn.execute(
            "UPDATE tasks SET current_attempt_id=? WHERE id=? "
            "AND status='sending' AND current_attempt_id IS ?",
            (attempt_id, task_id, expected_current_attempt_id),
        )
        if current.rowcount != 1:
            raise sqlite3.IntegrityError("task attempt ownership changed")

        outbox_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,"
            "status,attempts,next_retry_at,last_error,created_at,updated_at,"
            "domain_action_id,task_attempt_id,ordinal,retry_owner) "
            "VALUES (?,?,?,?,?,?,'pending',0,'','',?,?,?,?,0,'outbox')",
            (
                outbox_id, channel, target, group_id, text, template_json,
                now, now, action_id, attempt_id,
            ),
        )
        # Phase 2b 的 child 必须和 plan/outbox 同一事务物化；否则在
        # persist 与 worker claim 的短窗口内，另一连接重启会误判 coverage
        # 不完整并 fail-closed。
        self._materialize_task_action_attempt_conn(conn, attempt_id)
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending',updated_at=? "
            "WHERE id=? AND state='persisted'",
            (now, attempt_id),
        )
        self._append_task_action_event_conn(
            conn, attempt_id=attempt_id, event_type="outbox_linked",
            reason="text_action_frozen", from_state="persisted",
            to_state="outbox_pending", outbox_id=outbox_id,
            domain_action_id=action_id,
            metadata={"channel": channel, "target": target, "ordinal": 0},
        )
        return {
            "task_id": task_id, "attempt_id": attempt_id,
            "generation": int(generation), "plan_id": plan.plan_id,
            "outbox_id": outbox_id, "domain_action_id": action_id,
            "message": text,
        }

    def persist_task_action_plan(
            self, task_id: int, children: list[dict], *,
            role_id: str = "", library_id: str = "") -> dict:
        """原子冻结一个定时多 child ActionPlan 与 linked outbox。

        ``children`` 是已在执行层准备好的不可变传输快照：每项必须包含
        ``kind``、``payload``、``message``，可选 ``actual``/``review``。
        该方法只落盘，不调用平台；媒体生成、资产解析和模型快照必须在
        调用前完成，避免事务内持有外部服务或把随机解析推迟到重试。
        """
        from .action_contract import (
            ActionEnvelope, ConversationRef, build_action_receipt_template,
            derive_action_id,
        )
        from .action_plan import ActionPlan
        import uuid

        if not isinstance(children, list) or not children or len(children) > 20:
            raise ValueError("task action plan requires 1 to 20 children")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT id,owner_qq,group_id,status,current_attempt_id FROM tasks "
                "WHERE id=?", (int(task_id),),
            ).fetchone()
            if not task or task["status"] != "sending":
                conn.rollback()
                raise ValueError("task is not claimed for sending")
            if task["current_attempt_id"] is not None:
                conn.rollback()
                raise ValueError("task already owns an action attempt")
            generation = int(conn.execute(
                "SELECT COALESCE(MAX(generation),-1)+1 FROM task_action_attempts "
                "WHERE task_id=?", (int(task_id),),
            ).fetchone()[0])
            owner = str(task["owner_qq"] or "")
            group_id = str(task["group_id"] or "")
            channel = "group" if group_id else "private"
            target = group_id or owner
            scope_id = group_id or f"_private_{owner}"
            source_id = f"task:{int(task_id)}:attempt:{generation}"

            envelopes: list[ActionEnvelope] = []
            prepared: list[dict] = []
            for ordinal, spec in enumerate(children):
                if not isinstance(spec, dict):
                    raise ValueError("task action child must be an object")
                unknown = set(spec) - {"kind", "payload", "message", "actual", "review"}
                if unknown:
                    raise ValueError(f"unknown task action child fields: {sorted(unknown)}")
                kind = str(spec.get("kind") or "").strip()
                payload = spec.get("payload")
                message = str(spec.get("message") or "")
                if not isinstance(payload, dict):
                    raise ValueError("task action child payload must be an object")
                if not message.strip():
                    raise ValueError("task action child message is required")
                if kind in {"text", "voice"}:
                    conversation_ref = ConversationRef(
                        projection_kind="conversation_reply",
                        conversation_user_id=owner,
                        group_id=group_id,
                        source_chat_id=None,
                        self_memory_eligible=False,
                    )
                else:
                    conversation_ref = ConversationRef()
                action_id = derive_action_id(
                    source_id=source_id, scope_id=scope_id, kind=kind,
                    channel=channel, target=target, payload=payload,
                    ordinal=ordinal, schema_version=2, identity_version=1,
                )
                envelope = ActionEnvelope(
                    action_id=action_id, kind=kind, channel=channel,
                    target=target, payload=payload, review=bool(spec.get("review", False)),
                    schema_version=2, source_id=source_id, scope_id=scope_id,
                    ordinal=ordinal, identity_version=1,
                    conversation_ref=conversation_ref,
                )
                actual = dict(spec.get("actual") or {})
                if kind == "voice" and not str(actual.get("text") or "").strip():
                    actual["text"] = str(payload.get("text") or "").strip()
                actual.setdefault("requested", dict(payload))
                template = build_action_receipt_template(envelope, actual)
                envelopes.append(envelope)
                prepared.append({
                    "envelope": envelope, "message": message,
                    "template": json.dumps(
                        template, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ),
                })

            plan = ActionPlan.create(
                source_id=source_id, scope_id=scope_id, channel=channel,
                target=target, children=envelopes, created_at=now,
                role_id=str(role_id or ""), library_id=str(library_id or ""),
            )
            plan_json = json.dumps(
                plan.to_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            cur = conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,accounting_state," 
                "last_error,created_at,updated_at,finalized_at) "
                "VALUES (?,?,?,?,'persisted','none','',?,?,'')",
                (int(task_id), generation, plan.plan_id, plan_json, now, now),
            )
            attempt_id = int(cur.lastrowid)
            moved = conn.execute(
                "UPDATE tasks SET current_attempt_id=? WHERE id=? "
                "AND status='sending' AND current_attempt_id IS NULL",
                (attempt_id, int(task_id)),
            )
            if moved.rowcount != 1:
                raise sqlite3.IntegrityError("task attempt ownership changed")

            result_children: list[dict] = []
            for item in prepared:
                envelope = item["envelope"]
                outbox_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO send_outbox "
                    "(action_id,target_type,target_id,group_id,message,receipt_template," 
                    "status,attempts,next_retry_at,last_error,created_at,updated_at," 
                    "domain_action_id,task_attempt_id,ordinal,retry_owner) "
                    "VALUES (?,?,?,?,?,?,'pending',0,'','',?,?,?,?,?,'outbox')",
                    (
                        outbox_id, channel, target, group_id, item["message"],
                        item["template"], now, now, envelope.action_id,
                        attempt_id, envelope.ordinal,
                    ),
                )
                result_children.append({
                    "ordinal": envelope.ordinal,
                    "action_id": envelope.action_id,
                    "outbox_id": outbox_id,
                    "kind": envelope.kind,
                })
            self._materialize_task_action_attempt_conn(conn, attempt_id)
            updated = conn.execute(
                "UPDATE task_action_attempts SET state='outbox_pending',updated_at=? "
                "WHERE id=? AND state='persisted'", (now, attempt_id),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("task action outbox ownership changed")
            for child in result_children:
                self._append_task_action_event_conn(
                    conn, attempt_id=attempt_id, event_type="outbox_linked",
                    reason="media_action_frozen", from_state="persisted",
                    to_state="outbox_pending", outbox_id=child["outbox_id"],
                    domain_action_id=child["action_id"],
                    metadata={"channel": channel, "target": target,
                              "ordinal": child["ordinal"], "kind": child["kind"]},
                )
            conn.commit()
            return {
                "task_id": int(task_id), "attempt_id": attempt_id,
                "generation": generation, "plan_id": plan.plan_id,
                "children": result_children,
            }

    def persist_task_text_action(self, task_id: int, message: str) -> dict:
        """原子冻结 text ActionPlan、attempt 与唯一 outbox；不执行网络调用。"""
        text = str(message or "").strip()
        if not text:
            raise ValueError("task text action requires non-empty message")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT id,owner_qq,group_id,status,current_attempt_id FROM tasks "
                "WHERE id=?", (int(task_id),),
            ).fetchone()
            if not task or task["status"] != "sending":
                conn.rollback()
                raise ValueError("task is not claimed for sending")
            if task["current_attempt_id"] is not None:
                conn.rollback()
                raise ValueError("task already owns an action attempt")
            generation = int(conn.execute(
                "SELECT COALESCE(MAX(generation),-1)+1 "
                "FROM task_action_attempts WHERE task_id=?", (int(task_id),),
            ).fetchone()[0])
            action = self._insert_task_text_action_conn(
                conn, task=task, text=text, generation=generation,
                expected_current_attempt_id=None, now=now,
            )
            conn.commit()
            return action

    @staticmethod
    def _parse_retry_positive_id(value) -> tuple[str, int | None]:
        """返回审计用十进制 token；bool、空值和非正整数不冒充 ID。"""
        if isinstance(value, bool):
            return str(value).lower(), None
        raw = str(value if value is not None else "").strip()
        if not raw or len(raw) > 64:
            return raw, None
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return raw, None
        if parsed <= 0 or parsed > 2 ** 63 - 1:
            return raw, None
        return str(parsed), parsed

    @staticmethod
    def _load_task_retry_text_plan(plan_json: str, *, task_id: int,
                                   generation: int, owner_qq: str,
                                   group_id: str) -> dict:
        """严格恢复已冻结的单文本计划；未知字段也视为合同损坏。"""
        from .action_contract import ActionEnvelope, ConversationRef
        from .action_plan import ActionPlan

        data = json.loads(str(plan_json or ""))
        plan_fields = {
            "plan_id", "source_id", "scope_id", "channel", "target",
            "created_at", "role_id", "library_id", "schema_version", "children",
        }
        child_fields = {
            "action_id", "kind", "channel", "target", "payload", "review",
            "schema_version", "source_id", "scope_id", "ordinal",
            "identity_version", "conversation_ref",
        }
        if not isinstance(data, dict) or set(data) != plan_fields:
            raise ValueError("invalid task action plan fields")
        children = data.get("children")
        if not isinstance(children, list) or len(children) != 1:
            raise ValueError("Phase 2a requires one child")
        child_data = children[0]
        if not isinstance(child_data, dict) or set(child_data) != child_fields:
            raise ValueError("invalid task action child fields")
        payload = child_data.get("payload")
        if (not isinstance(payload, dict) or set(payload) != {"text"}
                or not isinstance(payload.get("text"), str)
                or not payload["text"].strip()):
            raise ValueError("Phase 2a requires one non-empty text payload")
        if (child_data.get("kind") != "text"
                or child_data.get("ordinal") != 0
                or child_data.get("review") is not False
                or child_data.get("schema_version") != 2):
            raise ValueError("unsupported task action child")

        expected_channel = "group" if group_id else "private"
        expected_target = str(group_id or owner_qq)
        expected_scope = str(group_id or f"_private_{owner_qq}")
        expected_source = f"task:{int(task_id)}:attempt:{int(generation)}"
        if (
            data.get("source_id") != expected_source
            or data.get("channel") != expected_channel
            or data.get("target") != expected_target
            or data.get("scope_id") != expected_scope
            or child_data.get("source_id") != expected_source
            or child_data.get("channel") != expected_channel
            or child_data.get("target") != expected_target
            or child_data.get("scope_id") != expected_scope
        ):
            raise ValueError("task action ownership does not match task")

        envelope = ActionEnvelope(
            action_id=child_data["action_id"], kind=child_data["kind"],
            channel=child_data["channel"], target=child_data["target"],
            payload=payload, review=child_data["review"],
            schema_version=child_data["schema_version"],
            source_id=child_data["source_id"], scope_id=child_data["scope_id"],
            ordinal=child_data["ordinal"],
            identity_version=child_data["identity_version"],
            conversation_ref=ConversationRef.from_value(
                child_data["conversation_ref"]
            ),
        )
        plan = ActionPlan.create(
            source_id=data["source_id"], scope_id=data["scope_id"],
            channel=data["channel"], target=data["target"],
            children=(envelope,), created_at=data["created_at"],
            role_id=data["role_id"], library_id=data["library_id"],
            schema_version=data["schema_version"], plan_id=data["plan_id"],
        )
        conversation = envelope.conversation_ref
        if (
            conversation.projection_kind != "conversation_reply"
            or conversation.conversation_user_id != str(owner_qq)
            or conversation.group_id != str(group_id or "")
            or conversation.source_chat_id is not None
            or conversation.self_memory_eligible
        ):
            raise ValueError("task action conversation ownership is invalid")
        return {
            "raw": data, "plan": plan, "child": envelope,
            "text": str(payload["text"]),
        }

    @staticmethod
    def _task_retry_business_json(plan: dict) -> str:
        """跨 generation 只忽略重新派生的身份与创建时间。"""
        business = json.loads(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        for key in ("plan_id", "source_id", "created_at"):
            business.pop(key, None)
        child = business["children"][0]
        child.pop("action_id", None)
        child.pop("source_id", None)
        return json.dumps(
            business, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _insert_task_retry_request_conn(
            conn, *, request_id: str, requested_task_id: str,
            requested_attempt_id: str, actor: str, verification_result: str,
            force_resend_ack: bool, result: str, reason_code: str,
            evidence: dict, created_at: str, task_id: int | None = None,
            expected_attempt_id: int | None = None,
            expected_generation: int | None = None,
            new_attempt_id: int | None = None,
            new_generation: int | None = None) -> None:
        selected = "[0]" if result == "accepted" else "[]"
        conn.execute(
            "INSERT INTO task_action_retry_requests "
            "(request_id,requested_task_id,requested_attempt_id,task_id,"
            "expected_attempt_id,expected_generation,new_attempt_id,new_generation,"
            "actor,verification_result,force_resend_ack,selected_ordinals_json,"
            "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id, requested_task_id, requested_attempt_id, task_id,
                expected_attempt_id, expected_generation, new_attempt_id,
                new_generation, actor, verification_result,
                1 if force_resend_ack else 0, selected, "[]", result,
                reason_code,
                json.dumps(
                    dict(evidence or {}), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at,
            ),
        )

    @staticmethod
    def _task_retry_result(row) -> dict:
        evidence = {}
        try:
            evidence = json.loads(str(row["evidence_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        accepted = str(row["result"]) == "accepted"
        current_attempt_id = (
            row["new_attempt_id"] if accepted
            else evidence.get("current_attempt_id")
        )
        current_generation = (
            row["new_generation"] if accepted
            else evidence.get("current_generation")
        )
        return {
            "ok": accepted,
            "code": str(row["reason_code"]),
            "task_id": int(row["task_id"]) if row["task_id"] is not None else None,
            "current_attempt_id": (
                int(current_attempt_id) if current_attempt_id is not None else None
            ),
            "current_generation": (
                int(current_generation) if current_generation is not None else None
            ),
            "new_attempt_id": (
                int(row["new_attempt_id"])
                if row["new_attempt_id"] is not None else None
            ),
            "new_generation": (
                int(row["new_generation"])
                if row["new_generation"] is not None else None
            ),
            "duplicate_risk": bool(row["force_resend_ack"]),
            "request_id": str(row["request_id"]),
        }

    @staticmethod
    def _task_retry_ephemeral_result(*, code: str, request_id: str) -> dict:
        return {
            "ok": False, "code": code, "task_id": None,
            "current_attempt_id": None, "current_generation": None,
            "new_attempt_id": None, "new_generation": None,
            "duplicate_risk": False, "request_id": request_id,
        }

    def _insert_task_retry_text_action_conn(
            self, conn, *, task, root_plan: dict, prior_outbox,
            expected_attempt_id: int, expected_generation: int,
            request_id: str, actor: str, now: str,
            verification_result: str = "NOT_REQUIRED",
            force_resend_ack: bool = False) -> dict:
        """在已持有 BEGIN IMMEDIATE 的事务中复制冻结计划并创建唯一 outbox。"""
        from .action_contract import (
            ActionEnvelope, build_action_receipt_template, derive_action_id,
        )
        from .action_plan import ActionPlan
        import uuid

        task_id = int(task["id"])
        new_generation = int(expected_generation) + 1
        root = root_plan["plan"]
        root_child = root_plan["child"]
        source_id = f"task:{task_id}:attempt:{new_generation}"
        payload = {"text": root_plan["text"]}
        action_id = derive_action_id(
            source_id=source_id, scope_id=root.scope_id, kind="text",
            channel=root.channel, target=root.target, payload=payload,
            ordinal=0, schema_version=2,
        )
        envelope = ActionEnvelope(
            action_id=action_id, kind="text", channel=root.channel,
            target=root.target, payload=payload, review=root_child.review,
            schema_version=2, source_id=source_id, scope_id=root.scope_id,
            ordinal=0, identity_version=root_child.identity_version,
            conversation_ref=root_child.conversation_ref,
        )
        plan = ActionPlan.create(
            source_id=source_id, scope_id=root.scope_id,
            channel=root.channel, target=root.target, children=(envelope,),
            created_at=now, role_id=root.role_id, library_id=root.library_id,
            schema_version=root.schema_version,
        )
        plan_json = json.dumps(
            plan.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        try:
            prior_template = json.loads(str(prior_outbox["receipt_template"] or ""))
            actual = dict(prior_template["actual"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid prior receipt template") from exc
        if str(actual.get("text") or "") != root_plan["text"]:
            raise ValueError("prior receipt text differs from frozen plan")
        receipt_template = build_action_receipt_template(envelope, actual)
        template_json = json.dumps(
            receipt_template, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )

        moved = conn.execute(
            "UPDATE tasks SET status='sending' WHERE id=? AND owner_qq=? "
            "AND status='failed' AND current_attempt_id=?",
            (task_id, str(task["owner_qq"]), int(expected_attempt_id)),
        )
        if moved.rowcount != 1:
            raise sqlite3.IntegrityError("task retry ownership changed")
        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,retry_request_id,state,"
            "accounting_state,last_error,created_at,updated_at,finalized_at) "
            "VALUES (?,?,?,?,?,'persisted','none','',?,?,'')",
            (
                task_id, new_generation, plan.plan_id, plan_json,
                request_id, now, now,
            ),
        )
        new_attempt_id = int(cur.lastrowid)
        pointer = conn.execute(
            "UPDATE tasks SET current_attempt_id=? WHERE id=? "
            "AND status='sending' AND current_attempt_id=?",
            (new_attempt_id, task_id, int(expected_attempt_id)),
        )
        if pointer.rowcount != 1:
            raise sqlite3.IntegrityError("task retry pointer changed")

        outbox_id = uuid.uuid4().hex
        group_id = str(task["group_id"] or "")
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,"
            "status,attempts,next_retry_at,last_error,created_at,updated_at,"
            "domain_action_id,task_attempt_id,ordinal,retry_owner) "
            "VALUES (?,?,?,?,?,?,'pending',0,'','',?,?,?,?,0,'outbox')",
            (
                outbox_id, plan.channel, plan.target, group_id,
                root_plan["text"], template_json, now, now,
                action_id, new_attempt_id,
            ),
        )
        # 与初代 persist 保持同一事务边界，避免 retry 创建到 worker claim
        # 之间重启时被 coverage validator 判为缺 child。
        self._materialize_task_action_attempt_conn(conn, new_attempt_id)
        updated = conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending',updated_at=? "
            "WHERE id=? AND state='persisted'",
            (now, new_attempt_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("task retry outbox ownership changed")
        event_meta = {
            "protocol": "task-retry-phase2a-v1",
            "request_id": request_id,
            "verification_result": verification_result,
            "force_resend_ack": bool(force_resend_ack),
            "expected_attempt_id": int(expected_attempt_id),
            "expected_generation": int(expected_generation),
            "new_attempt_id": new_attempt_id,
            "new_generation": new_generation,
            "ordinal": 0,
        }
        self._append_task_action_event_conn(
            conn, attempt_id=int(expected_attempt_id),
            event_type="manual_retry_requested", reason="definite_failure",
            from_state=str(task["attempt_state"]),
            to_state=str(task["attempt_state"]),
            outbox_id=str(prior_outbox["action_id"]),
            domain_action_id=str(prior_outbox["domain_action_id"]),
            metadata=event_meta, actor=actor,
        )
        self._append_task_action_event_conn(
            conn, attempt_id=new_attempt_id,
            event_type="manual_retry_created", reason="frozen_plan_cloned",
            from_state="persisted", to_state="outbox_pending",
            outbox_id=outbox_id, domain_action_id=action_id,
            metadata=event_meta, actor=actor,
        )
        return {
            "attempt_id": new_attempt_id, "generation": new_generation,
            "plan_id": plan.plan_id, "outbox_id": outbox_id,
            "domain_action_id": action_id,
        }

    def retry_task_generation(
            self, task_id: int, owner_qq: str, *, expected_attempt_id: int,
            request_id: str, verification_result: str,
            force_resend_ack: bool) -> dict:
        """Phase 2a：仅将有确定失败证据的当前单文本 generation 原样入队。"""
        requested_task_id, parsed_task_id = self._parse_retry_positive_id(task_id)
        requested_attempt_id, parsed_attempt_id = self._parse_retry_positive_id(
            expected_attempt_id
        )
        request_id = str(request_id or "").strip()
        owner_qq = str(owner_qq or "").strip()
        actor = f"qq:{owner_qq}:command" if owner_qq else ""
        verification_result = str(verification_result or "").strip()
        if (
            not request_id or len(request_id) > 128
            or not actor or len(actor) > 256
            or not owner_qq
            or not requested_task_id or len(requested_task_id) > 64
            or not requested_attempt_id or len(requested_attempt_id) > 64
            or (requested_task_id.isdigit() and parsed_task_id is None)
            or (requested_attempt_id.isdigit() and parsed_attempt_id is None)
            or not isinstance(force_resend_ack, bool)
        ):
            return self._task_retry_ephemeral_result(
                code="INVALID_REQUEST", request_id=request_id,
            )
        if verification_result not in {
                "NOT_REQUIRED", "VERIFIED_NOT_DELIVERED", "DELIVERY_UNKNOWN"}:
            return self._task_retry_ephemeral_result(
                code="INVALID_VERIFICATION", request_id=request_id,
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if existing:
                    same_request = (
                        str(existing["requested_task_id"]) == requested_task_id
                        and str(existing["requested_attempt_id"])
                        == requested_attempt_id
                        and str(existing["actor"]) == actor
                        and str(existing["verification_result"])
                        == verification_result
                        and bool(existing["force_resend_ack"])
                        is force_resend_ack
                    )
                    conn.rollback()
                    if same_request:
                        return self._task_retry_result(existing)
                    return self._task_retry_ephemeral_result(
                        code="REQUEST_ID_CONFLICT", request_id=request_id,
                    )

                task = None
                if parsed_task_id is not None:
                    task = conn.execute(
                        "SELECT id,owner_qq,group_id,status,current_attempt_id "
                        "FROM tasks WHERE id=? AND owner_qq=?",
                        (parsed_task_id, owner_qq),
                    ).fetchone()
                if not task:
                    self._insert_task_retry_request_conn(
                        conn, request_id=request_id,
                        requested_task_id=requested_task_id,
                        requested_attempt_id=requested_attempt_id,
                        actor=actor, verification_result=verification_result,
                        force_resend_ack=force_resend_ack, result="rejected",
                        reason_code="NOT_FOUND_OR_NOT_OWNER", evidence={
                            "protocol": "task-retry-phase2a-v1",
                        }, created_at=now,
                    )
                    row = conn.execute(
                        "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    conn.commit()
                    return self._task_retry_result(row)

                attempt = None
                if parsed_attempt_id is not None:
                    attempt = conn.execute(
                        "SELECT * FROM task_action_attempts "
                        "WHERE id=? AND task_id=?",
                        (parsed_attempt_id, int(task["id"])),
                    ).fetchone()
                current_id = task["current_attempt_id"]
                current = conn.execute(
                    "SELECT id,generation FROM task_action_attempts WHERE id=?",
                    (int(current_id),),
                ).fetchone() if current_id is not None else None
                current_evidence = {
                    "protocol": "task-retry-phase2a-v1",
                    "current_attempt_id": int(current["id"]) if current else None,
                    "current_generation": int(current["generation"]) if current else None,
                }
                if not attempt or int(current_id or 0) != int(attempt["id"]):
                    self._insert_task_retry_request_conn(
                        conn, request_id=request_id,
                        requested_task_id=requested_task_id,
                        requested_attempt_id=requested_attempt_id,
                        task_id=int(task["id"]),
                        expected_attempt_id=(int(attempt["id"]) if attempt else None),
                        expected_generation=(
                            int(attempt["generation"]) if attempt else None
                        ),
                        actor=actor, verification_result=verification_result,
                        force_resend_ack=force_resend_ack, result="rejected",
                        reason_code="STALE_ATTEMPT", evidence=current_evidence,
                        created_at=now,
                    )
                    row = conn.execute(
                        "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    conn.commit()
                    return self._task_retry_result(row)

                task = dict(task)
                task["attempt_state"] = str(attempt["state"])
                task["attempt_accounting_state"] = str(attempt["accounting_state"])
                expected_generation = int(attempt["generation"])

                rejection = ""
                # 状态/账务事实优先于请求者声明。这样即使调用方带了
                # 尚未支持的核验模式，也不会掩盖“正在发送”或“已送达”。
                if (str(task["status"]) == "sending"
                      or str(attempt["state"]) in {
                          "persisted", "outbox_pending", "sending",
                      }):
                    rejection = "IN_FLIGHT"
                elif str(attempt["accounting_state"]) not in {"none", "clean"}:
                    rejection = "ACCOUNTING_REPAIR_REQUIRED"
                elif str(attempt["state"]) == "confirmed":
                    rejection = "ALREADY_DELIVERED"
                elif verification_result != "NOT_REQUIRED" or force_resend_ack:
                    rejection = "VERIFICATION_NOT_SUPPORTED"
                elif (str(task["status"]) != "failed"
                      or str(attempt["state"]) not in {"failed", "dead"}):
                    rejection = "UNSUPPORTED_STATE"

                open_count = conn.execute(
                    "SELECT COUNT(*) FROM send_outbox o "
                    "JOIN task_action_attempts a ON a.id=o.task_attempt_id "
                    "WHERE a.task_id=? AND o.status IN ('pending','sending')",
                    (int(task["id"]),),
                ).fetchone()[0]
                if not rejection and int(open_count):
                    rejection = "IN_FLIGHT"
                confirmation_count = conn.execute(
                    "SELECT ("
                    "  SELECT COUNT(*) FROM task_action_attempts a "
                    "  WHERE a.task_id=? AND a.state='confirmed'"
                    ") + ("
                    "  SELECT COUNT(*) FROM send_outbox o "
                    "  JOIN task_action_attempts a ON a.id=o.task_attempt_id "
                    "  WHERE a.task_id=? AND o.status IN "
                    "  ('confirmed_unaccounted','confirmed_conflict')"
                    ") + ("
                    "  SELECT COUNT(*) FROM confirmed_action_facts f "
                    "  JOIN task_action_attempts a "
                    "    ON f.source_id=json_extract(a.plan_json,'$.source_id') "
                    "  WHERE a.task_id=?"
                    ")",
                    (int(task["id"]), int(task["id"]), int(task["id"])),
                ).fetchone()[0]
                if not rejection and int(confirmation_count):
                    rejection = "ALREADY_DELIVERED"

                root_attempt = conn.execute(
                    "SELECT * FROM task_action_attempts "
                    "WHERE task_id=? AND generation=0",
                    (int(task["id"]),),
                ).fetchone()
                max_generation = conn.execute(
                    "SELECT MAX(generation) FROM task_action_attempts WHERE task_id=?",
                    (int(task["id"]),),
                ).fetchone()[0]
                root_plan = current_plan = None
                if (not rejection and root_attempt
                        and int(max_generation) == expected_generation):
                    try:
                        root_plan = self._load_task_retry_text_plan(
                            root_attempt["plan_json"], task_id=int(task["id"]),
                            generation=0, owner_qq=str(task["owner_qq"]),
                            group_id=str(task["group_id"] or ""),
                        )
                        current_plan = self._load_task_retry_text_plan(
                            attempt["plan_json"], task_id=int(task["id"]),
                            generation=expected_generation,
                            owner_qq=str(task["owner_qq"]),
                            group_id=str(task["group_id"] or ""),
                        )
                        if self._task_retry_business_json(
                                root_plan["raw"]) != self._task_retry_business_json(
                                    current_plan["raw"]):
                            raise ValueError("task retry business plan drifted")
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        rejection = "INVALID_FROZEN_PLAN"
                elif not rejection:
                    rejection = "INVARIANT_BROKEN"

                prior_outboxes = conn.execute(
                    "SELECT * FROM send_outbox WHERE task_attempt_id=?",
                    (int(attempt["id"]),),
                ).fetchall()
                prior_outbox = prior_outboxes[0] if len(prior_outboxes) == 1 else None
                failure_proven = False
                if not rejection and prior_outbox is not None and current_plan is not None:
                    receipt = conn.execute(
                        "SELECT action_status,kind,ordinal,source_id,receipt_json "
                        "FROM action_receipt_mailbox WHERE scope_id=? AND action_id=?",
                        (
                            current_plan["plan"].scope_id,
                            str(prior_outbox["domain_action_id"] or ""),
                        ),
                    ).fetchone()
                    try:
                        receipt_json = json.loads(
                            str(receipt["receipt_json"] or "") if receipt else ""
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        receipt_json = {}
                    try:
                        prior_template_json = json.loads(
                            str(prior_outbox["receipt_template"] or "")
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        prior_template_json = {}
                    prior_actual = (
                        prior_template_json.get("actual")
                        if isinstance(prior_template_json, dict) else None
                    )
                    prior_template_proven = bool(
                        isinstance(prior_actual, dict)
                        and isinstance(prior_actual.get("text"), str)
                        and prior_actual["text"] == current_plan["text"]
                    )
                    failure_proven = bool(
                        str(prior_outbox["status"]) == "dead"
                        and int(prior_outbox["attempts"] or 0) > 0
                        and bool(str(prior_outbox["last_error"] or "").strip())
                        and int(prior_outbox["ordinal"]) == 0
                        and str(prior_outbox["retry_owner"]) == "outbox"
                        and str(prior_outbox["domain_action_id"])
                        == current_plan["child"].action_id
                        and str(prior_outbox["message"]) == current_plan["text"]
                        and prior_template_proven
                        and receipt is not None
                        and str(receipt["action_status"]) == "failed"
                        and str(receipt["kind"]) == "text"
                        and int(receipt["ordinal"]) == 0
                        and str(receipt["source_id"])
                        == current_plan["plan"].source_id
                        and str(receipt_json.get("action_id") or "")
                        == current_plan["child"].action_id
                    )
                if not rejection and not failure_proven:
                    rejection = "INVARIANT_BROKEN"

                if rejection:
                    evidence = dict(current_evidence)
                    evidence.update({
                        "attempt_state": str(attempt["state"]),
                        "accounting_state": str(attempt["accounting_state"]),
                    })
                    self._insert_task_retry_request_conn(
                        conn, request_id=request_id,
                        requested_task_id=requested_task_id,
                        requested_attempt_id=requested_attempt_id,
                        task_id=int(task["id"]),
                        expected_attempt_id=int(attempt["id"]),
                        expected_generation=expected_generation,
                        actor=actor, verification_result=verification_result,
                        force_resend_ack=force_resend_ack, result="rejected",
                        reason_code=rejection, evidence=evidence, created_at=now,
                    )
                    row = conn.execute(
                        "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    conn.commit()
                    return self._task_retry_result(row)

                try:
                    created = self._insert_task_retry_text_action_conn(
                        conn, task=task, root_plan=root_plan,
                        prior_outbox=prior_outbox,
                        expected_attempt_id=int(attempt["id"]),
                        expected_generation=expected_generation,
                        request_id=request_id, actor=actor, now=now,
                        verification_result=verification_result,
                        force_resend_ack=force_resend_ack,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    # 任何残余的冻结回执形状错误都必须落成可审计拒绝，
                    # 不能把内部 ValueError 泄漏到命令层或让请求无记录。
                    logger.warning(
                        "task retry receipt invariant failed: request_id=%s error=%s",
                        request_id, type(exc).__name__,
                    )
                    rejection = "INVARIANT_BROKEN"
                    evidence = dict(current_evidence)
                    evidence.update({
                        "attempt_state": str(attempt["state"]),
                        "accounting_state": str(attempt["accounting_state"]),
                        "receipt_validation_error": type(exc).__name__,
                    })
                    self._insert_task_retry_request_conn(
                        conn, request_id=request_id,
                        requested_task_id=requested_task_id,
                        requested_attempt_id=requested_attempt_id,
                        task_id=int(task["id"]),
                        expected_attempt_id=int(attempt["id"]),
                        expected_generation=expected_generation,
                        actor=actor, verification_result=verification_result,
                        force_resend_ack=force_resend_ack, result="rejected",
                        reason_code=rejection, evidence=evidence, created_at=now,
                    )
                    row = conn.execute(
                        "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    conn.commit()
                    return self._task_retry_result(row)
                business_hash = hashlib.sha256(
                    self._task_retry_business_json(root_plan["raw"]).encode("utf-8")
                ).hexdigest()
                evidence = {
                    "protocol": "task-retry-phase2a-v1",
                    "current_attempt_id": created["attempt_id"],
                    "current_generation": created["generation"],
                    "previous_outbox_id": str(prior_outbox["action_id"]),
                    "previous_delivery_state": "dead",
                    "previous_receipt_state": "failed",
                    "business_sha256": business_hash,
                }
                self._insert_task_retry_request_conn(
                    conn, request_id=request_id,
                    requested_task_id=requested_task_id,
                    requested_attempt_id=requested_attempt_id,
                    task_id=int(task["id"]),
                    expected_attempt_id=int(attempt["id"]),
                    expected_generation=expected_generation,
                    new_attempt_id=created["attempt_id"],
                    new_generation=created["generation"], actor=actor,
                    verification_result=verification_result,
                    force_resend_ack=force_resend_ack, result="accepted",
                    reason_code="RETRY_QUEUED", evidence=evidence,
                    created_at=now,
                )
                row = conn.execute(
                    "SELECT * FROM task_action_retry_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                conn.commit()
                return self._task_retry_result(row)
            except Exception:
                conn.rollback()
                raise

    def enqueue_send_outbox(self, target_type: str, target_id: str, message: str,
                            group_id: str = "", error: str = "",
                            receipt_template: dict | None = None) -> str:
        """持久化可重试发送；v2 domain action 在队列和永久账本内幂等。"""
        if target_type not in {"group", "private"}:
            raise ValueError(f"unsupported send target type: {target_type}")
        template_json = ""
        domain_action_id = ""
        if receipt_template:
            from .action_contract import finalize_action_receipt_template
            validated = finalize_action_receipt_template(
                receipt_template, status="failed",
            )
            # transport 目标是物理发送边界，必须与冻结的 receipt 合同一致；
            # 否则消息可能发到 A，永久事实却归账到 B。
            if (validated["channel"] != target_type
                    or str(validated["target"]) != str(target_id)):
                raise ValueError(
                    "outbox transport target conflicts with receipt contract"
                )
            persisted_template = {
                key: validated[key] for key in (
                    "schema_version", "action_id", "kind", "channel", "target",
                    "source_id", "scope_id", "ordinal", "actual",
                )
            }
            if int(validated.get("schema_version", 1)) >= 2:
                persisted_template["identity_version"] = validated["identity_version"]
                persisted_template["identity_payload"] = dict(
                    validated["identity_payload"]
                )
                persisted_template["conversation_ref"] = validated["conversation_ref"]
                domain_action_id = str(validated["action_id"])
            template_json = json.dumps(
                persisted_template, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
        import uuid
        outbox_id = uuid.uuid4().hex
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if domain_action_id:
                if conn.execute(
                    "SELECT 1 FROM confirmed_action_facts WHERE domain_action_id=?",
                    (domain_action_id,),
                ).fetchone():
                    conn.rollback()
                    raise ValueError(
                        f"domain action already confirmed: {domain_action_id}"
                    )
                existing = conn.execute(
                    "SELECT action_id,target_type,target_id,group_id,message,"
                    "receipt_template FROM send_outbox WHERE domain_action_id=?",
                    (domain_action_id,),
                ).fetchone()
                if existing:
                    same = tuple(existing[1:]) == (
                        target_type, str(target_id), str(group_id or ""),
                        str(message), template_json,
                    )
                    conn.rollback()
                    if same:
                        return str(existing[0])
                    raise ValueError(
                        f"domain action conflict in outbox: {domain_action_id}"
                    )
            conn.execute(
                "INSERT INTO send_outbox "
                "(action_id,target_type,target_id,group_id,message,receipt_template,"
                "status,attempts,next_retry_at,last_error,created_at,updated_at,"
                "domain_action_id) "
                "VALUES (?,?,?,?,?,?,'pending',0,?,?,?,?,?)",
                (outbox_id, target_type, str(target_id), str(group_id or ""),
                 str(message), template_json, "", str(error or "")[:500], now, now,
                 domain_action_id),
            )
            conn.commit()

        return outbox_id

    def get_send_outbox(self, action_id: str) -> dict | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM send_outbox WHERE action_id=?", (str(action_id),)
            ).fetchone()
        return dict(row) if row else None

    def recover_send_outbox_after_restart(self) -> int:
        """应用启动时调用一次；在途发送结果未知，转人工可见而不重放。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT action_id,receipt_template,task_attempt_id,domain_action_id "
                "FROM send_outbox "
                "WHERE status='sending'"
            ).fetchall()
            conn.execute(
                "UPDATE send_outbox SET status='uncertain', "
                "last_error=CASE WHEN last_error='' "
                "THEN 'process restarted while sending' ELSE last_error END, "
                "updated_at=datetime('now','localtime') WHERE status='sending'"
            )
            if rows:
                from .action_contract import finalize_action_receipt_template
                for action_id, template_json, attempt_id, domain_action_id in rows:
                    if template_json:
                        try:
                            receipt = finalize_action_receipt_template(
                                json.loads(template_json), status="uncertain",
                                error_code="PROCESS_RESTART_UNCERTAIN",
                            )
                            self._enqueue_action_receipt_conn(conn, receipt)
                        except (
                            TypeError, ValueError, json.JSONDecodeError,
                            sqlite3.IntegrityError,
                        ) as exc:
                            conn.execute(
                                "UPDATE send_outbox SET last_error=? WHERE action_id=?",
                                ("RECEIPT_TEMPLATE_INVALID", str(action_id)),
                            )
                            logger.error(
                                "outbox 回执模板损坏，已隔离: action_id=%s error=%s",
                                action_id, type(exc).__name__,
                            )
                    if attempt_id is not None:
                        self._settle_linked_task_attempt_conn(
                            conn, attempt_id=int(attempt_id), state="uncertain",
                            error="PROCESS_RESTART_UNCERTAIN",
                            outbox_id=str(action_id),
                            domain_action_id=str(domain_action_id or ""),
                            event_type="restart_quarantined",
                            reason="process_restarted_while_sending",
                        )
            conn.commit()
        return len(rows)

    def list_due_send_outbox(self, limit: int = 20) -> list[dict]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM send_outbox WHERE status='pending' "
                "AND (next_retry_at='' OR next_retry_at<=?) "
                "ORDER BY created_at,action_id LIMIT ?",
                (now, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_send_outbox(self, action_id: str) -> dict | None:
        """原子领取待发送动作；同一动作同一时刻只能有一个发送者。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT * FROM send_outbox WHERE action_id=? AND status='pending' "
                "AND (next_retry_at='' OR next_retry_at<=?)",
                (str(action_id), now),
            ).fetchone()
            if not candidate:
                conn.rollback()
                return None
            attempt_id = candidate["task_attempt_id"]
            attempt_old_state = ""
            if attempt_id is not None:
                attempt_row = conn.execute(
                    "SELECT a.state,t.current_attempt_id,t.status FROM task_action_attempts a "
                    "JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                    (int(attempt_id),),
                ).fetchone()
                if (not attempt_row or int(attempt_row[1] or 0) != int(attempt_id)
                        or str(attempt_row[2]) != "sending"
                        or str(attempt_row[0]) not in {"outbox_pending", "sending"}):
                    conn.rollback()
                    return None
                attempt_old_state = str(attempt_row[0])
                if attempt_old_state == "outbox_pending":
                    conn.execute(
                        "UPDATE task_action_attempts SET state='sending',updated_at=? WHERE id=?",
                        (now, int(attempt_id)),
                    )
            cur = conn.execute(
                "UPDATE send_outbox SET status='sending',attempts=attempts+1,"
                "updated_at=? WHERE action_id=? AND status='pending' "
                "AND (next_retry_at='' OR next_retry_at<=?)",
                (now, str(action_id), now),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            if attempt_id is not None:
                # child.state 与 send_outbox.status 是同一持久状态机的两面；
                # 必须在同一事务内推进，避免进程正好停在 claim 后时，下一次
                # Store() 启动校验先看到 pending/sending 漂移而拒绝启动，
                # 来不及把该 child quarantine 为 uncertain。
                conn.execute(
                    "UPDATE task_action_children SET state='sending',updated_at=? "
                    "WHERE attempt_id=? AND ordinal=? AND state='pending'",
                    (now, int(attempt_id), int(candidate["ordinal"])),
                )
            row = conn.execute(
                "SELECT * FROM send_outbox WHERE action_id=?", (str(action_id),)
            ).fetchone()
            if attempt_id is not None:
                self._append_task_action_event_conn(
                    conn, attempt_id=int(attempt_id), event_type="outbox_claimed",
                    reason="outbox_worker_claimed", from_state=attempt_old_state,
                    to_state="sending", outbox_id=str(action_id),
                    domain_action_id=str(row["domain_action_id"] or ""),
                    metadata={"attempts": int(row["attempts"])},
                )
            conn.commit()
        return dict(row)

    def complete_send_outbox(self, action_id: str) -> bool:
        """legacy 确认发送后删正文；linked 行必须走 settle 原子结算。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM send_outbox WHERE action_id=? AND status='sending' "
                "AND task_attempt_id IS NULL",
                (str(action_id),),
            )
            conn.commit()
        return cur.rowcount == 1

    def mark_send_outbox_uncertain(self, action_id: str, error: str) -> bool:
        job = self.get_send_outbox(action_id)
        if job and job.get("task_attempt_id") is not None:
            return self.settle_send_outbox(
                action_id, "uncertain", error_code=str(error or "")[:128],
                error_detail=error,
            ) == "uncertain"
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE send_outbox SET status='uncertain',last_error=?,"
                "updated_at=datetime('now','localtime') "
                "WHERE action_id=? AND status='sending'",
                (str(error or "unconfirmed delivery")[:500], str(action_id)),
            )
            conn.commit()
        return cur.rowcount == 1

    def fail_send_outbox(self, action_id: str, error: str, max_attempts: int = 3,
                         retry_delay_seconds: int = 0) -> bool:
        """有限重试；达到上限进入 dead letter，不形成无限热循环。"""
        job = self.get_send_outbox(action_id)
        if job and job.get("task_attempt_id") is not None:
            return self.settle_send_outbox(
                action_id, "failed", error_code=str(error or "")[:128],
                error_detail=error, max_attempts=max_attempts,
                retry_delay_seconds=retry_delay_seconds,
            ) in {"pending", "dead"}
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_retry = (now_dt + timedelta(
            seconds=max(0, int(retry_delay_seconds))
        )).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE send_outbox SET "
                "status=CASE WHEN attempts>=? THEN 'dead' ELSE 'pending' END,"
                "next_retry_at=?,last_error=?,updated_at=? "
                "WHERE action_id=? AND status='sending'",
                (max(1, int(max_attempts)), next_retry, str(error or "")[:500],
                 now, str(action_id)),
            )
            conn.commit()
        return cur.rowcount == 1

    def get_send_outbox_health(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) FROM send_outbox GROUP BY status"
            ).fetchall()
            stale_sending = int(conn.execute(
                "SELECT COUNT(*) FROM send_outbox WHERE status='sending' AND "
                "COALESCE(NULLIF(updated_at,''),created_at) <= "
                "datetime('now','localtime','-5 minutes')"
            ).fetchone()[0] or 0)
            phase2b_schema_exists = all(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                for table in (
                    "task_action_items", "task_action_children",
                    "task_action_confirmations",
                )
            )
            linked_invariant_violations = (
                int(conn.execute(_TASK_ACTION_INVARIANT_COUNT_SQL).fetchone()[0] or 0)
                if phase2b_schema_exists else 0
            )
            confirmed_fact_integrity_violations = (
                self._confirmed_fact_integrity_count_conn(conn)
            )
            bare_sending_tasks = int(conn.execute(
                _TASK_BARE_SENDING_COUNT_SQL
            ).fetchone()[0] or 0)
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {
            **counts,
            "open": sum(counts.get(status, 0) for status in ("pending", "sending")),
            "stale_sending": stale_sending,
            "linked_invariant_violations": linked_invariant_violations,
            "confirmed_fact_integrity_violations": confirmed_fact_integrity_violations,
            "bare_sending_tasks": bare_sending_tasks,
            "task_action_schema_missing": False,
            "needs_review": sum(counts.get(status, 0) for status in (
                "uncertain", "dead", "confirmed_unaccounted", "confirmed_conflict",
            )) + stale_sending + linked_invariant_violations
            + confirmed_fact_integrity_violations + bare_sending_tasks,
        }

    # ── 主动事件 inbox：来源事实的持久幂等边界 ──

    _PROACTIVE_EVENT_STATUSES = (
        "pending", "claimed", "decided", "executing",
        "confirmed", "failed", "uncertain", "skipped",
    )

    @staticmethod
    def _proactive_event_payload_json(payload) -> str:
        return json.dumps(
            payload if isinstance(payload, dict) else dict(payload or {}),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _proactive_event_dict(row) -> dict:
        if not row:
            return {}
        try:
            payload = json.loads(str(row[7] or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("PROACTIVE_EVENT_CORRUPT_PAYLOAD") from exc
        if not isinstance(payload, dict):
            raise ValueError("PROACTIVE_EVENT_CORRUPT_PAYLOAD")
        return {
            "id": int(row[0]),
            "event_id": str(row[1]),
            "idempotency_key": str(row[2]),
            "source": str(row[3]),
            "scope_id": str(row[4]),
            "channel": str(row[5]),
            "target": str(row[6]),
            "payload": payload,
            "created_at": str(row[8]),
            "recorded_at": str(row[9]),
            "status": str(row[10] or ""),
            "attempts": int(row[11] or 0),
            "lease_token": str(row[12] or ""),
            "lease_until": str(row[13] or ""),
            "decision_run_id": str(row[14] or ""),
            "action_plan_id": str(row[15] or ""),
            "error_code": str(row[16] or ""),
            "updated_at": str(row[17] or ""),
        }

    @staticmethod
    def _proactive_event_matches(row, event, payload_json: str) -> bool:
        """判断重复事件是否是同一不可变来源，而不是只撞了 event_id。"""
        if not row:
            return False
        return (
            str(row[1]) == str(event.event_id)
            and str(row[2]) == str(event.idempotency_key)
            and str(row[3]) == str(event.source)
            and str(row[4]) == str(event.scope_id)
            and str(row[5]) == str(event.channel)
            and str(row[6]) == str(event.target)
            and str(row[7]) == payload_json
            and (not str(event.created_at or "")
                 or str(row[8]) == str(event.created_at))
        )

    def record_proactive_event(self, event) -> dict:
        """持久化主动事件来源并返回其状态；重复写入安全幂等。

        该方法只登记“有一个主动事件等待决策”的来源事实，不 claim、不调用
        LLM/QQ，也不把 ``pending`` 解释为已发送。event_id 或幂等键撞上不同
        payload/作用域时 fail-closed，避免跨用户串线和静默覆盖。
        """
        from .interaction_contract import ProactiveEvent

        if not isinstance(event, ProactiveEvent):
            raise TypeError("record_proactive_event requires ProactiveEvent")
        payload_json = self._proactive_event_payload_json(dict(event.payload))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        created_at = str(event.created_at or now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT p.id,p.event_id,p.idempotency_key,p.source,p.scope_id,"
                "p.channel,p.target,p.payload_json,p.created_at,p.recorded_at,"
                "s.status,s.attempts,s.lease_token,s.lease_until,"
                "s.decision_run_id,s.action_plan_id,s.error_code,s.updated_at "
                "FROM proactive_events p JOIN proactive_event_state s "
                "ON s.event_id=p.event_id WHERE p.event_id=? OR p.idempotency_key=?",
                (str(event.event_id), str(event.idempotency_key)),
            ).fetchone()
            if row:
                if not self._proactive_event_matches(row, event, payload_json):
                    conn.rollback()
                    raise ValueError("PROACTIVE_EVENT_CONFLICT")
                conn.commit()
                return self._proactive_event_dict(row)

            conn.execute(
                "INSERT INTO proactive_events "
                "(event_id,idempotency_key,source,scope_id,channel,target,"
                "payload_json,created_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(event.event_id), str(event.idempotency_key), str(event.source),
                 str(event.scope_id), str(event.channel), str(event.target),
                 payload_json, created_at, now),
            )
            conn.execute(
                "INSERT INTO proactive_event_state "
                "(event_id,status,attempts,updated_at) VALUES (?,'pending',0,?)",
                (str(event.event_id), now),
            )
            row = conn.execute(
                "SELECT p.id,p.event_id,p.idempotency_key,p.source,p.scope_id,"
                "p.channel,p.target,p.payload_json,p.created_at,p.recorded_at,"
                "s.status,s.attempts,s.lease_token,s.lease_until,"
                "s.decision_run_id,s.action_plan_id,s.error_code,s.updated_at "
                "FROM proactive_events p JOIN proactive_event_state s "
                "ON s.event_id=p.event_id WHERE p.event_id=?",
                (str(event.event_id),),
            ).fetchone()
            conn.commit()
        return self._proactive_event_dict(row)

    def get_proactive_event(self, event_id: str) -> dict | None:
        """读取主动事件来源及状态；损坏 payload 不静默降级。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.id,p.event_id,p.idempotency_key,p.source,p.scope_id,"
                "p.channel,p.target,p.payload_json,p.created_at,p.recorded_at,"
                "s.status,s.attempts,s.lease_token,s.lease_until,"
                "s.decision_run_id,s.action_plan_id,s.error_code,s.updated_at "
                "FROM proactive_events p LEFT JOIN proactive_event_state s "
                "ON s.event_id=p.event_id WHERE p.event_id=?",
                (str(event_id),),
            ).fetchone()
        if not row:
            return None
        if row[10] is None:
            raise ValueError("PROACTIVE_EVENT_STATE_MISSING")
        return self._proactive_event_dict(row)

    def list_proactive_events(self, *, status: str = "", limit: int = 100) -> list[dict]:
        """按来源顺序读取主动事件，供后续 claim/开窗切片使用。"""
        normalized_status = str(status or "").strip()
        if normalized_status and normalized_status not in self._PROACTIVE_EVENT_STATUSES:
            raise ValueError("PROACTIVE_EVENT_STATUS_INVALID")
        limit = max(1, min(int(limit), 1000))
        where = "WHERE s.status=?" if normalized_status else ""
        params = (normalized_status, limit) if normalized_status else (limit,)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.id,p.event_id,p.idempotency_key,p.source,p.scope_id,"
                "p.channel,p.target,p.payload_json,p.created_at,p.recorded_at,"
                "s.status,s.attempts,s.lease_token,s.lease_until,"
                "s.decision_run_id,s.action_plan_id,s.error_code,s.updated_at "
                "FROM proactive_events p JOIN proactive_event_state s "
                "ON s.event_id=p.event_id " + where +
                " ORDER BY p.id LIMIT ?", params,
            ).fetchall()
        return [self._proactive_event_dict(row) for row in rows]

    def get_proactive_event_health(self) -> dict[str, int]:
        """返回主动事件来源/状态计数；不读取 payload 正文。"""
        with self._connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM proactive_events"
            ).fetchone()[0] or 0)
            rows = conn.execute(
                "SELECT COALESCE(s.status,''),COUNT(*) FROM proactive_events p "
                "LEFT JOIN proactive_event_state s ON s.event_id=p.event_id "
                "GROUP BY COALESCE(s.status,'')"
            ).fetchall()
            invalid_payload = int(conn.execute(
                "SELECT COUNT(*) FROM proactive_events "
                "WHERE json_valid(payload_json)=0 "
                "OR json_type(payload_json,'$')!='object'"
            ).fetchone()[0] or 0)
            bound = int(conn.execute(
                "SELECT COUNT(*) FROM proactive_event_state "
                "WHERE TRIM(COALESCE(decision_run_id,''))!=''"
            ).fetchone()[0] or 0)
        counts = {status: 0 for status in self._PROACTIVE_EVENT_STATUSES}
        unknown = 0
        for status, count in rows:
            status = str(status or "")
            if status in counts:
                counts[status] += int(count or 0)
            else:
                unknown += int(count or 0)
        counts.update({
            "total": total,
            "unknown": unknown,
            "invalid_payload": invalid_payload,
            "bound": bound,
            "open": counts["pending"] + counts["claimed"] + counts["executing"],
        })
        return counts

    # ── LLM 决策回合：只读终态事实 ──

    _DECISION_RUN_STATUSES = ("completed", "failed")

    @staticmethod
    def _decision_run_dict(row) -> dict:
        if not row:
            return {}
        try:
            tool_calls = json.loads(str(row[8] or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("DECISION_RUN_CORRUPT_TOOL_CALLS") from exc
        if not isinstance(tool_calls, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in tool_calls
        ):
            raise ValueError("DECISION_RUN_CORRUPT_TOOL_CALLS")
        return {
            "id": int(row[0]),
            "run_id": str(row[1]),
            "event_key": str(row[2]),
            "scope_id": str(row[3]),
            "correlation_id": str(row[4]),
            "model": str(row[5] or ""),
            "status": str(row[6]),
            "decision": str(row[7] or ""),
            "tool_calls": list(tool_calls),
            "started_at": str(row[9] or ""),
            "finished_at": str(row[10] or ""),
            "error_code": str(row[11] or ""),
            "recorded_at": str(row[12] or ""),
        }

    @staticmethod
    def _decision_run_matches(row, run, tool_calls_json: str) -> bool:
        return bool(row) and (
            str(row[1]) == str(run.run_id)
            and str(row[2]) == str(run.event_key)
            and str(row[3]) == str(run.scope_id)
            and str(row[4]) == str(run.correlation_id)
            and str(row[5] or "") == str(run.model or "")
            and str(row[6]) == str(run.status)
            and str(row[7] or "") == str(run.decision or "")
            and str(row[8]) == tool_calls_json
            and str(row[9] or "") == str(run.started_at or "")
            and str(row[10] or "") == str(run.finished_at or "")
            and str(row[11] or "") == str(run.error_code or "")
        )

    def record_decision_run(self, run) -> dict:
        """保存一个已结束的 DecisionRun；重复相同内容幂等，漂移拒绝。"""
        from .interaction_contract import DecisionRun

        if not isinstance(run, DecisionRun):
            raise TypeError("record_decision_run requires DecisionRun")
        if run.status not in self._DECISION_RUN_STATUSES:
            raise ValueError("DECISION_RUN_NOT_TERMINAL")
        tool_calls_json = json.dumps(
            list(run.tool_calls), ensure_ascii=False, separators=(",", ":"),
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id,run_id,event_key,scope_id,correlation_id,model,status,"
                "decision,tool_calls_json,started_at,finished_at,error_code,recorded_at "
                "FROM decision_runs WHERE run_id=?",
                (str(run.run_id),),
            ).fetchone()
            if row:
                if not self._decision_run_matches(row, run, tool_calls_json):
                    conn.rollback()
                    raise ValueError("DECISION_RUN_CONFLICT")
                conn.commit()
                return self._decision_run_dict(row)
            conn.execute(
                "INSERT INTO decision_runs (run_id,event_key,scope_id,correlation_id,"
                "model,status,decision,tool_calls_json,started_at,finished_at,error_code,"
                "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(run.run_id), str(run.event_key), str(run.scope_id),
                 str(run.correlation_id), str(run.model or ""), str(run.status),
                 str(run.decision or ""), tool_calls_json, str(run.started_at or ""),
                 str(run.finished_at or ""), str(run.error_code or ""), now),
            )
            row = conn.execute(
                "SELECT id,run_id,event_key,scope_id,correlation_id,model,status,"
                "decision,tool_calls_json,started_at,finished_at,error_code,recorded_at "
                "FROM decision_runs WHERE run_id=?",
                (str(run.run_id),),
            ).fetchone()
            conn.commit()
        return self._decision_run_dict(row)

    def get_decision_run(self, run_id: str) -> dict | None:
        """读取已结束的 DecisionRun；损坏工具列表不静默降级。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,run_id,event_key,scope_id,correlation_id,model,status,"
                "decision,tool_calls_json,started_at,finished_at,error_code,recorded_at "
                "FROM decision_runs WHERE run_id=?",
                (str(run_id or ""),),
            ).fetchone()
        return self._decision_run_dict(row) if row else None

    def find_feedback_for_turn(self, user_qq: str, after_ts: str,
                               reply_text: str, window_hours: int = 3) -> dict | None:
        """找到糖糖某条回复之后、对方给出的那条反应（`feedback` 表）。

        为什么不能按 id 精确关联：`feedback` 表没有 chat_log 外键，只存
        `bot_reply`（**拼接后的整段回复**）和反馈发生时间。而一个回合的回复
        在 chat_log 里是被拆成多条气泡的——所以「单条 message == bot_reply」
        根本不成立。改用两个松弛条件同时成立：

          ① 同一个人（`user_qq`）在 `after_ts` 之后 `window_hours` 内；
          ② 两边文本有前缀关系（任一方以另一方 60 字前缀开头）。

        取最早的一条：一条回复只该被第一次反应评价。匹配不到返回 None——
        界面据此显示「还没有后续反馈」，不编造。
        """
        uq = str(user_qq or "")
        ts = str(after_ts or "")
        prefix = str(reply_text or "").strip()[:60]
        if not uq or not ts or not prefix:
            return None
        try:
            upper = (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                     + timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sentiment, confidence, user_reaction, reply_ms, timestamp, "
                "direction_verified, bot_reply FROM feedback "
                "WHERE user_qq=? AND timestamp>=? AND timestamp<=? "
                "  AND (bot_reply LIKE ? || '%' OR ? LIKE bot_reply || '%') "
                "ORDER BY timestamp ASC LIMIT 1",
                (uq, ts, upper, prefix, prefix),
            ).fetchone()
        if not row:
            return None
        try:
            confidence = float(row[1] or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "sentiment": str(row[0] or ""),
            "confidence": confidence,
            "user_reaction": str(row[2] or ""),
            "reply_ms": int(row[3] or 0),
            "timestamp": str(row[4] or ""),
            "direction_verified": bool(row[5]),
            # 她这一回合实际发出去的整段回复（多气泡拼接后的原文）。
            # 2026-10-25 第 17 轮 ④：七段流程的「发送」段要显示它——
            # 之前只投影了六个字段，回放里看得见「对方怎么反应」却看不见
            # 「她到底说了什么」，时间轴第 7 段只能写「（未采集到）」。
            "bot_reply": str(row[6] or ""),
        }

    # ═══════════════════════════════════════════════════════════
    # 回合痕迹 turn_traces（2026-10-24）
    #
    # 「她说完那句话的时候，脑子里过了什么」——思维链 + 工具步骤。
    # 与 decision_runs 一一对应（run_id 同键），与 chat_log 靠 event_key 关联。
    # ═══════════════════════════════════════════════════════════
    #: 单条思维链最多落库多少字符。超长的思维链对「看懂她怎么想」没有增量，
    #: 却会让库涨得很快（实测一次回复的 reasoning 常在 1-3k 字）。
    TRACE_REASONING_LIMIT = 8000
    #: 单条工具步骤的「结果摘要」最多留多少字符（完整结果可能上万字）
    TRACE_TOOL_RESULT_LIMIT = 500
    #: 单轮 prompt 结构摘要最多留多少字符（它是结构不是全文，正常只有几百字）
    TRACE_DIGEST_LIMIT = 4000

    def record_turn_trace(self, *, run_id: str, event_key: str = "",
                          scope_id: str = "", model: str = "",
                          reasoning_text: str = "",
                          tool_steps: list | None = None,
                          prompt_chars: int = 0, duration_ms: int = 0,
                          prompt_digest: dict | None = None) -> dict:
        """保存一回合的痕迹。同一 run_id 幂等——重复写入不报错也不覆盖。

        幂等而非「冲突即抛」：痕迹是**观察数据**，写出问题不该影响业务
        （与 record_decision_run 的「漂移拒绝」不同，那是事实契约）。

        `prompt_digest`（2026-10-25 第三轮）：这一轮她**收到了什么**的结构摘要
        ——system prompt 的段标题、user message 里的 `<背景·X>` 标签、历史条数与
        字符数。由 `handler._summarize_prompt()` 生成，只存摘要不存全文。
        没有它，「她记错了」只能靠复刻 prompt 猜。
        """
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("TURN_TRACE_NEEDS_RUN_ID")
        reasoning_text = str(reasoning_text or "")[: self.TRACE_REASONING_LIMIT]
        steps = []
        for step in (tool_steps or []):
            if not isinstance(step, dict):
                continue
            steps.append({
                "name": str(step.get("name") or ""),
                "args": str(step.get("args") or "")[: self.TRACE_TOOL_RESULT_LIMIT],
                "result": str(step.get("result") or "")[: self.TRACE_TOOL_RESULT_LIMIT],
                "ms": int(step.get("ms") or 0),
            })
        tool_steps_json = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
        digest_json = ""
        if isinstance(prompt_digest, dict) and prompt_digest:
            try:
                digest_json = json.dumps(prompt_digest, ensure_ascii=False,
                                         separators=(",", ":"))[: self.TRACE_DIGEST_LIMIT]
            except (TypeError, ValueError):
                digest_json = ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM turn_traces WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                return {"id": int(existing[0]), "run_id": run_id, "duplicate": True}
            cur = conn.execute(
                "INSERT INTO turn_traces (run_id,event_key,scope_id,model,reasoning_text,"
                "tool_steps_json,prompt_chars,duration_ms,recorded_at,prompt_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, str(event_key or ""), str(scope_id or ""), str(model or ""),
                 reasoning_text, tool_steps_json, int(prompt_chars or 0),
                 int(duration_ms or 0), now, digest_json),
            )
            conn.commit()
            return {"id": int(cur.lastrowid or 0), "run_id": run_id,
                    "duplicate": False}

    def get_turn_trace(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,run_id,event_key,scope_id,model,reasoning_text,"
                "tool_steps_json,prompt_chars,duration_ms,recorded_at,prompt_digest "
                "FROM turn_traces WHERE run_id=?", (str(run_id or ""),)
            ).fetchone()
        return self._turn_trace_dict(row) if row else None

    def get_turn_trace_by_event(self, event_key: str) -> dict | None:
        """按 event_key 取最近一条痕迹——chat_log 的行就是这么挂上去的。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,run_id,event_key,scope_id,model,reasoning_text,"
                "tool_steps_json,prompt_chars,duration_ms,recorded_at,prompt_digest "
                "FROM turn_traces WHERE event_key=? ORDER BY id DESC LIMIT 1",
                (str(event_key or ""),)
            ).fetchone()
        return self._turn_trace_dict(row) if row else None

    def query_turn_traces(self, scope_id: str = "", limit: int = 50) -> list[dict]:
        """按会话查最近的痕迹（新→旧）。scope_id 空串 = 全部会话。"""
        limit = max(1, min(int(limit), 500))
        sql = ("SELECT id,run_id,event_key,scope_id,model,reasoning_text,"
               "tool_steps_json,prompt_chars,duration_ms,recorded_at,prompt_digest "
               "FROM turn_traces")
        params: list = []
        if scope_id:
            sql += " WHERE scope_id=?"
            params.append(str(scope_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._turn_trace_dict(r) for r in rows]

    @staticmethod
    def _turn_trace_dict(row) -> dict:
        try:
            steps = json.loads(row[6] or "[]")
        except (ValueError, TypeError):
            steps = []
        # prompt_digest：这一轮她「收到了什么」的结构摘要（2026-10-25 新增）。
        # 老行没有这一列/内容为空 → 给空 dict，不抛异常（观察数据必须容错）。
        try:
            digest = json.loads(row[10] or "{}")
        except (IndexError, ValueError, TypeError):
            digest = {}
        return {
            "id": int(row[0]),
            "run_id": row[1],
            "event_key": row[2],
            "scope_id": row[3],
            "model": row[4],
            "reasoning": row[5],
            "tool_steps": steps if isinstance(steps, list) else [],
            "prompt_chars": int(row[7] or 0),
            "duration_ms": int(row[8] or 0),
            "recorded_at": row[9],
            "prompt_digest": digest if isinstance(digest, dict) else {},
        }


    def get_decision_run_health(self) -> dict[str, int]:
        """返回 DecisionRun 终态计数和损坏行数，供运行观察读取。"""
        with self._connect() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM decision_runs"
            ).fetchone()[0] or 0)
            rows = conn.execute(
                "SELECT status,COUNT(*) FROM decision_runs GROUP BY status"
            ).fetchall()
            corrupt = int(conn.execute(
                "SELECT COUNT(*) FROM decision_runs WHERE json_valid(tool_calls_json)=0 "
                "OR json_type(tool_calls_json,'$')!='array'"
            ).fetchone()[0] or 0)
        counts = {status: 0 for status in self._DECISION_RUN_STATUSES}
        unknown = 0
        for status, count in rows:
            if str(status) in counts:
                counts[str(status)] += int(count or 0)
            else:
                unknown += int(count or 0)
        return {
            **counts, "total": total, "unknown": unknown, "corrupt": corrupt,
        }

    def claim_proactive_event(
            self, event_id: str, lease_token: str, lease_seconds: int = 120,
    ) -> bool:
        """原子领取 pending 主动事件；同一事件只有一个执行者。"""
        event_id = str(event_id or "").strip()
        lease_token = str(lease_token or "").strip()
        if not event_id or not lease_token:
            raise ValueError("PROACTIVE_LEASE_REQUIRED")
        seconds = max(1, min(int(lease_seconds), 3600))
        now = datetime.now()
        lease_until = (now + timedelta(seconds=seconds)).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proactive_event_state SET status='claimed',"
                "attempts=attempts+1,lease_token=?,lease_until=?,"
                "error_code=CASE WHEN status='claimed' "
                "THEN 'PROACTIVE_LEASE_EXPIRED' ELSE '' END,updated_at=? "
                "WHERE event_id=? AND (status='pending' OR ("
                "status='claimed' AND lease_until!='' AND "
                "lease_until<=datetime('now','localtime'))) ",
                (lease_token, lease_until, now_text, event_id),
            )
            conn.commit()
        return cur.rowcount == 1

    def mark_proactive_event_executing(
            self, event_id: str, lease_token: str,
    ) -> bool:
        """在 LLM/外部副作用即将开始前推进 executing，绑定同一租约。"""
        event_id = str(event_id or "").strip()
        lease_token = str(lease_token or "").strip()
        if not event_id or not lease_token:
            raise ValueError("PROACTIVE_LEASE_REQUIRED")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proactive_event_state SET status='executing',"
                "updated_at=? WHERE event_id=? AND status='claimed' "
                "AND lease_token=?",
                (now, event_id, lease_token),
            )
            conn.commit()
        return cur.rowcount == 1

    def release_proactive_event_claim(
            self, event_id: str, lease_token: str,
            *, error_code: str = "PROACTIVE_EXECUTION_NOT_STARTED",
    ) -> bool:
        """执行态推进失败时，仅释放本租约的 claimed 状态回 pending。"""
        event_id = str(event_id or "").strip()
        lease_token = str(lease_token or "").strip()
        if not event_id or not lease_token:
            raise ValueError("PROACTIVE_LEASE_REQUIRED")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proactive_event_state SET status='pending',"
                "lease_token='',lease_until='',error_code=?,updated_at=? "
                "WHERE event_id=? AND status='claimed' AND lease_token=?",
                (str(error_code or "")[:128], now, event_id, lease_token),
            )
            conn.commit()
        return cur.rowcount == 1

    def mark_proactive_event_decided(
            self, event_id: str, lease_token: str, decision_run_id: str,
            *, action_plan_id: str = "",
    ) -> bool:
        """绑定已完成的 LLM 决策；仅允许 executing 租约持有者推进。"""
        event_id = str(event_id or "").strip()
        lease_token = str(lease_token or "").strip()
        decision_run_id = str(decision_run_id or "").strip()
        if not event_id or not lease_token:
            raise ValueError("PROACTIVE_LEASE_REQUIRED")
        if not decision_run_id:
            raise ValueError("PROACTIVE_DECISION_RUN_REQUIRED")
        action_plan_id = str(action_plan_id or "").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                "SELECT s.status,s.lease_token,e.scope_id "
                "FROM proactive_event_state s "
                "JOIN proactive_events e ON e.event_id=s.event_id "
                "WHERE s.event_id=?",
                (event_id,),
            ).fetchone()
            # 先验证租约，再暴露决策引用错误；错误租约仍保持原来的
            # False 语义，避免让非持有者探测事件内部状态。
            if not state or str(state[0]) != "executing" or str(state[1]) != lease_token:
                conn.rollback()
                return False
            decision = conn.execute(
                "SELECT event_key,scope_id,status,decision "
                "FROM decision_runs WHERE run_id=?",
                (decision_run_id,),
            ).fetchone()
            if not decision:
                conn.rollback()
                raise ValueError("PROACTIVE_DECISION_RUN_NOT_FOUND")
            if str(decision[2]) != "completed":
                conn.rollback()
                raise ValueError("PROACTIVE_DECISION_RUN_NOT_COMPLETED")
            if str(decision[0]) != event_id:
                conn.rollback()
                raise ValueError("PROACTIVE_DECISION_RUN_EVENT_MISMATCH")
            if str(decision[1]) != str(state[2]):
                conn.rollback()
                raise ValueError("PROACTIVE_DECISION_RUN_SCOPE_MISMATCH")
            cur = conn.execute(
                "UPDATE proactive_event_state SET status='decided',"
                "decision_run_id=?,action_plan_id=?,updated_at=? "
                "WHERE event_id=? AND status='executing' AND lease_token=?",
                (decision_run_id, action_plan_id, now, event_id, lease_token),
            )
            conn.commit()
        return cur.rowcount == 1

    def finish_proactive_event(
            self, event_id: str, lease_token: str, status: str,
            *, error_code: str = "",
    ) -> bool:
        """提交主动事件终态并释放租约；终态不可被后续重放改写。"""
        event_id = str(event_id or "").strip()
        lease_token = str(lease_token or "").strip()
        status = str(status or "").strip()
        terminal = {"confirmed", "failed", "uncertain", "skipped"}
        if status not in terminal:
            raise ValueError("PROACTIVE_TERMINAL_STATUS_INVALID")
        if not event_id or not lease_token:
            raise ValueError("PROACTIVE_LEASE_REQUIRED")
        error_code = str(error_code or "")[:128]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proactive_event_state SET status=?,"
                "lease_token='',lease_until='',error_code=?,updated_at=? "
                "WHERE event_id=? AND status IN ('executing','decided') "
                "AND lease_token=?",
                (status, error_code, now, event_id, lease_token),
            )
            conn.commit()
        return cur.rowcount == 1

    def recover_proactive_events_after_restart(self) -> dict[str, int]:
        """恢复事件租约：未开始执行的可安全重试，执行中的结果不明。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            pending = conn.execute(
                "UPDATE proactive_event_state SET status='pending',"
                "lease_token='',lease_until='',"
                "error_code='PROCESS_RESTARTED_BEFORE_EVENT',updated_at=? "
                "WHERE status='claimed'",
                (now,),
            ).rowcount
            uncertain = conn.execute(
                "UPDATE proactive_event_state SET status='uncertain',"
                "lease_token='',lease_until='',"
                "error_code='PROCESS_RESTARTED_DURING_EVENT',updated_at=? "
                "WHERE status='executing'",
                (now,),
            ).rowcount
            conn.commit()
        return {"pending": int(pending), "uncertain": int(uncertain)}

    # ── 入站 inbox：QQ 事件的持久幂等边界 ──

    _INBOUND_STATUSES = (
        "received", "claimed", "executing", "processed", "failed", "uncertain",
    )

    def register_inbound_event(self, event_key: str, event_type: str) -> str:
        """登记事件并返回当前状态；重复登记绝不重置终态。"""
        if not event_key:
            return "untracked"
        started = time.perf_counter()
        insert_done = select_done = None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO inbound_events "
                "(event_key,event_type,status,attempts,last_error,received_at,updated_at) "
                "VALUES (?,?,'received',0,'',?,?)",
                (str(event_key), str(event_type), now, now),
            )
            insert_done = time.perf_counter()
            row = conn.execute(
                "SELECT status FROM inbound_events WHERE event_key=?",
                (str(event_key),),
            ).fetchone()
            select_done = time.perf_counter()
            conn.commit()
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 250:
            logger.warning(
                "inbound register slow | elapsed_ms=%.1f insert_ms=%.1f "
                "select_ms=%.1f commit_ms=%.1f",
                elapsed_ms,
                (insert_done - started) * 1000 if insert_done else -1.0,
                (select_done - insert_done) * 1000 if select_done and insert_done else -1.0,
                (time.perf_counter() - select_done) * 1000 if select_done else -1.0,
            )
        return str(row[0]) if row else "untracked"

    def claim_inbound_event(self, event_key: str) -> bool:
        """原子领取 received 事件；同一事件同一时刻只有一个 worker。"""
        started = time.perf_counter()
        update_done = None
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE inbound_events SET status='claimed',attempts=attempts+1,"
                "updated_at=datetime('now','localtime') "
                "WHERE event_key=? AND status='received'",
                (str(event_key),),
            )
            update_done = time.perf_counter()
            conn.commit()
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 250:
            logger.warning(
                "inbound claim slow | elapsed_ms=%.1f update_ms=%.1f commit_ms=%.1f",
                elapsed_ms,
                (update_done - started) * 1000 if update_done else -1.0,
                (time.perf_counter() - update_done) * 1000 if update_done else -1.0,
            )
        return cur.rowcount == 1

    def claim_and_mark_inbound_event_executing(self, event_key: str) -> bool:
        """一次事务完成领取并进入 executing，减少入站写事务争用。

        这是 ``claim_inbound_event`` + ``mark_inbound_event_executing`` 的
        原子等价路径：只有 received 能成功转换，attempts 仍只增加一次；
        进程在回调前崩溃时，重启恢复会把 executing 标为 uncertain，绝不
        因合并事务而改变“不确定结果禁止盲重放”的语义。
        """
        started = time.perf_counter()
        update_done = None
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE inbound_events SET status='executing',attempts=attempts+1,"
                "updated_at=datetime('now','localtime') "
                "WHERE event_key=? AND status='received'",
                (str(event_key),),
            )
            update_done = time.perf_counter()
            conn.commit()
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= 250:
            logger.warning(
                "inbound claim+executing slow | elapsed_ms=%.1f "
                "update_ms=%.1f commit_ms=%.1f",
                elapsed_ms,
                (update_done - started) * 1000 if update_done else -1.0,
                (time.perf_counter() - update_done) * 1000 if update_done else -1.0,
            )
        return cur.rowcount == 1

    def mark_send_outbox_confirmed_unaccounted(
            self, outbox_id: str, message_ids=(), error: str = "") -> bool:
        """保存已知平台确认；无 message_id 时只能降为 uncertain。"""
        normalized = _normalize_delivery_message_ids(message_ids)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        code = str(error or "PROJECTION_LOCAL_ERROR")[:128]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_attempt_id,domain_action_id FROM send_outbox "
                "WHERE action_id=? AND status='sending'",
                (str(outbox_id),),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            if not normalized:
                cur = conn.execute(
                    "UPDATE send_outbox SET status='uncertain',"
                    "confirmed_message_ids='[]',confirmed_at='',"
                    "projection_error='',last_error=?,updated_at=? "
                    "WHERE action_id=? AND status='sending'",
                    ("MESSAGE_ID_UNCONFIRMED", now, str(outbox_id)),
                )
                if cur.rowcount == 1 and row[0] is not None:
                    self._settle_linked_task_attempt_conn(
                        conn, attempt_id=int(row[0]), state="uncertain",
                        accounting_state="pending_repair",
                        error="MESSAGE_ID_UNCONFIRMED",
                        outbox_id=str(outbox_id),
                        domain_action_id=str(row[1] or ""),
                        event_type="known_confirmation_rejected",
                        reason="missing_platform_message_id",
                    )
                conn.commit()
                return False
            cur = conn.execute(
                "UPDATE send_outbox SET status='confirmed_unaccounted',"
                "confirmed_message_ids=?,confirmed_at=?,projection_error=?,"
                "last_error=?,updated_at=? WHERE action_id=? AND status='sending'",
                (json.dumps(normalized, separators=(",", ":")), now, code,
                 code, now, str(outbox_id)),
            )
            if cur.rowcount == 1 and row[0] is not None:
                self._settle_linked_task_attempt_conn(
                    conn, attempt_id=int(row[0]), state="confirmed",
                    accounting_state="pending_repair", error=code,
                    outbox_id=str(outbox_id),
                    domain_action_id=str(row[1] or ""),
                    event_type="known_confirmed_frozen",
                    reason="settle_local_error",
                )
            conn.commit()
        return cur.rowcount == 1

    def list_confirmed_projection_repairs(self, limit: int = 20) -> list[dict]:
        """列出待 DB-only 修复的 known-confirmed outbox。

        只返回 ``confirmed_unaccounted``：``confirmed_conflict`` 需要先由
        人工审查 immutable 快照，不能让后台循环反复尝试并掩盖冲突。该方法
        不触碰 QQ/NapCat，也不改变任何状态。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM send_outbox "
                "WHERE status='confirmed_unaccounted' "
                "ORDER BY confirmed_at,updated_at,action_id LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _confirmed_immutable_json(receipt: dict, *, outbox_id: str = "",
                                  confirmed_at: str = "",
                                  include_provenance: bool = True) -> str:
        immutable = {
            key: receipt.get(key) for key in (
                "action_id", "schema_version", "identity_version", "source_id",
                "scope_id", "kind", "channel", "target", "identity_payload",
                "ordinal", "conversation_ref", "actual", "message_ids",
            )
        }
        if include_provenance:
            immutable["outbox_id"] = str(outbox_id or "")
            immutable["confirmed_at"] = str(confirmed_at or "")
        return json.dumps(
            immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _confirmed_fact_integrity_count_conn(conn) -> int:
        """检查永久事实的快照字段及 v2 action 身份锚点。"""
        row = conn.execute(_CONFIRMED_FACT_INTEGRITY_COUNT_SQL).fetchone()
        violations = int(row[0] or 0) if row else 0
        try:
            from .action_contract import derive_action_id
            fact_rows = conn.execute(
                "SELECT domain_action_id,immutable_json "
                "FROM confirmed_action_facts"
            ).fetchall()
        except (sqlite3.DatabaseError, ImportError):
            return violations
        for domain_action_id, immutable_json in fact_rows:
            try:
                snapshot = json.loads(str(immutable_json or ""))
                schema_version = int(snapshot.get("schema_version", 1))
                # 本校验随 ordinal 快照字段一起上线；历史 v2 事实没有
                # ordinal 时无法可靠重算 action_id，保留旧列↔快照检查即可。
                if schema_version < 2 or "ordinal" not in snapshot:
                    continue
                identity_payload = snapshot.get("identity_payload")
                if not isinstance(identity_payload, dict):
                    violations += 1
                    continue
                expected = derive_action_id(
                    source_id=str(snapshot.get("source_id") or ""),
                    scope_id=str(snapshot.get("scope_id") or ""),
                    kind=str(snapshot.get("kind") or ""),
                    channel=str(snapshot.get("channel") or ""),
                    target=str(snapshot.get("target") or ""),
                    payload=identity_payload,
                    ordinal=int(snapshot.get("ordinal", 0)),
                    schema_version=schema_version,
                    identity_version=int(snapshot.get("identity_version", 1)),
                )
                if expected != str(domain_action_id):
                    violations += 1
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                violations += 1
        return violations

    @staticmethod
    def _validate_projection_source_conn(conn, conversation_ref: dict) -> None:
        source_chat_id = conversation_ref.get("source_chat_id")
        if source_chat_id is None:
            return
        row = conn.execute(
            "SELECT qq_id,group_id,is_bot_reply,is_synthetic,quarantined_at "
            "FROM chat_log WHERE id=?",
            (int(source_chat_id),),
        ).fetchone()
        expected = (
            str(conversation_ref.get("conversation_user_id") or ""),
            str(conversation_ref.get("group_id") or ""),
            0,
            0,
            "",
        )
        actual = (
            str(row[0]), str(row[1] or ""), int(row[2] or 0),
            int(row[3] or 0), str(row[4] or ""),
        ) if row else None
        if actual != expected:
            raise ConfirmedProjectionError("PROJECTION_INVALID_SOURCE")

    @staticmethod
    def _insert_projected_bot_chat_conn(conn, *, domain_action_id: str,
                                        user_id: str, group_id: str,
                                        text: str, timestamp: str) -> int:
        cur = conn.execute(
            "INSERT INTO chat_log "
            "(qq_id,group_id,is_bot_reply,message,timestamp,raw_message,segments,event_key) "
            "VALUES (?,?,1,?,?, '', '', ?)",
            (str(user_id), str(group_id or ""), str(text), str(timestamp),
             f"action:{domain_action_id}:chat"),
        )
        return int(cur.lastrowid)

    def _commit_confirmed_action_conn(self, conn, *, outbox_id: str,
                                      receipt: dict, confirmed_at: str,
                                      allow_negative_upgrade: bool = False) -> str:
        """在调用方事务内提交 v2 known-confirmed；不 begin/commit/rollback。"""
        if receipt.get("status") != "confirmed" or int(
                receipt.get("schema_version", 1)) < 2:
            raise ConfirmedProjectionError("PROJECTION_CONTRACT_INVALID")
        domain_action_id = str(receipt.get("action_id") or "").strip()
        conversation_ref = dict(receipt.get("conversation_ref") or {})
        projection_kind = str(conversation_ref.get("projection_kind") or "none")
        immutable_json = self._confirmed_immutable_json(
            receipt, outbox_id=str(outbox_id), confirmed_at=str(confirmed_at),
        )
        identity_json = self._confirmed_immutable_json(
            receipt, include_provenance=False,
        )
        existing = conn.execute(
            "SELECT immutable_json FROM confirmed_action_facts "
            "WHERE domain_action_id=?", (domain_action_id,),
        ).fetchone()
        if existing:
            try:
                existing_identity = json.loads(str(existing[0] or ""))
                if isinstance(existing_identity, dict):
                    existing_identity.pop("outbox_id", None)
                    existing_identity.pop("confirmed_at", None)
                    # 旧版 v2 快照没有 ordinal；它的历史合同只有
                    # ordinal=0，因此允许与新快照的显式 0 等价比较。
                    existing_identity.setdefault("ordinal", 0)
                existing_identity_json = json.dumps(
                    existing_identity, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                existing_identity_json = ""
            if existing_identity_json == identity_json:
                return "confirmed_duplicate"
            raise ConfirmedProjectionConflict(str(existing[0]), immutable_json)
        # T2.4（2026-10-13，审计 P0-3 / ADR-003:68）：永久事实是 append-only，
        # 而 chat_log 的 event_key 有唯一索引——同一个 domain action 若带着
        # **不同正文**再次投影，INSERT 会撞唯一键。裸抛 sqlite3.IntegrityError
        # 会让它被当成 operational 异常记进 confirmed_unaccounted，掩盖真正的
        # 数据损坏。ADR-003:113 明令「INSERT OR IGNORE 吞掉所有冲突」不采用，
        # 所以这里**显式判重**：chat 行已存在即隔离为冲突，由上层写
        # action_projection_conflicts 并进入 confirmed_conflict。
        chat_event_key = f"action:{domain_action_id}:chat"
        if conn.execute(
                "SELECT 1 FROM chat_log WHERE event_key=? LIMIT 1",
                (chat_event_key,),
        ).fetchone():
            raise ConfirmedProjectionConflict(
                json.dumps({"chat_event_key": chat_event_key},
                           ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                immutable_json,
            )

        actual = dict(receipt.get("actual") or {})
        text = str(actual.get("text") or "").strip()
        if projection_kind == "conversation_reply":
            if not text:
                raise ConfirmedProjectionError("PROJECTION_EMPTY_TEXT")
            if receipt.get("kind") == "voice" and actual.get("delivery_kind") not in {
                    "voice", "text"}:
                raise ConfirmedProjectionError("PROJECTION_INVALID_DELIVERY_KIND")
            self._validate_projection_source_conn(conn, conversation_ref)
            chat_log_id = self._insert_projected_bot_chat_conn(
                conn,
                domain_action_id=domain_action_id,
                user_id=str(conversation_ref.get("conversation_user_id") or ""),
                group_id=str(conversation_ref.get("group_id") or ""),
                text=text,
                timestamp=confirmed_at,
            )
        elif projection_kind == "none":
            chat_log_id = None
        else:
            raise ConfirmedProjectionError("PROJECTION_KIND_INVALID")

        actual_json = json.dumps(
            actual, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        message_ids_json = json.dumps(
            list(receipt.get("message_ids") or []), separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO confirmed_action_facts "
            "(domain_action_id,outbox_id,schema_version,identity_version,source_id,"
            "scope_id,kind,channel,target,actor_kind,projection_kind,"
            "conversation_user_id,group_id,source_chat_id,self_memory_eligible,"
            "actual_json,message_ids_json,immutable_json,confirmed_at,chat_log_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'bot',?,?,?,?,?,?,?,?,?,?)",
            (
                domain_action_id, str(outbox_id), int(receipt["schema_version"]),
                int(receipt.get("identity_version", 1)),
                str(receipt.get("source_id") or ""),
                str(receipt.get("scope_id") or ""), str(receipt.get("kind") or ""),
                str(receipt.get("channel") or ""), str(receipt.get("target") or ""),
                projection_kind,
                str(conversation_ref.get("conversation_user_id") or ""),
                str(conversation_ref.get("group_id") or ""),
                conversation_ref.get("source_chat_id"),
                1 if conversation_ref.get("self_memory_eligible") else 0,
                actual_json, message_ids_json, immutable_json, str(confirmed_at),
                chat_log_id,
            ),
        )
        if projection_kind == "conversation_reply":
            conn.execute(
                "INSERT INTO conversation_window_events "
                "(event_key,domain_action_id,scope_id,channel,actor_kind,"
                "conversation_user_id,group_id,chat_log_id,text,occurred_at) "
                "VALUES (?, ?, ?, ?, 'bot', ?, ?, ?, ?, ?)",
                (
                    f"action:{domain_action_id}:window", domain_action_id,
                    str(receipt.get("scope_id") or ""),
                    str(receipt.get("channel") or ""),
                    str(conversation_ref.get("conversation_user_id") or ""),
                    str(conversation_ref.get("group_id") or ""),
                    chat_log_id, text, str(confirmed_at),
                ),
            )
        try:
            self._enqueue_action_receipt_conn(
                conn, receipt, allow_negative_upgrade=allow_negative_upgrade,
            )
        except ValueError:
            mailbox = conn.execute(
                "SELECT receipt_json FROM action_receipt_mailbox "
                "WHERE scope_id=? AND action_id=?",
                (str(receipt.get("scope_id") or ""), domain_action_id),
            ).fetchone()
            raise ConfirmedProjectionConflict(
                str(mailbox[0]) if mailbox else "{}",
                json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
            )
        return "confirmed"

    @staticmethod
    def _ensure_task_action_projection_anchor_conn(
            conn, *, attempt_id: int, ordinal: int, action_id: str,
            schema_version: int) -> None:
        """为已物化 child 写入一次不可变 schema floor。"""
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='task_action_projection_anchors'"
        ).fetchone():
            # Phase 2b 初次 backfill 先建 child，再由外层小迁移建立 floor；
            # 该短窗口只存在于同一 savepoint 内，不能把迁移顺序当运行时
            # 可见状态。
            return
        conn.execute(
            "INSERT OR IGNORE INTO task_action_projection_anchors "
            "(attempt_id,ordinal,action_id,schema_version,created_at) "
            "VALUES (?,?,?,?,?)",
            (int(attempt_id), int(ordinal), str(action_id), int(schema_version),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def _materialize_task_action_attempt_conn(self, conn, attempt_id: int) -> None:
        """把 immutable plan 映射为逻辑 item/物理 child（幂等）。"""
        row = conn.execute(
            "SELECT task_id,generation,plan_json,state FROM task_action_attempts WHERE id=?",
            (int(attempt_id),),
        ).fetchone()
        if not row:
            raise sqlite3.IntegrityError("linked task attempt is missing")
        task_id, generation, plan_json, attempt_state = row
        try:
            plan = json.loads(str(plan_json or ""))
            children = plan["children"]
            if not isinstance(children, list) or not children:
                raise ValueError("children")
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise sqlite3.IntegrityError("task action plan cannot materialize") from exc
        for child in children:
            ordinal = int(child["ordinal"])
            canonical_json = json.dumps(
                self._phase2b_canonical_child(child), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            item = conn.execute(
                "SELECT id,canonical_json FROM task_action_items WHERE task_id=? AND ordinal=?",
                (int(task_id), ordinal),
            ).fetchone()
            if item:
                if str(item[1]) != canonical_json:
                    raise sqlite3.IntegrityError("task action logical item payload drift")
                item_id = int(item[0])
            else:
                item_id = int(conn.execute(
                    "INSERT INTO task_action_items(task_id,ordinal,canonical_json,created_at) "
                    "VALUES (?,?,?,?) RETURNING id",
                    (int(task_id), ordinal, canonical_json,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ).fetchone()[0])
            outbox = conn.execute(
                "SELECT action_id,status FROM send_outbox WHERE task_attempt_id=? AND ordinal=?",
                (int(attempt_id), ordinal),
            ).fetchone()
            action_id = str(child["action_id"])
            existing = conn.execute(
                "SELECT id,action_id FROM task_action_children WHERE attempt_id=? AND ordinal=?",
                (int(attempt_id), ordinal),
            ).fetchone()
            if existing:
                if str(existing[1]) != action_id:
                    raise sqlite3.IntegrityError("task action child identity drift")
                if outbox:
                    conn.execute(
                        "UPDATE task_action_children SET outbox_id=?,state=?,updated_at=? WHERE id=?",
                        (str(outbox[0]), str(outbox[1]),
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(existing[0])),
                    )
                self._ensure_task_action_projection_anchor_conn(
                    conn, attempt_id=attempt_id, ordinal=ordinal,
                    action_id=action_id,
                    schema_version=int(child["schema_version"]),
                )
                continue
            conn.execute(
                "INSERT INTO task_action_children(item_id,attempt_id,generation,ordinal,"
                "action_id,outbox_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (item_id, int(attempt_id), int(generation), ordinal, action_id,
                 str(outbox[0]) if outbox else "",
                 str(outbox[1]) if outbox else str(attempt_state),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._ensure_task_action_projection_anchor_conn(
                conn, attempt_id=attempt_id, ordinal=ordinal,
                action_id=action_id,
                schema_version=int(child["schema_version"]),
            )

    def _record_task_confirmation_conn(
            self, conn, *, outbox_id: str, message_ids=(),
            evidence_source: str = "outbox_settle", actor: str = "system",
            evidence: dict | None = None, confirmed_at: str = "") -> bool:
        """确认事实唯一写入点；先落事实，再允许 outbox 删除。"""
        if evidence_source not in {
            "outbox_settle", "late_settle", "known_confirmed_backfill",
        }:
            raise ValueError("invalid confirmation evidence source")
        row = conn.execute(
            "SELECT o.action_id,o.domain_action_id,o.task_attempt_id,o.ordinal,"
            "o.status,a.task_id,a.generation FROM send_outbox o "
            "JOIN task_action_attempts a ON a.id=o.task_attempt_id "
            "WHERE o.action_id=?",
            (str(outbox_id),),
        ).fetchone()
        if not row:
            raise ValueError("task confirmation outbox not found")
        if (evidence_source == "late_settle"
                and str(row[4]) not in {"uncertain", "failed", "dead"}):
            raise ValueError("late confirmation outbox is not terminal")
        self._materialize_task_action_attempt_conn(conn, int(row[2]))
        child = conn.execute(
            "SELECT item_id,action_id FROM task_action_children WHERE attempt_id=? AND ordinal=?",
            (int(row[2]), int(row[3])),
        ).fetchone()
        if not child or str(child[1]) != str(row[1]):
            raise ValueError("task confirmation identity mismatch")
        mids = _normalize_delivery_message_ids(message_ids)
        if not mids:
            raise ValueError("task confirmation requires message id evidence")
        now = confirmed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_action_confirmations "
            "(task_id,attempt_id,item_id,generation,ordinal,action_id,outbox_id,"
            "evidence_source,message_ids_json,evidence_json,confirmed_at,recorded_at,actor) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(row[5]), int(row[2]), int(child[0]), int(row[6]), int(row[3]),
             str(row[1]), str(outbox_id), evidence_source,
             json.dumps(mids, separators=(",", ":")),
             json.dumps(dict(evidence or {}), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")),
             str(now), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             str(actor or "system")),
        )
        conn.execute(
            "UPDATE task_action_children SET state='confirmed',outbox_id=?,updated_at=? "
            "WHERE attempt_id=? AND ordinal=?",
            (str(outbox_id), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             int(row[2]), int(row[3])),
        )
        return cur.rowcount == 1

    def _reduce_task_action_attempt_conn(self, conn, attempt_id: int,
                                         *, event_type: str = "",
                                         reason: str = "") -> str:
        """只归约单 generation；不会直接投影跨代 task。"""
        self._materialize_task_action_attempt_conn(conn, int(attempt_id))
        row = conn.execute(
            "SELECT state,task_id,generation,accounting_state FROM task_action_attempts WHERE id=?",
            (int(attempt_id),),
        ).fetchone()
        if not row:
            raise sqlite3.IntegrityError("linked task attempt is missing")
        old_state, task_id, generation, _accounting = row
        children = conn.execute(
            "SELECT ch.ordinal,ch.action_id,COALESCE(o.status,ch.state) "
            "FROM task_action_children ch LEFT JOIN send_outbox o "
            "ON o.action_id=ch.outbox_id WHERE ch.attempt_id=? ORDER BY ch.ordinal",
            (int(attempt_id),),
        ).fetchall()
        statuses = []
        for ordinal, action_id, status in children:
            confirmed = conn.execute(
                "SELECT 1 FROM task_action_confirmations WHERE attempt_id=? AND ordinal=?",
                (int(attempt_id), int(ordinal)),
            ).fetchone()
            effective = "confirmed" if confirmed else str(status)
            statuses.append(effective)
            conn.execute(
                "UPDATE task_action_children SET state=?,updated_at=? WHERE attempt_id=? AND ordinal=?",
                (effective, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 int(attempt_id), int(ordinal)),
            )
        open_states = {"pending", "sending"}
        if any(s in open_states for s in statuses):
            new_state = (
                "sending" if "sending" in statuses
                or ("confirmed" in statuses and "pending" in statuses)
                else "outbox_pending"
            )
        elif statuses and all(s == "confirmed" for s in statuses):
            new_state = "confirmed"
        elif "confirmed" in statuses and any(s in {"uncertain", "dead", "failed"} for s in statuses):
            new_state = "partial"
        elif "uncertain" in statuses:
            new_state = "uncertain"
        elif statuses and all(s in {"dead", "failed"} for s in statuses):
            new_state = "dead" if "dead" in statuses else "failed"
        elif statuses and all(s == "cancelled" for s in statuses):
            new_state = "cancelled"
        else:
            new_state = str(old_state)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if new_state != str(old_state):
            updates = ["state=?", "updated_at=?"]
            values = [new_state, now]
            if new_state in {"confirmed", "uncertain", "failed", "dead", "partial"}:
                updates.append("finalized_at=COALESCE(NULLIF(finalized_at,''),?)")
                values.append(now)
            values.append(int(attempt_id))
            conn.execute(f"UPDATE task_action_attempts SET {','.join(updates)} WHERE id=?", values)
        return new_state

    def record_task_confirmation(
            self, outbox_id: str, message_ids=(), *,
            evidence_source: str = "late_settle", actor: str = "system",
            evidence: dict | None = None) -> str:
        """拒绝不带平台能力的手工确认，避免伪造永久“已发送”事实。"""
        raise ValueError(
            "late confirmation requires a trusted platform callback"
        )

    def _record_task_confirmation_from_platform(
            self, outbox_id: str, message_ids=(), *,
            evidence_source: str = "late_settle", actor: str = "system",
            evidence: dict | None = None, _capability=None) -> str:
        """由已验证的平台适配器摄取迟到确认；不发送、不重放。"""
        if _capability is not _LATE_CONFIRMATION_CAPABILITY:
            raise ValueError(
                "late confirmation requires a trusted platform callback"
            )
        if evidence_source != "late_settle":
            raise ValueError("platform confirmation API only accepts late_settle")
        mids = _normalize_delivery_message_ids(message_ids)
        if not mids:
            raise ValueError("late confirmation requires message id evidence")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT o.action_id,o.task_attempt_id,o.ordinal,a.task_id,a.generation,o.status "
                "FROM send_outbox o JOIN task_action_attempts a ON a.id=o.task_attempt_id "
                "WHERE o.action_id=?", (str(outbox_id),),
            ).fetchone()
            if not row:
                conn.rollback()
                return "missing"
            if str(row[5]) not in {"uncertain", "failed", "dead"}:
                conn.rollback()
                return "not_late_confirmable"
            inserted = self._record_task_confirmation_conn(
                conn, outbox_id=str(outbox_id), message_ids=mids,
                evidence_source="late_settle", actor=actor, evidence=evidence,
            )
            # 迟到 API 只有平台消息号，没有完整 receipt，不能删除 outbox
            # 或声称本地投影已完成；保留为 known-confirmed 待修复，避免
            # dead outbox + confirmed attempt 形成健康不变量冲突。
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE send_outbox SET status='confirmed_unaccounted',"
                "confirmed_message_ids=?,confirmed_at=?,"
                "projection_error='LATE_CONFIRMATION_NO_RECEIPT',"
                "last_error='LATE_CONFIRMATION_NO_RECEIPT',updated_at=? "
                "WHERE action_id=? AND status IN ('uncertain','failed','dead',"
                "'confirmed_unaccounted')",
                (json.dumps(mids, separators=(",", ":")), now, now,
                 str(outbox_id)),
            )
            # 迟到确认先于后代 claim 到达时，后代尚未产生外部副作用，安全取消；
            # 已 claim 的行保留并由 duplicate trigger 记录风险。
            descendants = conn.execute(
                "SELECT DISTINCT a.id FROM task_action_attempts a "
                "JOIN send_outbox o ON o.task_attempt_id=a.id "
                "WHERE a.task_id=? AND a.generation>? AND o.ordinal=? "
                "AND o.status='pending' AND o.attempts=0",
                (int(row[3]), int(row[4]), int(row[2])),
            ).fetchall()
            for (descendant_id,) in descendants:
                conn.execute(
                    "UPDATE send_outbox SET status='cancelled',last_error=? "
                    "WHERE task_attempt_id=? AND ordinal=? AND status='pending' AND attempts=0",
                    ("cancelled_by_late_confirmation", int(descendant_id), int(row[2])),
                )
                self._settle_linked_task_attempt_conn(
                    conn, attempt_id=int(descendant_id), state="cancelled",
                    error="cancelled_by_late_confirmation",
                    event_type="late_confirmation_cancelled_descendant",
                    reason="late_confirmation_before_claim",
                )
            self._settle_linked_task_attempt_conn(
                conn, attempt_id=int(row[1]), state="confirmed",
                accounting_state="pending_repair",
                error="LATE_CONFIRMATION_NO_RECEIPT",
                outbox_id=str(outbox_id), domain_action_id="",
                event_type="late_confirmation_recorded",
                reason="late_delivery_evidence",
            )
            self._reduce_task_action_task_conn(conn, int(row[3]))
            conn.commit()
            return "confirmed" if inserted else "duplicate"

    def _reduce_task_action_task_conn(self, conn, task_id: int) -> str:
        """跨 generation 归约 task；current pointer 永不回拨。"""
        attempts = conn.execute(
            "SELECT id,generation,state FROM task_action_attempts WHERE task_id=? ORDER BY generation",
            (int(task_id),),
        ).fetchall()
        if not attempts:
            return str(conn.execute("SELECT status FROM tasks WHERE id=?", (int(task_id),)).fetchone()[0])
        current_id, current_state, current_status = conn.execute(
            "SELECT t.current_attempt_id,COALESCE(a.state,''),t.status "
            "FROM tasks t LEFT JOIN task_action_attempts a "
            "ON a.id=t.current_attempt_id WHERE t.id=?", (int(task_id),)
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) FROM send_outbox WHERE task_attempt_id IN "
            "(SELECT id FROM task_action_attempts WHERE task_id=?) "
            "AND status IN ('pending','sending')", (int(task_id),)
        ).fetchone()[0]
        if open_count:
            new_status = "sending"
        else:
            items = conn.execute(
                "SELECT ordinal FROM task_action_items WHERE task_id=? ORDER BY ordinal",
                (int(task_id),),
            ).fetchall()
            outcomes = []
            for (ordinal,) in items:
                confirmed = conn.execute(
                    "SELECT 1 FROM task_action_confirmations WHERE task_id=? AND ordinal=?",
                    (int(task_id), int(ordinal)),
                ).fetchone()
                if confirmed:
                    outcomes.append("confirmed")
                    continue
                occurrence = conn.execute(
                    "SELECT ch.state FROM task_action_children ch "
                    "JOIN task_action_attempts a ON a.id=ch.attempt_id "
                    "WHERE a.task_id=? AND ch.ordinal=? ORDER BY a.generation DESC LIMIT 1",
                    (int(task_id), int(ordinal)),
                ).fetchone()
                outcomes.append(str(occurrence[0]) if occurrence else "uncertain")
            if outcomes and all(s == "confirmed" for s in outcomes):
                # Phase 2b 的 done 是逻辑 item 投影：current pointer 仍指向
                # 最新 generation，但该 generation 可以因旧代迟到确认而
                # cancelled/uncertain/dead，不得因此回滚已确认的逻辑结果。
                new_status = "done"
            elif "confirmed" in outcomes:
                new_status = "partial"
            elif "uncertain" in outcomes:
                new_status = "uncertain"
            elif outcomes and all(s in {"failed", "dead", "cancelled"} for s in outcomes):
                new_status = "failed"
            else:
                new_status = str(current_status)
        if str(new_status) != str(current_status):
            conn.execute(
                "UPDATE tasks SET status=? WHERE id=? AND current_attempt_id IS ?",
                (new_status, int(task_id), current_id),
            )
        return new_status

    def get_task_ordinal_ledger(self, task_id: int) -> list[dict]:
        """返回跨代逻辑 item 的只读确认/状态账本。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT i.ordinal,i.canonical_json,ch.generation,ch.attempt_id,ch.state,"
                "ch.action_id,c.message_ids_json,c.confirmed_at,c.evidence_source "
                "FROM task_action_items i JOIN task_action_children ch ON ch.item_id=i.id "
                "LEFT JOIN task_action_confirmations c ON c.action_id=ch.action_id "
                "WHERE i.task_id=? ORDER BY i.ordinal,ch.generation",
                (int(task_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_confirmed_projection_state_conn(
            self, conn, *, outbox_id: str, state: str, receipt: dict,
            confirmed_at: str, error_code: str, existing_json: str = "",
            incoming_json: str = "") -> None:
        """在调用方事务内保存 known-confirmed，并同步 linked task。"""
        if state not in {"confirmed_unaccounted", "confirmed_conflict"}:
            raise ValueError("invalid confirmed projection terminal state")
        normalized_message_ids = _normalize_delivery_message_ids(
            (receipt or {}).get("message_ids") or ()
        )
        row = conn.execute(
            "SELECT task_attempt_id,domain_action_id,ordinal FROM send_outbox "
            "WHERE action_id=? AND status IN "
            "('sending','confirmed_unaccounted','confirmed_conflict')",
            (str(outbox_id),),
        ).fetchone()
        if not row:
            return
        attempt_id = row[0]
        if not normalized_message_ids:
            # 没有平台消息号就没有 known-confirmed 证据；降为 uncertain，
            # 保留 outbox 以便人工核验，禁止创建 confirmation/fact。
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE send_outbox SET status='uncertain',"
                "confirmed_message_ids='[]',confirmed_at='',"
                "projection_error='',last_error=?,updated_at=? "
                "WHERE action_id=? AND status IN "
                "('sending','confirmed_unaccounted','confirmed_conflict')",
                ("MESSAGE_ID_UNCONFIRMED", now, str(outbox_id)),
            )
            if attempt_id is not None:
                self._settle_linked_task_attempt_conn(
                    conn, attempt_id=int(attempt_id), state="uncertain",
                    accounting_state="pending_repair",
                    error="MESSAGE_ID_UNCONFIRMED", outbox_id=str(outbox_id),
                    domain_action_id=str(row[1] or ""),
                    event_type="known_confirmation_rejected",
                    reason="missing_platform_message_id",
                )
            return
        message_ids_json = json.dumps(
            normalized_message_ids, separators=(",", ":"),
        )
        domain_action_id = str(row[1] or receipt.get("action_id") or "")
        if attempt_id is not None:
            existing_confirmation = conn.execute(
                "SELECT message_ids_json,evidence_source FROM "
                "task_action_confirmations WHERE attempt_id=? AND ordinal=? "
                "AND action_id=? AND outbox_id=?",
                (int(attempt_id), int(row[2]), domain_action_id, str(outbox_id)),
            ).fetchone()
            if existing_confirmation:
                try:
                    existing_message_ids = _normalize_delivery_message_ids(
                        json.loads(str(existing_confirmation[0] or "[]"))
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_message_ids = []
                if existing_message_ids != normalized_message_ids:
                    # confirmation 是 append-only；同一物理动作出现不同平台
                    # 消息号时冻结为冲突，不能被 INSERT OR IGNORE 静默吞掉。
                    state = "confirmed_conflict"
                    error_code = "CONFIRMATION_EVIDENCE_CONFLICT"
                    existing_json = str(existing_confirmation[0] or "[]")
                    incoming_json = message_ids_json
            else:
                self._record_task_confirmation_conn(
                    conn, outbox_id=str(outbox_id),
                    message_ids=receipt.get("message_ids") or (),
                    evidence_source="outbox_settle", actor="outbox_worker",
                    evidence={"state": state, "error_code": str(error_code or ""),
                              "receipt": dict(receipt or {})},
                    confirmed_at=str(confirmed_at),
                )
        conn.execute(
            "UPDATE send_outbox SET status=?,confirmed_message_ids=?,"
            "confirmed_at=?,projection_error=?,last_error=?,updated_at=? "
            "WHERE action_id=? AND status IN "
            "('sending','confirmed_unaccounted','confirmed_conflict')",
            (state, message_ids_json, str(confirmed_at), str(error_code)[:128],
             str(error_code)[:500], str(confirmed_at), str(outbox_id)),
        )
        if state == "confirmed_conflict":
            conn.execute(
                "INSERT INTO action_projection_conflicts "
                "(domain_action_id,outbox_id,existing_json,incoming_json,detected_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(outbox_id) DO UPDATE SET "
                "existing_json=excluded.existing_json,"
                "incoming_json=excluded.incoming_json,detected_at=excluded.detected_at",
                (domain_action_id, str(outbox_id), str(existing_json or "{}"),
                 str(incoming_json or "{}"), str(confirmed_at)),
            )
        if attempt_id is not None:
            self._settle_linked_task_attempt_conn(
                conn, attempt_id=int(attempt_id), state="confirmed",
                accounting_state=(
                    "conflict" if state == "confirmed_conflict"
                    else "pending_repair"
                ),
                error=error_code, outbox_id=str(outbox_id),
                domain_action_id=domain_action_id,
                event_type="known_confirmed_frozen",
                reason=state,
            )

    def _record_confirmed_projection_state(self, *, outbox_id: str, state: str,
                                           receipt: dict, confirmed_at: str,
                                           error_code: str,
                                           existing_json: str = "",
                                           incoming_json: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._record_confirmed_projection_state_conn(
                conn, outbox_id=outbox_id, state=state, receipt=receipt,
                confirmed_at=confirmed_at, error_code=error_code,
                existing_json=existing_json, incoming_json=incoming_json,
            )
            conn.commit()

    def _settle_task_send_outbox_conn(
            self, conn, row, terminal_state: str, *, message_ids=(),
            error_code: str = "", error_detail: str = "",
            max_attempts: int = 3, retry_delay_seconds: int = 0) -> str:
        """提交 task-linked 单动作 outbox，并在同一事务推进 attempt/task。"""
        from .action_contract import finalize_action_receipt_template

        normalized_message_ids = _normalize_delivery_message_ids(message_ids)
        if terminal_state == "confirmed" and not normalized_message_ids:
            terminal_state = "uncertain"
            error_code = str(error_code or "MESSAGE_ID_UNCONFIRMED")
            error_detail = str(error_detail or "MESSAGE_ID_UNCONFIRMED")
        message_ids = normalized_message_ids

        outbox_id = str(row["action_id"])
        attempt_id = int(row["task_attempt_id"])
        domain_action_id = str(row["domain_action_id"] or "")
        attempts = int(row["attempts"])
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_retry = (now_dt + timedelta(
            seconds=max(0, int(retry_delay_seconds))
        )).strftime("%Y-%m-%d %H:%M:%S")
        final_state = terminal_state
        if terminal_state == "failed":
            final_state = (
                "dead" if attempts >= max(1, int(max_attempts)) else "pending"
            )

        if final_state == "pending":
            conn.execute(
                "UPDATE send_outbox SET status='pending',next_retry_at=?,"
                "last_error=?,updated_at=? WHERE action_id=? AND status='sending'",
                (next_retry, str(error_detail or error_code or "SEND_FAILED")[:500],
                 now, outbox_id),
            )
            self._settle_linked_task_attempt_conn(
                conn, attempt_id=attempt_id, state="outbox_pending",
                error=error_detail or error_code, outbox_id=outbox_id,
                domain_action_id=domain_action_id,
                event_type="outbox_retry_scheduled", reason="retryable_failure",
            )
            conn.commit()
            return "pending"

        try:
            template = json.loads(str(row["receipt_template"] or ""))
            receipt = finalize_action_receipt_template(
                template,
                status="failed" if final_state == "dead" else final_state,
                message_ids=message_ids, error_code=error_code,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid_receipt = {
                "action_id": domain_action_id,
                "message_ids": [
                    int(item) for item in message_ids
                    if not isinstance(item, bool) and isinstance(item, int)
                    and item != 0
                ],
            }
            if terminal_state == "confirmed":
                self._record_confirmed_projection_state_conn(
                    conn, outbox_id=outbox_id,
                    state="confirmed_unaccounted", receipt=invalid_receipt,
                    confirmed_at=now, error_code="RECEIPT_TEMPLATE_INVALID",
                )
                conn.commit()
                logger.error(
                    "task outbox known-confirmed 模板损坏，已冻结: "
                    "outbox_id=%s error=%s", outbox_id, type(exc).__name__,
                )
                return "confirmed_unaccounted"
            closed_state = "uncertain" if final_state == "uncertain" else "dead"
            conn.execute(
                "UPDATE send_outbox SET status=?,last_error=?,updated_at=? "
                "WHERE action_id=? AND status='sending'",
                (closed_state, "RECEIPT_TEMPLATE_INVALID", now, outbox_id),
            )
            self._settle_linked_task_attempt_conn(
                conn, attempt_id=attempt_id, state=closed_state,
                error="RECEIPT_TEMPLATE_INVALID", outbox_id=outbox_id,
                domain_action_id=domain_action_id,
                event_type="outbox_settled", reason="receipt_template_invalid",
            )
            conn.commit()
            return closed_state

        if (terminal_state == "confirmed" and domain_action_id
                and str(receipt.get("action_id") or "") != domain_action_id):
            self._record_confirmed_projection_state_conn(
                conn, outbox_id=outbox_id, state="confirmed_conflict",
                receipt=receipt, confirmed_at=now,
                error_code="RECEIPT_DOMAIN_ID_MISMATCH",
                existing_json=json.dumps(
                    {"domain_action_id": domain_action_id}, separators=(",", ":"),
                ),
                incoming_json=json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            conn.commit()
            return "confirmed_conflict"

        if terminal_state == "confirmed":
            self._record_task_confirmation_conn(
                conn, outbox_id=outbox_id, message_ids=message_ids,
                evidence_source="outbox_settle", actor="outbox_worker",
                evidence={"receipt": receipt}, confirmed_at=now,
            )
            conn.execute("SAVEPOINT task_confirmed_projection")
            try:
                projection_state = self._commit_confirmed_action_conn(
                    conn, outbox_id=outbox_id, receipt=receipt,
                    confirmed_at=now,
                )
                conn.execute("RELEASE SAVEPOINT task_confirmed_projection")
            except ConfirmedProjectionConflict as exc:
                conn.execute("ROLLBACK TO SAVEPOINT task_confirmed_projection")
                conn.execute("RELEASE SAVEPOINT task_confirmed_projection")
                self._record_confirmed_projection_state_conn(
                    conn, outbox_id=outbox_id, state="confirmed_conflict",
                    receipt=receipt, confirmed_at=now, error_code=exc.code,
                    existing_json=exc.existing, incoming_json=exc.incoming,
                )
                conn.commit()
                return "confirmed_conflict"
            except ConfirmedProjectionError as exc:
                conn.execute("ROLLBACK TO SAVEPOINT task_confirmed_projection")
                conn.execute("RELEASE SAVEPOINT task_confirmed_projection")
                self._record_confirmed_projection_state_conn(
                    conn, outbox_id=outbox_id,
                    state="confirmed_unaccounted", receipt=receipt,
                    confirmed_at=now, error_code=exc.code,
                )
                conn.commit()
                return "confirmed_unaccounted"
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT task_confirmed_projection")
                conn.execute("RELEASE SAVEPOINT task_confirmed_projection")
                self._record_confirmed_projection_state_conn(
                    conn, outbox_id=outbox_id,
                    state="confirmed_unaccounted", receipt=receipt,
                    confirmed_at=now, error_code="PROJECTION_LOCAL_ERROR",
                )
                conn.commit()
                logger.exception(
                    "task outbox known-confirmed 本地投影异常: outbox_id=%s",
                    outbox_id,
                )
                return "confirmed_unaccounted"

            conn.execute(
                "DELETE FROM send_outbox WHERE action_id=? AND status='sending'",
                (outbox_id,),
            )
            self._settle_linked_task_attempt_conn(
                conn, attempt_id=attempt_id, state="confirmed",
                accounting_state="clean", outbox_id=outbox_id,
                domain_action_id=domain_action_id,
                event_type="outbox_confirmed", reason="platform_confirmed",
            )
            conn.commit()
            return projection_state

        try:
            self._enqueue_action_receipt_conn(conn, receipt)
        except (ValueError, sqlite3.IntegrityError):
            error_code = "ACTION_RECEIPT_CONFLICT"
        closed_state = "uncertain" if final_state == "uncertain" else "dead"
        conn.execute(
            "UPDATE send_outbox SET status=?,next_retry_at=?,last_error=?,"
            "updated_at=? WHERE action_id=? AND status='sending'",
            (closed_state, next_retry, str(
                error_detail or error_code or "SEND_FAILED"
            )[:500], now, outbox_id),
        )
        self._settle_linked_task_attempt_conn(
            conn, attempt_id=attempt_id, state=closed_state,
            error=error_detail or error_code, outbox_id=outbox_id,
            domain_action_id=domain_action_id, event_type="outbox_settled",
            reason=("delivery_uncertain" if closed_state == "uncertain"
                    else "delivery_dead"),
        )
        conn.commit()
        return closed_state

    def settle_send_outbox(self, outbox_id: str, terminal_state: str, *,
                           message_ids=(), error_code: str = "",
                           error_detail: str = "",
                           max_attempts: int = 3,
                           retry_delay_seconds: int = 0) -> str:
        """提交发送终局；known-confirmed 的本地失败永不降级或重发。"""
        if terminal_state not in {"confirmed", "uncertain", "failed"}:
            raise ValueError(f"unsupported outbox terminal state: {terminal_state}")
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_retry = (now_dt + timedelta(
            seconds=max(0, int(retry_delay_seconds))
        )).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM send_outbox "
                "WHERE action_id=? AND status='sending'", (str(outbox_id),),
            ).fetchone()
            if not row:
                conn.rollback()
                return "missing"
            normalized_message_ids = _normalize_delivery_message_ids(message_ids)
            if (terminal_state == "confirmed"
                    and row["task_attempt_id"] is not None
                    and not normalized_message_ids):
                terminal_state = "uncertain"
                error_code = str(error_code or "MESSAGE_ID_UNCONFIRMED")
                error_detail = str(error_detail or "MESSAGE_ID_UNCONFIRMED")
            message_ids = normalized_message_ids
            if row["task_attempt_id"] is not None:
                return self._settle_task_send_outbox_conn(
                    conn, row, terminal_state, message_ids=message_ids,
                    error_code=error_code, error_detail=error_detail,
                    max_attempts=max_attempts,
                    retry_delay_seconds=retry_delay_seconds,
                )
            attempts = int(row["attempts"])
            template_json = str(row["receipt_template"] or "")
            stored_domain_action_id = str(row["domain_action_id"] or "")
            final_state = terminal_state
            if terminal_state == "failed":
                final_state = "dead" if attempts >= max(1, int(max_attempts)) else "pending"

            receipt = None
            if template_json and final_state in {"confirmed", "uncertain", "dead"}:
                from .action_contract import finalize_action_receipt_template
                try:
                    receipt = finalize_action_receipt_template(
                        json.loads(template_json),
                        status="failed" if final_state == "dead" else final_state,
                        message_ids=message_ids,
                        error_code=error_code,
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    if terminal_state == "confirmed":
                        conn.rollback()
                        invalid_receipt = {
                            "action_id": stored_domain_action_id,
                            "message_ids": [
                                int(item) for item in message_ids
                                if not isinstance(item, bool)
                                and isinstance(item, int) and item != 0
                            ],
                        }
                        self._record_confirmed_projection_state(
                            outbox_id=str(outbox_id),
                            state="confirmed_unaccounted",
                            receipt=invalid_receipt,
                            confirmed_at=now,
                            error_code="RECEIPT_TEMPLATE_INVALID",
                        )
                        logger.error(
                            "known-confirmed outbox 模板损坏，已冻结本地修复: "
                            "outbox_id=%s error=%s",
                            outbox_id, type(exc).__name__,
                        )
                        return "confirmed_unaccounted"
                    conn.execute(
                        "UPDATE send_outbox SET status=?,last_error=?,updated_at=? "
                        "WHERE action_id=? AND status='sending'",
                        (final_state, "RECEIPT_TEMPLATE_INVALID", now,
                         str(outbox_id)),
                    )
                    conn.commit()
                    logger.error(
                        "outbox 回执模板损坏，已隔离: outbox_id=%s error=%s",
                        outbox_id, type(exc).__name__,
                    )
                    return "receipt_error"

            if (terminal_state == "confirmed" and receipt is not None
                    and stored_domain_action_id
                    and str(receipt.get("action_id") or "")
                    != stored_domain_action_id):
                # receipt_template 是入队时冻结的 domain 身份；即使本地
                # 数据被篡改成另一个合法模板，也不能把该物理 outbox 改绑。
                incoming = json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                conn.rollback()
                self._record_confirmed_projection_state(
                    outbox_id=str(outbox_id), state="confirmed_conflict",
                    receipt=receipt, confirmed_at=now,
                    error_code="RECEIPT_DOMAIN_ID_MISMATCH",
                    existing_json=json.dumps(
                        {"domain_action_id": stored_domain_action_id},
                        separators=(",", ":"),
                    ),
                    incoming_json=incoming,
                )
                logger.error(
                    "outbox receipt domain 身份与入队快照不一致，禁止归账/重发: "
                    "outbox_id=%s",
                    outbox_id,
                )
                return "confirmed_conflict"

            stored_error = str(error_detail or error_code or "")[:500]
            if terminal_state == "confirmed" and receipt is not None and int(
                    receipt.get("schema_version", 1)) >= 2:
                try:
                    projection_state = self._commit_confirmed_action_conn(
                        conn, outbox_id=str(outbox_id), receipt=receipt,
                        confirmed_at=now,
                    )
                    conn.execute(
                        "DELETE FROM send_outbox WHERE action_id=? AND status='sending'",
                        (str(outbox_id),),
                    )
                    conn.commit()
                    return projection_state
                except ConfirmedProjectionConflict as exc:
                    conn.rollback()
                    self._record_confirmed_projection_state(
                        outbox_id=str(outbox_id), state="confirmed_conflict",
                        receipt=receipt, confirmed_at=now,
                        error_code=exc.code, existing_json=exc.existing,
                        incoming_json=exc.incoming,
                    )
                    logger.error(
                        "known-confirmed 永久事实冲突，禁止重发: outbox_id=%s",
                        outbox_id,
                    )
                    return "confirmed_conflict"
                except ConfirmedProjectionError as exc:
                    conn.rollback()
                    self._record_confirmed_projection_state(
                        outbox_id=str(outbox_id), state="confirmed_unaccounted",
                        receipt=receipt, confirmed_at=now, error_code=exc.code,
                    )
                    logger.error(
                        "known-confirmed 本地归账未完成: outbox_id=%s error=%s",
                        outbox_id, exc.code,
                    )
                    return "confirmed_unaccounted"
                except Exception as exc:
                    conn.rollback()
                    self._record_confirmed_projection_state(
                        outbox_id=str(outbox_id), state="confirmed_unaccounted",
                        receipt=receipt, confirmed_at=now,
                        error_code="PROJECTION_LOCAL_ERROR",
                    )
                    logger.exception(
                        "known-confirmed 本地归账异常，已冻结且禁止重发: "
                        "outbox_id=%s error=%s",
                        outbox_id, type(exc).__name__,
                    )
                    return "confirmed_unaccounted"

            if receipt is not None:
                try:
                    self._enqueue_action_receipt_conn(conn, receipt)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    if terminal_state == "confirmed":
                        existing = conn.execute(
                            "SELECT receipt_json FROM action_receipt_mailbox "
                            "WHERE scope_id=? AND action_id=?",
                            (str(receipt.get("scope_id") or ""),
                             str(receipt.get("action_id") or "")),
                        ).fetchone()
                        incoming = json.dumps(
                            receipt, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        )
                        conn.rollback()
                        self._record_confirmed_projection_state(
                            outbox_id=str(outbox_id), state="confirmed_conflict",
                            receipt=receipt, confirmed_at=now,
                            error_code="ACTION_RECEIPT_CONFLICT",
                            existing_json=str(existing[0]) if existing else "{}",
                            incoming_json=incoming,
                        )
                        logger.error(
                            "known-confirmed 回执冲突，已冻结且禁止重发: "
                            "outbox_id=%s error=%s",
                            outbox_id, type(exc).__name__,
                        )
                        return "confirmed_conflict"
                    conn.execute(
                        "UPDATE send_outbox SET status='uncertain',last_error=?,"
                        "updated_at=? WHERE action_id=? AND status='sending'",
                        ("ACTION_RECEIPT_CONFLICT", now, str(outbox_id)),
                    )
                    conn.commit()
                    logger.error(
                        "outbox 终态与既有回执冲突，已隔离: outbox_id=%s error=%s",
                        outbox_id, type(exc).__name__,
                    )
                    return "receipt_conflict"

            if terminal_state == "confirmed":
                conn.execute(
                    "DELETE FROM send_outbox WHERE action_id=? AND status='sending'",
                    (str(outbox_id),),
                )
            elif terminal_state == "uncertain":
                conn.execute(
                    "UPDATE send_outbox SET status='uncertain',last_error=?,"
                    "updated_at=? WHERE action_id=? AND status='sending'",
                    (stored_error or "unconfirmed delivery", now,
                     str(outbox_id)),
                )
            else:
                conn.execute(
                    "UPDATE send_outbox SET status=?,next_retry_at=?,last_error=?,"
                    "updated_at=? WHERE action_id=? AND status='sending'",
                    (final_state, next_retry, stored_error or "SEND_FAILED",
                     now, str(outbox_id)),
                )

            conn.commit()
            return final_state

    def repair_confirmed_projection(self, outbox_id: str) -> str:
        """只修 SQLite 归账；不得调用或持有任何 QQ/NapCat 发送能力。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM send_outbox WHERE action_id=? AND status IN "
                "('confirmed_unaccounted','confirmed_conflict')",
                (str(outbox_id),),
            ).fetchone()
            if not row:
                conn.rollback()
                return "missing"
            original_state = str(row["status"] or "")
            receipt = None
            stored_domain_action_id = str(row["domain_action_id"] or "").strip()
            try:
                confirmed_message_ids = json.loads(
                    str(row["confirmed_message_ids"] or "[]")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                confirmed_message_ids = []
            confirmed_message_ids = _normalize_delivery_message_ids(
                confirmed_message_ids
            )
            if not confirmed_message_ids:
                # 平台没有可核验的消息号时，永远不能补写永久事实；保留
                # known-confirmed 行待人工核验，而不是把空证据升级为 confirmed。
                conn.rollback()
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self._connect() as retry_conn:
                    retry_conn.execute(
                        "UPDATE send_outbox SET projection_error=?,last_error=?,"
                        "updated_at=? WHERE action_id=? AND status=?",
                        ("MESSAGE_ID_UNCONFIRMED", "MESSAGE_ID_UNCONFIRMED", now,
                         str(outbox_id), original_state),
                    )
                    retry_conn.commit()
                return original_state
            try:
                from .action_contract import finalize_action_receipt_template
                receipt = finalize_action_receipt_template(
                    json.loads(str(row["receipt_template"] or "")),
                    status="confirmed",
                    message_ids=confirmed_message_ids,
                )
                if (stored_domain_action_id
                        and receipt.get("action_id") != stored_domain_action_id):
                    raise ConfirmedProjectionConflict(
                        json.dumps(
                            {"domain_action_id": stored_domain_action_id},
                            separators=(",", ":"),
                        ),
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")),
                    )
                result = self._commit_confirmed_action_conn(
                    conn, outbox_id=str(outbox_id), receipt=receipt,
                    confirmed_at=str(row["confirmed_at"] or "") or
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    allow_negative_upgrade=True,
                )
                if row["task_attempt_id"] is not None:
                    self._record_task_confirmation_conn(
                        conn, outbox_id=str(outbox_id),
                        message_ids=confirmed_message_ids,
                        evidence_source="known_confirmed_backfill", actor="repair_worker",
                        evidence={"source": "projection_repair", "receipt": receipt},
                        confirmed_at=str(row["confirmed_at"] or "") or
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                conn.execute(
                    "DELETE FROM send_outbox WHERE action_id=? AND status IN "
                    "('confirmed_unaccounted','confirmed_conflict')",
                    (str(outbox_id),),
                )
                if row["task_attempt_id"] is not None:
                    self._settle_linked_task_attempt_conn(
                        conn, attempt_id=int(row["task_attempt_id"]),
                        state="confirmed", accounting_state="clean",
                        outbox_id=str(outbox_id),
                        domain_action_id=stored_domain_action_id,
                        event_type="projection_repaired",
                        reason="db_only_projection_repair",
                    )
                conn.commit()
                return result
            except ConfirmedProjectionConflict as exc:
                conn.rollback()
                if receipt is None:
                    if original_state == "confirmed_conflict":
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        with self._connect() as retry_conn:
                            retry_conn.execute(
                                "UPDATE send_outbox SET projection_error=?,"
                                "last_error=?,updated_at=? WHERE action_id=? "
                                "AND status='confirmed_conflict'",
                                ("PROJECTION_CONFLICT_BEFORE_RECEIPT",
                                 "PROJECTION_CONFLICT_BEFORE_RECEIPT", now,
                                 str(outbox_id)),
                            )
                            retry_conn.commit()
                        return "confirmed_conflict"
                    # finalize/反序列化阶段提前抛出同类异常时尚未得到可投影
                    # receipt，只能保留平台确认的最小快照，不能引用未绑定变量
                    # 或伪造 immutable 冲突。
                    fallback_receipt = {
                        "action_id": stored_domain_action_id,
                        "message_ids": confirmed_message_ids,
                    }
                    self._record_confirmed_projection_state(
                        outbox_id=str(outbox_id), state="confirmed_unaccounted",
                        receipt=fallback_receipt,
                        confirmed_at=str(row["confirmed_at"] or "") or
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        error_code="PROJECTION_CONFLICT_BEFORE_RECEIPT",
                    )
                    return "confirmed_unaccounted"
                self._record_confirmed_projection_state(
                    outbox_id=str(outbox_id), state="confirmed_conflict",
                    receipt=receipt,
                    confirmed_at=str(row["confirmed_at"] or "") or
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    error_code=exc.code, existing_json=exc.existing,
                    incoming_json=exc.incoming,
                )
                return "confirmed_conflict"
            except Exception as exc:
                conn.rollback()
                error_code = getattr(exc, "code", "PROJECTION_LOCAL_ERROR")
                if original_state == "confirmed_conflict":
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with self._connect() as retry_conn:
                        retry_conn.execute(
                            "UPDATE send_outbox SET projection_error=?,"
                            "last_error=?,updated_at=? WHERE action_id=? "
                            "AND status='confirmed_conflict'",
                            (str(error_code)[:128], str(error_code)[:500], now,
                             str(outbox_id)),
                        )
                        retry_conn.commit()
                    return "confirmed_conflict"
                fallback_receipt = receipt or {
                    "action_id": stored_domain_action_id,
                    "message_ids": confirmed_message_ids,
                }
                self._record_confirmed_projection_state(
                    outbox_id=str(outbox_id), state="confirmed_unaccounted",
                    receipt=fallback_receipt,
                    confirmed_at=str(row["confirmed_at"] or "") or
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    error_code=error_code,
                )
                return "confirmed_unaccounted"

    @staticmethod
    def _window_event_dict(row) -> dict:
        return {
            "id": int(row[0]),
            "event_key": str(row[1]),
            "domain_action_id": str(row[2]),
            "scope_id": str(row[3]),
            "channel": str(row[4]),
            "actor_kind": str(row[5]),
            "conversation_user_id": str(row[6]),
            "group_id": str(row[7] or ""),
            "chat_log_id": int(row[8]),
            "reply_text": str(row[9]),
            "occurred_at": str(row[10]),
        }

    def list_conversation_window_events_after(
            self, after_id: int, limit: int = 200) -> list[dict]:
        """按 durable high-water 读取窗口增量；只返回明确白名单字段。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,event_key,domain_action_id,scope_id,channel,actor_kind,"
                "conversation_user_id,group_id,chat_log_id,text,occurred_at "
                "FROM conversation_window_events WHERE id>? ORDER BY id LIMIT ?",
                (max(0, int(after_id)), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._window_event_dict(row) for row in rows]

    def get_conversation_window_rebuild_snapshot(
            self, group_occurred_since: str,
            private_per_user: int = 5) -> dict:
        """同一读事务冻结 cursor，并返回活跃群窗和每位用户私聊尾部。"""
        fields = (
            "id,event_key,domain_action_id,scope_id,channel,actor_kind,"
            "conversation_user_id,group_id,chat_log_id,text,occurred_at"
        )
        with self._connect() as conn:
            conn.execute("BEGIN")
            cursor = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM conversation_window_events"
            ).fetchone()[0] or 0)
            group_rows = conn.execute(
                f"SELECT {fields} FROM conversation_window_events "
                "WHERE id<=? AND channel='group' AND occurred_at>=? ORDER BY id",
                (cursor, str(group_occurred_since)),
            ).fetchall()
            private_rows = conn.execute(
                "WITH ranked AS ("
                f"SELECT {fields},ROW_NUMBER() OVER ("
                "PARTITION BY conversation_user_id ORDER BY id DESC) AS rn "
                "FROM conversation_window_events WHERE id<=? AND channel='private'"
                ") SELECT id,event_key,domain_action_id,scope_id,channel,actor_kind,"
                "conversation_user_id,group_id,chat_log_id,text,occurred_at "
                "FROM ranked WHERE rn<=? ORDER BY id",
                (cursor, max(1, min(int(private_per_user), 20))),
            ).fetchall()
            conn.commit()
        events = [
            self._window_event_dict(row) for row in group_rows + private_rows
        ]
        events.sort(key=lambda item: item["id"])
        return {"cursor": cursor, "events": events}

    def mark_inbound_event_executing(self, event_key: str) -> bool:
        """回调调用前落盘；此后崩溃结果不明，只能转 uncertain。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE inbound_events SET status='executing',"
                "updated_at=datetime('now','localtime') "
                "WHERE event_key=? AND status='claimed'",
                (str(event_key),),
            )
            conn.commit()
        return cur.rowcount == 1

    def complete_inbound_event(self, event_key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE inbound_events SET status='processed',last_error='',"
                "updated_at=datetime('now','localtime') "
                "WHERE event_key=? AND status='executing'",
                (str(event_key),),
            )
            conn.commit()
        return cur.rowcount == 1

    def fail_inbound_event(self, event_key: str, error: str) -> bool:
        """回调明确抛错仍可能已有部分副作用；保留 failed 等人工判断，不盲重放。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE inbound_events SET status='failed',last_error=?,"
                "updated_at=datetime('now','localtime') "
                "WHERE event_key=? AND status IN ('claimed','executing')",
                (str(error or "callback failed")[:500], str(event_key)),
            )
            conn.commit()
        return cur.rowcount == 1

    def recover_inbound_events_after_restart(self) -> dict[str, int]:
        """claimed 尚未开始回调，可安全重试；executing 结果未知，禁止重放。"""
        with self._connect() as conn:
            claimed = conn.execute(
                "UPDATE inbound_events SET status='received',"
                "last_error='process restarted before callback',"
                "updated_at=datetime('now','localtime') WHERE status='claimed'"
            ).rowcount
            uncertain = conn.execute(
                "UPDATE inbound_events SET status='uncertain',"
                "last_error='process restarted during callback',"
                "updated_at=datetime('now','localtime') WHERE status='executing'"
            ).rowcount
            conn.commit()
        return {"received": int(claimed), "uncertain": int(uncertain)}

    def get_inbound_event_health(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) FROM inbound_events GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in self._INBOUND_STATUSES}
        unknown = 0
        for status, count in rows:
            status = str(status or "")
            if status in counts:
                counts[status] += int(count or 0)
            else:
                unknown += int(count or 0)
        counts["unknown"] = unknown
        counts["open"] = (
            counts["received"] + counts["claimed"] + counts["executing"]
        )
        counts["needs_review"] = (
            counts["failed"] + counts["uncertain"] + unknown
        )
        return counts

    def query_memories(self, qq_id: str = "", limit: int | None = 200,
                       include_retracted: bool = False, target_qq: str = "",
                       trusted_only: bool = False,
                       uncertain_only: bool = False,
                       min_importance: int = 0, min_confidence: float = 0.0,
                       source_group_id: str | None = None,
                       key: str = "") -> list[dict]:
        # uncertain_only（2026-10-24 不确定性三档）：**反选**——只取信任门之外的那些。
        # 与 trusted_only 严格互斥：两个同时为真时结果必然为空集，而空集会被读成
        # 「这个人没有记忆」——静默的错误前提比抛错危险得多（教训 #19 同族）。
        if trusted_only and uncertain_only:
            raise ValueError(
                "trusted_only 与 uncertain_only 互斥：同时为真只会得到空集，"
                "空集会被误读成「没有记忆」")
        # key（2026-10-12 M3）：按 key 过滤必须**下推到 SQL**。
        # 承诺结算的候选查询原先取 limit=20 的混合行再在 Python 里筛 promise，
        # 而 active 的 said(=5) 有 300+ 条、importance 相近——实测 target=2180254043
        # 的 20 行里 only 5 条是 promise（该对象共 25 条 active promise）。
        # 候选被截断，LLM 就无从点名结算。
        """查询记忆（按 importance DESC, timestamp DESC），可限定 qq_id。
        2026-08-16 批 2：默认只返回 status='active' 的行——retracted/superseded
        是审计历史，不参与任何读路径；include_retracted=True 仅清洗/诊断用。"""
        conditions = []
        params = []
        if qq_id:
            conditions.append("qq_id = ?")
            params.append(qq_id)
        if not include_retracted:
            conditions.append("COALESCE(status,'active') = 'active'")
        if target_qq:
            conditions.append("target_qq = ?")
            params.append(target_qq)
        if source_group_id is not None:
            conditions.append("COALESCE(source_group_id, '') = ?")
            params.append(str(source_group_id))
        if key:
            conditions.append("key = ?")
            params.append(str(key))
        if trusted_only:
            conditions.append(
                f"COALESCE(trust_level,'legacy_unverified') IN {_TRUSTED_SQL_IN}")
        elif uncertain_only:
            # 反选集合与放行集合是同一个常量的补集——不另写一份字面量，
            # 否则改信任模型时两边必然漂移（口径字典 §二）。
            conditions.append(
                f"COALESCE(trust_level,'legacy_unverified') NOT IN {_TRUSTED_SQL_IN}")
        # 质量门槛必须**下推**到 SQL（2026-10-24）：模糊档有 10,060 条，若先按
        # importance DESC 取 limit 行再在 Python 筛 confidence，噪声会把窗口吃满
        # （与 key 那次同族：候选被截断，上层就无从挑）。0 表示不过滤。
        if min_importance:
            conditions.append("importance >= ?")
            params.append(int(min_importance))
        if min_confidence:
            conditions.append("COALESCE(confidence, 0.7) >= ?")
            params.append(float(min_confidence))
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            limit_clause = " LIMIT ?" if limit is not None else ""
            query_params = params + ([max(0, int(limit))] if limit is not None else [])
            rows = conn.execute(
                f"""SELECT id, qq_id, key, value, timestamp, importance,
                          COALESCE(last_recalled, '') as last_recalled,
                          COALESCE(recall_count, 0) as recall_count,
                          COALESCE(cognitive, 'semantic') as cognitive,
                          COALESCE(confidence, 0.7) as confidence,
                          COALESCE(origin, 'extracted') as origin,
                          COALESCE(status, 'active') as status,
                          COALESCE(target_qq, '') as target_qq,
                          COALESCE(source_group_id, '') as source_group_id,
                          COALESCE(evidence_ids, '') as evidence_ids,
                          COALESCE(trust_level, 'legacy_unverified') as trust_level,
                          COALESCE(retention, 'normal') as retention,
                          COALESCE(event_time, '') as event_time,
                          COALESCE(ingested_at, '') as ingested_at,
                          COALESCE(valid_from, '') as valid_from,
                          COALESCE(valid_to, '') as valid_to,
                          superseded_by,
                          COALESCE(idempotency_key, '') as idempotency_key
                   FROM memories{where_clause}
                   ORDER BY importance DESC,
                            COALESCE(NULLIF(event_time,''), timestamp) DESC, id DESC
                   {limit_clause}""",
                tuple(query_params)
            ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # 未闭合事件（open_loops，2026-10-24 设计一）
    # ═══════════════════════════════════════════════════════════════
    # 「悬着的那些事」。与三条既有机制的边界（写在这里防止以后被合并）：
    # · `memories`：只读历史，只能被检索；open_loops 有状态、会被改写。
    # · `key='promise'` 自忆：**糖糖自己**答应别人的事，有目标对象+证据链，
    #   由 fulfill_self_promise 结算；这里是**对方的**未闭合事件，方向相反。
    # · `drives.commitment`：她「此刻多在意」，不是「哪件事没结」。
    # 方案：docs/开发规划/未闭合事件_技术方案_20261024.md

    _OPEN_LOOP_TERMINAL = ("resolved", "dropped", "expired")

    @staticmethod
    def _open_loop_norm_topic(topic: str) -> str:
        """话题归一化——去空格、全角空格、标点，用于去重比较。

        只做**结构**归一（不去语义改写）：同一件事的两种说法能不能认出来，
        交给提取时的 LLM；这里只防「多打一个空格就建两行」。
        """
        text = str(topic or "")
        for ch in (" ", "\u3000", "\t", "\n", "\r"):
            text = text.replace(ch, "")
        for ch in "，。、；：！？,.;:!?…—－-":
            text = text.replace(ch, "")
        return text.strip().lower()

    def _upsert_open_loop_conn(self, conn, qq_id: str, topic: str, *,
                               kind: str = "plan", expected_at: str = "",
                               source_memory_id: int = 0,
                               source_group_id: str = "") -> int:
        """在**调用方事务内**新增/更新一条未闭合事件，返回行 id（0 = 输入无效）。

        2026-10-25 第 12 轮抽出：提取落库走 `complete_extraction_job` 的单事务
        （`BEGIN IMMEDIATE`），里面**不能**再开一条连接去写库（同一库第二个写者
        会等锁）。而 `add_open_loop` 自己开连接，所以真正的落库路径只能另写一份
        SQL——两份去重规则必然漂移（教训 #38）。抽到这里的唯一实现，
        `add_open_loop` 与提取路径共用它。

        去重键是 `(qq_id, 归一化 topic)` 且**只在 open 行之间**去重：
        已经收尾（resolved/dropped）的事再被提起，应该开一条新的——
        「上次面试」结了之后「这次面试」是新的一件事。
        """
        qq = str(qq_id or "").strip()
        topic = str(topic or "").strip()
        if not qq or not topic:
            return 0
        norm = self._open_loop_norm_topic(topic)
        if not norm:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 同一件事被再次提到：只更新「时间锚点」，不新建行。
        # 比较用归一化后的 topic（在 Python 里做——SQL 里没有这个归一函数，
        # 而把归一逻辑写两份必然漂移）。
        existing = conn.execute(
            "SELECT id, topic FROM open_loops WHERE qq_id=? AND status='open'",
            (qq,),
        ).fetchall()
        for row in existing:
            if self._open_loop_norm_topic(row[1]) == norm:
                if expected_at:
                    conn.execute(
                        "UPDATE open_loops SET expected_at=?, updated_at=? "
                        "WHERE id=? AND status='open'",
                        (str(expected_at), now, int(row[0])),
                    )
                return int(row[0])
        cur = conn.execute(
            "INSERT INTO open_loops (qq_id, topic, kind, expected_at, "
            "source_memory_id, source_group_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'open',?,?)",
            (qq, topic, str(kind or "plan"), str(expected_at or ""),
             int(source_memory_id or 0), str(source_group_id or ""), now, now),
        )
        return int(cur.lastrowid or 0)

    def add_open_loop(self, qq_id: str, topic: str, *, kind: str = "plan",
                      expected_at: str = "", source_memory_id: int = 0,
                      source_group_id: str = "") -> int:
        """新增或更新一条未闭合事件，返回行 id（0 = 输入无效）。

        去重规则见 `_upsert_open_loop_conn`（唯一实现）。本方法是"自己开事务"
        的便捷入口；生产提取路径走的是 `_apply_extraction_item` → 同一事务内
        调用 `_upsert_open_loop_conn`。
        """
        with self._connect() as conn:
            loop_id = self._upsert_open_loop_conn(
                conn, qq_id, topic, kind=kind, expected_at=expected_at,
                source_memory_id=source_memory_id,
                source_group_id=source_group_id,
            )
            conn.commit()
            return loop_id

    def _record_open_loop_if_needed(self, conn, qq_id: str, item: dict,
                                    memory_id: int, source_group: str) -> int:
        """提取条目若是 open_loop，在同一事务里补写一行 open_loops。返回行 id。

        ⚠ 2026-10-25 第 12 轮的根因记录：这段逻辑原本只写在
        `handler._persist_extracted_items` 里，而那个方法**生产零调用**
        （`tests/test_integration_contract.py` 自己把它登记为「仅测试引用」）。
        生产提取早已改走 `complete_extraction_job` → `_apply_extraction_item`，
        于是 L2 的 open_loop 事件从未落过库：`open_loops` 表 11145 个任务、0 行。
        教训：**接线要接在生产真正走的那条路上**，接在死方法里等于没接，
        而且看起来像接好了（闸门只查"符号有调用点"）。
        """
        if str(item.get("type") or "") != "open_loop":
            return 0
        return self._upsert_open_loop_conn(
            conn, qq_id, str(item.get("value") or ""),
            kind=str(item.get("kind") or "plan"),
            expected_at=str(item.get("expected_at") or ""),
            source_memory_id=int(memory_id or 0),
            source_group_id=str(source_group or ""),
        )

    def list_open_loops(self, qq_id: str, *, limit: int = 3,
                        max_asked: int = 2, today: str = "") -> list[dict]:
        """取这个人当前**悬着的**事（供注入），按「到期优先、其次新近」排序。

        三条硬约束（每一条都对应一种骚扰/错配）：
        1. 只认 `status='open'`——收尾/放弃/过期的不再提；
        2. `asked_count < max_asked`——问过上限的不再浮上来；
        3. `qq_id` 严格相等——群里把甲的悬事说给乙听是最坏的一类串。
        """
        qq = str(qq_id or "").strip()
        if not qq:
            return []
        today = str(today or datetime.now().strftime("%Y-%m-%d"))[:10]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, qq_id, topic, kind, expected_at, source_memory_id,
                          source_group_id, status, asked_count, last_asked_at,
                          created_at, updated_at
                   FROM open_loops
                   WHERE qq_id=? AND status='open' AND asked_count < ?
                   ORDER BY CASE WHEN expected_at != '' AND expected_at <= ? THEN 0 ELSE 1 END,
                            CASE WHEN expected_at != '' THEN expected_at ELSE '9999-12-31' END,
                            id DESC
                   LIMIT ?""",
                (qq, int(max_asked), today, max(0, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_open_loop(self, loop_id: int, *, qq_id: str, outcome: str,
                          note: str = "") -> bool:
        """收尾一条未闭合事件。返回是否真的改了（False = 拒绝，不静默成功）。

        `outcome`：
        - `asked`   —— 问过了，状态仍 open，`asked_count += 1`（不可逆地记次数）
        - `resolved`—— 知道结果了，终态
        - `dropped` —— 不再跟进，终态

        三条拒绝（全部 fail-closed）：
        1. 行不存在 / `qq_id` 不匹配（跨人越权）；
        2. 已经是终态（resolved/dropped/expired）——**终态不可逆**，
           「已收尾的事被重新打开」会让注入层重复打扰；
        3. `outcome` 不认识。
        """
        loop_id = int(loop_id or 0)
        qq = str(qq_id or "").strip()
        outcome = str(outcome or "").strip().lower()
        if loop_id <= 0 or not qq:
            return False
        if outcome not in ("asked", "resolved", "dropped"):
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id, status, asked_count FROM open_loops WHERE id=?",
                (loop_id,),
            ).fetchone()
            if not row:
                return False
            if str(row[0] or "") != qq:
                return False  # 跨人
            if str(row[1] or "") in self._OPEN_LOOP_TERMINAL:
                return False  # 终态不可逆
            if outcome == "asked":
                conn.execute(
                    "UPDATE open_loops SET asked_count=asked_count+1, "
                    "last_asked_at=?, updated_at=? WHERE id=? AND status='open'",
                    (now, now, loop_id),
                )
            else:
                conn.execute(
                    "UPDATE open_loops SET status=?, resolution=?, resolved_at=?, "
                    "updated_at=? WHERE id=? AND status='open'",
                    (outcome, str(note or "")[:400], now, now, loop_id),
                )
            conn.commit()
            return True

    def expire_open_loops(self, *, days_overdue: int = 30, max_asked: int = 2,
                          today: str = "") -> int:
        """把「过期很久且已经问过上限」的行标成 expired，返回条数。

        为什么需要它：不清理的话，一个三年前没结的事会永远占着注入位，
        而注入位只有 3 个——真正的近期悬事会被历史垃圾挤掉。
        只动「问过 2 次以上仍没结果」的：没问过的不许替她放弃。
        """
        today = str(today or datetime.now().strftime("%Y-%m-%d"))[:10]
        try:
            days = max(1, int(days_overdue))
            cutoff = datetime.strptime(today, "%Y-%m-%d").date().toordinal() - days
            cutoff_date = datetime.fromordinal(cutoff).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE open_loops SET status='expired', updated_at=? "
                "WHERE status='open' AND expected_at != '' AND expected_at <= ? "
                "AND asked_count >= ?",
                (now, cutoff_date, int(max_asked)),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def query_all_memories(self) -> list[dict]:
        """查询所有记忆（用于全局统计）"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, importance, timestamp,
                          COALESCE(last_recalled, '') as last_recalled,
                          COALESCE(recall_count, 0) as recall_count
                   FROM memories"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════════
    # 记忆变更流水（memory_revisions，2026-10-24）
    # ═══════════════════════════════════════════════════════════════
    # 「未来所有记忆更正都留痕」的落地点：任何会让记忆**内容或真值**发生变化的
    # 写操作，都在同一个事务里追加一条不可变流水。设计说明与取证矩阵见
    # docs/开发规划/记忆变更留痕_20261024.md。
    #
    # 两条不能让步的边界（写在这里防止以后被「补全」）：
    # 1. **不记纯计数**——recall_count / last_recalled / total_chats / intimacy
    #    微增是统计漂移，不是真值变更；记它们会让表按「召回次数 × 记忆数」爆炸，
    #    而信息量已由这些列自己承载。但「显式设定 importance」要记，那是决策。
    # 2. **不记新生**——insert_memory 的行本身就是它诞生的记录（timestamp /
    #    ingested_at），再记一条只会让真正的更正被 22k 条流水淹没。

    def _row_snapshot(self, conn, table: str, id_column: str, key) -> dict:
        """取整行快照（删除前留底用）。

        表名/列名只接受本文件内的字面常量，不来自外部输入。
        不用 conn.row_factory——那是连接级设置，会在调用方的事务里留下副作用。
        """
        try:
            cur = conn.execute(f"SELECT * FROM {table} WHERE {id_column}=?", (key,))
            columns = [str(item[0]) for item in (cur.description or [])]
            row = cur.fetchone()
        except sqlite3.Error:
            return {}
        return dict(zip(columns, row)) if row else {}

    def _record_revision(self, conn, *, entity: str, entity_id, qq_id: str = "",
                         op: str, field: str = "", before: object = None,
                         after: object = None, reason: str = "",
                         actor: str = "") -> bool:
        """在**调用方的事务里**追加一条记忆变更流水。

        故意不 commit：留痕必须和业务写入同一个事务——否则崩溃时的四种半成品
        状态里，「改了但没留痕」是最坏的一种（数据已经错了，还查不出错在哪）。
        由调用方统一 commit。

        `actor` 默认落 `unknown`：这是刻意的——没有 actor 的流水回答不了
        「这次是谁改的」，等于只有一半价值。`test_memory_revision_trail.py`
        断言生产代码里不存在 unknown（新增写路径忘记标就会红）。

        返回是否写入成功。留痕失败只告警、不抛：观察数据不能反过来阻断记忆
        本身；但必须留日志，否则缺行会静默到没人发现（教训 #30 同族）。
        """
        try:
            target = _revision_text(entity_id).strip()
            if not target:
                return False
            conn.execute(
                "INSERT INTO memory_revisions "
                "(entity, entity_id, qq_id, op, field, before_value, after_value, "
                "reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _revision_text(entity).strip() or "memories",
                    target,
                    _revision_text(qq_id).strip(),
                    _revision_text(op).strip() or "update",
                    _revision_text(field).strip(),
                    _revision_text(before),
                    _revision_text(after),
                    _revision_text(reason).strip(),
                    _revision_text(actor).strip() or "unknown",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            return True
        except sqlite3.Error:
            logger.warning("记忆流水写入失败（业务写入保留）: %s", entity, exc_info=True)
            return False

    def memory_revisions(self, *, qq_id: str = "", entity: str = "",
                         entity_id=None, actor: str = "", limit: int = 200) -> list[dict]:
        """读记忆变更流水（新的在前）。全部条件可省略，用于演化史/审计。"""
        wheres, params = [], []
        if str(qq_id or "").strip():
            wheres.append("qq_id=?")
            params.append(str(qq_id).strip())
        if str(entity or "").strip():
            wheres.append("entity=?")
            params.append(str(entity).strip())
        if entity_id is not None and str(entity_id).strip():
            wheres.append("entity_id=?")
            params.append(str(entity_id).strip())
        if str(actor or "").strip():
            wheres.append("actor LIKE ?")
            params.append(f"{str(actor).strip()}%")
        sql = "SELECT * FROM memory_revisions"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit or 200))))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def memory_revision_stats(self, qq_id: str = "") -> dict:
        """流水总览：总数 / 按 actor / 按 op——用于「留痕真的在跑吗」的自检。

        `qq_id` 为空时统计全库；给定时只看这个人的变更（演化史页要回答
        「她的记忆被改过多少次、都是谁改的」）。
        """
        scope, params = "", ()
        if str(qq_id or "").strip():
            scope, params = " WHERE qq_id=?", (str(qq_id).strip(),)
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_revisions" + scope, params
            ).fetchone()
            by_actor = conn.execute(
                "SELECT actor, COUNT(*) FROM memory_revisions" + scope
                + " GROUP BY actor ORDER BY COUNT(*) DESC LIMIT 40",
                params,
            ).fetchall()
            by_op = conn.execute(
                "SELECT op, COUNT(*) FROM memory_revisions" + scope
                + " GROUP BY op ORDER BY COUNT(*) DESC LIMIT 40",
                params,
            ).fetchall()
        return {
            "total": int(total[0] if total else 0),
            "by_actor": [{"actor": str(r[0] or ""), "count": int(r[1])} for r in by_actor],
            "by_op": [{"op": str(r[0] or ""), "count": int(r[1])} for r in by_op],
        }

    def delete_stale_memories(self, days: int = 90, *,
                              actor: str = "unknown", reason: str = "") -> int:
        """删除长期未回忆的低重要性记忆。
        条件：importance <= 2 且从未被回忆 且 超过 days 天。
        2026-08-16 Codex M2：只清 active 业务行（审计历史走专门 retention），
        并成对删除 embedding。

        2026-10-24：删除前逐条写流水快照。被清掉的行里可能有「后来才发现
        不该清」的内容（importance 是相关性判断，不是真值判断），没有快照
        就再也拿不回来。
        """
        with self._connect() as conn:
            scanned = conn.execute(
                """SELECT * FROM memories
                   WHERE importance <= 2
                   AND (recall_count IS NULL OR recall_count = 0)
                   AND datetime(timestamp) < datetime('now', ? || ' days')
                   AND COALESCE(status,'active')='active'
                   AND COALESCE(retention,'normal')!='permanent'""",
                (f"-{days}",)
            )
            columns = [str(item[0]) for item in (scanned.description or [])]
            snapshots = [dict(zip(columns, row)) for row in scanned.fetchall()]
            old_ids = [int(item["id"]) for item in snapshots]
            for item in snapshots:
                self._record_revision(
                    conn, entity="memories", entity_id=item.get("id"),
                    qq_id=str(item.get("qq_id") or ""), op="delete",
                    before=item,
                    reason=reason or f"陈旧清理：importance≤2 且从未回忆且 >{days} 天",
                    actor=actor,
                )
            if old_ids:
                conn.execute(
                    "DELETE FROM memories WHERE id IN (%s)" % ",".join("?" * len(old_ids)),
                    old_ids
                )
                conn.executemany("DELETE FROM memory_embeddings WHERE memory_id=?",
                                 [(i,) for i in old_ids])
            conn.commit()
            return len(old_ids)

    def delete_memory_by_id(self, memory_id: int, *,
                            actor: str = "unknown", reason: str = "") -> bool:
        """物理删除一条记忆——删除前先把整行快照写进流水。

        返回是否真的删掉了一行；False 表示 id 不存在（不做无快照的删除）。
        """
        with self._connect() as conn:
            snapshot = self._row_snapshot(conn, "memories", "id", memory_id)
            if not snapshot:
                return False
            self._record_revision(
                conn, entity="memories", entity_id=memory_id,
                qq_id=str(snapshot.get("qq_id") or ""), op="delete",
                before=snapshot, reason=reason or "按 id 删除", actor=actor,
            )
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
            conn.commit()
            return True

    # ═══ 批 2 纠正闭环（2026-08-16）═══
    # 撤销不改 importance（importance 是相关性不是真值，且 setter 钳最小值 1）——
    # 统一走 status 过滤，retracted/superseded 行保留作审计历史

    def set_memory_status(self, memory_id: int, status: str = "retracted",
                          *, actor: str = "unknown", reason: str = "") -> bool:
        """标记一条记忆的真值状态：active / retracted（被否定）/ superseded（被纠正替代）

        ⚠️ 只改状态、**不写** `superseded_by`。需要留下「被谁取代」时用
        `link_superseded`——纠正流程是「先标旧行、后写新事实」，标记的那一刻
        新事实还没有 id，所以指针只能晚一步补。

        2026-10-24：状态变更写流水（旧状态 → 新状态 + actor + reason）。
        status 是真值不是统计，改它必须能回答「谁改的、为什么」。
        状态本来就等于目标值时不留流水（避免把幂等重放写成"变更"）。

        返回是否发生了实际变更。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id, COALESCE(status,'active') FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if not row:
                return False
            previous = str(row[1] or "active")
            target = str(status or "")
            if previous == target:
                return False
            self._record_revision(
                conn, entity="memories", entity_id=memory_id,
                qq_id=str(row[0] or ""), op="status", field="status",
                before=previous, after=target, reason=reason, actor=actor,
            )
            conn.execute("UPDATE memories SET status = ? WHERE id = ?", (status, memory_id))
            conn.commit()
            return True

    def link_superseded(self, old_ids: list, new_id: int,
                        valid_to: str = "", *, actor: str = "unknown",
                        reason: str = "") -> int:
        """给已标成 superseded 的旧记忆补上「被谁取代」的指针。

        为什么单独一个方法而不是并进 `set_memory_status`：纠正流程先标旧行、
        后写纠正事实——标记时新事实还没有 id。等拿到 id 再补这一刀，旧行已经
        在 superseded 状态，只需 UPDATE 指针，不重复写 status。

        返回实际更新的行数。`new_id` 非法时直接返回 0（演化史是观察数据，
        不能因为补链失败影响纠正本身）。
        """
        ids = []
        for item in (old_ids or []):
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
        try:
            target = int(new_id)
        except (TypeError, ValueError):
            return 0
        if not ids or target <= 0:
            return 0
        stamp = str(valid_to or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" * len(ids))
        with self._connect() as conn:
            # **单一判定点**：这里查出「真正会被补链的行」，流水与 UPDATE 共用这一个
            # 集合。曾经写成「查询带幂等条件 + UPDATE 再带一遍」，两处条件一旦漂移
            # 就会产生「UPDATE 了但没留痕」或「留痕了但没 UPDATE」——正是本机制要
            # 防的事（变异对照 M4 用旧写法时判定为假变异，根因就是两处各判各的）。
            pending = conn.execute(
                f"SELECT id, qq_id FROM memories WHERE id IN ({placeholders}) "
                f"AND COALESCE(superseded_by,'')=''",
                tuple(ids),
            ).fetchall()
            if not pending:
                return 0
            pending_ids = [int(row[0]) for row in pending]
            for mem_id, mem_qq in pending:
                self._record_revision(
                    conn, entity="memories", entity_id=mem_id,
                    qq_id=str(mem_qq or ""), op="supersede", field="superseded_by",
                    before="", after=str(target),
                    reason=reason or f"被 #{target} 取代",
                    actor=actor,
                )
            target_placeholders = ",".join("?" * len(pending_ids))
            conn.execute(
                f"UPDATE memories SET superseded_by=?, valid_to=? "
                f"WHERE id IN ({target_placeholders})",
                tuple([target, stamp, *pending_ids]),
            )
            conn.commit()
            return len(pending_ids)

    def fulfill_self_promise(self, promise_id: int, completion_id: int, *,
                             actor: str = "system:fulfill", reason: str = "") -> bool:
        """用同对象、同作用域且有证据的已完成动作结算一条自我承诺。

        ``fulfilled`` 是承诺的历史终态，不等同于 ``retracted``（被否定）或
        ``superseded``（被纠正）。完成记忆通过 ``superseded_by`` 保留可追溯
        指针；所有读路径仍只读取 ``status='active'``，因此已履行承诺不会继续
        伪装成待办事项注入上下文。
        """
        try:
            promise_id = int(promise_id)
            completion_id = int(completion_id)
        except (TypeError, ValueError):
            return False
        if promise_id <= 0 or completion_id <= 0 or promise_id == completion_id:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            promise = conn.execute(
                "SELECT key, origin, target_qq, COALESCE(source_group_id,''), status, "
                "qq_id FROM memories WHERE id=?",
                (promise_id,),
            ).fetchone()
            completion = conn.execute(
                "SELECT key, origin, target_qq, COALESCE(source_group_id,''), status "
                "FROM memories WHERE id=?",
                (completion_id,),
            ).fetchone()
            valid = bool(
                promise and completion
                and str(promise[0]) == "promise"
                and str(promise[1]) == "self"
                and str(promise[4] or "active") == "active"
                and str(completion[0]) == "action_completed"
                and str(completion[1]) == "self"
                and str(completion[4] or "active") == "active"
                and str(promise[2] or "")
                and str(promise[2] or "") == str(completion[2] or "")
                and str(promise[3] or "") == str(completion[3] or "")
            )
            if valid:
                evidence_count = conn.execute(
                    "SELECT COUNT(*) FROM memory_evidence WHERE memory_id=?",
                    (completion_id,),
                ).fetchone()[0]
                valid = int(evidence_count or 0) > 0
            if not valid:
                conn.rollback()
                return False
            cur = conn.execute(
                "UPDATE memories SET status='fulfilled', valid_to=?, "
                "superseded_by=? WHERE id=? AND status='active'",
                (now, completion_id, promise_id),
            )
            if int(cur.rowcount or 0) == 1:
                self._record_revision(
                    conn, entity="memories", entity_id=promise_id,
                    qq_id=str(promise[5] or ""), op="fulfill", field="status",
                    before="active", after="fulfilled",
                    reason=reason or f"由完成事实 #{completion_id} 结算",
                    actor=actor,
                )
            conn.commit()
        return cur.rowcount == 1

    def count_active_fact_memories(self, qq_id: str) -> int:
        """批 3：合成触发计数——只算 active 且非合成行的记忆。
        旧实现统计全表（含合成自己写的行）→ 触发自激循环（事故放大器）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE qq_id=? "
                "AND COALESCE(status,'active')='active' "
                "AND key NOT IN ('profile_synthesis','fact_synthesis')",
                (qq_id,)
            ).fetchone()
        return row[0] if row else 0

    def replace_profile_snapshot(self, qq_id: str, profile: str, key_facts: list[str],
                                 importance: int = 4, confidence: float = 0.7,
                                 origin: str = "summarized",
                                 source_memory_ids: list[int] | None = None,
                                 source_group_id: str = "",
                                 *, actor: str = "synthesis:profile",
                                 reason: str = "") -> dict:
        """2026-08-16 Codex I6：画像世代单事务——notes 换入、dirty 清零、
        两类合成行 supersede（含删 embedding）、新行插入，全部在一个连接
        一个事务内完成。读者只会看到完整旧世代或完整新世代，不存在
        「新 notes + 旧事实」的半世代。返回 {"profile_id": int, "fact_ids": [..]}。

        2026-10-24：旧世代的 status 变更与旧 notes 覆盖都写流水。画像重合成是
        「糖糖对这个人是谁的认知」整体换版，旧版内容必须可追溯——尤其
        `people.notes` 是被就地覆盖的，没有流水就等于上一个人生被抹掉了。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        ingested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = {"profile_id": 0, "fact_ids": []}
        with self._connect() as conn:
            source_ids = list(dict.fromkeys(
                int(item) for item in (source_memory_ids or []) if int(item) > 0
            ))
            source_rows = []
            if source_ids:
                placeholders = ",".join("?" * len(source_ids))
                source_rows = conn.execute(
                    f"SELECT id, event_time, timestamp, "
                    f"COALESCE(source_group_id,''), COALESCE(origin,''), "
                    f"COALESCE(trust_level,'legacy_unverified') FROM memories "
                    f"WHERE id IN ({placeholders}) AND qq_id=? "
                    "AND COALESCE(status,'active')='active' "
                    "AND COALESCE(trust_level,'legacy_unverified') "
                    "IN ('verified','manual','corrected')",
                    tuple(source_ids + [qq_id]),
                ).fetchall()
            lineage_valid = (
                bool(source_ids)
                and len(source_rows) == len(source_ids)
                and all(str(row[3] or "") == str(source_group_id)
                        for row in source_rows)
            )
            event_time = ""
            evidence_chat_ids = []
            if lineage_valid:
                evidence_counts = {
                    int(row[0]): int(row[1]) for row in conn.execute(
                        "SELECT memory_id,COUNT(DISTINCT chat_id) "
                        "FROM memory_evidence WHERE memory_id IN ("
                        + ",".join("?" * len(source_ids))
                        + ") GROUP BY memory_id",
                        tuple(source_ids),
                    ).fetchall()
                }
                evidence_lineage_valid = all(
                    evidence_counts.get(memory_id, 0) > 0
                    for memory_id in source_ids
                )
                manual_lineage_valid = all(
                    str(row[4] or "") in {"manual", "corrected"}
                    and str(row[5] or "") in {"manual", "corrected"}
                    for row in source_rows
                )
                # 人工/纠正事实本身就是显式确认，不要求它们伪造聊天证据；
                # 自动提取与旧摘要则必须逐条有 memory_evidence 才能继承可信级别。
                lineage_valid = evidence_lineage_valid or manual_lineage_valid
            if lineage_valid:
                event_time = max(
                    str(row[1] or row[2] or "") for row in source_rows
                )
                placeholders = ",".join("?" * len(source_ids))
                evidence_chat_ids = [
                    int(row[0]) for row in conn.execute(
                        f"SELECT DISTINCT chat_id FROM memory_evidence "
                        f"WHERE memory_id IN ({placeholders}) ORDER BY chat_id",
                        tuple(source_ids),
                    ).fetchall()
                ]
            notes_trust = "verified" if lineage_valid else "legacy_unverified"
            notes_sources = ",".join(str(item) for item in source_ids) if lineage_valid else ""
            evidence_csv = ",".join(str(item) for item in evidence_chat_ids)

            old_ids = []
            for key in ("profile_synthesis", "fact_synthesis"):
                old = conn.execute(
                    "SELECT id FROM memories WHERE qq_id=? AND key=? "
                    "AND COALESCE(status,'active')='active' "
                    "AND COALESCE(source_group_id,'')=?",
                    (qq_id, key, str(source_group_id))).fetchall()
                old_ids.extend(int(row[0]) for row in old)
            cur = conn.execute(
                "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
                "cognitive, confidence, origin, status, evidence_ids, trust_level, "
                "retention, event_time, ingested_at, source_group_id) "
                "VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)",
                (qq_id, "profile_synthesis", profile.strip(), now, importance,
                 "semantic", confidence, origin, evidence_csv, notes_trust,
                 "durable", event_time, ingested_at, str(source_group_id)))
            result["profile_id"] = int(cur.lastrowid)
            for v in key_facts:
                v = (v or "").strip()
                if not v:
                    continue
                cur = conn.execute(
                    "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
                    "cognitive, confidence, origin, status, evidence_ids, trust_level, "
                    "retention, event_time, ingested_at, source_group_id) "
                    "VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)",
                    (qq_id, "fact_synthesis", v, now, importance, "semantic",
                     confidence, origin, evidence_csv, notes_trust, "durable",
                     event_time, ingested_at, str(source_group_id)))
                result["fact_ids"].append(int(cur.lastrowid))

            new_ids = [result["profile_id"], *result["fact_ids"]]
            if evidence_chat_ids:
                conn.executemany(
                    "INSERT OR IGNORE INTO memory_evidence "
                    "(memory_id, chat_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                    [
                        (memory_id, chat_id, ingested_at)
                        for memory_id in new_ids for chat_id in evidence_chat_ids
                    ],
                )
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(
                    f"UPDATE memories SET status='superseded', valid_to=?, superseded_by=? "
                    f"WHERE id IN ({placeholders})",
                    tuple([ingested_at, result["profile_id"], *old_ids]),
                )
                for old_id in old_ids:
                    self._record_revision(
                        conn, entity="memories", entity_id=old_id, qq_id=str(qq_id),
                        op="supersede", field="status", before="active",
                        after="superseded",
                        reason=reason or f"画像重合成：被 #{result['profile_id']} 取代",
                        actor=actor,
                    )
                conn.executemany(
                    "DELETE FROM memory_embeddings WHERE memory_id=?",
                    [(item,) for item in old_ids],
                )
            previous_notes = conn.execute(
                "SELECT COALESCE(notes,''), COALESCE(notes_trust_level,'') "
                "FROM people WHERE qq_id=?",
                (qq_id,),
            ).fetchone()
            conn.execute(
                "UPDATE people SET notes=?, notes_dirty=0, notes_trust_level=?, "
                "notes_source_ids=? WHERE qq_id=?",
                (profile, notes_trust, notes_sources, qq_id),
            )
            old_notes = str(previous_notes[0] or "") if previous_notes else ""
            old_trust = str(previous_notes[1] or "") if previous_notes else ""
            if old_notes != str(profile or ""):
                self._record_revision(
                    conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                    op="update", field="notes", before=old_notes,
                    after=str(profile or ""),
                    reason=reason or "画像重合成换入", actor=actor,
                )
            if old_trust != notes_trust:
                self._record_revision(
                    conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                    op="update", field="notes_trust_level", before=old_trust,
                    after=notes_trust,
                    reason=reason or "画像重合成换入", actor=actor,
                )
            conn.commit()
        return result

    def replace_synthesis_memories(self, qq_id: str, key: str, values: list[str],
                                   importance: int = 4, confidence: float = 0.7,
                                   origin: str = "summarized", *,
                                   actor: str = "synthesis:fact",
                                   reason: str = "") -> list[int]:
        """批 3：合成快照事务化替换——同一事务内撤销旧集合并插入新集合。

        读者只会看到完整旧集合或完整新集合（无空窗/半套）；旧行标 superseded
        保留审计历史，其 embedding 同步删除（不再可检索）。返回新行 id 列表。

        2026-10-24 写入侧补链：旧行现在**必须**留下「被谁取代」的指针，否则
        演化史里就是一条断链（实测全库 1261 条 `status='superseded'` 却
        `superseded_by` 为空，绝大多数来自这里）。

        **集合级取代，不做逐条配对**：旧集合和新集合之间没有稳定的一一映射
        （归纳会让条目合并、拆分、重排），硬按序号配会造出一条看起来很真、
        实际上错的版本链——那比断链更糟。所以整批旧行统一指向新集合的第一条，
        并在演化史里按 N→1 的并入关系展示。

        另一个边界：`values` 全空时**不能**记 superseded——没有替代品就不是
        「被取代」，是「被否定」，改记 `retracted`。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            old = conn.execute(
                "SELECT id FROM memories WHERE qq_id=? AND key=? "
                "AND COALESCE(status,'active')='active'",
                (qq_id, key)
            ).fetchall()
            old_ids = [int(r[0]) for r in old]
            ids: list[int] = []
            for v in values:
                v = (v or "").strip()
                if not v:
                    continue
                cur = conn.execute(
                    "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
                    "cognitive, confidence, origin, status) VALUES (?,?,?,?,?,?,?,?,'active')",
                    (qq_id, key, v, now, importance, "semantic", confidence, origin)
                )
                ids.append(int(cur.lastrowid))
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                if ids:
                    conn.execute(
                        f"UPDATE memories SET status='superseded', valid_to=?, "
                        f"superseded_by=? WHERE id IN ({placeholders})",
                        tuple([now, ids[0], *old_ids]),
                    )
                    for old_id in old_ids:
                        self._record_revision(
                            conn, entity="memories", entity_id=old_id,
                            qq_id=str(qq_id), op="supersede", field="status",
                            before="active", after="superseded",
                            reason=reason or f"合成快照替换（{key}）：被 #{ids[0]} 取代",
                            actor=actor,
                        )
                else:
                    conn.execute(
                        f"UPDATE memories SET status='retracted', valid_to=? "
                        f"WHERE id IN ({placeholders})",
                        tuple([now, *old_ids]),
                    )
                    for old_id in old_ids:
                        self._record_revision(
                            conn, entity="memories", entity_id=old_id,
                            qq_id=str(qq_id), op="retract", field="status",
                            before="active", after="retracted",
                            reason=reason or f"合成快照替换（{key}）：新集合为空，旧集合被否定",
                            actor=actor,
                        )
                conn.executemany(
                    "DELETE FROM memory_embeddings WHERE memory_id=?",
                    [(i,) for i in old_ids],
                )
            conn.commit()
        return ids

    def search_memories_by_text(self, qq_id: str, text: str, limit: int = 20,
                                source_group_id: str | None = None) -> list[dict]:
        """纠正匹配用：值包含给定文本的 active 记忆（保守精确包含）。"""
        scope_sql = ""
        params: list = [qq_id, f"%{text}%"]
        if source_group_id is not None:
            scope_sql = " AND COALESCE(source_group_id,'')=?"
            params.append(str(source_group_id))
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT id, qq_id, key, value, importance, confidence, origin,
                           COALESCE(source_group_id,'') AS source_group_id
                   FROM memories
                   WHERE qq_id=? AND COALESCE(status,'active')='active' AND value LIKE ?
                   {scope_sql}
                   ORDER BY importance DESC LIMIT ?""",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def enrich_latest_image_message(self, qq_id: str, group_id: str, enriched: str) -> bool:
        """识图写回（2026-08-16 结构性修复）：把识图描述写进 chat_log 里最近一条
        图片占位符消息——历史里的「发了张图」从此带着内容，而不是只出现在
        当轮背景块（下一轮就丢）。占位符在入口已规范为「（发了张图片）」。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM chat_log WHERE qq_id=? AND group_id=? AND "
                "(message LIKE '[图片%' OR message='（发了张图片）') "
                "ORDER BY id DESC LIMIT 1",
                (qq_id, group_id)
            ).fetchone()
            if not row:
                return False
            conn.execute("UPDATE chat_log SET message=? WHERE id=?", (enriched, row[0]))
            conn.commit()
            return True

    def retract_cluster_fact(self, fact_id: int, status: str = "retracted", *,
                             actor: str = "unknown", reason: str = "") -> int:
        """撤销一条簇内原子事实并清空所属簇摘要（摘要已不可信，禁止继续注入）。
        返回所属 cluster_id（0 = 未找到）。

        2026-10-24：清空簇摘要前先把旧摘要写进流水——摘要是由多条原子事实
        归纳出的结论，清了就再也拼不回来。同时记原子事实的 status 变更。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cluster_id, subject_qq, COALESCE(status,'active'), fact "
                "FROM cluster_facts WHERE id=?",
                (fact_id,),
            ).fetchone()
            if not row:
                return 0
            cluster_id, subject_qq = row[0], str(row[1] or "")
            previous_status = str(row[2] or "active")
            fact_text = str(row[3] or "")
            previous_summary = conn.execute(
                "SELECT COALESCE(summary,'') FROM fact_clusters WHERE id=?",
                (cluster_id,),
            ).fetchone()
            old_summary = str(previous_summary[0] or "") if previous_summary else ""
            if previous_status != str(status or ""):
                self._record_revision(
                    conn, entity="cluster_facts", entity_id=fact_id,
                    qq_id=subject_qq, op="status", field="status",
                    before=previous_status, after=str(status or ""),
                    reason=reason or f"簇事实撤销：{fact_text[:80]}",
                    actor=actor,
                )
            conn.execute("UPDATE cluster_facts SET status=? WHERE id=?", (status, fact_id))
            conn.execute(
                "UPDATE fact_clusters SET summary='', updated_at=? WHERE id=?",
                (now, cluster_id)
            )
            if old_summary:
                self._record_revision(
                    conn, entity="fact_clusters", entity_id=cluster_id,
                    qq_id=subject_qq, op="update", field="summary",
                    before=old_summary, after="",
                    reason=reason or "簇内事实被撤销，摘要已不可信故清空",
                    actor=actor,
                )
            conn.commit()
            return cluster_id

    def search_cluster_facts_by_text(self, subject_qq: str, text: str, limit: int = 10) -> list[dict]:
        """纠正匹配用：事实文本包含给定内容的 active 原子事实。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, cluster_id, subject_qq, fact, confidence
                   FROM cluster_facts
                   WHERE subject_qq=? AND COALESCE(status,'active')='active' AND fact LIKE ?
                   ORDER BY confidence DESC LIMIT ?""",
                (subject_qq, f"%{text}%", limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_memories_for_user(self, qq_id: str, *, actor: str = "unknown",
                                 reason: str = "") -> int:
        """清空某人的所有记忆——删除前逐条写流水快照。返回删除行数。"""
        with self._connect() as conn:
            scanned = conn.execute("SELECT * FROM memories WHERE qq_id = ?", (qq_id,))
            columns = [str(item[0]) for item in (scanned.description or [])]
            snapshots = [dict(zip(columns, row)) for row in scanned.fetchall()]
            for item in snapshots:
                self._record_revision(
                    conn, entity="memories", entity_id=item.get("id"),
                    qq_id=str(qq_id), op="delete", before=item,
                    reason=reason or "清空该用户的全部记忆", actor=actor,
                )
            conn.execute("DELETE FROM memories WHERE qq_id = ?", (qq_id,))
            conn.commit()
            return len(snapshots)

    def reinforce_memory_by_id(self, memory_id: int, now: str):
        """强化一条记忆：importance+1, 更新 last_recalled 和 recall_count（精确ID匹配）

        ⚠️ 刻意**不写** memory_revisions：这是召回时的统计漂移（每条记忆每
        30 分钟最多 +1），不是对记忆内容的更正。记它会让流水表按
        「召回次数 × 记忆数」爆炸，而信息量已由 recall_count / last_recalled
        这两列承载。要查「权重是怎么涨上去的」看这两列，不要往流水里捞。
        对照：`set_memory_importance` 是显式设定，那个要记。
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE memories
                   SET importance = MIN(10, importance + 1),
                       last_recalled = ?,
                       recall_count = recall_count + 1
                   WHERE id = ?""",
                (now, memory_id)
            )
            conn.commit()

    def update_memory_value(self, memory_id: int, value: str, *,
                            actor: str = "unknown", reason: str = "") -> bool:
        """更新记忆内容并提升权重。

        2026-10-24：这是**旧值会永久消失**的写点——语义去重命中（相似度>0.85）
        时走这里，旧表述被新表述就地覆盖。所以必须先把旧值写进流水，
        否则「她以前是怎么记这件事的」这个问题永远无法回答。

        `importance+1` 是顺带的强化增量（与 `reinforce_memory_by_id` 同类），
        不单独记流水。返回是否找到并写入了该行。
        """
        new_value = str(value or "")[:100]
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id, COALESCE(value,'') FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if not row:
                return False
            old_value = str(row[1] or "")
            if old_value != new_value:
                self._record_revision(
                    conn, entity="memories", entity_id=memory_id,
                    qq_id=str(row[0] or ""), op="update", field="value",
                    before=old_value, after=new_value,
                    reason=reason or "语义去重合并：高相似新记忆覆盖旧表述",
                    actor=actor,
                )
            conn.execute(
                "UPDATE memories SET value = ?, importance = MIN(10, importance + 1) WHERE id = ?",
                (new_value, memory_id)
            )
            conn.commit()
            return True

    def record_memory_reverification_disposition(
            self, memory_id: int, disposition: str, *, reviewer: str,
            reason: str, reviewed_at: str) -> bool:
        """记录 legacy 人工复核的拒绝/延后决定，不改记忆真值状态。"""
        disposition = str(disposition or "").strip().lower()
        reviewer = str(reviewer or "").strip()
        reason = str(reason or "").strip()
        reviewed_at = str(reviewed_at or "").strip()
        if (disposition not in {"rejected", "deferred"}
                or not reviewer or not reason or not reviewed_at):
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT origin,trust_level,evidence_ids FROM memories "
                "WHERE id=? AND COALESCE(status,'active')='active'",
                (memory_id,),
            ).fetchone()
            if (not row or str(row[0] or "") != "extracted"
                    or str(row[1] or "legacy_unverified")
                    in TRUSTED_TRUST_LEVELS):
                return False
            conn.execute(
                "INSERT INTO memory_reverification_dispositions "
                "(memory_id,disposition,evidence_ids,reviewer,reason,reviewed_at) "
                "VALUES (?,?,?,?,?,?)",
                (memory_id, disposition, str(row[2] or ""), reviewer, reason, reviewed_at),
            )
            conn.commit()
            return True

    def update_memory_evidence(self, memory_id: int, evidence_ids: str,
                               source_group_id: str = "",
                               evidence_quote: str = "",
                               *, allow_legacy_promotion: bool = False,
                               reviewer: str = "", review_reason: str = "",
                               actor: str = "") -> bool:
        """校验并 union 记忆证据；成功时同步提升信任与事件时间。

        所有新增证据都必须给出被原文支持的 quote。legacy 行默认只保留为
        线索，禁止自动升级；只有 ``allow_legacy_promotion=True`` 且提供
        reviewer/review_reason 的受控重验证操作才可提升为 verified。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id, origin, target_qq, source_group_id, evidence_ids, trust_level, "
                "confidence FROM memories WHERE id=? AND COALESCE(status,'active')='active'",
                (memory_id,),
            ).fetchone()
            if not row:
                return False
            qq_id, origin, target_qq, old_group, old_evidence, old_trust, confidence = row
            requested_group = str(source_group_id or "")
            trusted_levels = TRUSTED_TRUST_LEVELS
            if old_trust in trusted_levels and str(old_group or "") != requested_group:
                return False
            is_legacy = old_trust not in trusted_levels
            if is_legacy:
                # legacy 的作用域是历史事实的一部分，重验证不得借机迁移。
                # 旧行可能尚未保存 scope；此时只能由同一批已校验的原始
                # 证据补齐，已有非空 scope 绝不可被重验证覆盖。
                if str(old_group or "") and str(old_group or "") != requested_group:
                    return False
                if (not allow_legacy_promotion or str(origin or "") != "extracted"
                        or not str(reviewer or "").strip()
                        or not str(review_reason or "").strip()):
                    return False
                # 2026-10-25 第五轮：**「说过这句」不等于「这是关于 ta 本人的事实」**。
                #
                # 语义回挂（`tools/语义回挂证据.py`）只验证一件事：这句话确实在
                # `chat_log` 里被逐字说过。但同一句原话可以是小说情节、玩笑、
                # 转述、引用、假设——**证据存在 ≠ 语义归属**。
                #
                # 现场（2026-10-25 抽样）：`mem#25510`「小说开头设定主角是广告公司
                # 策划，凌晨三点改完第十八版方案后猝死（小说情节内容）」被升成
                # verified 并进入注入；`mem#19337`「自称是Ezfic大神」（玩笑）同理。
                # 这 155 条共同特征是 **confidence < 0.7**——提取它的 LLM 当时就
                # 不确定这是不是真事实，回挂却把「说过」当成了「可信」。
                #
                # 判据来源：`insert_memory` 对新提取的 verified 就要求
                # `claim_type == "stated" and confidence >= 0.75`（见该函数注释），
                # 而重验证这条路**从来没有置信度门槛**——同一件事两套口径。
                # 这里取 0.7（比 0.75 略宽）的理由：重验证额外拿到了原文证据，
                # 证据强度更高；且 0.7 是常规提取的默认置信度，卡 0.75 会把
                # 大量正常记忆一并挡在门外。
                if float(confidence or 0) < LEGACY_PROMOTION_MIN_CONFIDENCE:
                    return False

            incoming = self._parse_evidence_ids(evidence_ids)
            if not incoming or not str(evidence_quote or "").strip():
                return False
            # 新证据必须支持 quote（来源校验 + 原文支持）——不重验旧证据。
            incoming_rows = self._validate_memory_evidence(
                conn, qq_id, origin, target_qq, requested_group,
                ",".join(str(i) for i in incoming),
                evidence_quote=evidence_quote,
            )
            if not incoming_rows:
                return False
            if old_trust in trusted_levels:
                combined = self._parse_evidence_ids(old_evidence)
                combined.extend(item for item in incoming if item not in combined)
            else:
                # 未核验旧 CSV 不能跟着新证据“洗白”。
                combined = incoming
            combined_csv = ",".join(str(item) for item in combined)
            evidence_rows = self._validate_memory_evidence(
                conn, qq_id, origin, target_qq, requested_group, combined_csv,
            )
            if not evidence_rows:
                return False

            new_trust = old_trust if old_trust in {"manual", "corrected"} else "verified"
            event_time = max(str(item[4] or "") for item in evidence_rows)
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE memories SET evidence_ids=?, source_group_id=?, "
                "trust_level=?, event_time=? WHERE id=?",
                (combined_csv, requested_group, new_trust, event_time, memory_id),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO memory_evidence "
                "(memory_id, chat_id, relation, created_at) VALUES (?, ?, 'supports', ?)",
                [(memory_id, item[0], created_at) for item in evidence_rows],
            )
            # 证据链就是这个项目的「真值依据」：多一条原话 = 这条记忆从「听说」
            # 变成「有据」。它变了必须留痕，否则事后问「这条事实当初凭什么可信」
            # 只能靠 memory_evidence 的 created_at 猜。
            if str(old_group or "") != requested_group:
                self._record_revision(
                    conn, entity="memories", entity_id=memory_id,
                    qq_id=str(qq_id or ""), op="update", field="source_group_id",
                    before=str(old_group or ""), after=requested_group,
                    reason=review_reason or "证据补录：作用域随之确定",
                    actor=actor or ("review:" + reviewer if reviewer else ""),
                )
            if str(old_evidence or "") != combined_csv:
                self._record_revision(
                    conn, entity="memories", entity_id=memory_id,
                    qq_id=str(qq_id or ""), op="update", field="evidence_ids",
                    before=str(old_evidence or ""), after=combined_csv,
                    reason=review_reason or "证据补录：新增被原文支持的聊天证据",
                    actor=actor or ("review:" + reviewer if reviewer else ""),
                )
            if str(old_trust or "") != new_trust:
                self._record_revision(
                    conn, entity="memories", entity_id=memory_id,
                    qq_id=str(qq_id or ""), op="update", field="trust_level",
                    before=str(old_trust or ""), after=new_trust,
                    reason=review_reason or "证据补录：信任等级随之升级",
                    actor=actor or ("review:" + reviewer if reviewer else ""),
                )
            if is_legacy:
                conn.execute(
                    "INSERT INTO memory_reverification_events "
                    "(memory_id,previous_trust_level,new_trust_level,evidence_ids,"
                    "evidence_quote,reviewer,reason,reviewed_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (memory_id, str(old_trust or "legacy_unverified"), new_trust,
                     combined_csv, str(evidence_quote).strip(),
                     str(reviewer).strip(), str(review_reason).strip(), created_at),
                )
            conn.commit()
            return True

    def rollback_memory_reverification(self, memory_id: int, *, reviewer: str,
                                       review_reason: str, actor: str = "") -> bool:
        """撤回一次受控重验证，使记忆重新退出可信召回。

        不删除原始证据或既有审计记录；回退只降低 trust_level，因此可以
        保留复核过程并避免把后来发现有问题的事实继续提供给模型。
        """
        reviewer = str(reviewer or "").strip()
        review_reason = str(review_reason or "").strip()
        if not reviewer or not review_reason:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trust_level,evidence_ids,qq_id FROM memories "
                "WHERE id=? AND COALESCE(status,'active')='active'",
                (memory_id,),
            ).fetchone()
            if not row or str(row[0] or "") not in TRUSTED_TRUST_LEVELS:
                return False
            promotion = conn.execute(
                "SELECT previous_trust_level,new_trust_level,evidence_ids,evidence_quote "
                "FROM memory_reverification_events WHERE memory_id=? "
                "AND previous_trust_level NOT IN ('verified','manual','corrected') "
                "ORDER BY id DESC LIMIT 1",
                (memory_id,),
            ).fetchone()
            if not promotion:
                return False
            previous_trust, promoted_trust, evidence_ids, evidence_quote = promotion
            if str(row[0] or "") != str(promoted_trust or ""):
                return False
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE memories SET trust_level=? WHERE id=?",
                (str(previous_trust), memory_id),
            )
            conn.execute(
                "INSERT INTO memory_reverification_events "
                "(memory_id,previous_trust_level,new_trust_level,evidence_ids,"
                "evidence_quote,reviewer,reason,reviewed_at) VALUES (?,?,?,?,?,?,?,?)",
                (memory_id, str(promoted_trust), str(previous_trust),
                 str(evidence_ids or row[1] or ""), str(evidence_quote or ""),
                 reviewer, review_reason, created_at),
            )
            # 重验证本身已进 memory_reverification_events，但它回答的是「复核流程」，
            # 不是「这条记忆的信任等级变过几次」。流水补上后者，让信任漂移和
            # 内容变更在同一个时间线上可读。
            self._record_revision(
                conn, entity="memories", entity_id=memory_id,
                qq_id=str(row[2] or ""), op="update", field="trust_level",
                before=str(promoted_trust or ""), after=str(previous_trust or ""),
                reason=f"撤回受控重验证：{review_reason}",
                actor=actor or f"review:{reviewer}",
            )
            conn.commit()
            return True

    def set_memory_importance(self, memory_id: int, importance: int, *,
                              actor: str = "unknown", reason: str = "") -> bool:
        """直接设置记忆的 importance（用于去重更新）。

        2026-10-24：显式设定是决策，写流水。与 `reinforce_memory_by_id`
        的区别是语义上的——那是召回时的统计漂移（不记），这是有人（或某条
        业务规则）明确决定了这条记忆的权重（记）。
        """
        target = min(10, max(1, importance))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT qq_id, COALESCE(importance,0) FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if not row:
                return False
            previous = int(row[1] or 0)
            if previous != target:
                self._record_revision(
                    conn, entity="memories", entity_id=memory_id,
                    qq_id=str(row[0] or ""), op="update", field="importance",
                    before=previous, after=target,
                    reason=reason, actor=actor,
                )
            conn.execute(
                "UPDATE memories SET importance = ? WHERE id = ?",
                (target, memory_id)
            )
            conn.commit()
            return True

    def get_memory_by_id(self, memory_id: int) -> dict | None:
        """根据 id 获取单条记忆"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM memories "
                "WHERE id=? AND COALESCE(status,'active')='active'",
                (memory_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_memory_evidence(self, memory_id: int) -> list[dict]:
        """读取一条记忆已验证的关系化证据。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id, relation FROM memory_evidence "
                "WHERE memory_id=? ORDER BY chat_id, relation",
                (memory_id,),
            ).fetchall()
        return [{"chat_id": int(row[0]), "relation": row[1]} for row in rows]

    # ═══════════════════════════════════════
    # 持久化记忆提取任务（P1）
    # ═══════════════════════════════════════

    def migrate_extraction_cursors(self, forward: dict | None = None,
                                   backfill: dict | None = None):
        """幂等导入旧 JSON 游标；前向只增、回填只向更小 ID 推进。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            for qq_id, value in (forward or {}).items():
                cursor = int(value or 0)
                conn.execute(
                    "INSERT INTO extraction_cursors "
                    "(pipeline, qq_id, direction, cursor_chat_id, updated_at) "
                    "VALUES ('memory', ?, 'forward', ?, ?) "
                    "ON CONFLICT(pipeline,qq_id,direction) DO UPDATE SET "
                    "cursor_chat_id=MAX(cursor_chat_id, excluded.cursor_chat_id), "
                    "updated_at=excluded.updated_at",
                    (str(qq_id), cursor, now),
                )
            for qq_id, value in (backfill or {}).items():
                cursor = int(value or 0)
                if cursor <= 0:
                    continue
                conn.execute(
                    "INSERT INTO extraction_cursors "
                    "(pipeline, qq_id, direction, cursor_chat_id, updated_at) "
                    "VALUES ('memory', ?, 'backfill', ?, ?) "
                    "ON CONFLICT(pipeline,qq_id,direction) DO UPDATE SET "
                    "cursor_chat_id=CASE WHEN cursor_chat_id<=0 THEN excluded.cursor_chat_id "
                    "ELSE MIN(cursor_chat_id, excluded.cursor_chat_id) END, "
                    "updated_at=excluded.updated_at",
                    (str(qq_id), cursor, now),
                )
            conn.commit()

    def get_extraction_cursor(self, qq_id: str, direction: str = "forward") -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor_chat_id FROM extraction_cursors "
                "WHERE pipeline='memory' AND qq_id=? AND direction=?",
                (str(qq_id), direction),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_extraction_cursors(self, direction: str = "forward") -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, cursor_chat_id FROM extraction_cursors "
                "WHERE pipeline='memory' AND direction=?",
                (direction,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def create_extraction_job(self, qq_id: str, messages: list[dict],
                              direction: str = "forward",
                              protocol_version: str = "memory-v1") -> dict:
        """为一个确定消息窗口建幂等任务；相同窗口/协议永远返回同一行。"""
        if direction not in {"forward", "backfill"}:
            raise ValueError(f"unsupported extraction direction: {direction}")
        message_ids = sorted({int(item["id"]) for item in messages if item.get("id")})
        if not message_ids:
            raise ValueError("extraction job requires messages")
        groups = {str(item.get("group_id") or "") for item in messages}
        scope_id = next(iter(groups)) if len(groups) == 1 else "__mixed__"
        import hashlib
        raw_key = (
            f"memory|{qq_id}|{direction}|{message_ids[0]}|{message_ids[-1]}|"
            f"{protocol_version}|{','.join(str(item) for item in message_ids)}"
        )
        job_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO extraction_jobs "
                "(job_key,pipeline,qq_id,scope_id,range_start,range_end,message_ids,direction,"
                "status,protocol_version,created_at,updated_at) "
                "VALUES (?,'memory',?,?,?,?,?,?,'pending',?,?,?)",
                (job_key, str(qq_id), scope_id, message_ids[0], message_ids[-1],
                 ",".join(str(item) for item in message_ids), direction,
                 protocol_version, now, now),
            )
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM extraction_jobs WHERE job_key=?", (job_key,)
            ).fetchone()
            conn.commit()
        result = dict(row)
        # 仅作为本次调用的瞬时观测字段，不落入 extraction_jobs schema；
        # 调用方据此区分真正新建与幂等复用，避免把“重复观察”算成准入。
        result["created_now"] = bool(inserted.rowcount == 1)
        return result

    def get_extraction_job(self, job_id: int) -> dict | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM extraction_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
        return dict(row) if row else None

    def get_extraction_job_messages(self, job_id: int) -> list[dict]:
        """按任务创建时冻结的精确 ID 集合恢复输入，不用范围猜测成员。"""
        messages, _missing_ids = self.get_extraction_job_messages_with_integrity(
            job_id
        )
        return messages

    def get_extraction_job_messages_with_integrity(
            self, job_id: int) -> tuple[list[dict], list[int]]:
        """恢复冻结消息，同时返回已丢失的冻结 ID。"""
        job = self.get_extraction_job(job_id)
        if not job:
            return [], []
        message_ids = []
        for raw in str(job.get("message_ids") or "").split(","):
            try:
                message_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not message_ids:
            # 兼容极短窗口内创建的早期任务；限定主体和范围，避免跨用户恢复。
            with self._connect() as conn:
                message_ids = [
                    int(row[0]) for row in conn.execute(
                        "SELECT id FROM chat_log WHERE qq_id=? AND id BETWEEN ? AND ? "
                        "ORDER BY id",
                        (job["qq_id"], job["range_start"], job["range_end"]),
                    ).fetchall()
                ]
        if not message_ids:
            return [], []
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id,message,timestamp,is_bot_reply,group_id FROM chat_log "
                f"WHERE qq_id=? AND id IN ({placeholders}) ORDER BY id",
                tuple([job["qq_id"]] + message_ids),
            ).fetchall()
        messages = [
            {
                "id": int(row[0]), "message": row[1], "timestamp": row[2],
                "is_bot_reply": int(row[3] or 0), "group_id": row[4] or "",
            }
            for row in rows
        ]
        found_ids = {int(row[0]) for row in rows}
        missing_ids = [message_id for message_id in message_ids
                       if message_id not in found_ids]
        return messages, missing_ids

    # ── ADR-002 动作回执邮箱（事实交付，不负责副作用重放）──

    def _enqueue_action_receipt_conn(self, conn, receipt: dict, *,
                                     per_scope_limit: int = 20,
                                     global_limit: int = 1000,
                                     allow_negative_upgrade: bool = False) -> bool:
        """在调用方事务内幂等写回执；不 begin/commit/rollback。"""
        if not isinstance(receipt, dict):
            raise ValueError("action receipt must be a dict")
        action_id = str(receipt.get("action_id") or "").strip()
        scope_id = str(receipt.get("scope_id") or "").strip()
        status = str(receipt.get("status") or "").strip()
        if not action_id or not scope_id:
            raise ValueError("action receipt requires action_id and scope_id")
        if status not in ("confirmed", "uncertain", "failed"):
            raise ValueError("action receipt must have a terminal status")
        schema_version = int(receipt.get("schema_version", 1))
        if schema_version >= 2:
            from .action_contract import finalize_action_receipt_template
            try:
                # v2 mailbox 是可信事实边界：统一走契约终局化，重算
                # source identity、scope 和 identity payload，拒绝手工伪造。
                receipt = finalize_action_receipt_template(
                    receipt,
                    status=status,
                    message_ids=receipt.get("message_ids") or (),
                    error_code=str(receipt.get("error_code") or ""),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid v2 action receipt") from exc
            action_id = str(receipt.get("action_id") or "").strip()
            scope_id = str(receipt.get("scope_id") or "").strip()
        for key in ("kind", "channel", "target"):
            if not str(receipt.get(key) or "").strip():
                raise ValueError(f"action receipt requires {key}")
        canonical = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = conn.execute(
            "SELECT receipt_json FROM action_receipt_mailbox "
            "WHERE scope_id=? AND action_id=?", (scope_id, action_id),
        ).fetchone()
        if existing:
            if str(existing[0]) != canonical:
                try:
                    previous = json.loads(str(existing[0] or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous = None
                immutable_keys = (
                    "action_id", "schema_version", "identity_version", "source_id",
                    "scope_id", "kind", "channel", "target", "ordinal",
                    "identity_payload", "conversation_ref", "actual",
                )
                previous_identity = {
                    key: previous.get(key) for key in immutable_keys
                } if isinstance(previous, dict) else None
                incoming_identity = {
                    key: receipt.get(key) for key in immutable_keys
                }
                if (allow_negative_upgrade and status == "confirmed"
                        and isinstance(previous, dict)
                        and previous.get("status", previous.get("action_status"))
                        in {"uncertain", "failed"}
                        and previous_identity == incoming_identity):
                    # 迟到平台确认是同一动作的状态升级：旧负向回执不应
                    # 阻断 DB-only 永久事实归账。保留 mailbox 行的主键，
                    # 仅替换可消费的最新 receipt；不触发任何 QQ 发送。
                    conn.execute(
                        "UPDATE action_receipt_mailbox SET action_status=?,"
                        "receipt_json=?,state='deliverable',lease_token='',"
                        "lease_until='',consumed_at='' WHERE scope_id=? AND action_id=?",
                        (status, canonical, scope_id, action_id),
                    )
                    return False
                raise ValueError("conflicting action receipt for existing action_id")
            return False
        conn.execute(
            "INSERT INTO action_receipt_mailbox "
            "(scope_id,action_id,schema_version,source_id,kind,channel,target,"
            "action_status,ordinal,receipt_json,state,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'deliverable',?)",
            (scope_id, action_id, schema_version,
             str(receipt.get("source_id") or ""), str(receipt["kind"]),
             str(receipt["channel"]), str(receipt["target"]), status,
             int(receipt.get("ordinal", 0)), canonical, now),
        )
        scope_limit = max(1, int(per_scope_limit))
        conn.execute(
            "UPDATE action_receipt_mailbox SET state='expired',lease_token='',"
            "lease_until='' WHERE scope_id=? AND state='deliverable' AND action_id IN ("
            "SELECT action_id FROM action_receipt_mailbox WHERE scope_id=? "
            "AND state='deliverable' ORDER BY rowid DESC "
            "LIMIT -1 OFFSET ?)", (scope_id, scope_id, scope_limit),
        )
        total_limit = max(1, int(global_limit))
        conn.execute(
            "UPDATE action_receipt_mailbox SET state='expired',lease_token='',"
            "lease_until='' WHERE state='deliverable' AND (scope_id,action_id) IN ("
            "SELECT scope_id,action_id FROM action_receipt_mailbox "
            "WHERE state='deliverable' ORDER BY rowid DESC "
            "LIMIT -1 OFFSET ?)", (total_limit,),
        )
        excess = max(0, int(conn.execute(
            "SELECT COUNT(*) FROM action_receipt_mailbox"
        ).fetchone()[0]) - total_limit)
        if excess:
            conn.execute(
                "DELETE FROM action_receipt_mailbox WHERE rowid IN ("
                "SELECT rowid FROM action_receipt_mailbox "
                "WHERE state IN ('consumed','expired') ORDER BY rowid LIMIT ?)",
                (excess,),
            )
        return True

    def enqueue_action_receipt(self, receipt: dict, *,
                               per_scope_limit: int = 20,
                               global_limit: int = 1000) -> bool:
        """幂等保存 terminal receipt；同 action_id 的冲突事实必须显式失败。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = self._enqueue_action_receipt_conn(
                conn, receipt,
                per_scope_limit=per_scope_limit,
                global_limit=global_limit,
            )
            conn.commit()
            return inserted

    def get_action_receipts(self, scope_id: str,
                            action_ids) -> dict[str, dict]:
        """读取动作终局事实供执行器幂等跳过；不 lease、不改变状态。

        mailbox 是近期可消费回执；已消费的 confirmed 回执从
        ``confirmed_action_facts`` 的不可变快照恢复。失败/不确定回执若已过
        mailbox TTL 不会被猜测为成功，调用方应继续走保守执行策略。
        """
        scope = str(scope_id or "").strip()
        ids = [str(item or "").strip() for item in (action_ids or ())]
        ids = list(dict.fromkeys(item for item in ids if item))
        if not scope or not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        result: dict[str, dict] = {}
        with self._connect() as conn:
            mailbox_rows = conn.execute(
                f"SELECT action_id,receipt_json FROM action_receipt_mailbox "
                f"WHERE scope_id=? AND action_id IN ({placeholders})",
                tuple([scope] + ids),
            ).fetchall()
            for action_id, receipt_json in mailbox_rows:
                try:
                    receipt = json.loads(str(receipt_json or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(receipt, dict):
                    result[str(action_id)] = receipt

            missing = [item for item in ids if item not in result]
            if missing:
                missing_ph = ",".join("?" * len(missing))
                fact_rows = conn.execute(
                    f"SELECT domain_action_id,immutable_json FROM confirmed_action_facts "
                    f"WHERE scope_id=? AND domain_action_id IN ({missing_ph})",
                    tuple([scope] + missing),
                ).fetchall()
                for action_id, immutable_json in fact_rows:
                    try:
                        receipt = json.loads(str(immutable_json or ""))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(receipt, dict):
                        receipt["status"] = "confirmed"
                        receipt.setdefault("error_code", "")
                        result[str(action_id)] = receipt
        return result

    def lease_action_receipts(self, scope_id: str, *, limit: int = 5,
                              lease_seconds: int = 900,
                              ttl_seconds: int = 86400) -> dict:
        """按 scope 原子过期、恢复并领取回执；邮箱不执行任何动作。"""
        import uuid
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            raise ValueError("scope_id is required")
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        ttl = max(2, int(ttl_seconds))
        lease_ttl = min(max(1, int(lease_seconds)), ttl - 1)
        expires_before = (now_dt - timedelta(seconds=ttl)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lease_until = (now_dt + timedelta(
            seconds=lease_ttl
        )).strftime("%Y-%m-%d %H:%M:%S")
        token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE action_receipt_mailbox SET state='expired',lease_token='',"
                "lease_until='' WHERE scope_id=? AND state IN ('deliverable','leased') "
                "AND created_at<=?", (scope_id, expires_before),
            )
            conn.execute(
                "UPDATE action_receipt_mailbox SET state='deliverable',lease_token='',"
                "lease_until='' WHERE scope_id=? AND state='leased' "
                "AND (lease_until='' OR lease_until<=?)", (scope_id, now),
            )
            rows = conn.execute(
                "SELECT action_id,receipt_json FROM action_receipt_mailbox "
                "WHERE scope_id=? AND state='deliverable' "
                "ORDER BY rowid LIMIT ?",
                (scope_id, max(1, int(limit))),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" * len(rows))
                conn.execute(
                    f"UPDATE action_receipt_mailbox SET state='leased',lease_token=?,"
                    f"lease_until=? WHERE scope_id=? AND action_id IN ({placeholders}) "
                    "AND state='deliverable'",
                    tuple([token, lease_until, scope_id] + [row[0] for row in rows]),
                )
            conn.commit()
        return {
            "lease_token": token if rows else "",
            "receipts": [json.loads(row[1]) for row in rows],
        }

    def ack_action_receipts(self, scope_id: str, lease_token: str) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE action_receipt_mailbox SET state='consumed',consumed_at=?,"
                "lease_token='',lease_until='' WHERE scope_id=? AND state='leased' "
                "AND lease_token=?", (now, str(scope_id), str(lease_token)),
            )
            conn.commit()
            return int(cur.rowcount)

    def release_action_receipts(self, scope_id: str, lease_token: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE action_receipt_mailbox SET state='deliverable',lease_token='',"
                "lease_until='' WHERE scope_id=? AND state='leased' AND lease_token=?",
                (str(scope_id), str(lease_token)),
            )
            conn.commit()
            return int(cur.rowcount)

    def expire_action_receipts(self, ttl_seconds: int = 86400) -> int:
        cutoff = (datetime.now() - timedelta(
            seconds=max(1, int(ttl_seconds))
        )).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE action_receipt_mailbox SET state='expired',lease_token='',"
                "lease_until='' WHERE state IN ('deliverable','leased') AND created_at<=?",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount)

    def get_action_receipt_mailbox_health(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state,COUNT(*),MIN(created_at) FROM action_receipt_mailbox "
                "GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("deliverable", "leased", "consumed", "expired")}
        counts.update({str(row[0]): int(row[1]) for row in rows})
        oldest = min(
            (str(row[2]) for row in rows if row[0] == "deliverable" and row[2]),
            default="",
        )
        return {
            **counts,
            "total": sum(counts.values()),
            "open": counts["deliverable"] + counts["leased"],
            "oldest_deliverable_at": oldest,
            "expired_unconsumed": counts["expired"],
        }

    def list_resumable_extraction_jobs(self, limit: int = 20) -> list[dict]:
        """列出 ready、pending 和租约已过期的任务，ready 始终优先。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM extraction_jobs WHERE pipeline='memory' AND "
                "(status IN ('ready','pending') OR "
                "(status='leased' AND (lease_until='' OR lease_until<=?))) "
                "ORDER BY CASE status WHEN 'ready' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
                "updated_at, id LIMIT ?",
                (now, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_open_extraction_users(self) -> set[str]:
        """返回仍有未完成 memory 任务的用户（包含未过期 leased）。

        `list_resumable_extraction_jobs` 有意排除未过期租约，供 worker
        恢复使用；准入层则必须把这些租约也视为 open，避免重复准入和
        虚增 admitted 指标。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT qq_id FROM extraction_jobs "
                "WHERE pipeline='memory' AND status IN ('pending','leased','ready')"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def get_extraction_queue_health(self) -> dict:
        """可观测队列快照；用于健康检查和千人持续流量验收。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*),MIN(created_at) FROM extraction_jobs "
                "WHERE pipeline='memory' GROUP BY status"
            ).fetchall()
        # 可观测接口必须保持固定 schema；稀疏 GROUP BY 结果会让调用方在空队列
        # 或尚未进入 dead 时得到 KeyError，掩盖真正的状态与超时原因。
        counts = {status: 0 for status in ("pending", "leased", "ready", "done", "dead")}
        counts.update({str(row[0]): int(row[1]) for row in rows})
        oldest = min(
            (str(row[2]) for row in rows
             if row[0] in ("pending", "leased", "ready") and row[2]),
            default="",
        )
        return {
            **counts,
            "total_open": sum(
                count for status, count in counts.items()
                if status in ("pending", "leased", "ready")
            ),
            "oldest_open_at": oldest,
        }

    def lease_extraction_job(self, job_id: int, lease_seconds: int = 300) -> dict | None:
        """领取 pending/过期 leased 任务；ready 不再调用 LLM。"""
        import uuid
        now_dt = datetime.now()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        lease_until = (now_dt + timedelta(seconds=max(30, lease_seconds))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lease_until FROM extraction_jobs WHERE id=?",
                (int(job_id),),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            status, old_until = row
            if status != "pending" and not (
                status == "leased" and (not old_until or str(old_until) <= now)
            ):
                conn.rollback()
                return None
            conn.execute(
                "UPDATE extraction_jobs SET status='leased', lease_token=?, lease_until=?, "
                "attempts=attempts+1, updated_at=?, error='' WHERE id=?",
                (token, lease_until, now, int(job_id)),
            )
            conn.row_factory = sqlite3.Row
            leased = conn.execute(
                "SELECT * FROM extraction_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            conn.commit()
        return dict(leased)

    def mark_extraction_job_ready(self, job_id: int, lease_token: str,
                                  items: list[dict], stats: dict | None = None) -> bool:
        """持久化 LLM 结果；ready 任务重启后直接落库，不再调用 LLM。"""
        import json
        payload = json.dumps(
            {"items": items, "stats": stats or {}}, ensure_ascii=False,
            separators=(",", ":"),
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE extraction_jobs SET status='ready', result_json=?, "
                "lease_token='', lease_until='', updated_at=? "
                "WHERE id=? AND status='leased' AND lease_token=?",
                (payload, now, int(job_id), str(lease_token)),
            )
            conn.commit()
            return cur.rowcount == 1

    def fail_extraction_job(self, job_id: int, lease_token: str, error: str,
                            max_attempts: int = 5) -> bool:
        """释放失败租约；达到上限后进入 dead，避免无限热循环。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE extraction_jobs SET "
                "status=CASE WHEN attempts>=? THEN 'dead' ELSE 'pending' END, "
                "lease_token='', lease_until='', error=?, updated_at=? "
                "WHERE id=? AND status='leased' AND lease_token=?",
                (max(1, int(max_attempts)), str(error or "")[:500], now,
                 int(job_id), str(lease_token)),
            )
            conn.commit()
            return cur.rowcount == 1

    def quarantine_extraction_job(self, job_id: int,
                                  error: str = "missing_frozen_messages") -> bool:
        """将无法重放的任务移出可运行队列，等待人工核验。

        冻结消息丢失属于不可重试的数据完整性问题，不应留在 pending
        队首反复占用 worker；使用 dead 状态保持现有 schema 兼容，并由
        `requeue_dead_extraction_jobs` 排除该错误前缀。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reason = str(error or "missing_frozen_messages")[:500]
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE extraction_jobs SET status='dead', lease_token='', "
                "lease_until='', error=?, updated_at=? "
                "WHERE id=? AND status IN ('pending','leased')",
                (reason, now, int(job_id)),
            )
            conn.commit()
            return cur.rowcount == 1

    def requeue_dead_extraction_jobs(self, limit: int = 5,
                                     retry_after_seconds: int = 1800) -> int:
        """冷却后自动重试死信；保留错误文本，避免永久卡住该用户游标。"""
        cutoff = (
            datetime.now() - timedelta(seconds=max(0, int(retry_after_seconds)))
        ).strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            ids = [
                int(row[0]) for row in conn.execute(
                "SELECT id FROM extraction_jobs WHERE pipeline='memory' "
                    "AND status='dead' AND error NOT LIKE 'missing_frozen_messages%' "
                    "AND updated_at<=? ORDER BY updated_at,id LIMIT ?",
                    (cutoff, max(1, int(limit))),
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE extraction_jobs SET status='pending', attempts=0, "
                f"lease_token='', lease_until='', updated_at=? "
                f"WHERE status='dead' AND id IN ({placeholders})",
                tuple([now] + ids),
            )
            conn.commit()
            return int(cur.rowcount)

    def _apply_extraction_item(self, conn, job_id: int, qq_id: str,
                               item: dict, origin: str,
                               actor: str = "extract:llm") -> int:
        """在调用方事务内写一条自动提取结果；无有效原文证据则拒绝。"""
        type_map = {
            "identity": "fact", "preference": "like", "habit": "habit",
            "event": "event", "relationship": "fact", "note": "said",
            # 2026-10-25 第 12 轮：open_loop 先按 event 记忆落库（她什么时候说过
            # 这话），再在下面补一行 open_loops（哪件事没结）。此前这里没有
            # open_loop，条目会落到默认的 "fact"——悬事被记成事实。
            "open_loop": "event",
        }
        value = str(item.get("value") or "").strip()[:100]
        confidence = float(item.get("confidence", 0.7) or 0.7)
        if not value or confidence < 0.5:
            return 0
        evidence = item.get("evidence_ids") or []
        if not isinstance(evidence, list):
            return 0
        evidence_csv = ",".join(
            str(int(evidence_id)) for evidence_id in evidence
            if isinstance(evidence_id, (int, float)) and float(evidence_id).is_integer()
        )
        source_group = str(item.get("source_group_id") or "")
        # P0-D2（审查 Critical 3）：LLM 提供 evidence_quote 时校验 claim 被
        # 证据原文支持——来源存在性 ≠ claim 被支持。无 quote 的旧提取器仍
        # 保留待复核记录，但绝不能写入可信层。
        evidence_quote = str(item.get("evidence_quote") or "").strip()
        evidence_rows = self._validate_memory_evidence(
            conn, qq_id, origin, "", source_group, evidence_csv,
            evidence_quote=evidence_quote,
        )
        if not evidence_rows:
            return 0
        # 推断/低置信结果不得进入 verified（Critical 3）：只有明确陈述
        # （claim_type=stated）且置信度 >= 0.75 才可写 verified。
        claim_type = str(item.get("claim_type") or "").strip().lower()
        trust_level = (
            "verified"
            if (evidence_quote and claim_type == "stated" and confidence >= 0.75)
            else "legacy_unverified" if not evidence_quote else "unverified"
        )

        if item.get("type") == "alias":
            alias = _sanitize_display_name(value)
            if len(alias) < 2:
                return 0
            conn.execute(
                "INSERT OR IGNORE INTO aliases(alias,qq_id,source,created_at) "
                "VALUES (?,?,'llm',?)",
                (alias, qq_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return 1

        key = type_map.get(str(item.get("type") or "note"), "fact")
        importance = int(item.get("importance", 5) or 5)
        if confidence < 0.75:
            importance = max(1, importance - 2)
        importance = min(10, max(1, importance))
        cognitive = str(item.get("cognitive") or "semantic")
        retention = (
            "permanent" if item.get("type") == "identity"
            else "durable" if item.get("type") in {"preference", "habit", "relationship"}
            else "transient" if cognitive == "episodic" else "normal"
        )
        event_time = max(str(row[4] or "") for row in evidence_rows)
        ingested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        exact = conn.execute(
            "SELECT id, importance, trust_level, source_group_id FROM memories "
            "WHERE qq_id=? AND value=? AND COALESCE(status,'active')='active' "
            "AND COALESCE(source_group_id,'')=? "
            "ORDER BY id DESC LIMIT 1",
            (qq_id, value, source_group),
        ).fetchone()
        if exact:
            memory_id, old_importance, old_trust, _old_group = exact
            # 2026-10-12 M2（审计 P1-5）：原来这里套着
            #   if old_trust != "verified" or str(old_group or "") == source_group:
            # 但上面的 SELECT 已经按 COALESCE(source_group_id,'')=? 限定同一来源域，
            # 第二子句**恒为真** → 整个条件恒成立，真正在做的只有"合并证据"。
            # 危害在下面那条 UPDATE：CASE 只保 manual/corrected，**漏了 verified**，
            # 于是已核验记忆会被同 value 的弱提取静默降级（域内探针实证，且无审计事件）。
            # 语义定死：**同 value、同来源域 → 合并证据；可信级别只升不降。**
            if old_trust == "verified" and trust_level != "verified":
                logger.info(
                    "🔐 已核验记忆拒绝被弱提取降级: id=%s 新级别=%s（保留 verified）",
                    memory_id, trust_level,
                )
            old_ids = [
                int(row[0]) for row in conn.execute(
                    "SELECT chat_id FROM memory_evidence WHERE memory_id=? "
                    "AND relation='supports' ORDER BY chat_id",
                    (memory_id,),
                ).fetchall()
            ]
            combined = old_ids + [
                int(row[0]) for row in evidence_rows if int(row[0]) not in old_ids
            ]
            combined_csv = ",".join(str(item_id) for item_id in combined)
            effective_importance = max(importance, int(old_importance))
            effective_trust = (
                str(old_trust or "")
                if str(old_trust or "") in {"manual", "corrected", "verified"}
                else trust_level
            )
            conn.execute(
                "UPDATE memories SET importance=?, evidence_ids=?, source_group_id=?, "
                "trust_level=CASE WHEN trust_level IN ('manual','corrected','verified') "
                "THEN trust_level ELSE ? END, event_time=? WHERE id=?",
                (effective_importance, combined_csv,
                 source_group, trust_level, event_time, memory_id),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO memory_evidence "
                "(memory_id,chat_id,relation,created_at) VALUES (?,?,'supports',?)",
                [(memory_id, row[0], ingested_at) for row in evidence_rows],
            )
            # 同值合并看起来「没改内容」，但它改了这条记忆的可信依据与权重——
            # 「这条事实后来多了一条原话支持」是审计要回答的问题之一。
            self._record_revision(
                conn, entity="memories", entity_id=memory_id, qq_id=str(qq_id),
                op="merge", field="evidence_ids",
                before=",".join(str(item_id) for item_id in old_ids),
                after=combined_csv,
                reason=(
                    f"同值提取合并证据（importance {old_importance}→{effective_importance}"
                    f"，trust {old_trust}→{effective_trust}）"
                ),
                actor=actor,
            )
            # 同一件「未结的事」被再次提取到：记忆合并了，open_loops 也要跟上
            # （否则 source_memory_id 停在旧行、expected_at 不更新）。
            self._record_open_loop_if_needed(
                conn, qq_id, item, memory_id, source_group)
            return 1

        import hashlib
        normalized = " ".join(value.split()).casefold()
        idem_raw = f"{job_id}|{qq_id}|{key}|{normalized}|{evidence_csv}"
        idempotency_key = hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO memories "
            "(qq_id,key,value,timestamp,importance,cognitive,confidence,origin,status,"
            "source_group_id,evidence_ids,trust_level,retention,event_time,ingested_at,"
            "idempotency_key) VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)",
            (qq_id, key, value, event_time[:16], importance, cognitive, confidence,
             origin, source_group, evidence_csv, trust_level, retention, event_time,
             ingested_at, idempotency_key),
        )
        if cur.rowcount == 0:
            return 0
        memory_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO memory_evidence(memory_id,chat_id,relation,created_at) "
            "VALUES (?,?,'supports',?)",
            [(memory_id, row[0], ingested_at) for row in evidence_rows],
        )
        # 记忆落好、拿到 memory_id 之后，同一事务里补 open_loops 行——
        # 两问两答（「哪件事没结」/「她哪句话说的」）要么一起生效，要么一起回滚。
        self._record_open_loop_if_needed(
            conn, qq_id, item, memory_id, source_group)
        return 1

    def complete_extraction_job(self, job_id: int, origin: str = "extracted") -> dict:
        """单事务写记忆/证据/游标并确认任务，崩溃时全部回滚。"""
        import json
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            job = conn.execute(
                "SELECT * FROM extraction_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if not job:
                raise ValueError(f"extraction job not found: {job_id}")
            job_dict = dict(job)
            if job_dict["status"] == "done":
                cursor_row = conn.execute(
                    "SELECT cursor_chat_id FROM extraction_cursors "
                    "WHERE pipeline='memory' AND qq_id=? AND direction=?",
                    (job_dict["qq_id"], job_dict["direction"]),
                ).fetchone()
                cursor = int(cursor_row[0]) if cursor_row else 0
                conn.rollback()
                return {
                    "status": "done",
                    "count": 0,
                    "cursor_chat_id": cursor,
                }
            if job_dict["status"] != "ready":
                conn.rollback()
                raise ValueError(f"extraction job is not ready: {job_dict['status']}")
            try:
                payload = json.loads(job_dict["result_json"] or "{}")
                if not isinstance(payload, dict):
                    raise TypeError("ready payload must be an object")
                items = payload.get("items")
                if not isinstance(items, list) or any(
                        not isinstance(item, dict) for item in items):
                    raise TypeError("ready payload items must be a list of objects")
                # 在写入任何记忆前完成数值字段预检。否则前一条已写、后一条
                # 转型失败会让 ready 永久热循环并饿死同一 worker 的其它任务。
                import math
                for item in items:
                    confidence = item.get("confidence", 0.7)
                    importance = item.get("importance", 5)
                    if (
                        isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not math.isfinite(float(confidence))
                    ):
                        raise TypeError("item confidence must be a finite number")
                    if (
                        isinstance(importance, bool)
                        or not isinstance(importance, (int, float))
                        or not math.isfinite(float(importance))
                        or not float(importance).is_integer()
                    ):
                        raise TypeError("item importance must be an integer")
                    evidence = item.get("evidence_ids") or []
                    if not isinstance(evidence, list):
                        raise TypeError("item evidence_ids must be a list")
                    if any(
                        isinstance(evidence_id, bool)
                        or not isinstance(evidence_id, (int, float))
                        or not math.isfinite(float(evidence_id))
                        or not float(evidence_id).is_integer()
                        for evidence_id in evidence
                    ):
                        raise TypeError("item evidence_ids must contain integers")
            except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
                # ready 是持久化提交边界；损坏结果不能继续热循环，也不能推进游标。
                # 先隔离为 dead，保留原始 result_json 取证；现有冷却重排会从
                # 冻结消息重新调用 LLM，生成新的 ready 结果后再提交。
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "UPDATE extraction_jobs SET status='dead', lease_token='', "
                    "lease_until='', error=?, updated_at=? WHERE id=? AND status='ready'",
                    (f"corrupt_ready_payload:{type(exc).__name__}:{exc}"[:500],
                     now, int(job_id)),
                )
                conn.commit()
                raise ValueError(
                    f"corrupt ready payload for extraction job {job_id}"
                ) from exc
            count = 0
            for item in items:
                count += self._apply_extraction_item(
                    conn, int(job_id), job_dict["qq_id"], item, origin,
                )

            # P1（2026-10-24）：本次提取可能带回「名字是X」这类自述——归档成自称。
            # 放在同一事务里：记忆和它的名字证据要么一起生效，要么一起回滚。
            # 注意它**不改 nickname**（自述可能是角色扮演，见方法 docstring）。
            self._promote_self_declared_names(conn, job_dict["qq_id"])

            current_row = conn.execute(
                "SELECT cursor_chat_id FROM extraction_cursors "
                "WHERE pipeline='memory' AND qq_id=? AND direction=?",
                (job_dict["qq_id"], job_dict["direction"]),
            ).fetchone()
            current = int(current_row[0]) if current_row else 0
            if job_dict["direction"] == "forward":
                cursor = max(current, int(job_dict["range_end"]))
            else:
                cursor = (
                    int(job_dict["range_start"]) if current <= 0
                    else min(current, int(job_dict["range_start"]))
                )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO extraction_cursors "
                "(pipeline,qq_id,direction,cursor_chat_id,updated_at) "
                "VALUES ('memory',?,?,?,?) "
                "ON CONFLICT(pipeline,qq_id,direction) DO UPDATE SET "
                "cursor_chat_id=excluded.cursor_chat_id, updated_at=excluded.updated_at",
                (job_dict["qq_id"], job_dict["direction"], cursor, now),
            )
            conn.execute(
                "UPDATE extraction_jobs SET status='done', lease_token='', lease_until='', "
                "updated_at=?, error='' WHERE id=?",
                (now, int(job_id)),
            )
            conn.commit()
        return {"status": "done", "count": count, "cursor_chat_id": cursor}


    def search_memories_by_keyword(self, keyword: str, limit: int = 5,
                                   trusted_only: bool = True) -> list[dict]:
        """全文搜索记忆（先查外号定位，再 LIKE 搜索）；默认只投影可信行。"""
        trust_sql = (
            " AND COALESCE(trust_level,'legacy_unverified') "
            "IN ('verified','manual','corrected')"
            if trusted_only else ""
        )
        with self._connect() as conn:
            # 先查是不是某人的外号
            alias_rows = conn.execute(
                "SELECT qq_id FROM aliases WHERE alias = ?", (keyword,)
            ).fetchall()
            if alias_rows:
                all_rows = []
                for (aq_id,) in alias_rows:
                    rows = conn.execute(
                        """SELECT qq_id, key, value, timestamp, importance
                           FROM memories WHERE qq_id = ?
                           AND COALESCE(status,'active')='active'
                           """ + trust_sql + """
                           ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                        (aq_id, limit)
                    ).fetchall()
                    all_rows.extend(rows)
                return [
                    {"qq_id": r[0], "key": r[1], "value": r[2],
                     "timestamp": r[3], "importance": r[4]}
                    for r in all_rows[:limit]
                ]

            rows = conn.execute(
                """SELECT qq_id, key, value, timestamp, importance
                   FROM memories WHERE value LIKE ?
                   AND COALESCE(status,'active')='active'
                   """ + trust_sql + """
                   ORDER BY importance DESC, timestamp DESC LIMIT ?""",
                (f"%{keyword}%", limit)
            ).fetchall()
        return [
            {"qq_id": r[0], "key": r[1], "value": r[2],
             "timestamp": r[3], "importance": r[4]}
            for r in rows
        ]

    # ═══════════════════════════════════════
    # chat_log 表
    # ═══════════════════════════════════════

    def insert_chat(self, qq_id: str, message: str, group_id: str = "",
                    is_bot: bool = False, timestamp: str = "",
                    raw_message: str = "", segments: str = "",
                    event_key: str = "", speaker_qq: str = ""):
        """记录一条聊天。非机器人消息同时更新 people 的 total_chats/last_chat（单事务）。

        `speaker_qq`（2026-10-24）：真正的发言人账号。空 = 按 `is_bot` 推断
        （历史行为不变）。桌面端「代发到 QQ」的行会同时是 `is_bot=True`（外部
        事实：QQ 上显示是糖糖的号）+ `speaker_qq=主人`（内部事实：谁打的字），
        镜像据此显示「你（代发）」而不是冒充糖糖。

        2026-08-10 修复计数链路断裂：之前只写 chat_log，从不更新计数——
        事实簇提取阈值、画像合成、里程碑、每日首次互动奖励长期不触发，
        last_chat 停在建档时间导致每天重复 +5。

        2026-08-28 任务A 新增可选参数（默认全部保持旧行为，旧调用方不受影响）：
          raw_message/segments：原始事件持久化——批处理先逐条落真实事件，
            合并文本只作 LLM 视图，不得覆盖原始事实
          event_key：非空时按 (scope,platform_message_id) 幂等——NapCat 重投/
            重连重放返回 None 且不重复记账；message_id 缺失(=0)不设 event_key 不强去重
        """
        if not timestamp:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        started = time.perf_counter()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO chat_log (qq_id, group_id, is_bot_reply, message, timestamp,"
                    " raw_message, segments, event_key, speaker_qq) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (qq_id, group_id, 1 if is_bot else 0, message, timestamp,
                     raw_message, segments, event_key, str(speaker_qq or ""))
                )
                if not is_bot:
                    # 非机器人消息：自增聊天计数 + 更新最后活跃时间（同一事务）
                    conn.execute(
                        "UPDATE people SET total_chats = total_chats + 1, last_chat = ? WHERE qq_id = ?",
                        (timestamp, qq_id)
                    )
                write_done = time.perf_counter()
                conn.commit()
                elapsed_ms = (time.perf_counter() - started) * 1000
                if elapsed_ms >= 250:
                    logger.warning(
                        "chat insert slow | elapsed_ms=%.1f write_ms=%.1f commit_ms=%.1f",
                        elapsed_ms,
                        (write_done - started) * 1000,
                        (time.perf_counter() - write_done) * 1000,
                    )
                # 2026-08-16 Codex I5：返回行 id——识图写回按精确 id CAS，
                # 避免并发下「最近占位符」定位写错图片
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            # 2026-08-28 任务A（Codex 复核收窄）：只有「非空 event_key 的唯一键冲突」
            # （重复事件，同 scope+message_id）才幂等返回 None；其他完整性错误
            # （NOT NULL 等）必须继续抛出，避免静默吞错。
            if event_key and "chat_log.event_key" in str(e):
                return None
            raise

    def enrich_chat_message(self, chat_id: int, enriched: str,
                            expect: str = "（发了张图片）") -> bool:
        """2026-08-16 Codex I5：识图写回 CAS——只当该行仍是占位符时更新。
        已被写过的行拒绝覆盖（两张图乱序完成时不会把描述写到错误图片）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE chat_log SET message=? WHERE id=? AND message=?",
                (enriched, chat_id, expect)
            )
            conn.commit()
            return cur.rowcount > 0

    # ═══════════════════════════════════════
    # 合成行 quarantine（2026-08-28 任务A）
    # ═══════════════════════════════════════
    # 历史批处理把合并文本当 chat_log 落库，多人消息被归到第一人（生产约 1.4 万行）。
    # 修复原则：先逐条落真实事件（insert_chat 的 event_key/raw/segments），
    # 历史合成行只做可回滚标记（quarantined_at 非空），禁止直接删除唯一历史。
    # 匹配是 batcher 自己生成的确定性文本前缀，不是 LLM 语义判断；
    # 格式必须与 agent/message_batcher.py 的合并文本同步（测试闸门锁定）。

    GROUP_SYNTHETIC_PREFIX = "【同时有"
    GROUP_SYNTHETIC_TAIL = "个人找你"
    PRIVATE_SYNTHETIC_PREFIX = "【对方连续发了多条消息"

    def quarantine_synthetic_chat_logs(self) -> int:
        """把历史批处理产生的合成合并行标记为 quarantine（可回滚，不删除）。

        幂等：已标记（quarantined_at 非空）的行跳过。返回本次标记的行数。
        标记后默认被历史查询/最近消息/提取数据源排除（各读路径自带
        COALESCE(quarantined_at,'')='' 过滤）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE chat_log SET is_synthetic=1, quarantined_at=? "
                "WHERE quarantined_at='' AND is_synthetic=0 "
                "AND ((message LIKE ? AND message LIKE ?) OR message LIKE ?)",
                (now,
                 self.GROUP_SYNTHETIC_PREFIX + "%",
                 "%" + self.GROUP_SYNTHETIC_TAIL + "%",
                 self.PRIVATE_SYNTHETIC_PREFIX + "%")
            )
            conn.commit()
            return cur.rowcount

    def unquarantine_chat_log(self, chat_ids: list[int]) -> int:
        """回滚 quarantine（任务A 的可回滚保证）：清除标记，行恢复参与查询/提取。"""
        if not chat_ids:
            return 0
        placeholders = ",".join("?" for _ in chat_ids)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE chat_log SET is_synthetic=0, quarantined_at='' "
                f"WHERE id IN ({placeholders})",
                chat_ids
            )
            conn.commit()
            return cur.rowcount

    def count_quarantined_chat_logs(self) -> int:
        """当前 quarantine 行数（运维/健康检查用）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE quarantined_at != ''"
            ).fetchone()
            return row[0] if row else 0

    # ═══════════════════════════════════════════════════════════
    # 会客厅镜像（2026-10-24）：把 chat_log 按会话聚合成可浏览的流
    #
    # 只读聚合，不改任何既有写入路径。会话 = chat_log 的一个分组
    # （私聊按 qq_id、群聊按 group_id），消息按 id 单调递增。
    # ═══════════════════════════════════════════════════════════

    def list_chat_scopes(self, kind: str, limit: int = 100,
                         exclude_id: str = "") -> list[dict]:
        """列出活跃会话摘要（会客厅左侧列表）。

        kind='private' 按 qq_id 分组，'group' 按 group_id 分组。
        exclude_id：把糖糖自己的 QQ 从私聊列表剔除（否则会冒出「和糖糖的私聊」
        这个空壳会话——她的自述消息 `is_bot_reply=1` 挂在 bot_qq 名下）。

        排除隔离行（`quarantined_at`）：那是历史批处理把多人消息合并后才有的
        脏行，不该出现在镜像里冒充某人说过的话。
        """
        kind = "group" if kind == "group" else "private"
        limit = max(1, min(int(limit), 300))
        col = "group_id" if kind == "group" else "qq_id"
        where = [f"{col} != ''", "COALESCE(quarantined_at,'') = ''"]
        if kind == "private":
            # ⚠ 私聊口径必须限定 `group_id` 为空（2026-10-25 第 17 轮修）。
            #
            # 漏了这一条，`private:<qq>` 分组会把该 QQ 在**所有群**里的发言也算进来：
            # 全库实测 21,435 条真私聊 vs 279,784 条群消息，按 `qq_id` 分组后
            # 12 个活跃会话里 11 个的 83%~100% 是群消息（其中 4 个 100%）。
            # 主人报「虽然标着私聊标签，但聊天记录显示的全是群里的内容」就是这个。
            #
            # 全项目私聊口径的统一约定是 `COALESCE(group_id,'')=''`——同文件另有
            # 17 处（read_chat_feed / get_user_recent_messages / _chat_scope /
            # 活跃统计 / 未读计数…）都写了，只有本函数和 read_chat_feed 漏了。
            # 教训 #38：同一个物理量的换算点必须全部一致，漏一个就是静默错数据。
            where.append("COALESCE(group_id,'') = ''")
        params: list = []
        if kind == "private" and exclude_id:
            where.append("qq_id != ?")
            params.append(str(exclude_id))
        params.append(limit)
        sql = (
            f"SELECT {col} AS sid, MAX(id) AS last_id, COUNT(*) AS cnt "
            f"FROM chat_log WHERE {' AND '.join(where)} "
            f"GROUP BY {col} ORDER BY last_id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            if not rows:
                return []
            ids = [int(r[1]) for r in rows]
            marks = ",".join("?" * len(ids))
            tail = {
                int(r[0]): (str(r[1] or ""), str(r[2] or ""))
                for r in conn.execute(
                    f"SELECT id, message, timestamp FROM chat_log "
                    f"WHERE id IN ({marks})", tuple(ids))
            }
        out: list[dict] = []
        for sid, last_id, cnt in rows:
            message, timestamp = tail.get(int(last_id), ("", ""))
            out.append({
                "scope_id": str(sid),
                "kind": kind,
                "last_id": int(last_id),
                "message_count": int(cnt),
                "last_message": message,
                "last_time": timestamp,
            })
        return out

    def read_chat_feed(self, kind: str, scope_id: str, after_id: int = 0,
                       limit: int = 100) -> tuple[list[dict], bool]:
        """读某会话的消息流，并把该条的回合痕迹摘要挂上去。

        返回 (messages, has_more)。`after_id>0` 时只取更新的消息（增量轮询）。
        `trace` 只有糖糖的回复可能有——靠 `event_key` 关联 `turn_traces`，
        只带摘要（思维链全文走 `/trace` 单独拉，避免一次传输几十 KB）。
        """
        kind = "group" if kind == "group" else "private"
        limit = max(1, min(int(limit), 500))
        scope_id = str(scope_id or "")
        if not scope_id:
            return [], False
        try:
            after = max(0, int(after_id or 0))
        except (TypeError, ValueError):
            after = 0
        col = "c.group_id" if kind == "group" else "c.qq_id"
        # ⚠ 私聊口径的第二半（2026-10-25 第 17 轮修，与 list_chat_scopes 同源）：
        # 只按 `c.qq_id=?` 取，会把该 QQ 在**所有群**里的发言都塞进他的「私聊」——
        # 实测 `private:2959482956` 最近 8 条全是 `群833475319` 的消息。
        # 私聊 = `group_id` 为空串（全库 21,435 条；糖糖的私聊回复同样挂空串，
        # 实测 bot 回复 10,505 条在空串 9,669 条在群，所以加这条不会误伤她的回复）。
        scope_cond = (f"{col} = ?" if kind == "group"
                      else f"{col} = ? AND COALESCE(c.group_id,'') = ''")
        select = (
            "SELECT c.id, c.qq_id, c.is_bot_reply, c.message, c.timestamp, "
            "       COALESCE(c.event_key,'') AS event_key, "
            "       COALESCE(p.nickname,'') AS nickname, "
            "       t.run_id, t.duration_ms, "
            "       LENGTH(COALESCE(t.reasoning_text,'')) AS reasoning_chars, "
            "       t.tool_steps_json, COALESCE(d.status,'') AS run_status, "
            "       COALESCE(c.speaker_qq,'') AS speaker_qq, "
            "       COALESCE(c.segments,'') AS segments "
            "FROM chat_log c "
            "LEFT JOIN people p ON p.qq_id = c.qq_id "
            # 糖糖回复的 event_key 是 `delivery:<触发消息id>:<气泡序号>`，而
            # turn_traces 记的是**触发消息**的 `v2:` 键——直接 join 命中 0 条
            # （2026-10-24 实测：380 条带键的 bot 回复，命中 0）。所以先按
            # `delivery:` 解析出触发消息 id 找回那一行，再用它的键去关联。
            # `CAST('299209:1' AS INTEGER)` 在 SQLite 里取前缀数字 = 299209。
            "LEFT JOIN chat_log trig ON c.event_key LIKE 'delivery:%' "
            "  AND trig.id = CAST(substr(c.event_key, 10) AS INTEGER) "
            "LEFT JOIN turn_traces t "
            "  ON t.event_key = COALESCE(NULLIF(trig.event_key,''), c.event_key) "
            "LEFT JOIN decision_runs d ON d.run_id = t.run_id "
        )
        # 首次（after=0）要的是**最近** limit 条，不是最早的 limit 条——
        # 镜像打开就该看到最新对话，往上翻才是历史。
        if after > 0:
            sql = (select + f"WHERE {scope_cond} AND COALESCE(c.quarantined_at,'') = '' "
                   "AND c.id > ? ORDER BY c.id ASC LIMIT ?")
            params = (scope_id, after, limit + 1)
            reverse = False
        else:
            sql = (select + f"WHERE {scope_cond} AND COALESCE(c.quarantined_at,'') = '' "
                   "ORDER BY c.id DESC LIMIT ?")
            params = (scope_id, limit + 1)
            reverse = True
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        if reverse:
            rows = list(reversed(rows))
        messages: list[dict] = []
        for r in rows:
            is_bot = bool(r[2])
            steps = []
            if r[10]:
                try:
                    steps = json.loads(r[10]) or []
                except (ValueError, TypeError):
                    steps = []
            run_id = str(r[7] or "")
            # speaker_qq 非空 = 桌面端代发：QQ 上显示是糖糖的号，但打字的是主人。
            # 不显示成「糖糖」——那会让镜像撒谎（她本人根本没打过这段字）。
            if r[12]:
                speaker = "你（桌面代发）"
            elif is_bot:
                speaker = "糖糖"
            else:
                speaker = str(r[6] or "") or f"QQ{r[1]}"
            messages.append({
                "id": int(r[0]),
                "role": "tangtang" if is_bot else "user",
                "speaker": speaker,
                "content": str(r[3] or ""),
                "ts": _parse_chat_ts(str(r[4] or "")),
                "event_key": str(r[5] or ""),
                # 原始 segments 只给上层（console_api）解析媒体用，序列化前要去掉
                "segments": str(r[13] or ""),
                "trace": ({
                    "run_id": run_id,
                    "reasoning_chars": int(r[9] or 0),
                    "tool_count": len(steps) if isinstance(steps, list) else 0,
                    "duration_ms": int(r[8] or 0),
                    "status": str(r[11] or "completed"),
                    "event_key": str(r[5] or ""),
                } if run_id else None),
            })
        # 痕迹归属修正：`DecisionRun.event_key` 是**入站**消息的键（这一轮由谁触发），
        # 所以 LEFT JOIN 出来的 trace 天然挂在「用户那句」上。但读者要看的是
        # 「她这条回复是怎么想出来的」——把它挪到紧随其后的糖糖消息上。
        pending = None
        for msg in messages:
            if msg["role"] == "user":
                if msg["trace"]:
                    pending = msg["trace"]
                    msg["trace"] = None
            elif pending is not None:
                msg["trace"] = pending
                pending = None
        return messages, has_more

    def list_group_names(self) -> dict[str, str]:
        """group_id → 群名（会客厅左侧显示用）。没有群名的群不返回。"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT group_id, group_name FROM group_info "
                    "WHERE COALESCE(group_name,'') != ''"
                ).fetchall()
            return {str(r[0]): str(r[1]) for r in rows}
        except Exception:
            return {}

    def get_user_recent_messages(self, qq_id: str, limit: int = 20,
                                 group_id: str | None = None) -> list[str]:
        """获取某用户最近发言；group_id=None 表示不限定，空串表示仅私聊。"""
        try:
            with self._connect() as conn:
                group_clause = " AND group_id=?" if group_id is not None else ""
                params = [str(qq_id)]
                if group_id is not None:
                    params.append(group_id)
                params.append(limit)
                rows = conn.execute(
                    "SELECT message, timestamp, group_id FROM chat_log "
                    f"WHERE qq_id = ? AND is_bot_reply = 0{group_clause}"
                    " AND COALESCE(quarantined_at,'')='' "
                    "ORDER BY id DESC LIMIT ?",
                    tuple(params)
                ).fetchall()
            return [f"[{r[1]}] {r[2] if r[2] else '私聊'}: {r[0][:120]}" for r in reversed(rows)]
        except Exception:
            return []

    def get_recent_dialogue(self, qq_id: str, limit: int = 12,
                            group_id: str | None = None) -> list[str]:
        """获取最近对话；group_id=None 表示不限定，空串表示仅私聊。

        2026-08-15：主动说话此前只有记忆/画像/驱动力，没有最近对话——
        糖糖不知道上次聊到哪，主动开口像凭空冒出来。
        """
        try:
            with self._connect() as conn:
                group_clause = " AND group_id=?" if group_id is not None else ""
                params = [str(qq_id)]
                if group_id is not None:
                    params.append(str(group_id))
                params.append(limit)
                rows = conn.execute(
                    "SELECT message, timestamp, is_bot_reply FROM chat_log "
                    f"WHERE qq_id = ?{group_clause}"
                    " AND COALESCE(quarantined_at,'')='' "
                    "ORDER BY id DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            out = []
            for msg, ts, is_bot in reversed(rows):
                who = "糖糖" if is_bot else "ta"
                out.append(f"[{str(ts)[:16]}] {who}: {str(msg)[:80]}")
            return out
        except Exception:
            return []

    def get_unprocessed_messages(self, qq_id: str, last_id: int, limit: int = 20,
                                  newest_first: bool = False,
                                  include_bot_replies: bool = False,
                                  lower_bound_id: int = 0) -> list[dict]:
        """获取某人尚未被 LLM 提取的消息。
        newest_first=False: 从最早未处理开始（正常流式提取）
        newest_first=True:  从最新消息倒序（回填——先记最近的事）
        include_bot_replies（2026-08-16 批 4）：带糖糖的私聊回复——提取批次有
        对话语境，LLM 才能分辨玩笑/引用/测试语句（单句「特摄仙人」零语境入库事故）"""
        order = "DESC" if newest_first else "ASC"
        op = "<" if newest_first else ">"
        if include_bot_replies:
            cond = "(is_bot_reply = 0 OR (is_bot_reply = 1 AND group_id = ''))"
        else:
            cond = "is_bot_reply = 0"
        lower_sql = " AND id > ?" if newest_first and lower_bound_id > 0 else ""
        params = [qq_id, last_id]
        if lower_sql:
            params.append(lower_bound_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT id, message, timestamp, is_bot_reply, group_id FROM chat_log
                   WHERE qq_id = ? AND {cond} AND id {op} ?
                   AND COALESCE(quarantined_at,'')=''
                   {lower_sql}
                   ORDER BY id {order} LIMIT ?""",
                params
            ).fetchall()
        return [{"id": r[0], "message": r[1], "timestamp": r[2],
                 "is_bot_reply": r[3], "group_id": r[4] or ""}
                for r in rows]

    def count_unprocessed_messages(self, qq_id: str, last_id: int,
                                   before_id: int = 0) -> int:
        """精确统计某用户游标后的用户消息数；可用 before_id 限定开区间上界。

        chat_log.id 是全表自增值，不能用 MAX(id)-last_id 充当消息条数。
        """
        upper_sql = " AND id < ?" if before_id > 0 else ""
        params = [qq_id, last_id]
        if upper_sql:
            params.append(before_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_log"
                " WHERE qq_id=? AND is_bot_reply=0 AND id>?"
                " AND COALESCE(quarantined_at,'')=''" + upper_sql,
                params,
            ).fetchone()
        return int(row[0]) if row else 0

    def find_last_group(self, qq_id: str) -> str:
        """查聊天记录，找到对方最后发言的群号"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT group_id FROM chat_log WHERE qq_id = ? AND group_id != ''"
                " AND COALESCE(quarantined_at,'')='' "
                "ORDER BY id DESC LIMIT 1",
                (qq_id,)
            ).fetchone()
        return row[0] if row else ""

    def get_earliest_chats(self, qq_id: str, limit: int = 5) -> list[dict]:
        """获取与某人的早期有意义对话——过滤纯图/纯表情/单字寒暄"""
        import re as _re
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # 取最早 50 条，从中筛选有实质内容的
            rows = conn.execute(
                "SELECT is_bot_reply, message FROM chat_log WHERE qq_id=? AND group_id=''"
                " AND COALESCE(quarantined_at,'')='' "
                "ORDER BY id ASC LIMIT 50",
                (qq_id,)
            ).fetchall()

        results = []
        for r in rows:
            msg = r["message"] or ""
            # 剥离 CQ 码
            clean = _re.sub(r'\[CQ:[^\]]+\]', '', msg).strip()
            # 过滤：纯图、纯表情、单字寒暄、过短
            if not clean:
                continue
            if clean in ("在吗", "嗯", "哦", "好", "在", "嗯嗯", "哈哈", "。。。", "…"):
                continue
            if len(clean) < 4:
                continue
            results.append({"is_bot": bool(r["is_bot_reply"]), "message": clean[:120]})
            if len(results) >= limit:
                break
        return results

    def search_chat_history(self, qq_id: str, keywords: list[str], limit: int = 5,
                            group_id: str = "", include_bot_replies: bool = False,
                            bot_qq: str = "") -> list[dict]:
        """按关键词搜索聊天记录——用于按需记忆检索。

        默认仍保持旧契约（指定用户的私聊、只看用户发言）；需要核验糖糖
        自己说过的话时显式传 group_id/include_bot_replies/bot_qq，避免把
        私聊和群聊、用户和机器人说话混在一起。
        """
        import re as _re
        results = []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for kw in keywords[:3]:
                params = [f"%{kw}%"]
                if qq_id:
                    speaker = "(qq_id=?"
                    params.append(qq_id)
                    if include_bot_replies and bot_qq:
                        speaker += " OR (qq_id=? AND is_bot_reply=1)"
                        params.append(bot_qq)
                    speaker += ")"
                else:
                    speaker = "1=1"
                group_clause = " AND group_id=?" if group_id else (" AND group_id=''" if not group_id and not include_bot_replies else "")
                if group_id:
                    params.append(group_id)
                params.append(limit)
                rows = conn.execute(
                    "SELECT is_bot_reply, message, timestamp FROM chat_log "
                    f"WHERE message LIKE ? AND {speaker}"
                    " AND COALESCE(quarantined_at,'')=''"
                    f"{' ' + group_clause if group_clause else ''}"
                    f"{' ' if include_bot_replies else ' AND is_bot_reply=0 '}"
                    "ORDER BY id DESC LIMIT ?",
                    tuple(params)
                ).fetchall()
                for r in rows:
                    msg = r["message"] or ""
                    clean = _re.sub(r'\[CQ:[^\]]+\]', '', msg).strip()
                    if not clean or len(clean) < 4:
                        continue
                    if clean in ("在吗", "嗯", "哦", "好", "在", "嗯嗯", "哈哈"):
                        continue
                    results.append({
                        "is_bot": bool(r["is_bot_reply"]),
                        "message": clean[:150],
                        "time": r["timestamp"] or "",
                    })
        # 去重 + 按时间排序
        seen = set()
        unique = []
        for r in sorted(results, key=lambda x: x["time"]):
            key = r["message"][:60]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[-8:]

    def find_recent_image_senders(self, group_id: str, bot_qq: str, limit: int = 50) -> list[tuple]:
        """找最近在群里发图/表情的群友 (qq_id, nickname)"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.qq_id, p.nickname
                FROM chat_log c
                LEFT JOIN people p ON c.qq_id = p.qq_id
                WHERE c.group_id = ? AND c.qq_id != ? AND c.is_bot_reply = 0
                  AND (c.message LIKE '%[CQ:image%' OR c.message LIKE '%[CQ:face%')
                GROUP BY c.qq_id
                ORDER BY MAX(c.id) DESC
                LIMIT ?
            """, (group_id, bot_qq, limit)).fetchall()
        return rows

    def get_active_groups(self) -> list[str]:
        """获取所有有 bot 回复记录的群号"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT group_id FROM chat_log WHERE is_bot_reply = 1 AND group_id != ''"
            ).fetchall()
        return [r[0] for r in rows]

    def get_recently_active_chats(self, minutes: int = 30) -> list[dict]:
        """获取最近 N 分钟内有互动的会话（群聊和私聊）。
        返回 [{"type": "group"|"private", "id": "群号或QQ号"}, ...]"""
        from datetime import datetime, timedelta
        since_ts = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        logger.debug(f"📢 get_recently_active_chats: cutoff={since_ts}")
        with self._connect() as conn:
            # 群聊：最近 N 分钟内有非 bot 消息的群
            groups = conn.execute(
                "SELECT DISTINCT group_id FROM chat_log "
                "WHERE group_id != '' AND is_bot_reply = 0 AND timestamp >= ?"
                " AND COALESCE(quarantined_at,'')=''",
                (since_ts,)
            ).fetchall()
            logger.debug(f"📢 活跃群聊: {[(g[0],) for g in groups]}")
            # 私聊：最近 N 分钟内有非 bot 消息的会话
            privates = conn.execute(
                "SELECT DISTINCT qq_id FROM chat_log "
                "WHERE group_id = '' AND is_bot_reply = 0 AND timestamp >= ?"
                " AND COALESCE(quarantined_at,'')=''",
                (since_ts,)
            ).fetchall()
            logger.debug(f"📢 活跃私聊: {[(p[0],) for p in privates]}")
        result = []
        for (gid,) in groups:
            if gid:
                result.append({"type": "group", "id": gid})
        for (uid,) in privates:
            if uid:
                result.append({"type": "private", "id": uid})
        return result

    def get_group_messages_since(self, group_id: str, since: str, limit: int = 500) -> list[dict]:
        """获取某群从指定时间戳之后的非机器人消息（带昵称）"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT c.qq_id, c.message, c.timestamp,
                              COALESCE(p.nickname, c.qq_id) as nickname
                       FROM chat_log c
                       LEFT JOIN people p ON c.qq_id = p.qq_id
                       WHERE c.group_id = ? AND c.timestamp >= ? AND c.is_bot_reply = 0
                         AND COALESCE(c.quarantined_at,'')=''
                       ORDER BY c.id ASC LIMIT ?""",
                    (group_id, since, limit)
                ).fetchall()
            return [
                {"qq_id": r[0], "message": r[1], "timestamp": r[2], "nickname": r[3]}
                for r in rows
            ]
        except Exception:
            return []

    def get_group_message_count_since(self, group_id: str, since: str) -> int:
        """获取某群从指定时间戳之后的非机器人消息数量"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM chat_log "
                    "WHERE group_id = ? AND timestamp >= ? AND is_bot_reply = 0"
                    " AND COALESCE(quarantined_at,'')=''",
                    (group_id, since)
                ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def get_recent_group_messages(self, group_id: str, limit: int = 30) -> list[dict]:
        """获取某群最近 N 条消息（不限时间），给 LLM 摘要做上下文锚点"""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT c.qq_id, c.message, c.timestamp,
                              COALESCE(p.nickname, c.qq_id) as nickname
                       FROM chat_log c
                       LEFT JOIN people p ON c.qq_id = p.qq_id
                       WHERE c.group_id = ? AND c.is_bot_reply = 0
                         AND COALESCE(c.quarantined_at,'')=''
                       ORDER BY c.id DESC LIMIT ?""",
                    (group_id, limit)
                ).fetchall()
            result = [
                {"qq_id": r[0], "message": r[1], "timestamp": r[2], "nickname": r[3]}
                for r in reversed(rows)
            ]
            return result
        except Exception:
            return []

    # ═══════════════════════════════════════
    # 统计
    # ═══════════════════════════════════════

    def get_stats(self, qq_id: str) -> dict:
        """获取与某人的互动统计（不含记忆列表，纯数值）"""
        person = self.get_or_create_person(qq_id)
        with self._connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE qq_id = ?", (qq_id,)
            ).fetchone()
        memory_count = count_row[0] if count_row else 0
        return {
            "nickname": person.get("nickname", ""),
            "intimacy": person.get("intimacy", 0),
            "relationship": person.get("relationship", "stranger"),
            "total_chats": person.get("total_chats", 0),
            "memory_count": memory_count,
        }

    # ═══════════════════════════════════════
    # 关系档案
    # ═══════════════════════════════════════

    def set_relationship_summary(self, qq_id: str, summary: str, *,
                                 actor: str = "unknown", reason: str = "") -> bool:
        """更新关系摘要。

        2026-10-24：这是就地覆盖——上一版「糖糖怎么看这段关系」会消失，
        所以写流水保留旧版。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(relationship_summary,'') FROM people WHERE qq_id=?",
                (qq_id,),
            ).fetchone()
            previous = str(row[0] or "") if row else ""
            if previous != str(summary or ""):
                self._record_revision(
                    conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                    op="update", field="relationship_summary",
                    before=previous, after=str(summary or ""),
                    reason=reason or "关系摘要重写", actor=actor,
                )
            conn.execute("UPDATE people SET relationship_summary=? WHERE qq_id=?", (summary, qq_id))
        return True

    def get_relationship_summary(self, qq_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT relationship_summary FROM people WHERE qq_id=?", (qq_id,)).fetchone()
            return (row[0] or "") if row else ""

    def _update_relationship_timestamp(self, qq_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE people SET relationship_updated=(SELECT total_chats FROM people WHERE qq_id=?) WHERE qq_id=?",
                (qq_id, qq_id)
            )

    # ═══════════════════════════════════════
    # 生日管理
    # ═══════════════════════════════════════

    def set_birthday(self, qq_id: str, birthday: str, *,
                     actor: str = "unknown", reason: str = "") -> bool:
        """设置群友生日，格式: MM-DD 或 M月D日

        2026-10-24：生日被写错一次就再也没法对照（比如「10-24」被记成
        「12-04」）。旧值写流水。
        """
        import re
        # 统一为 MM-DD
        m = re.match(r'(\d{1,2})月(\d{1,2})日', birthday)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
        else:
            m = re.match(r'(\d{2})-(\d{2})', birthday)
            if m:
                month, day = int(m.group(1)), int(m.group(2))
            else:
                return False
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return False
        birthday = f"{month:02d}-{day:02d}"
        # 确保群友档案存在
        self.get_or_create_person(qq_id)
        with self._connect() as conn:
            previous_row = conn.execute(
                "SELECT COALESCE(birthday,'') FROM people WHERE qq_id=?", (qq_id,)
            ).fetchone()
            previous = str(previous_row[0] or "") if previous_row else ""
            if previous != birthday:
                self._record_revision(
                    conn, entity="people", entity_id=qq_id, qq_id=str(qq_id),
                    op="update", field="birthday",
                    before=previous, after=birthday,
                    reason=reason or "记录生日", actor=actor,
                )
            conn.execute(
                "UPDATE people SET birthday = ? WHERE qq_id = ?",
                (birthday, qq_id)
            )
            conn.commit()
        return True

    def get_birthday(self, qq_id: str) -> str:
        """获取群友生日，返回 MM-DD 或空"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT birthday FROM people WHERE qq_id = ?", (qq_id,)
            ).fetchone()
        return (row[0] if row and row[0] else "")

    def get_today_birthdays(self) -> list[dict]:
        """获取今天过生日的群友列表"""
        today = datetime.now()
        bday = today.strftime("%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, nickname, birthday FROM people WHERE birthday = ?",
                (bday,)
            ).fetchall()
        return [
            {"qq_id": r[0], "nickname": r[1], "birthday": r[2]}
            for r in rows
        ]

    def get_all_birthdays(self) -> list[dict]:
        """获取所有已记录的生日"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id, nickname, birthday FROM people WHERE birthday != '' ORDER BY birthday"
            ).fetchall()
        return [
            {"qq_id": r[0], "nickname": r[1], "birthday": r[2]}
            for r in rows
        ]

    def get_global_stats(self) -> dict:
        """全局统计：人数、记忆数、聊天记录数"""
        with self._connect() as conn:
            people_n = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            memories_n = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active')='active'"
            ).fetchone()[0]  # 2026-08-16 Codex M2：审计行不算业务记忆
            chats_n = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE COALESCE(quarantined_at,'')=''"
            ).fetchone()[0]
        return {
            "people_count": people_n,
            "memory_count": memories_n,
            "chat_count": chats_n,
        }

    # ═══════════════════════════════════════
    # 任务/提醒
    # ═══════════════════════════════════════

    def create_task(self, owner_qq: str, description: str, remind_at: str, group_id: str = "",
                    action_payload: dict | None = None, idempotency_key: str = "") -> int:
        """创建一个任务。remind_at 格式：YYYY-MM-DD HH:MM。group_id 非空 = 到点发群里。

        P0-C（2026-08-28）：action_payload 为 typed 动作（text/sticker_emotion/
        voice_text 可组合）；idempotency_key 非空时幂等——同 key 返回既有任务 id，
        不重复创建（LLM 工具重调用/重投场景）。旧调用（无新参数）行为不变。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_json = json.dumps(action_payload, ensure_ascii=False) if action_payload else ""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO tasks (owner_qq, description, remind_at, created_at, status,"
                    " group_id, action_payload, idempotency_key) "
                    "VALUES (?,?,?,?,'pending',?,?,?)",
                    (owner_qq, description, remind_at, now, group_id,
                     payload_json, idempotency_key)
                )
                conn.commit()
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            # P0-C（Codex 复核收窄）：只吞「非空 idempotency_key 的唯一键冲突」
            # （同源事件重试）——其他完整性错误必须重抛，避免静默吞错。
            if idempotency_key and "tasks.idempotency_key" in str(e):
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT id FROM tasks WHERE idempotency_key=?", (idempotency_key,)
                    ).fetchone()
                    if row:
                        return int(row[0])
            raise

    def get_due_tasks(self) -> list[dict]:
        """获取所有到期未发送的提醒"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='pending' AND remind_at <= ?", (now,)
            ).fetchall()
            return [dict(r) for r in rows]

    def claim_task_for_send(self, task_id: int) -> bool:
        """原子领取待发送提醒；只有领取者能执行外部发送。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='sending' WHERE id=? AND status='pending'",
                (task_id,),
            )
            return cur.rowcount > 0

    def mark_task_done(self, task_id: int) -> None:
        """标记任务已完成"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=? AND status='sending'",
                (task_id,),
            )

    def mark_task_uncertain(self, task_id: int) -> None:
        """记录提醒已被网关接受但未确认送达，禁止后台循环盲目重放。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='uncertain' WHERE id=? AND status='sending'",
                (task_id,),
            )

    def release_task_claim(self, task_id: int) -> bool:
        """确定未送达时释放领取，允许下一轮重试。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending' WHERE id=? AND status='sending' "
                "AND current_attempt_id IS NULL",
                (task_id,),
            )
            return cur.rowcount == 1

    def recover_sending_tasks(self) -> int:
        """只隔离旧式裸 sending；linked outbox 由其唯一 owner 自行恢复。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='uncertain' "
                "WHERE status='sending' AND current_attempt_id IS NULL"
            )
            return cur.rowcount

    def retry_task(self, task_id: int, owner_qq: str) -> bool:
        """仅恢复 legacy uncertain；linked retry 等待 Phase 2 核验契约。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT id,owner_qq,group_id,status,current_attempt_id FROM tasks "
                "WHERE id=? AND owner_qq=?",
                (int(task_id), str(owner_qq)),
            ).fetchone()
            if not task or task["status"] != "uncertain":
                conn.rollback()
                return False
            current_attempt_id = task["current_attempt_id"]
            if current_attempt_id is None:
                cur = conn.execute(
                    "UPDATE tasks SET status='pending' "
                    "WHERE id=? AND owner_qq=? AND status='uncertain' "
                    "AND current_attempt_id IS NULL",
                    (int(task_id), str(owner_qq)),
                )
                conn.commit()
                return cur.rowcount == 1
            conn.rollback()
            return False

    # ═══════════════════════════════════════
    # 意见征集（2026-08-16）——主人发起、糖糖私聊发布、窗口收集
    # ═══════════════════════════════════════

    def create_opinion_campaign(self, topic: str) -> int:
        """创建征集活动，返回 campaign_id"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO opinion_campaigns (topic, status, created_at) VALUES (?, 'open', ?)",
                (topic, now),
            )
            return cur.lastrowid

    def create_opinion_campaign_with_participants(
            self, topic: str, participants: list[tuple[str, str]],
            bot_qq: str, opening_message: str) -> int | None:
        """原子创建活动、排队参与者和发起消息。

        返回 campaign_id；若已有 open 活动则返回 ``None``。所有写入共享一个
        ``BEGIN IMMEDIATE`` 事务，避免活动创建到一半时留下 open 半成品，且
        通过数据库唯一索引收口跨进程竞态。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM opinion_campaigns WHERE status='open' LIMIT 1"
            ).fetchone()
            if existing:
                conn.rollback()
                return None
            cur = conn.execute(
                "INSERT INTO opinion_campaigns (topic, status, created_at) "
                "VALUES (?, 'open', ?)",
                (topic, now),
            )
            campaign_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO opinion_participants "
                "(campaign_id, qq_id, nickname, status, last_msg_ts) "
                "VALUES (?, ?, ?, 'queued', ?)",
                [
                    (campaign_id, str(qq_id), str(nickname), now)
                    for qq_id, nickname in participants
                ],
            )
            conn.execute(
                "INSERT INTO opinion_messages "
                "(campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (campaign_id, str(bot_qq), "糖糖", opening_message, now),
            )
            conn.commit()
            return campaign_id

    def add_opinion_participant(self, campaign_id: int, qq_id: str, nickname: str,
                                status: str = "pending") -> None:
        # 2026-08-16 Codex：last_msg_ts 记录邀请发出时间——pending 超时判定用
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO opinion_participants "
                "(campaign_id, qq_id, nickname, status, last_msg_ts) VALUES (?, ?, ?, ?, ?)",
                (campaign_id, qq_id, nickname, status, now),
            )

    def update_opinion_participant(self, campaign_id: int, qq_id: str, status: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "UPDATE opinion_participants SET status=?, last_msg_ts=?, "
                "claim_token='', claim_ts='' "
                "WHERE campaign_id=? AND qq_id=?",
                (status, now, campaign_id, qq_id),
            )

    def get_opinion_participants(self, campaign_id: int) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM opinion_participants WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_opinion_participant(self, campaign_id: int, qq_id: str) -> dict | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM opinion_participants WHERE campaign_id=? AND qq_id=?",
                (campaign_id, qq_id),
            ).fetchone()
            return dict(row) if row else None

    def claim_opinion_invite(
            self, campaign_id: int, qq_id: str, claim_token: str,
            lease_seconds: int = 900) -> bool:
        """原子认领 queued 邀请，防止多实例/重启重复发送。

        只有没有有效租约的 queued 行能被认领；进程在外部发送前崩溃时，
        租约过期后可再次认领。状态仍保持 queued，避免把“已发送”伪装成
        pending；真正发送前由调用方转为 invite_uncertain。
        """
        token = str(claim_token or "").strip()
        if not token:
            raise ValueError("claim_token is required")
        now = datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_text = (now - timedelta(seconds=max(1, int(lease_seconds)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE opinion_participants SET claim_token=?, claim_ts=? "
                "WHERE campaign_id=? AND qq_id=? AND status='queued' "
                "AND (COALESCE(claim_token,'')='' OR COALESCE(claim_ts,'') < ?)",
                (token, now_text, campaign_id, qq_id, cutoff_text),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True

    def mark_opinion_invite_uncertain(
            self, campaign_id: int, qq_id: str, claim_token: str = "") -> bool:
        """发送前把已认领的 queued 邀请冻结为 invite_uncertain。

        带 token 时只允许对应租约转换；无 token 仅为旧版离线 fake 兼容，
        正式调度始终传入 token。
        """
        token = str(claim_token or "").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if token:
                updated = conn.execute(
                    "UPDATE opinion_participants SET status='invite_uncertain', last_msg_ts=? "
                    "WHERE campaign_id=? AND qq_id=? AND status='queued' AND claim_token=? "
                    "AND EXISTS (SELECT 1 FROM opinion_campaigns c "
                    "WHERE c.id=? AND c.status='open')",
                    (now, campaign_id, qq_id, token, campaign_id),
                )
            else:
                updated = conn.execute(
                    "UPDATE opinion_participants SET status='invite_uncertain', last_msg_ts=? "
                    "WHERE campaign_id=? AND qq_id=? AND status='queued' "
                    "AND COALESCE(claim_token,'')='' "
                    "AND EXISTS (SELECT 1 FROM opinion_campaigns c "
                    "WHERE c.id=? AND c.status='open')",
                    (now, campaign_id, qq_id, campaign_id),
                )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True

    def set_opinion_invite_delivery_state(
            self, campaign_id: int, qq_id: str, claim_token: str,
            status: str) -> bool:
        """按邀请租约收口未确认/失败状态，拒绝过期租约复活。"""
        if status not in {"invite_uncertain", "invite_failed"}:
            raise ValueError("unsupported invite delivery state")
        token = str(claim_token or "").strip()
        if not token:
            raise ValueError("claim_token is required")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE opinion_participants SET status=?, last_msg_ts=?, "
                "claim_token=CASE WHEN ?='invite_failed' THEN '' ELSE claim_token END, "
                "claim_ts=CASE WHEN ?='invite_failed' THEN '' ELSE claim_ts END "
                "WHERE campaign_id=? AND qq_id=? AND status='invite_uncertain' "
                "AND claim_token=? AND EXISTS (SELECT 1 FROM opinion_campaigns c "
                "WHERE c.id=? AND c.status='open')",
                (status, now, status, status, campaign_id, qq_id, token, campaign_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True

    def record_opinion_response(
            self, campaign_id: int, qq_id: str, nickname: str,
            message: str, verdict: str) -> str:
        """按当前参与者状态原子收录一条用户回复。

        LLM 判定在事务外完成；提交时重新读取状态并做 CAS，防止自动关窗在
        判定等待期间将 ``pending/participating`` 终结后又被旧结果复活。
        返回 ``participating``、``refused``、``done``、``recorded``、
        ``ignored`` 或 ``stale``。
        """
        verdict = str(verdict or "").strip().lower()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            campaign = conn.execute(
                "SELECT status FROM opinion_campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign or campaign[0] != "open":
                conn.rollback()
                return "stale"
            row = conn.execute(
                "SELECT status FROM opinion_participants "
                "WHERE campaign_id=? AND qq_id=?",
                (campaign_id, qq_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return "stale"
            status = str(row[0] or "")
            if verdict in {"agree", "refuse"}:
                if status not in {"pending", "invite_uncertain", "queued"}:
                    conn.rollback()
                    return "stale"
                next_status = "participating" if verdict == "agree" else "refused"
                updated = conn.execute(
                    "UPDATE opinion_participants SET status=?, last_msg_ts=? "
                    "WHERE campaign_id=? AND qq_id=? AND status IN "
                    "('pending','invite_uncertain','queued')",
                    (next_status, now, campaign_id, qq_id),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return "stale"
                conn.execute(
                    "INSERT INTO opinion_messages "
                    "(campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (campaign_id, qq_id, nickname, message, now),
                )
                conn.commit()
                return next_status

            if status != "participating":
                conn.rollback()
                return "stale"
            if verdict == "close":
                updated = conn.execute(
                    "UPDATE opinion_participants SET status='done', last_msg_ts=? "
                    "WHERE campaign_id=? AND qq_id=? AND status='participating'",
                    (now, campaign_id, qq_id),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return "stale"
                conn.execute(
                    "INSERT INTO opinion_messages "
                    "(campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (campaign_id, qq_id, nickname, message, now),
                )
                conn.commit()
                return "done"
            if verdict == "keep":
                conn.execute(
                    "INSERT INTO opinion_messages "
                    "(campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (campaign_id, qq_id, nickname, message, now),
                )
                conn.commit()
                return "recorded"
            conn.rollback()
            return "ignored"

    def close_opinion_participant_if_idle(
            self, campaign_id: int, qq_id: str,
            observed_message_id: int) -> bool:
        """仅在快照仍是最新用户消息时把参与者 CAS 关闭。

        自动关窗先计算空闲时间，提交时再次核对最新非 bot 消息 ID；
        多自治实例或并发新消息不会重复发送致谢、也不会把新意见标成 done。
        """
        try:
            observed_id = int(observed_message_id)
        except (TypeError, ValueError):
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT id FROM opinion_messages "
                "WHERE campaign_id=? AND qq_id=? AND is_bot=0 "
                "ORDER BY id DESC LIMIT 1",
                (campaign_id, qq_id),
            ).fetchone()
            if not latest or int(latest[0]) != observed_id:
                conn.rollback()
                return False
            updated = conn.execute(
                "UPDATE opinion_participants SET status='done', last_msg_ts=?, "
                "claim_token='', claim_ts='' "
                "WHERE campaign_id=? AND qq_id=? AND status='participating'",
                (now, campaign_id, qq_id),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True

    def add_opinion_message(self, campaign_id: int, qq_id: str, nickname: str,
                            message: str, is_bot: bool = False) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO opinion_messages (campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, qq_id, nickname, message, 1 if is_bot else 0, now),
            )

    def settle_opinion_invite_confirmed(
            self, campaign_id: int, qq_id: str, nickname: str, message: str,
            claim_token: str = "") -> bool:
        """确认送达后原子落 pending 状态与邀请消息。

        外部发送已确认但数据库写入失败时，事务回滚并保留
        ``invite_uncertain``，宁可要求人工核验，也不留下 pending 却没有
        邀请正文的半成品。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        token = str(claim_token or "").strip()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if token:
                updated = conn.execute(
                    "UPDATE opinion_participants SET status='pending', last_msg_ts=?, "
                    "claim_token='', claim_ts='' "
                    "WHERE campaign_id=? AND qq_id=? AND status='invite_uncertain' "
                    "AND claim_token=? AND EXISTS (SELECT 1 FROM opinion_campaigns c "
                    "WHERE c.id=? AND c.status='open')",
                    (now, campaign_id, qq_id, token, campaign_id),
                )
            else:
                updated = conn.execute(
                    "UPDATE opinion_participants SET status='pending', last_msg_ts=?, "
                    "claim_token='', claim_ts='' "
                    "WHERE campaign_id=? AND qq_id=? AND status='invite_uncertain' "
                    "AND COALESCE(claim_token,'')='' "
                    "AND EXISTS (SELECT 1 FROM opinion_campaigns c "
                    "WHERE c.id=? AND c.status='open')",
                    (now, campaign_id, qq_id, campaign_id),
                )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO opinion_messages "
                "(campaign_id, qq_id, nickname, message, is_bot, timestamp) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (campaign_id, qq_id, nickname, message, now),
            )
            conn.commit()
            return True

    def get_opinion_messages(self, campaign_id: int) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM opinion_messages WHERE campaign_id=? ORDER BY id", (campaign_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_open_opinion_campaign(self) -> dict | None:
        """当前打开的征集活动（同时最多一个）"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM opinion_campaigns WHERE status='open' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def close_opinion_campaign(self, campaign_id: int) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "UPDATE opinion_campaigns SET status='closed', closed_at=? WHERE id=?",
                (now, campaign_id),
            )

    def close_opinion_campaign_snapshot(self, campaign_id: int) -> dict | None:
        """原子冻结活动快照并关闭活动，阻止迟到回复污染已导出视图。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            campaign_row = conn.execute(
                "SELECT * FROM opinion_campaigns WHERE id=? AND status='open'",
                (campaign_id,),
            ).fetchone()
            if not campaign_row:
                conn.rollback()
                return None
            parts = [dict(row) for row in conn.execute(
                "SELECT * FROM opinion_participants WHERE campaign_id=? ORDER BY id",
                (campaign_id,),
            ).fetchall()]
            messages = [dict(row) for row in conn.execute(
                "SELECT * FROM opinion_messages WHERE campaign_id=? ORDER BY id",
                (campaign_id,),
            ).fetchall()]
            for participant in parts:
                if participant["status"] == "pending":
                    conn.execute(
                        "UPDATE opinion_participants SET status='no_reply', "
                        "last_msg_ts=?, claim_token='', claim_ts='' "
                        "WHERE campaign_id=? AND qq_id=? AND status='pending'",
                        (now, campaign_id, participant["qq_id"]),
                    )
                    participant["status"] = "no_reply"
                elif participant["status"] in {"queued", "invite_uncertain"}:
                    conn.execute(
                        "UPDATE opinion_participants SET status='invite_expired', "
                        "last_msg_ts=?, claim_token='', claim_ts='' "
                        "WHERE campaign_id=? AND qq_id=? AND status IN "
                        "('queued','invite_uncertain')",
                        (now, campaign_id, participant["qq_id"]),
                    )
                    participant["status"] = "invite_expired"
            conn.execute(
                "UPDATE opinion_campaigns SET status='closed', closed_at=? WHERE id=?",
                (now, campaign_id),
            )
            conn.commit()
            campaign = dict(campaign_row)
            campaign["status"] = "closed"
            campaign["closed_at"] = now
            return {"campaign": campaign, "parts": parts, "messages": messages}

    def list_tasks(self, owner_qq: str) -> list[dict]:
        """列出某人的可操作提醒，并附当前 generation/attempt 令牌。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT t.*,a.generation AS current_generation,"
                "a.state AS attempt_state,a.accounting_state AS accounting_state,"
                "(SELECT o.status FROM send_outbox o "
                " WHERE o.task_attempt_id=a.id ORDER BY o.ordinal LIMIT 1) "
                "AS outbox_status "
                "FROM tasks t LEFT JOIN task_action_attempts a "
                "ON a.id=t.current_attempt_id "
                "WHERE t.owner_qq=? AND t.status IN "
                "('pending','sending','uncertain','failed','partial') "
                "ORDER BY t.remind_at,t.id",
                (owner_qq,)
            ).fetchall()
            return [dict(r) for r in rows]

    def cancel_task(self, task_id: int, owner_qq: str) -> bool:
        """取消尚未发送的任务；linked uncertain 保留审计并返回 False。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='cancelled' "
                "WHERE id=? AND owner_qq=? AND (status='pending' OR "
                "(status='uncertain' AND current_attempt_id IS NULL))",
                (task_id, owner_qq)
            )
            return cur.rowcount > 0

    # ═══════════════════════════════════════
    # 🆕 向量嵌入——语义记忆检索
    # ═══════════════════════════════════════

    def set_embedding(self, memory_id: int, embedding):
        """存储一条记忆的向量嵌入（numpy array → BLOB）"""
        import numpy as np
        blob = embedding.astype(np.float32).tobytes()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding) VALUES (?, ?)",
                (memory_id, blob)
            )
            conn.execute(
                "DELETE FROM memory_embedding_failures WHERE memory_id=?",
                (memory_id,),
            )

    def get_embedding(self, memory_id: int):
        """读取一条记忆的向量嵌入"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding FROM memory_embeddings WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row:
                import numpy as np
                return np.frombuffer(row[0], dtype=np.float32)
            return None

    def get_memories_without_embeddings(self, limit: int = 500) -> list[int]:
        """返回当前可重试的缺失 embedding；失败行冷却时不会阻塞后续。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.id FROM memories m
                   LEFT JOIN memory_embeddings e ON m.id = e.memory_id
                   LEFT JOIN memory_embedding_failures f ON m.id = f.memory_id
                   WHERE e.memory_id IS NULL AND COALESCE(m.status,'active')='active'
                   AND (f.memory_id IS NULL OR datetime(f.retry_after) <= datetime('now'))
                   ORDER BY m.importance DESC, m.id
                   LIMIT ?""",
                (limit,)
            ).fetchall()
            return [r[0] for r in rows]

    def mark_embedding_failure(self, memory_id: int, error: str = "") -> int:
        """记录毒丸并指数退避；返回累计失败次数。"""
        now = datetime.now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT failure_count FROM memory_embedding_failures WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            failure_count = int(row[0] or 0) + 1 if row else 1
            backoff_seconds = min(86400, 5 * (2 ** min(failure_count - 1, 10)))
            conn.execute(
                "INSERT OR REPLACE INTO memory_embedding_failures "
                "(memory_id,failure_count,last_error,last_attempt_at,retry_after) "
                "VALUES (?,?,?,?,?)",
                (
                    memory_id, failure_count, str(error or "")[:200],
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    (now + timedelta(seconds=backoff_seconds)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        return failure_count

    # ═══════════════════════════════════════
    # daily_digests / tangtang_journal（情节记忆）
    # ═══════════════════════════════════════

    def insert_daily_digest(self, date: str, group_id: str, summary: str,
                            topics: str = "[]", message_count: int = 0):
        """插入一条每日群摘要（2026-08-10：UPSERT——同天同群重复反思时覆盖而非插新行）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daily_digests (date, group_id, summary, message_count, topics, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date, group_id) DO UPDATE SET "
                "summary = excluded.summary, message_count = excluded.message_count, "
                "topics = excluded.topics, created_at = excluded.created_at",
                (date, group_id, summary, message_count, topics, now)
            )
            conn.commit()

    def get_daily_digests_since(self, since: str, group_id: str = "",
                                 limit: int = 30) -> list[dict]:
        """获取从某天以来的每日摘要"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if group_id:
                rows = conn.execute(
                    "SELECT * FROM daily_digests WHERE date >= ? AND group_id = ? "
                    "ORDER BY date DESC LIMIT ?",
                    (since, group_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM daily_digests WHERE date >= ? "
                    "ORDER BY date DESC LIMIT ?",
                    (since, limit)
                ).fetchall()
            return [dict(r) for r in rows]

    def search_daily_digests(self, query: str = "", date: str = "",
                              limit: int = 5, group_id: str | None = None) -> list[dict]:
        """搜索每日摘要——按关键词或日期"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conditions = []
            params = []
            if date:
                conditions.append("date = ?")
                params.append(date)
            if query:
                conditions.append("summary LIKE ?")
                params.append(f"%{query}%")
            if group_id is not None:
                conditions.append("group_id = ?")
                params.append(group_id)
            where = " AND ".join(conditions) if conditions else "1=1"
            rows = conn.execute(
                f"SELECT * FROM daily_digests WHERE {where} ORDER BY date DESC LIMIT ?",
                params + [limit]
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_tangtang_journal(self, date: str, entry: str, mood: str = ""):
        """插入糖糖的日记"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tangtang_journal (date, entry, mood, created_at) VALUES (?, ?, ?, ?)",
                (date, entry, mood, now)
            )
            conn.commit()

    def get_tangtang_journal(self, limit: int = 10) -> list[dict]:
        """获取糖糖最近的日记"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM tangtang_journal ORDER BY date DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_journal(self, limit: int = 200) -> list[dict]:
        """糖糖日记——按**写入顺序**倒序（会客厅独白频道的数据源，2026-10-24）。

        与 `get_tangtang_journal` 的区别：那个按 `date DESC`，同一天写了两条时
        先后不可知；独白是一条时间线，必须用自增 id 定序。
        """
        limit = max(1, min(int(limit or 200), 1000))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, date, entry, mood, created_at FROM tangtang_journal "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def search_chat(self, keyword: str, limit: int = 40) -> tuple[list[dict], int]:
        """在 chat_log **正文**里搜关键词（会客厅搜索，2026-10-24）。

        返回 `(hits, total)`，`total` 是精确命中数——实测 29.8 万行全表
        `LIKE '%中文%'` 约 50~60ms（WAL，内存热），值得给准确数字，
        不能拿「取到的条数」冒充总数（那是撒谎）。

        搜的是**内容**不是会话标题。排除隔离行（`quarantined_at`）——那些是
        历史批处理把多人消息合并出来的脏行，出现在搜索结果里等于伪造发言。
        """
        text = str(keyword or "").strip()
        if not text:
            return [], 0
        limit = max(1, min(int(limit or 40), 200))
        # LIKE 通配符转义：用户搜 "100%" 不能被当成通配
        esc = (text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        pattern = f"%{esc}%"
        base = ("FROM chat_log c "
                "WHERE c.message LIKE ? ESCAPE '\\' "
                "  AND COALESCE(c.quarantined_at,'') = ''")
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            total = int(conn.execute(
                f"SELECT COUNT(*) {base}", (pattern,)).fetchone()[0])
            rows = conn.execute(
                "SELECT c.id, c.qq_id, c.group_id, c.is_bot_reply, c.message, "
                "       c.timestamp, COALESCE(c.speaker_qq,'') AS speaker_qq, "
                "       COALESCE(p.nickname,'') AS nickname "
                "FROM chat_log c LEFT JOIN people p ON p.qq_id = c.qq_id "
                "WHERE c.message LIKE ? ESCAPE '\\' "
                "  AND COALESCE(c.quarantined_at,'') = '' "
                "ORDER BY c.id DESC LIMIT ?", (pattern, limit)
            ).fetchall()
            return [dict(r) for r in rows], total

    def get_chat_row(self, kind: str, scope_id: str, msg_id: int) -> dict | None:
        """按 id 取一条 chat_log 行（媒体端点用：要 message + segments 解析附件）。

        必须带 scope 校验——否则 `?scope=private:<那个人>&id=<别人群里的行>`
        就能把不属于这个会话的内容读出去。
        """
        kind = "group" if kind == "group" else "private"
        col = "group_id" if kind == "group" else "qq_id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT id, qq_id, group_id, is_bot_reply, message, timestamp, "
                f"       COALESCE(segments,'') AS segments "
                f"FROM chat_log WHERE id = ? AND {col} = ? "
                f"  AND COALESCE(quarantined_at,'') = '' LIMIT 1",
                (int(msg_id), str(scope_id or ""))
            ).fetchone()
            return dict(row) if row else None

    def get_chat_by_event(self, event_key: str) -> dict | None:
        """按 event_key 取那条 chat_log 行（回合回放用）。

        `turn_traces` / `decision_runs` 都有 event_key，唯独没有 chat_log 的 id；
        回放要显示「她说的是哪句话」就得从 event_key 反查回来。event_key 是
        v2 格式的稳定键（`v2:<scope>:<ts>:<qq>:<msg_id>`），一条消息对应一条。
        """
        key = str(event_key or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, qq_id, group_id, is_bot_reply, message, timestamp, "
                "       COALESCE(event_key,'') AS event_key, "
                "       COALESCE(speaker_qq,'') AS speaker_qq "
                "FROM chat_log WHERE event_key = ? LIMIT 1", (key,)
            ).fetchone()
            return dict(row) if row else None

    def get_chat_by_id(self, msg_id: int) -> dict | None:
        """按 id 取一行（内部用：解析 `delivery:` 键，找回触发这一回合的用户消息）。

        为什么需要：糖糖回复的 event_key 是 `delivery:<触发消息id>:<气泡序号>`，
        而 `turn_traces` 记的 event_key 是**触发消息**的 `v2:` 键——两者直接
        join 不上（实测 380 条 bot 回复与 turn_traces 命中 0 条）。
        """
        try:
            mid = int(msg_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, qq_id, group_id, is_bot_reply, message, timestamp, "
                "       COALESCE(event_key,'') AS event_key, "
                "       COALESCE(speaker_qq,'') AS speaker_qq "
                "FROM chat_log WHERE id = ? LIMIT 1", (mid,)
            ).fetchone()
            return dict(row) if row else None

    def get_bot_reply_text_for_trigger(self, trigger_id: int) -> str:
        """取糖糖针对某条消息发出去的**整段回复**（多气泡按序拼接）。

        `chat_log` 里她的回复 event_key 是 `delivery:<触发消息id>:<气泡序号>`
        ——同一次回复可能被拆成多条气泡（`[分段]` 协议），所以要按 id 升序拼。

        **为什么需要它**（2026-10-25 第 17 轮）：`find_feedback_for_turn` 的
        匹配条件是「两边文本有前缀关系」，而 `console_api.turn_replay` 原来把
        **触发消息**（对方说的那句）当成 `reply_text` 传了进去 —— 对方的原话
        与糖糖的回复前缀几乎不可能相同，于是 `feedback` 长期匹配不到，
        回放里「对方怎么反应」和第 7 段永远是空的。改成先取回她真正说的话。
        """
        try:
            tid = int(trigger_id)
        except (TypeError, ValueError):
            return ""
        if tid <= 0:
            return ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT message FROM chat_log WHERE event_key LIKE ? "
                "ORDER BY id ASC", (f"delivery:{tid}:%",)).fetchall()
        return "".join(str(r[0] or "") for r in rows)

    def search_similar_memories(self, query_vec, qq_id: str = "", top_k: int = 10) -> list[dict]:
        """语义搜索最相似的记忆——返回 [(memory_dict, similarity_score), ...]"""
        import numpy as np
        results = []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            base = "COALESCE(m.status,'active')='active'"
            where = f"WHERE m.qq_id=? AND {base}" if qq_id else f"WHERE {base}"
            params = (qq_id,) if qq_id else ()
            rows = conn.execute(
                f"SELECT m.id, m.qq_id, m.key, m.value, m.importance, "
                f"m.recall_count, m.last_recalled, e.embedding "
                f"FROM memories m JOIN memory_embeddings e ON m.id=e.memory_id {where}",
                params
            ).fetchall()

            for row in rows:
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                sim = float(np.dot(query_vec, emb))  # 余弦相似度（已归一化）
                results.append((sim, dict(row)))

        results.sort(key=lambda x: x[0], reverse=True)
        return [(r[1], r[0]) for r in results[:top_k]]

    def delete_embedding(self, memory_id: int):
        """删除一条记忆的向量嵌入"""
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))

    # ═══════════════════════════════════════
    # 🆕 聊天索引——清洗后的私聊消息 + 向量
    # ═══════════════════════════════════════

    def index_chat(self, chat_id: int, qq_id: str, clean_text: str, embedding=None):
        """索引一条私聊消息"""
        import numpy as np
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chat_index (chat_id, qq_id, clean_text, embedding) VALUES (?,?,?,?)",
                (chat_id, qq_id, clean_text, blob)
            )

    def search_chat_index(self, qq_id: str, query_vec, top_k: int = 5) -> list[dict]:
        """语义搜索私聊索引，并回投原始 chat_log 的证据字段。

        chat_index 只保存清洗文本和向量；时间、群域、说话人仍以 chat_log
        为唯一事实源，避免语义检索结果被 LLM 当成无时间锚的摘要。
        """
        import numpy as np
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT i.chat_id, i.clean_text, i.embedding, "
                "COALESCE(c.timestamp,'') AS timestamp, "
                "COALESCE(c.group_id,'') AS group_id, "
                "COALESCE(c.is_bot_reply,0) AS is_bot_reply "
                "FROM chat_index i JOIN chat_log c "
                "ON c.id=i.chat_id AND c.qq_id=i.qq_id "
                "WHERE i.qq_id=? AND i.embedding IS NOT NULL "
                "AND COALESCE(c.group_id,'')=''",
                (qq_id,)
            ).fetchall()
            if not rows:
                return []
            # 2026-08-15 整体审查性能 M4：矩阵化一次点积——旧代码逐行
            # frombuffer+dot（单用户上万行时每次工具调用 50-100ms）
            try:
                mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                sims = mat @ np.asarray(query_vec, dtype=np.float32)
                results = []
                for sim, row in zip(sims, rows):
                    s = float(sim)
                    if s > 0.4:  # 相似度阈值
                        results.append((
                            s, row["chat_id"], row["clean_text"],
                            row["timestamp"], row["group_id"],
                            row["is_bot_reply"],
                        ))
            except ValueError:
                # 维度不一致的历史脏数据 → 退回逐行
                results = []
                for row in rows:
                    try:
                        emb = np.frombuffer(row["embedding"], dtype=np.float32)
                        s = float(np.dot(query_vec, emb))
                        if s > 0.4:
                            results.append((
                                s, row["chat_id"], row["clean_text"],
                                row["timestamp"], row["group_id"],
                                row["is_bot_reply"],
                            ))
                    except Exception:
                        continue
        results.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "chat_id": r[1],
                "text": r[2][:200],
                "score": round(r[0], 3),
                "timestamp": r[3] or "",
                "group_id": r[4] or "",
                "is_bot_reply": bool(r[5]),
                "source": f"chat_log#{r[1]}",
            }
            for r in results[:top_k]
        ]

    def count_chat_index(self, qq_id: str = "") -> int:
        """统计已索引的消息数"""
        with self._connect() as conn:
            if qq_id:
                row = conn.execute("SELECT COUNT(*) FROM chat_index WHERE qq_id=?", (qq_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM chat_index").fetchone()
            return row[0] if row else 0

    def has_embeddings(self, qq_id: str = "") -> bool:
        """检查是否有向量嵌入"""
        with self._connect() as conn:
            if qq_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings e "
                    "JOIN memories m ON e.memory_id=m.id WHERE m.qq_id=?", (qq_id,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
            return (row[0] or 0) > 0

    # ═══════════════════════════════════════
    # 🆕 结构化记忆层——事实簇 + 原子事实
    # ═══════════════════════════════════════

    def upsert_fact_cluster(self, subject_qq: str, category: str, title: str,
                            summary: str = "", permanence: str = "normal",
                            embedding=None) -> int:
        """创建或更新一个事实簇。按 subject_qq + title 去重。
        如果已存在同 title 的簇，更新 summary/permanence/embedding。
        返回 cluster_id。"""
        import numpy as np
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._connect() as conn:
            # ON CONFLICT + UNIQUE 索引——消除 SELECT-then-INSERT 竞态
            cur = conn.execute(
                """INSERT INTO fact_clusters (subject_qq, category, title, summary, permanence, created_at, updated_at, embedding)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(subject_qq, title) DO UPDATE SET
                       summary=excluded.summary,
                       permanence=excluded.permanence,
                       updated_at=excluded.updated_at,
                       embedding=COALESCE(excluded.embedding, embedding)""",
                (subject_qq, category, title, summary, permanence, now, now, blob)
            )
            conn.commit()
            # ON CONFLICT DO UPDATE 时 lastrowid 为 0，需重新查询
            if cur.lastrowid:
                return cur.lastrowid
            row = conn.execute(
                "SELECT id FROM fact_clusters WHERE subject_qq=? AND title=?",
                (subject_qq, title)
            ).fetchone()
            return row[0] if row else 0

    def add_cluster_fact(self, cluster_id: int, subject_qq: str, fact: str,
                         source_qq: str = "", confidence: float = 1.0,
                         importance: int = 5, embedding=None,
                         evidence_ids: str = "") -> int:
        """向簇中添加一条原子事实。自动去重——相同 fact 文本不重复插入。
        返回实际插入的 id（重复时返回 0——调用方据此计数，不虚增）。
        evidence_ids（2026-08-16 批 4）：支持该事实的 chat_log id 列表（逗号分隔），
        纠正/撤销时可回溯证据来源。"""
        import numpy as np
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._connect() as conn:
            # INSERT OR IGNORE + UNIQUE 索引——消除 SELECT-then-INSERT 竞态
            cur = conn.execute(
                """INSERT OR IGNORE INTO cluster_facts (cluster_id, subject_qq, fact, source_qq, confidence, importance, created_at, embedding, evidence_ids)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (cluster_id, subject_qq, fact, source_qq, confidence, importance, now, blob, evidence_ids)
            )
            # 更新簇的 fact_count 和时间（只数 active——批 2 起撤销行不计）
            conn.execute(
                "UPDATE fact_clusters SET fact_count = (SELECT COUNT(*) FROM cluster_facts "
                "WHERE cluster_id=? AND COALESCE(status,'active')='active'), updated_at=? WHERE id=?",
                (cluster_id, now, cluster_id)
            )
            conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            # INSERT OR IGNORE 跳过了已存在的行——返回 0（批 4：调用方据此
            # 计数「实际插入」，不虚增 new_facts）
            return 0

    def get_fact_clusters(self, subject_qq: str) -> list[dict]:
        """获取某人的所有事实簇。

        ``has_unanchored_active_facts`` 只用于维护/提取边界：历史 active
        原子事实没有证据时，簇摘要不能再作为 LLM 的可信上下文。
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT fc.*,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM cluster_facts cf
                           WHERE cf.cluster_id = fc.id
                             AND COALESCE(cf.status,'active') = 'active'
                             AND TRIM(COALESCE(cf.evidence_ids,'')) = ''
                       ) THEN 1 ELSE 0 END AS has_unanchored_active_facts
                FROM fact_clusters fc
                WHERE fc.subject_qq=?
                ORDER BY fc.fact_count DESC, fc.updated_at DESC
                """,
                (subject_qq,)
            ).fetchall()
        return [dict(r) for r in rows]

    def search_fact_clusters(self, query_vec, subject_qq: str, top_k: int = 3) -> list[dict]:
        """语义搜索最相关的事实簇。返回 [(cluster_dict, similarity), ...]"""
        import numpy as np
        results = []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM fact_clusters WHERE subject_qq=? AND embedding IS NOT NULL",
                (subject_qq,)
            ).fetchall()
            for row in rows:
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                sim = float(np.dot(query_vec, emb))
                if sim > 0.3:
                    results.append((sim, dict(row)))
        results.sort(key=lambda x: x[0], reverse=True)
        return [(r[1], r[0]) for r in results[:top_k]]

    def get_cluster_facts(self, cluster_id: int, include_retracted: bool = False) -> list[dict]:
        """获取一个簇内的所有原子事实（批 2：默认只返回 active）"""
        status_clause = "" if include_retracted else " AND COALESCE(status,'active')='active'"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM cluster_facts WHERE cluster_id=?{status_clause} "
                "ORDER BY importance DESC, confidence DESC",
                (cluster_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_cluster_summary(self, cluster_id: int, summary: str, embedding=None,
                               *, actor: str = "unknown", reason: str = "") -> bool:
        """更新簇的摘要和向量。

        2026-10-24：摘要是由多条原子事实归纳出的结论，就地覆盖前留旧值。
        """
        import numpy as np
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(subject_qq,''), COALESCE(summary,'') "
                "FROM fact_clusters WHERE id=?",
                (cluster_id,),
            ).fetchone()
            if row and str(row[1] or "") != str(summary or ""):
                self._record_revision(
                    conn, entity="fact_clusters", entity_id=cluster_id,
                    qq_id=str(row[0] or ""), op="update", field="summary",
                    before=str(row[1] or ""), after=str(summary or ""),
                    reason=reason or "簇摘要重写", actor=actor,
                )
            if blob:
                conn.execute(
                    "UPDATE fact_clusters SET summary=?, embedding=?, updated_at=? WHERE id=?",
                    (summary, blob, now, cluster_id)
                )
            else:
                conn.execute(
                    "UPDATE fact_clusters SET summary=?, updated_at=? WHERE id=?",
                    (summary, now, cluster_id)
                )
            conn.commit()
        return True

    def set_cluster_title(self, cluster_id: int, title: str, *,
                          actor: str = "unknown", reason: str = "") -> bool:
        """更新事实簇的标题（就地覆盖前留旧值）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(subject_qq,''), COALESCE(title,'') "
                "FROM fact_clusters WHERE id=?",
                (cluster_id,),
            ).fetchone()
            if row and str(row[1] or "") != str(title or ""):
                self._record_revision(
                    conn, entity="fact_clusters", entity_id=cluster_id,
                    qq_id=str(row[0] or ""), op="update", field="title",
                    before=str(row[1] or ""), after=str(title or ""),
                    reason=reason or "簇标题重写", actor=actor,
                )
            conn.execute(
                "UPDATE fact_clusters SET title=?, updated_at=? WHERE id=?",
                (title, datetime.now().strftime("%Y-%m-%d %H:%M"), cluster_id)
            )
            conn.commit()
        return True

    def delete_cluster(self, cluster_id: int, *, actor: str = "unknown",
                       reason: str = "") -> bool:
        """删除一个事实簇及其所有原子事实（CASCADE）。

        2026-10-24：删之前把簇行和它的原子事实逐条写进流水。外键 CASCADE 会把
        原子事实一起带走——不先留底，这些事实就永久消失了。
        """
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cluster = self._row_snapshot(conn, "fact_clusters", "id", cluster_id)
            if not cluster:
                return False
            subject = str(cluster.get("subject_qq") or "")
            self._record_revision(
                conn, entity="fact_clusters", entity_id=cluster_id, qq_id=subject,
                op="delete", before=cluster,
                reason=reason or "删除事实簇", actor=actor,
            )
            scanned = conn.execute(
                "SELECT * FROM cluster_facts WHERE cluster_id=?", (cluster_id,)
            )
            columns = [str(item[0]) for item in (scanned.description or [])]
            for raw in scanned.fetchall():
                item = dict(zip(columns, raw))
                self._record_revision(
                    conn, entity="cluster_facts", entity_id=item.get("id"),
                    qq_id=subject, op="delete", before=item,
                    reason=reason or f"随簇 #{cluster_id} 级联删除", actor=actor,
                )
            conn.execute("DELETE FROM fact_clusters WHERE id=?", (cluster_id,))
            conn.commit()
            return True

    def get_all_fact_cluster_subjects(self) -> list[str]:
        """获取所有已被提取过事实簇的 QQ 号"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject_qq FROM fact_clusters"
            ).fetchall()
        return [r[0] for r in rows]

    def count_fact_clusters(self, subject_qq: str = "") -> int:
        """统计事实簇数量"""
        with self._connect() as conn:
            if subject_qq:
                row = conn.execute(
                    "SELECT COUNT(*) FROM fact_clusters WHERE subject_qq=?", (subject_qq,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM fact_clusters").fetchone()
        return row[0] if row else 0

    # ═══ 辅助查询（LLM 工具用） ═══

    def search_chat_keywords(self, qq_id: str, keywords: list[str], limit: int = 10,
                             group_id: str | None = None,
                             include_bot_replies: bool = False) -> list[dict]:
        """精确关键词搜索聊天记录。传入多个关键词时用 OR 连接。"""
        with self._connect() as conn:
            clauses = " OR ".join(["message LIKE ?" for _ in keywords])
            params = [f"%{kw}%" for kw in keywords]
            if qq_id:
                clauses = f"({clauses}) AND qq_id=?"
                params.append(qq_id)
            speaker_clause = "" if include_bot_replies else " AND is_bot_reply=0"
            group_clause = " AND group_id=?" if group_id is not None else ""
            if group_id is not None:
                params.append(group_id)
            rows = conn.execute(
                f"SELECT id AS chat_log_id, qq_id, message, timestamp, group_id, is_bot_reply FROM chat_log "
                f"WHERE {clauses}{speaker_clause}{group_clause}"
                " AND COALESCE(quarantined_at,'')='' "
                f"ORDER BY id DESC LIMIT ?",
                params + [limit]
            ).fetchall()
        return [{"chat_log_id": r[0], "qq_id": r[1], "message": r[2],
                 "timestamp": r[3], "group_id": r[4],
                 "is_bot_reply": bool(r[5])} for r in rows]

    def get_group_activity(self, group_id: str, hours: int = 24) -> dict:
        """群活跃速览：最近N小时发言人数、消息数、活跃TOP5"""
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT qq_id) FROM chat_log "
                "WHERE group_id=? AND is_bot_reply=0 AND timestamp >= ?"
                " AND COALESCE(quarantined_at,'')=''",
                (group_id, since)
            ).fetchone()
            msg_count, people_count = row[0] or 0, row[1] or 0

            top_rows = conn.execute(
                "SELECT qq_id, COUNT(*) as c FROM chat_log "
                "WHERE group_id=? AND is_bot_reply=0 AND timestamp >= ?"
                " AND COALESCE(quarantined_at,'')='' "
                "GROUP BY qq_id ORDER BY c DESC LIMIT 5",
                (group_id, since)
            ).fetchall()

            # 查群名
            gname = conn.execute("SELECT group_name FROM group_info WHERE group_id=?", (group_id,)).fetchone()

        top = []
        for r in top_rows:
            p = self.get_or_create_person(r[0])
            top.append((r[0], p.get("nickname", r[0]), r[1]))
        return {
            "group_name": gname[0] if gname else "",
            "hours": hours, "msg_count": msg_count, "people_count": people_count,
            "top5": top,
        }

    def count_user_messages(self, qq_id: str, keyword: str = "",
                            group_id: str | None = None) -> dict:
        """统计某人的消息；group_id=None 表示不限定，空串表示仅私聊。"""
        with self._connect() as conn:
            group_clause = " AND group_id=?" if group_id is not None else ""
            base_params = [qq_id] + ([group_id] if group_id is not None else [])
            total = conn.execute(
                f"SELECT COUNT(*) FROM chat_log WHERE qq_id=? AND is_bot_reply=0{group_clause}"
                " AND COALESCE(quarantined_at,'')=''",
                tuple(base_params)
            ).fetchone()[0]
            result = {"total_messages": total}
            if keyword:
                kw_count = conn.execute(
                    f"SELECT COUNT(*) FROM chat_log WHERE qq_id=? AND is_bot_reply=0"
                    f"{group_clause} AND message LIKE ?"
                    " AND COALESCE(quarantined_at,'')=''",
                    tuple(base_params + [f"%{keyword}%"])
                ).fetchone()[0]
                result["keyword"] = keyword
                result["keyword_count"] = kw_count
            # 最早和最晚发言
            first = conn.execute(
                f"SELECT timestamp FROM chat_log WHERE qq_id=? AND is_bot_reply=0"
                f"{group_clause} AND COALESCE(quarantined_at,'')=''"
                " ORDER BY id ASC LIMIT 1",
                tuple(base_params)
            ).fetchone()
            last = conn.execute(
                f"SELECT timestamp FROM chat_log WHERE qq_id=? AND is_bot_reply=0"
                f"{group_clause} AND COALESCE(quarantined_at,'')=''"
                " ORDER BY id DESC LIMIT 1",
                tuple(base_params)
            ).fetchone()
            result["first_seen"] = first[0] if first else ""
            result["last_seen"] = last[0] if last else ""
        return result

    def get_last_conversation(self, bot_qq: str, user_qq: str, limit: int = 20,
                              group_id: str | None = None) -> list[str]:
        """获取最近对话；group_id=None 表示不限定，空串表示仅私聊。"""
        with self._connect() as conn:
            group_clause = " AND group_id=?" if group_id is not None else ""
            params = [user_qq, bot_qq]
            if group_id is not None:
                params.append(group_id)
            params.append(limit)
            rows = conn.execute(
                "SELECT qq_id, message, timestamp, is_bot_reply FROM chat_log "
                "WHERE qq_id=?"
                f"{group_clause}"
                " AND COALESCE(quarantined_at,'')='' "
                "ORDER BY id DESC LIMIT ?",
                tuple([user_qq] + ([group_id] if group_id is not None else []) + [limit])
            ).fetchall()
        lines = []
        for r in reversed(rows):
            who = "糖糖" if r[3] else "对方"
            lines.append(f"[{r[2]}] {who}: {r[1][:150]}")
        return lines

    def search_aliases(self, keyword: str, limit: int = 5) -> list[tuple[str, str]]:
        """搜索外号——通过关键词匹配 qq_id 或 alias"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT qq_id, alias FROM aliases "
                "WHERE alias LIKE ? OR qq_id LIKE ? LIMIT ?",
                (f"%{keyword}%", f"%{keyword}%", limit)
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    # ═══════════════════════════════════════
    # R3-1: Episode 聚合
    # ═══════════════════════════════════════

    def insert_episode(self, qq_id: str, title: str, summary: str,
                       time_start: str, time_end: str,
                       paragraph_ids: str = "", feeling: str = "",
                       source_group_id: str = "") -> int:
        """插入一个聚合后的 Episode，返回自增 id"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO episodes (qq_id, source_group_id, title, summary, "
                "time_start, time_end, paragraph_ids, feeling) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (qq_id, str(source_group_id), title, summary, time_start,
                 time_end, paragraph_ids, feeling)
            )
            conn.commit()
            return cur.lastrowid

    def query_episodes(self, qq_id: str = "", limit: int = 5,
                       source_group_id: str | None = None) -> list[dict]:
        """查询最近 Episode；scope=None 为维护视图，空串仅私聊。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conditions = []
            params = []
            if qq_id:
                conditions.append("qq_id=?")
                params.append(str(qq_id))
            if source_group_id is not None:
                conditions.append("COALESCE(source_group_id,'__legacy_unscoped__')=?")
                params.append(str(source_group_id))
            where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM episodes{where_clause} "
                "ORDER BY time_end DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_episodes_for_user(self, qq_id: str) -> int:
        """某人的 episode 数量"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE qq_id=?", (qq_id,)
            ).fetchone()
            return row[0] if row else 0

    # ═══════════════════════════════════════
    # 收口方法（2026-08-10 第三轮：直连 SQL 统一收口到 Store）
    # 之前 handler_autonomy/health_check/mood_tracker/memory 直接 sqlite3.connect
    # 绕过 Store——现在全部走这里，统一 _connect()（busy_timeout + 外键）
    # ═══════════════════════════════════════

    # ── 事实簇 ──

    def get_fact_cluster(self, cluster_id: int) -> Optional[dict]:
        """查询单个事实簇（收口 memory.py 直连）"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM fact_clusters WHERE id=?", (cluster_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── 回填/积压 ──

    def get_unprocessed_backlog(
            self, last_ids: dict[str, int],
            before_ids: Optional[dict[str, int]] = None) -> list[tuple[str, int, int]]:
        """一次连接批量统计所有用户的未处理消息。

        临时游标表让 SQLite 在一条聚合查询里应用每个用户不同的前向/回填
        边界，替代自治层对数百用户逐个开连接计数。
        """
        before_ids = before_ids or {}
        cursor_users = set(last_ids) | set(before_ids)
        cursor_rows = [
            (str(qq_id), int(last_ids.get(qq_id, 0) or 0),
             int(before_ids.get(qq_id, 0) or 0))
            for qq_id in cursor_users
        ]
        with self._connect() as conn:
            conn.execute(
                "CREATE TEMP TABLE extraction_scan_cursors ("
                "qq_id TEXT PRIMARY KEY, last_id INTEGER NOT NULL, "
                "before_id INTEGER NOT NULL)"
            )
            if cursor_rows:
                conn.executemany(
                    "INSERT INTO extraction_scan_cursors "
                    "(qq_id, last_id, before_id) VALUES (?,?,?)",
                    cursor_rows,
                )
            rows = conn.execute(
                "SELECT c.qq_id, COUNT(*), MAX(c.id) FROM chat_log c "
                "LEFT JOIN extraction_scan_cursors s ON s.qq_id=c.qq_id "
                "WHERE c.is_bot_reply=0 "
                "AND COALESCE(c.quarantined_at,'')='' "
                "AND c.id>COALESCE(s.last_id,0) "
                "AND (COALESCE(s.before_id,0)<=0 OR c.id<s.before_id) "
                "GROUP BY c.qq_id"
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]

    #: 计入提取积压时**要排除**的「无内容消息」。
    #:
    #: 为什么（2026-10-15 提取积压调查）：图片消息在入口处被替换成
    #: `（发了张图片）` 占位符（handler.py:3929 / :5084），识图没触发或写回失败时
    #: 就只剩这个占位。实测积压 517 条里 **229 条（44.3%）是它**（全库基线仅 8.7%，
    #: 富集 5.1 倍），另有 16 条空串。而提取准入是按「消息行数」数阈值的——
    #: 一条没有任何内容的占位照样把计数灌满、照样触发一次 LLM 调用，
    #: 换来一个必然为空的提取结果。`memory.py:840` 早就按「无内容」处理过它，
    #: 这里把同一口径搬到**计数**上。
    _NO_CONTENT_SQL = (
        "TRIM(COALESCE(c.message,'')) <> '' "
        "AND c.message <> '（发了张图片）'"
    )

    def get_persisted_unprocessed_backlog(self) -> list[tuple[str, int, int]]:
        """用数据库游标一次查询前向积压，供健康检查避免双连接和进程缓存。

        只数**有内容**的消息（见 _NO_CONTENT_SQL）——否则占位符会把积压数字灌虚。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.qq_id, COUNT(*), MAX(c.id) FROM chat_log c "
                "LEFT JOIN extraction_cursors e ON e.pipeline='memory' "
                "AND e.direction='forward' AND e.qq_id=c.qq_id "
                "WHERE COALESCE(c.is_bot_reply,0)=0 "
                "AND COALESCE(c.quarantined_at,'')='' "
                f"AND {self._NO_CONTENT_SQL} "
                "AND c.id>COALESCE(e.cursor_chat_id,0) "
                "GROUP BY c.qq_id"
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]

    def get_extraction_backlog_snapshot(self) -> dict:
        """一次连接返回前向积压明细和容量基线，供调度与健康检查共用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.qq_id, COUNT(*), MIN(c.id), MAX(c.id), "
                "MIN(NULLIF(c.timestamp,'')) FROM chat_log c "
                "LEFT JOIN extraction_cursors e ON e.pipeline='memory' "
                "AND e.direction='forward' AND e.qq_id=c.qq_id "
                "WHERE COALESCE(c.is_bot_reply,0)=0 "
                "AND COALESCE(c.quarantined_at,'')='' "
                f"AND {self._NO_CONTENT_SQL} "
                "AND c.id>COALESCE(e.cursor_chat_id,0) "
                "GROUP BY c.qq_id "
                "ORDER BY COALESCE(MIN(NULLIF(c.timestamp,'')),'9999-12-31'), "
                "MIN(c.id), c.qq_id"
            ).fetchall()
            total_user_messages = int(conn.execute(
                "SELECT COUNT(*) FROM chat_log "
                "WHERE COALESCE(is_bot_reply,0)=0"
                " AND COALESCE(quarantined_at,'')=''"
            ).fetchone()[0])
        users = [
            {
                "qq_id": str(row[0]),
                "messages": int(row[1]),
                "oldest_id": int(row[2]),
                "max_id": int(row[3]),
                "oldest_at": str(row[4] or ""),
            }
            for row in rows
        ]
        policy = summarize_extraction_backlog(users, datetime.now())
        return {
            "total_user_messages": total_user_messages,
            "backlog_messages": sum(row["messages"] for row in users),
            "backlog_users": len(users),
            "over_30_users": sum(row["messages"] > 30 for row in users),
            "oldest_at": next(
                (row["oldest_at"] for row in users if row["oldest_at"]), ""
            ),
            **policy,
            "users": users,
        }

    def get_max_chat_id(self, qq_id: str, exclude_bot: bool = False) -> Optional[int]:
        """某用户消息的最大 id。exclude_bot=True 时只算用户消息（回填游标用）"""
        with self._connect() as conn:
            sql = "SELECT MAX(id) FROM chat_log WHERE qq_id=?"
            if exclude_bot:
                sql += " AND is_bot_reply=0"
            row = conn.execute(sql, (qq_id,)).fetchone()
            return row[0] if row else None

    def get_recent_active_users(self, days: int = 3, limit: int = 20,
                                exclude_qq: str = "") -> list[tuple]:
        """最近 N 天活跃用户（按消息数倒序）——自治循环回填扫描用"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT qq_id FROM chat_log"
                " WHERE is_bot_reply = 0 AND qq_id != ?"
                " AND timestamp >= datetime('now', ?)"
                " AND COALESCE(quarantined_at,'')=''"
                " GROUP BY qq_id ORDER BY COUNT(*) DESC LIMIT ?",
                (exclude_qq, f"-{days} days", limit)
            ).fetchall()
            return rows

    def get_people_with_memories_gt(self, min_count: int = 10, empty_notes: bool = True,
                                    limit: int = 10,
                                    include_dirty: bool = False,
                                    legacy_notes: bool = False) -> list[tuple]:
        """记忆数 > min_count 的用户——画像补全/脏画像重试扫描用。

        legacy_notes=True（2026-10-15 新增）：把「有画像但不可信」的人也算进候选。
        为什么需要这个开关：注入门 `memory.active_notes` 只放行
        verified/manual/corrected，而候选扫描原先只认「notes 为空」和「dirty」——
        于是**非空 legacy 画像两头不靠**：既不会被注入（对，这是防事故的），
        也永远不会被重合成刷新（不对，这是死角）→ 永久冻结。
        查库实测（2026-10-15）：people 529 里 notes 非空 118（legacy 68 / verified 50），
        其中 21 人已有 ≥10 条可信来源记忆（够重合成），16 人一条可信来源都没有
        （重合成也只会再落回 legacy——但那是 fail-closed 的正确结果）。
        打开它**不放宽注入门**，只是把「重合成」这条唯一的复活路径接通。
        """
        notes_cond = "p.notes IS NULL OR p.notes = ''" if empty_notes else "1=1"
        if include_dirty:
            notes_cond = f"({notes_cond}) OR COALESCE(p.notes_dirty,0)=1"
        if legacy_notes:
            # COALESCE 把 NULL 也算成 legacy_unverified——与注入门同一口径
            notes_cond = (
                f"({notes_cond}) OR (COALESCE(p.notes,'')<>''"
                f" AND COALESCE(p.notes_trust_level,'legacy_unverified')"
                f" NOT IN ('verified','manual','corrected'))"
            )
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT p.qq_id FROM people p"
                f" WHERE ({notes_cond})"
                f" AND (SELECT COUNT(*) FROM memories m WHERE m.qq_id = p.qq_id"
                f" AND COALESCE(m.status,'active')='active') > ?"
                f" ORDER BY (SELECT COUNT(*) FROM memories m WHERE m.qq_id = p.qq_id"
                f" AND COALESCE(m.status,'active')='active') DESC"
                f" LIMIT ?",
                (min_count, limit)
            ).fetchall()
            return rows

    def get_zero_memory_users(self, min_chats: int = 50,
                              min_interactions: int = 5) -> list[tuple]:
        """有直接互动证据但零记忆的用户（M11 指标）。

        群环境消息会增加 total_chats，却不代表与糖糖建立了关系；私聊消息或
        糖糖实际回复才作为直接互动代理，避免把普通群友误报为“被遗忘”。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.qq_id, p.nickname FROM people p"
                " WHERE p.total_chats > ?"
                " AND ((SELECT COUNT(*) FROM chat_log c WHERE c.qq_id=p.qq_id"
                " AND COALESCE(c.is_bot_reply,0)=0 AND COALESCE(c.group_id,'')='') >= ?"
                " OR (SELECT COUNT(*) FROM chat_log c WHERE c.qq_id=p.qq_id"
                " AND COALESCE(c.is_bot_reply,0)=1) >= ?)"
                " AND (SELECT COUNT(*) FROM memories m WHERE m.qq_id = p.qq_id"
                " AND COALESCE(m.status,'active')='active') = 0",
                (min_chats, min_interactions, min_interactions)
            ).fetchall()
            return rows

    def count_zero_memory_users(self, min_chats: int = 50,
                                min_interactions: int = 5) -> int:
        """有直接互动证据但零记忆的用户数（健康检查）"""
        return len(self.get_zero_memory_users(min_chats, min_interactions))

    # ── 记忆统计（健康检查用）──

    def count_memories_today(self) -> int:
        """今日入库的记忆数（推理泄露检测）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE timestamp >= date('now')"
            ).fetchone()
            return row[0] if row else 0

    def find_memory_by_value(self, qq_id: str, value: str) -> Optional[dict]:
        """按 (qq_id, value) 精确查最近一条记忆——去重专用。
        不受 query_memories 的 top-100/importance 排序限制，全库精确匹配。"""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, qq_id, key, value, importance FROM memories "
                "WHERE qq_id=? AND value=? AND COALESCE(status,'active')='active' "
                "ORDER BY id DESC LIMIT 1",
                (qq_id, value)
            ).fetchone()
            return dict(row) if row else None

    def count_duplicate_memories_today(self) -> int:
        """今日重复记忆组数（去重是否生效）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM (SELECT qq_id, value, COUNT(*) as cnt FROM memories"
                " WHERE timestamp >= date('now') GROUP BY qq_id, value HAVING cnt > 1)"
            ).fetchone()
            return row[0] if row else 0

    def get_max_recall_count(self) -> Optional[int]:
        """最高 recall_count（遗忘曲线/冷却检测）"""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(recall_count) FROM memories").fetchone()
            return row[0] if row else None

    def count_memories_for(self, qq_id: str) -> int:
        """某用户记忆数"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE qq_id=? "
                "AND COALESCE(status,'active')='active'", (qq_id,)
            ).fetchone()  # 2026-08-16 Codex M2：审计行不算
            return row[0] if row else 0

    def count_embedding_coverage(self) -> tuple[int, int]:
        """(记忆总数, 有 embedding 数)——embedding 覆盖率检查"""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            with_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
            return total or 0, with_emb or 0

    # ── 心情表（mood_log 归 Store 管理）──

    def get_mood_log(self, qq_id: str, date: str) -> Optional[tuple]:
        """查某用户某天的心情记录 (id, score, message_count)"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, score, message_count FROM mood_log WHERE qq_id=? AND date=?",
                (qq_id, date)
            ).fetchone()
            return row if row else None

    def upsert_mood_log(self, qq_id: str, date: str, score: float, message_count: int = 1):
        """写入/更新心情记录——当天已存在则平均累计"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, score, message_count FROM mood_log WHERE qq_id=? AND date=?",
                (qq_id, date)
            ).fetchone()
            if row:
                old_score, count = row[1], row[2]
                new_count = count + message_count
                new_score = (old_score * count + score) / new_count
                conn.execute(
                    "UPDATE mood_log SET score=?, message_count=? WHERE id=?",
                    (new_score, new_count, row[0])
                )
            else:
                conn.execute(
                    "INSERT INTO mood_log (qq_id, date, score, message_count) VALUES (?, ?, ?, ?)",
                    (qq_id, date, score, message_count)
                )
            conn.commit()

    def get_mood_trend(self, qq_id: str, since: str) -> list[tuple]:
        """最近心情趋势 [(date, score, message_count), ...]"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, score, message_count FROM mood_log "
                "WHERE qq_id=? AND date>=? ORDER BY date",
                (qq_id, since)
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    def get_chat_max_ids_by_user(self, exclude_bot: bool = True) -> list[tuple]:
        """每个用户的最大消息 id（回填扫描用）"""
        bot_cond = " WHERE is_bot_reply=0" if exclude_bot else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT qq_id, MAX(id) as max_id FROM chat_log{bot_cond} GROUP BY qq_id"
            ).fetchall()
            return rows

    def count_reasoning_leaks_today(self) -> int:
        """今日入库的推理泄露记忆数（LLM 把提示词/说明当记忆存了）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE timestamp >= date('now')"
                " AND (value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
                " OR value LIKE '%注意：%' OR value LIKE '%备注：%' OR value LIKE '%说明：%'"
                " OR value LIKE '%该用户%' OR value LIKE '%这个用户%' OR value LIKE '%此处%'"
                " OR value LIKE '%信息不一致%')"
            ).fetchone()
            return row[0] if row else 0

    def count_episodes(self) -> tuple[int, int]:
        """(episodes 总数, 今日新增数)——情节记忆健康检查"""
        with self._connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            today_cnt = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE date(time_start) = date('now')"
            ).fetchone()[0]
            return cnt or 0, today_cnt or 0


class ReadOnlyStore(Store):
    """运维/验收工具专用的只读数据访问层。

    不执行 ``Store._init_db``，缺失数据库不会被监测器意外创建；
    每个连接同时使用 SQLite ``mode=ro`` 和 ``query_only`` 双保险。
    只继承 Store 已有的读方法，生产业务仍使用原 Store。
    """

    _RUNTIME_METRIC_NAMES = (
        "gateway_send_attempts",
        "gateway_send_confirmed",
        "gateway_send_uncertain",
        "gateway_send_failed",
        "gateway_events_dropped_global",
        "gateway_events_dropped_scopes",
        "gateway_events_dropped_scope_queue",
        "gateway_callback_errors",
        "gateway_events_deduped_persistent",
        "gateway_inbox_errors",
        "gateway_inbox_claim_latency_samples",
        "gateway_inbox_claim_latency_le_5ms",
        "gateway_inbox_claim_latency_le_10ms",
        "gateway_inbox_claim_latency_le_25ms",
        "gateway_inbox_claim_latency_le_50ms",
        "gateway_inbox_claim_latency_le_100ms",
        "gateway_inbox_claim_latency_gt_100ms",
        # 群聊忙线背压与事实簇失败退避；只读观察器必须能区分
        # 正常跳过、队列容量压力和格式失败风暴。
        "pending_queue_enqueued",
        "pending_queue_coalesced",
        "pending_queue_overflow",
        "pending_queue_dropped",
        "pending_dispatch_rolled_back",
        "fact_cluster_failures",
        "fact_cluster_backoff_skipped",
        # E1 窗口决策漏斗：生产观察器必须同时看到候选、入口和最终决策，
        # 不能只凭 reply/skip 日志推断“每条都回复”或“自主沉默正常”。
        "window_candidate",
        "window_entry_direct",
        "window_continuation",
        "window_routed",
        "window_decisions_total",
        "window_decisions_reply",
        "window_decisions_skip",
        "window_fade_after_silence",
        # 提取质量与尝试漏斗；与 job 生命周期计数并列，供观察器做
        # attempts→outcome→memory 产出的闭环对账。
        "extract_attempts",
        "extract_busy_queued",
        "extract_outcomes_total",
        "extract_protocol_success",
        "extract_with_items",
        "extract_memories_total",
        "extract_success_empty",
        "extract_rejected_all",
        "extract_invalid_json",
        "extract_transport_error",
        "extract_rejected",
        "extract_successes",
        "extract_llm_failures",
        "confidence_high",
        "confidence_mid",
        "confidence_low",
        "cognitive_episodic",
        "cognitive_semantic",
        "self_memories_today",
        "dedup_hits",
        # 记忆提取 job 级生命周期；与 extract_outcomes_* 的 LLM 子批次
        # 计数分开，供运行观察器计算真实排队/处理吞吐。
        "extract_jobs_created",
        "extract_jobs_admitted",
        "extract_jobs_deferred",
        "extract_jobs_lease_acquired",
        "extract_jobs_lease_missed",
        "extract_jobs_ready",
        "extract_jobs_completed",
        "extract_jobs_failed",
        "extract_jobs_dead",
        "extract_jobs_requeued",
        "extract_jobs_missing_messages",
        "extract_job_llm_started",
        "extract_job_llm_succeeded",
        "extract_job_llm_failed",
    )
    _TRUST_LEVELS = frozenset({
        "legacy_unverified", "unverified", "verified", "manual", "corrected",
    })
    _QUEUE_STATUSES = frozenset({
        "pending", "leased", "ready", "done", "dead", "failed",
    })
    _OUTBOX_STATUSES = frozenset({
        "pending", "sending", "uncertain", "dead",
        "confirmed_unaccounted", "confirmed_conflict",
    })
    _PROACTIVE_EVENT_STATUSES = frozenset({
        "pending", "claimed", "decided", "executing",
        "confirmed", "failed", "uncertain", "skipped",
    })

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = str(Path(db_path).resolve())
        uri_path = quote(Path(self.db_path).as_posix(), safe="/:")
        self._readonly_uri = f"file:{uri_path}?mode=ro"

    def _connect(self):
        conn = sqlite3.connect(self._readonly_uri, uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA query_only=ON")
        return conn

    def list_inbound_events(
            self, *, statuses: tuple[str, ...] = ("failed", "uncertain"),
            limit: int = 100) -> list[dict]:
        """列出需要人工复核的入站事实；只返回元数据，不返回消息正文。

        这是运维取证接口，不拥有重试、重放或状态修改能力。``chat_log``
        关联只用于证明原始消息是否已经落成事实行；正文必须通过已有的、
        明确作用域的历史查询路径读取，避免诊断脚本意外把隐私数据批量输出。
        """
        allowed = tuple(
            str(status) for status in statuses
            if str(status) in {"received", "claimed", "executing", "processed",
                               "failed", "uncertain"}
        )
        limit = max(1, min(int(limit or 100), 200))
        conditions = []
        params: list = []
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            conditions.append(f"e.status IN ({placeholders})")
            params.extend(allowed)
        where = " AND ".join(conditions) if conditions else "1=1"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.event_key,e.event_type,e.status,e.attempts,e.last_error,"
                "e.received_at,e.updated_at,c.id,c.is_bot_reply,c.timestamp "
                "FROM inbound_events e LEFT JOIN chat_log c "
                "ON c.event_key=e.event_key "
                f"WHERE {where} ORDER BY e.updated_at DESC,e.event_key ASC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [
            {
                "event_key": str(row[0] or ""),
                "event_type": str(row[1] or ""),
                "status": str(row[2] or ""),
                "attempts": int(row[3] or 0),
                "last_error": str(row[4] or ""),
                "received_at": str(row[5] or ""),
                "updated_at": str(row[6] or ""),
                "chat_log_id": int(row[7]) if row[7] is not None else None,
                "chat_log_present": row[7] is not None,
                "chat_log_is_bot": bool(row[8]) if row[8] is not None else None,
                "chat_log_timestamp": str(row[9] or ""),
            }
            for row in rows
        ]

    def get_runtime_observer_snapshot(
        self, captured_at: datetime, *, after_memory_id: int | None = None,
    ) -> dict:
        """返回生产观察器所需的脱敏聚合；只读取显式允许的指标键。"""
        day = captured_at.strftime("%Y-%m-%d")
        next_day = (captured_at.replace(
            hour=0, minute=0, second=0, microsecond=0,
        ) + timedelta(days=1)).strftime("%Y-%m-%d")
        patterns = [f"metric:%:{name}" for name in self._RUNTIME_METRIC_NAMES]
        where = " OR ".join("key LIKE ?" for _ in patterns)
        metrics = {}
        metrics_total = {name: 0 for name in self._RUNTIME_METRIC_NAMES}
        invalid_metric_values = 0
        negative_metric_values = 0
        with self._connect() as conn:
            # 显式开启一个只读事务，让后续所有 SELECT 共享同一 WAL
            # 快照；否则连接级 autocommit 可能让一个“快照”横跨多次写入。
            conn.execute("BEGIN")
            # 使用观察快照的本地日期，而不是 SQLite 的 UTC ``date('now')``；
            # 这样跨午夜或回放固定时间的验收不会把昨天/今天混在一起。
            counter_day = day
            for key, value in conn.execute(
                f"SELECT key,value FROM kv_store WHERE {where}", patterns,
            ).fetchall():
                try:
                    _, partition, name = str(key).split(":", 2)
                    if name not in self._RUNTIME_METRIC_NAMES:
                        continue
                    numeric_value = int(value)
                except (TypeError, ValueError):
                    invalid_metric_values += 1
                    continue
                if numeric_value < 0:
                    negative_metric_values += 1
                    continue
                # ``metric:latest:<name>`` 是 MemoryMetrics 的合法镜像键，
                # 只代表最近一次分区值，不能和每日日志相加；忽略它既不
                # 重复计数，也不把 ``latest`` 误报成非法日期。每日键才
                # 参与 all-time 总计与当前日期读数。
                if partition == "latest":
                    continue
                try:
                    datetime.strptime(partition, "%Y-%m-%d")
                except ValueError:
                    invalid_metric_values += 1
                    continue
                metrics_total[name] += numeric_value
                if partition == day:
                    metrics[name] = numeric_value

            backlog_rows = conn.execute(
                "SELECT COUNT(*),MIN(NULLIF(c.timestamp,'')) FROM chat_log c "
                "LEFT JOIN extraction_cursors e ON e.pipeline='memory' "
                "AND e.direction='forward' AND e.qq_id=c.qq_id "
                "WHERE COALESCE(c.is_bot_reply,0)=0 "
                "AND COALESCE(c.quarantined_at,'')='' "
                "AND c.id>COALESCE(e.cursor_chat_id,0) GROUP BY c.qq_id"
            ).fetchall()
            backlog_users = len(backlog_rows)
            backlog_messages = sum(int(row[0] or 0) for row in backlog_rows)
            over_30_users = sum(int(row[0] or 0) > 30 for row in backlog_rows)
            oldest_at = min(
                (str(row[1]) for row in backlog_rows if row[1]), default=""
            )
            backlog_policy = summarize_extraction_backlog([
                {"messages": int(row[0] or 0), "oldest_at": str(row[1] or "")}
                for row in backlog_rows
            ], captured_at)
            total_user_messages = int(conn.execute(
                "SELECT COUNT(*) FROM chat_log "
                "WHERE COALESCE(is_bot_reply,0)=0"
                " AND COALESCE(quarantined_at,'')=''"
            ).fetchone()[0])
            active = int(conn.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE COALESCE(status,'active')='active'"
            ).fetchone()[0])
            trust_rows = conn.execute(
                "SELECT COALESCE(trust_level,'legacy_unverified'),COUNT(*) "
                "FROM memories WHERE COALESCE(status,'active')='active' GROUP BY 1"
            ).fetchall()
            reasoning_leaks = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE timestamp >= ? AND timestamp < ?"
                " AND (value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
                " OR value LIKE '%注意：%' OR value LIKE '%备注：%' OR value LIKE '%说明：%'"
                " OR value LIKE '%该用户%' OR value LIKE '%这个用户%' OR value LIKE '%此处%'"
                " OR value LIKE '%信息不一致%')", (day, next_day)
            ).fetchone()[0])
            reasoning_leaks_total = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE "
                "value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
                " OR value LIKE '%注意：%' OR value LIKE '%备注：%' OR value LIKE '%说明：%'"
                " OR value LIKE '%该用户%' OR value LIKE '%这个用户%' OR value LIKE '%此处%'"
                " OR value LIKE '%信息不一致%'"
            ).fetchone()[0])
            duplicate_groups = int(conn.execute(
                "SELECT COUNT(*) FROM (SELECT qq_id,value,COUNT(*) FROM memories "
                "WHERE timestamp >= ? AND timestamp < ? GROUP BY qq_id,value HAVING COUNT(*)>1)",
                (day, next_day),
            ).fetchone()[0])
            duplicate_groups_total = int(conn.execute(
                "SELECT COUNT(*) FROM (SELECT qq_id,value,COUNT(*) FROM memories "
                "GROUP BY qq_id,value HAVING COUNT(*)>1)"
            ).fetchone()[0])
            max_memory_id = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM memories"
            ).fetchone()[0])
            # AUTOINCREMENT 的高水位不会因删除最高一行而回退；观察器用它
            # 判断数据库是否被恢复到旧快照，不能把正常清理误报成回滚。
            memory_id_high_water = int(conn.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence "
                "WHERE name='memories'),0)"
            ).fetchone()[0] or 0)
            chat_id_high_water = int(conn.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence "
                "WHERE name='chat_log'),0)"
            ).fetchone()[0] or 0)
            new_reasoning_leaks = 0
            new_duplicate_rows = 0
            if after_memory_id is not None:
                boundary = max(0, int(after_memory_id))
                new_reasoning_leaks = int(conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE id>? AND ("
                    "value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
                    " OR value LIKE '%注意：%' OR value LIKE '%备注：%' OR value LIKE '%说明：%'"
                    " OR value LIKE '%该用户%' OR value LIKE '%这个用户%' OR value LIKE '%此处%'"
                    " OR value LIKE '%信息不一致%')", (boundary,)
                ).fetchone()[0])
                new_duplicate_rows = int(conn.execute(
                    "SELECT COUNT(*) FROM memories m WHERE m.id>? AND EXISTS ("
                 "SELECT 1 FROM memories p WHERE p.qq_id=m.qq_id "
                 "AND p.value=m.value AND p.id<m.id)", (boundary,)
                ).fetchone()[0])

            # 队列和发送 outbox 与其余字段在同一个只读事务内读取，避免
            # 两次独立连接跨越写入时刻而产生不一致的验收快照。
            queue_rows = conn.execute(
                "SELECT status,COUNT(*),MIN(created_at) FROM extraction_jobs "
                "WHERE pipeline='memory' GROUP BY status"
            ).fetchall()
            queue_counts = {
                status: 0 for status in self._QUEUE_STATUSES
            }
            queue_unknown = 0
            for row in queue_rows:
                status = str(row[0] or "")
                count = int(row[1] or 0)
                if status in self._QUEUE_STATUSES:
                    queue_counts[status] += count
                else:
                    queue_unknown += count
            queue_health = {
                **queue_counts,
                "unknown": queue_unknown,
                "total_open": sum(
                    count for status, count in queue_counts.items()
                    if status in ("pending", "leased", "ready")
                ),
                "oldest_open_at": min(
                    (str(row[2]) for row in queue_rows
                     if row[0] in ("pending", "leased", "ready") and row[2]),
                    default="",
                ),
            }
            outbox_rows = conn.execute(
                "SELECT status,COUNT(*) FROM send_outbox GROUP BY status"
            ).fetchall()
            outbox_counts = {
                status: 0 for status in self._OUTBOX_STATUSES
            }
            outbox_unknown = 0
            for row in outbox_rows:
                status = str(row[0] or "")
                count = int(row[1] or 0)
                if status in self._OUTBOX_STATUSES:
                    outbox_counts[status] += count
                else:
                    outbox_unknown += count
            stale_cutoff = (
                captured_at - timedelta(minutes=5)
            ).strftime("%Y-%m-%d %H:%M:%S")
            stale_sending = int(conn.execute(
                "SELECT COUNT(*) FROM send_outbox WHERE status='sending' AND "
                "COALESCE(NULLIF(updated_at,''),created_at)<=?",
                (stale_cutoff,),
            ).fetchone()[0] or 0)
            task_action_schema_exists = all(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                for table in ("tasks", "task_action_attempts", "send_outbox")
            )
            task_action_phase2b_exists = all(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                for table in (
                    "task_action_items", "task_action_children",
                    "task_action_confirmations",
                )
            )
            linked_invariant_violations = (
                int(conn.execute(
                    _TASK_ACTION_INVARIANT_COUNT_SQL
                ).fetchone()[0] or 0)
                if task_action_schema_exists and task_action_phase2b_exists else 0
            )
            bare_sending_tasks = (
                int(conn.execute(
                    _TASK_BARE_SENDING_COUNT_SQL
                ).fetchone()[0] or 0)
                if task_action_schema_exists else 0
            )
            task_confirmed_total = int(conn.execute(
                "SELECT COUNT(*) FROM confirmed_action_facts "
                "WHERE source_id LIKE 'task:%:attempt:%'"
            ).fetchone()[0] or 0)
            confirmed_fact_integrity_violations = (
                Store._confirmed_fact_integrity_count_conn(conn)
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='confirmed_action_facts'"
                ).fetchone() else 0
            )
            outbox_health = {
                **outbox_counts,
                "unknown": outbox_unknown,
                "open": sum(
                    outbox_counts.get(status, 0) for status in ("pending", "sending")
                ),
                "needs_review": (
                    outbox_counts.get("uncertain", 0)
                    + outbox_counts.get("dead", 0)
                    + outbox_counts.get("confirmed_unaccounted", 0)
                    + outbox_counts.get("confirmed_conflict", 0)
                    + stale_sending
                    + linked_invariant_violations
                    + confirmed_fact_integrity_violations
                    + bare_sending_tasks
                ),
                "stale_sending": stale_sending,
                "linked_invariant_violations": linked_invariant_violations,
                "confirmed_fact_integrity_violations": confirmed_fact_integrity_violations,
                "bare_sending_tasks": bare_sending_tasks,
                "task_action_schema_missing": not task_action_schema_exists,
                "task_confirmed_total": task_confirmed_total,
            }
            inbox_rows = conn.execute(
                "SELECT status,COUNT(*) FROM inbound_events GROUP BY status"
            ).fetchall()
            inbox_counts = {
                status: 0 for status in self._INBOUND_STATUSES
            }
            inbox_unknown = 0
            for row in inbox_rows:
                status = str(row[0] or "")
                count = int(row[1] or 0)
                if status in inbox_counts:
                    inbox_counts[status] += count
                else:
                    inbox_unknown += count
            inbox_health = {
                **inbox_counts,
                "unknown": inbox_unknown,
                "open": sum(
                    inbox_counts.get(status, 0)
                    for status in ("received", "claimed", "executing")
                ),
                "needs_review": (
                    inbox_counts.get("failed", 0)
                    + inbox_counts.get("uncertain", 0)
                    + inbox_unknown
                ),
            }
            proactive_schema_exists = all(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                for table in ("proactive_events", "proactive_event_state")
            )
            proactive_counts = {
                status: 0 for status in self._PROACTIVE_EVENT_STATUSES
            }
            proactive_unknown = 0
            proactive_total = 0
            proactive_invalid_payload = 0
            proactive_decision_runs = 0
            proactive_decision_runs_unknown = 0
            proactive_decision_runs_corrupt = 0
            if proactive_schema_exists:
                proactive_total = int(conn.execute(
                    "SELECT COUNT(*) FROM proactive_events"
                ).fetchone()[0] or 0)
                proactive_rows = conn.execute(
                    "SELECT COALESCE(s.status,''),COUNT(*) "
                    "FROM proactive_events p LEFT JOIN proactive_event_state s "
                    "ON s.event_id=p.event_id GROUP BY COALESCE(s.status,'')"
                ).fetchall()
                proactive_invalid_payload = int(conn.execute(
                    "SELECT COUNT(*) FROM proactive_events "
                    "WHERE json_valid(payload_json)=0 "
                    "OR json_type(payload_json,'$')!='object'"
                ).fetchone()[0] or 0)
                for row in proactive_rows:
                    status = str(row[0] or "")
                    count = int(row[1] or 0)
                    if status in proactive_counts:
                        proactive_counts[status] += count
                    else:
                        proactive_unknown += count
            proactive_health = {
                **proactive_counts,
                "total": proactive_total,
                "unknown": proactive_unknown,
                "invalid_payload": proactive_invalid_payload,
                "proactive_decision_runs": proactive_decision_runs,
                "proactive_decision_runs_unknown": proactive_decision_runs_unknown,
                "proactive_decision_runs_corrupt": proactive_decision_runs_corrupt,
                "open": sum(
                    proactive_counts.get(status, 0)
                    for status in ("pending", "claimed", "executing")
                ),
                "schema_missing": not proactive_schema_exists,
            }
            decision_schema_exists = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='decision_runs'"
            ).fetchone())
            if decision_schema_exists and proactive_schema_exists:
                proactive_decision_runs = int(conn.execute(
                    "SELECT COUNT(*) FROM decision_runs d "
                    "INNER JOIN proactive_events p ON p.event_id=d.event_key"
                ).fetchone()[0] or 0)
                proactive_decision_runs_corrupt = int(conn.execute(
                    "SELECT COUNT(*) FROM decision_runs d "
                    "INNER JOIN proactive_events p ON p.event_id=d.event_key "
                    "WHERE json_valid(d.tool_calls_json)=0 OR "
                    "json_type(d.tool_calls_json,'$')!='array'"
                ).fetchone()[0] or 0)
                proactive_decision_runs_unknown = int(conn.execute(
                    "SELECT COUNT(*) FROM decision_runs d "
                    "INNER JOIN proactive_events p ON p.event_id=d.event_key "
                    "WHERE d.status NOT IN ('completed','failed')"
                ).fetchone()[0] or 0)
            proactive_health.update({
                "proactive_decision_runs": proactive_decision_runs,
                "proactive_decision_runs_unknown": proactive_decision_runs_unknown,
                "proactive_decision_runs_corrupt": proactive_decision_runs_corrupt,
            })
            decision_counts = {status: 0 for status in ("completed", "failed")}
            decision_total = decision_unknown = decision_corrupt = 0
            if decision_schema_exists:
                decision_total = int(conn.execute(
                    "SELECT COUNT(*) FROM decision_runs"
                ).fetchone()[0] or 0)
                decision_rows = conn.execute(
                    "SELECT status,COUNT(*) FROM decision_runs GROUP BY status"
                ).fetchall()
                decision_corrupt = int(conn.execute(
                    "SELECT COUNT(*) FROM decision_runs WHERE "
                    "json_valid(tool_calls_json)=0 OR "
                    "json_type(tool_calls_json,'$')!='array'"
                ).fetchone()[0] or 0)
                for status, count in decision_rows:
                    if str(status) in decision_counts:
                        decision_counts[str(status)] += int(count or 0)
                    else:
                        decision_unknown += int(count or 0)
            decision_health = {
                **decision_counts,
                "total": decision_total,
                "unknown": decision_unknown,
                "corrupt": decision_corrupt,
                "schema_missing": not decision_schema_exists,
            }
            # P0-1b：仅以脱敏聚合证明业务链路是否可关联。绝不输出
            # event_key、QQ、群号、正文、run_id 或 receipt payload；这些计数
            # 让观察器区分“没有确认动作样本”和“入站到决策已断线”。
            interaction_tables = (
                "inbound_events", "chat_log", "decision_runs",
                "confirmed_action_facts",
            )
            interaction_schema_exists = all(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                for table in interaction_tables
            )
            interaction_lifecycle = {
                "inbound_chat_links": 0,
                "inbound_decision_runs": 0,
                "inbound_confirmed_action_receipts": 0,
                "complete_lifecycles": 0,
                "schema_missing": not interaction_schema_exists,
            }
            if interaction_schema_exists:
                interaction_lifecycle.update({
                    "inbound_chat_links": int(conn.execute(
                        "SELECT COUNT(DISTINCT e.event_key) "
                        "FROM inbound_events e JOIN chat_log c "
                        "ON c.event_key=e.event_key"
                    ).fetchone()[0] or 0),
                    "inbound_decision_runs": int(conn.execute(
                        "SELECT COUNT(DISTINCT d.run_id) "
                        "FROM decision_runs d JOIN inbound_events e "
                        "ON e.event_key=d.event_key"
                    ).fetchone()[0] or 0),
                    "inbound_confirmed_action_receipts": int(conn.execute(
                        "SELECT COUNT(DISTINCT f.domain_action_id) "
                        "FROM confirmed_action_facts f JOIN chat_log c "
                        "ON c.id=f.source_chat_id JOIN inbound_events e "
                        "ON e.event_key=c.event_key"
                    ).fetchone()[0] or 0),
                    "complete_lifecycles": int(conn.execute(
                        "SELECT COUNT(DISTINCT e.event_key) "
                        "FROM inbound_events e JOIN chat_log c "
                        "ON c.event_key=e.event_key JOIN decision_runs d "
                        "ON d.event_key=e.event_key JOIN confirmed_action_facts f "
                        "ON f.source_chat_id=c.id"
                    ).fetchone()[0] or 0),
                })
            window_schema_exists = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='conversation_window_events'"
            ).fetchone())
            if window_schema_exists:
                window_event_count = int(conn.execute(
                    "SELECT COUNT(*) FROM conversation_window_events"
                ).fetchone()[0] or 0)
                window_max_id = int(conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM conversation_window_events"
                ).fetchone()[0] or 0)
                window_invalid = int(conn.execute(
                    "SELECT COUNT(*) FROM conversation_window_events WHERE "
                    "conversation_user_id='' OR actor_kind!='bot' OR "
                    "(channel='group' AND (group_id='' OR scope_id!=group_id)) OR "
                    "(channel='private' AND (group_id!='' OR scope_id!='_private_' || conversation_user_id)) OR "
                    "channel NOT IN ('group','private')"
                ).fetchone()[0] or 0)
                window_future = int(conn.execute(
                    "SELECT COUNT(*) FROM conversation_window_events WHERE occurred_at>?",
                    (captured_at.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()[0] or 0)
            else:
                window_event_count = window_max_id = window_invalid = window_future = 0

        trust_levels = {
            level: 0 for level in self._TRUST_LEVELS
        }
        unknown_trust_levels = 0
        for row in trust_rows:
            level = str(row[0] or "")
            count = int(row[1] or 0)
            if level in self._TRUST_LEVELS:
                trust_levels[level] += count
            else:
                unknown_trust_levels += count
        trust_levels["unknown"] = unknown_trust_levels
        trusted = sum(
            trust_levels.get(level, 0)
            for level in ("verified", "manual", "corrected")
        )
        return {
            "ok": True,
            "metrics": metrics,
            "metrics_total": metrics_total,
            "metrics_total_complete": True,
            "metric_integrity": {
                "invalid_values": invalid_metric_values,
                "negative_values": negative_metric_values,
            },
            "backlog": {
                "total_user_messages": total_user_messages,
                "backlog_messages": backlog_messages,
                "backlog_users": int(backlog_users or 0),
                "over_30_users": int(over_30_users or 0),
                "oldest_at": oldest_at,
                **backlog_policy,
            },
            "queue": queue_health,
            "outbox": outbox_health,
            "inbox": inbox_health,
            "proactive": proactive_health,
            "decision_runs": decision_health,
            "interaction_lifecycle": interaction_lifecycle,
            "window": {
                "event_count": window_event_count,
                "max_id": window_max_id,
                "invalid_events": window_invalid,
                "future_events": window_future,
                "schema_missing": not window_schema_exists,
            },
            "memory": {
                "active": active,
                "trusted": trusted,
                "trust_levels": trust_levels,
                "counter_day": counter_day,
                "reasoning_leaks_today": reasoning_leaks,
                "reasoning_leaks_total": reasoning_leaks_total,
                "duplicate_groups_today": duplicate_groups,
                "duplicate_groups_total": duplicate_groups_total,
                "max_id": max_memory_id,
                "id_high_water": memory_id_high_water,
                "new_reasoning_leaks": new_reasoning_leaks,
                "new_duplicate_rows": new_duplicate_rows,
            },
            "chat_id_high_water": chat_id_high_water,
        }
