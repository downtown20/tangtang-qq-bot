"""
小糖糖的记忆系统 🧠
短期记忆 + 长期记忆 + 亲密度追踪
"""

import json
import logging
import math
import re

logger = logging.getLogger("糖糖.Memory")
from datetime import datetime, timedelta
from collections import deque
from typing import Optional
from dataclasses import dataclass

from .store import Store, TRUSTED_TRUST_LEVELS, TRUST_INJECT_PRIORITY
from . import protocols as _protocols  # 画像标注/根基契约常量（2026-08-15）
from .async_io import run_bounded_blocking, run_bounded_store_io


# ── 行首列表标记剥离（2026-10-15 T58，修 D1）──
# 旧写法 `^[\s\-•·\d.]+\s*` 锚不住「列表标记」：它对**裸数字**照吃不误，
# 于是 `2026年10月5日休学…` 被啃成 `年10月5日休学…`——年份在库里的记忆与画像里
# 已经丢了一个多月（DB 实测 33 条 fact_synthesis + 14 份 people.notes，最近 10-13 仍在产生）。
# 注释写的是「支持 - 开头或数字开头的列表」，实现却在吃任意连续数字。
# 现在只认三种标记：- / • / · 开头，或 `1.` `1、` `1)` 这类**短**编号
#（\d{1,3} 刻意排除 4 位年份与点分日期）。正例/反例见
# tests/test_memory_recall_budget.py::test_list_marker_strip_keeps_year。
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-•·]\s*|\d{1,3}[.、)]\s*)\s*")


def _strip_list_marker(line: str) -> str:
    """剥掉行首列表标记，保留裸数字——年份/日期/数量都不是标记。"""
    return _LIST_MARKER_RE.sub("", line).strip()


@dataclass
class MemoryEntry:
    """一条记忆"""
    id: int = 0         # 数据库主键（用于 UPDATE/DELETE 定位）
    qq_id: str = ""
    key: str = ""       # 记忆类型：like/hate/fact/event/said
    cognitive: str = "semantic"  # 认知类型：episodic（发生过的事）| semantic（知道的事）
    value: str = ""     # 记忆内容
    timestamp: str = ""
    importance: int = 3 # 1-10（原始权重）
    confidence: float = 0.7    # LLM 提取时的置信度 (0.0-1.0)
    origin: str = "extracted"  # 记忆来源：extracted | summarized | manual | self
    effective: float = 0.0   # 衰减后的有效权重
    last_recalled: str = ""  # 上次被回忆的时间
    recall_count: int = 0    # 被回忆的次数
    target_qq: str = ""      # 自我记忆对应的对话对象（普通记忆为空）
    source_group_id: str = ""  # 自我记忆来源群（普通记忆为空）
    evidence_ids: str = ""    # 支持该记忆的 chat_log id（逗号分隔）
    trust_level: str = "legacy_unverified"
    retention: str = "normal"
    event_time: str = ""
    ingested_at: str = ""
    valid_from: str = ""
    valid_to: str = ""
    superseded_by: int | None = None
    idempotency_key: str = ""


class MemorySystem:
    """三层记忆：短期缓冲 → 长期存储 → 摘要提取"""

    @staticmethod
    def _qq_suffix(qq_id: str) -> str:
        """身份锚：QQ尾号4位，帮助LLM跨昵称识别同一个人。
        短于4位的QQ号直接全用（不会出现，QQ号都5位以上）。"""
        return qq_id[-4:] if len(qq_id) >= 4 else qq_id

    @staticmethod
    def _char_similarity(a: str, b: str) -> float:
        """字符级相似度——embedding 缺失时的去重降级方案。
        用 2-gram 重叠率，对中文短文本效果接近 BGE（「不是gay」vs「不是gay」= 1.0）。"""
        if a == b:
            return 1.0
        if len(a) < 4 or len(b) < 4:
            return 1.0 if a == b else 0.0
        def _grams(s): return {s[i:i+2] for i in range(len(s)-1)}
        ga, gb = _grams(a), _grams(b)
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / min(len(ga), len(gb))

    @staticmethod
    def _truncate_natural(text: str, max_len: int = 100) -> str:
        """自然断点截断——在 max_len 内找最后一个句号/分号/逗号处截断。
        避免「在清华大学计算机系读大三，平时喜欢打篮」这种切碎句子的情况。"""
        if len(text) <= max_len:
            return text
        for sep in ['。', '；', '，', '、', ' ']:
            pos = text.rfind(sep, 0, max_len)
            if pos > max_len * 0.6:  # 至少保留 60% 才有意义
                return text[:pos + 1]
        return text[:max_len]

    def __init__(self, db_path: str = "./memory.db", short_term_size: int = 50,
                 store: Store = None):
        self.db_path = db_path
        self.short_term_size = short_term_size

        # 数据访问层（可注入，不传则自动创建）
        self.store = store if store is not None else Store(db_path)

        # 短期记忆：每个群的最近消息
        self.short_term: dict[str, deque] = {}   # group_id -> deque of messages

        # 当前正在聊的群友（用于追踪上下文）
        self.active_conversations: dict[str, list] = {}  # qq_id -> recent exchanges

        # LLM 记忆提取去重：每个QQ已处理到的最大消息ID
        self._last_extracted_id: dict[str, int] = {}

        # auto_learn 去重缓存：防止同一事实被重复存储
        self._dedup_cache: set[tuple] = set()
        self._dedup_max = 5000

    # ---- 人物档案 ----

    def get_or_create_person(self, qq_id: str, nickname: str = "") -> dict:
        """获取或创建人物档案（委托给 Store）"""
        return self.store.get_or_create_person(qq_id, nickname)

    def update_person(self, qq_id: str, *, actor: str = "", reason: str = "",
                      **kwargs):
        """更新人物档案（委托给 Store）。

        `actor` / `reason` 只用于记忆留痕（memory_revisions），不是 people 列。
        传空则由 Store 落 `unknown`——生产调用点必须显式给出，否则
        `test_memory_revision_trail.py` 会红。
        """
        self.store.update_person(qq_id, actor=actor or "unknown",
                                 reason=reason, **kwargs)

    def active_notes(self, qq_id: str) -> str:
        """画像统一出口——dirty 或无可信谱系一律返回空。
        所有画像消费点（注入/人物图/工具返回/搜索）必须走这里，禁止直接
        person.get("notes")——否则纠正后的旧画像从旁路回到 LLM 视野。

        2026-10-12 拍板（审计第九部分 #5）：**legacy_unverified 明确停用自动注入**，
        不做批量重合成。查库实测：非空画像 109 份，其中 legacy 85 / verified 24
        （2026-08-16 事故防线，不放松证据门控）。要让某份 legacy 画像重新可注入，
        只能经显式重合成产出 verified 快照（handler 的纠正后重合成，
        或人工 update_person）——**不要在这里放宽这个集合**。"""
        person = self.get_or_create_person(qq_id)
        if not person or person.get("notes_dirty"):
            return ""
        # 放行集合来自 store.TRUSTED_TRUST_LEVELS——与 `query_memories(trusted_only)`
        # 同一个常量。这两道门（画像注入 / 记忆召回）必须放行同一批等级，否则
        # 「剖面页说可注入 N 条」与「实际能召回多少」会变成两个数。
        if person.get("notes_trust_level") not in TRUSTED_TRUST_LEVELS:
            return ""
        return person.get("notes", "") or ""

    # ---- 外号/别名 ----

    def add_alias(self, qq_id: str, alias: str, source: str = "auto"):
        """记录一个人的外号（委托给 Store）"""
        self.store.add_alias(qq_id, alias, source)

    def get_aliases(self, qq_id: str) -> list[str]:
        """获取一个人的所有外号（委托给 Store）"""
        return self.store.get_aliases(qq_id)

    def add_intimacy(self, qq_id: str, amount: int) -> int:
        """增加亲密度，返回新值（委托给 Store）"""
        return self.store.add_intimacy(qq_id, amount)

    # ---- 记忆 ----

    def remember(self, qq_id: str, key: str, value: str, importance: int = 3,
                 cognitive: str = "semantic", confidence: float = 0.7,
                 origin: str = "extracted", target_qq: str = "",
                 source_group_id: str = "", evidence_ids: str = "",
                 retention: str = "normal", event_time: str = "",
                 valid_from: str = "", valid_to: str = "",
                 idempotency_key: str = "",
                 evidence_quote: str = "", claim_type: str = "stated") -> int:
        """记录一条长期记忆，返回自增 id。

        P0-D2 收口：evidence_quote/claim_type 透传给 insert_memory 的统一
        证据校验（旧调用零影响）。"""
        return self.store.insert_memory(qq_id, key, value, importance,
                                        cognitive=cognitive, confidence=confidence,
                                        origin=origin, target_qq=target_qq,
                                        source_group_id=source_group_id,
                                        evidence_ids=evidence_ids,
                                        retention=retention, event_time=event_time,
                                        valid_from=valid_from, valid_to=valid_to,
                                        idempotency_key=idempotency_key,
                                        evidence_quote=evidence_quote,
                                        claim_type=claim_type)

    def correct_memory(self, subject_qq: str, wrong_fact: str,
                       corrected_fact: str = "", embed_engine=None,
                       source_group_id: str | None = None) -> dict:
        """纠正闭环执行体（2026-08-16 批 2）——correct_memory/forget_memory 工具调用。
        同步、不调 LLM（工具执行器内禁止嵌套 LLM 调用，教训 #27）。

        策略（Codex 对齐）：
        1. 保守匹配：精确 LIKE 包含为主，BGE 语义 >0.7 兜底——低相似不批量撤销
        2. 命中行标 status：合成行 superseded，原始事实 retracted（不动 importance）
        3. 同文本簇事实撤销并清空簇摘要（摘要已不可信）
        4. wrong_fact 关键词出现在 people.notes → notes_dirty=1——禁止继续注入，
           由 handler 在主调用结束后后台重合成
        5. corrected_fact 非空 → 写 origin='corrected' confidence=1.0 的纠正事实
        """
        wrong = (wrong_fact or "").strip()
        if not wrong:
            return {"retracted": 0, "superseded": 0, "notes_dirty": False, "corrected_id": 0}

        # 1) 精确包含匹配（保守主路）
        hits = self.store.search_memories_by_text(
            subject_qq, wrong, source_group_id=source_group_id,
        )
        hit_ids = {h["id"]: h for h in hits}

        # 2) BGE 语义兜底（>0.7 才撤销）
        rows_all = self.store.query_memories(
            subject_qq, limit=200, source_group_id=source_group_id,
        )
        eng = embed_engine or getattr(self, "embed_engine", None)
        if eng and eng.ready:
            try:
                qv = eng.encode(wrong)
                if qv is not None:
                    embs = self.store.batch_get_embeddings([r["id"] for r in rows_all])
                    for r in rows_all:
                        if r["id"] in hit_ids:
                            continue
                        emb = embs.get(r["id"])
                        if emb is not None and eng.similarity(qv, emb) > 0.7:
                            hit_ids[r["id"]] = r
            except Exception:
                pass

        retracted, superseded = [], []
        for mem_id, mem in hit_ids.items():
            status = "superseded" if mem["key"] in _protocols.SYNTHESIS_KEYS else "retracted"
            # 2026-10-24：留痕必须带 actor + reason——只记「变了」回答不了
            # 「谁改的、为什么改」，而纠正是最需要事后复盘的一类变更。
            self.store.set_memory_status(
                mem_id, status, actor="tool:correct_memory",
                reason=f"纠正：否定「{wrong[:60]}」",
            )
            (superseded if status == "superseded" else retracted).append(mem_id)

        # 3) 旧事实簇没有会话作用域，只允许无作用域的维护调用修改。
        cluster_retracted = 0
        if source_group_id is None:
            for cf in self.store.search_cluster_facts_by_text(subject_qq, wrong):
                self.store.retract_cluster_fact(
                    cf["id"], "retracted", actor="tool:correct_memory",
                    reason=f"纠正：否定簇事实「{wrong[:60]}」",
                )
                cluster_retracted += 1

        # 4) notes 脏标（jieba 分词关键词在 notes 中出现即标脏）
        notes_dirty = False
        try:
            import jieba as _jb
            kws = [w for w in _jb.cut(wrong) if len(w) >= 2]
        except Exception:
            kws = [wrong[:8]]
        person = self.store.get_or_create_person(subject_qq, "")
        notes = person.get("notes", "") or ""
        notes_source_ids = {
            int(item) for item in str(person.get("notes_source_ids") or "").split(",")
            if item.strip().isdigit() and int(item) > 0
        }
        audit_by_id = {
            int(row["id"]): row for row in self.store.query_memories(
                subject_qq, limit=None, include_retracted=True,
            )
        }
        source_rows = [audit_by_id[item] for item in notes_source_ids if item in audit_by_id]
        profile_scopes = {
            str(row.get("source_group_id") or "") for row in source_rows
        }
        if source_group_id is None:
            profile_scope_matches = True
        elif notes_source_ids:
            profile_scope_matches = (
                len(source_rows) == len(notes_source_ids)
                and profile_scopes == {str(source_group_id)}
            )
        else:
            # 人工/纠正画像没有自动谱系，视为用户显式确认的全局资料。
            profile_scope_matches = person.get("notes_trust_level") in {"manual", "corrected"}
        notes_hit = bool(
            notes and profile_scope_matches
            and (
                any(kw in notes for kw in kws)
                or bool(notes_source_ids & set(hit_ids))
            )
        )
        if notes_hit:
            self.store.update_person(
                subject_qq, notes_dirty=1, actor="tool:correct_memory",
                reason="纠正命中画像关键词：标脏，禁止继续注入旧 notes",
            )
            notes_dirty = True

        # 4.5) 零匹配 fail-closed（2026-08-17 事故）：wrong_fact 在 subject 的记忆、
        # 簇事实、画像里全都匹配不到 → 极可能是纠正错了人（现场：消息里只有昵称、
        # LLM 解析不出 QQ，把纠正写到了当前用户头上）。此时拒绝写纠正事实，
        # 返回 matched=0 让工具层提示「先确认这个人是谁」。
        matched = len(hit_ids) + cluster_retracted + (1 if notes_hit else 0)
        if matched == 0:
            return {"retracted": 0, "superseded": 0, "notes_dirty": False,
                    "corrected_id": 0, "matched": 0}

        # 5) 写纠正事实（同步建 embedding，保证纠正后立即可检索）
        corrected_id = 0
        if corrected_fact and corrected_fact.strip():
            corrected_id = self.remember(
                subject_qq, "fact_correction", corrected_fact.strip(),
                importance=8, cognitive="semantic", confidence=1.0,
                origin="corrected",
                source_group_id=str(source_group_id or ""),
            )
            if eng and eng.ready:
                try:
                    vec = eng.encode(corrected_fact.strip())
                    if vec is not None:
                        self.store.set_embedding(corrected_id, vec)
                except Exception:
                    pass

        # 6) 补「被谁取代」的指针。
        #
        # 纠正流程是「先标旧行、后写纠正事实」——第 2 步标 status 的那一刻，
        # 纠正事实（第 5 步）还没有 id，指针没法写。所以在这里补最后一刀，
        # 否则演化史里这些行就是断链（实测全库 1261 条 superseded 无指针）。
        #
        # 只在**写了纠正事实**时才补：没有 corrected_fact 的纠正只是「撤回」，
        # 被否定的事物本来就没有替代品，硬编一个指针等于伪造版本关系。
        if corrected_id and superseded:
            try:
                self.store.link_superseded(
                    superseded, corrected_id, actor="tool:correct_memory",
                    reason=f"纠正：被纠正事实 #{corrected_id} 取代",
                )
            except Exception:
                logger.debug("纠正：补 superseded_by 指针失败（不影响纠正本体）",
                             exc_info=True)

        return {
            "retracted": len(retracted), "superseded": len(superseded),
            "notes_dirty": notes_dirty, "corrected_id": corrected_id,
            "matched": matched,
        }

    def recall(self, qq_id: str, limit: int = 20, query_text: str = "",
               embed_engine=None, reranker=None, query_vec=None,
               target_qq: str = "", grounded_only: bool | None = None,
               trusted_only: bool = True,
               uncertain_only: bool = False,
               min_importance: int = 0, min_confidence: float = 0.0,
               source_group_id: str | None = None,
               reinforce_after: bool = False) -> list[MemoryEntry]:
        """回忆：BGE语义粗筛 → Reranker精排 → importance + 时间衰减。

        三阶段检索：
        1. 语义粗筛：BGE相似度取 top-50 缩小候选集
        2. Reranker精排：Cross-Encoder 对 top-30 精排到 top-20（可选，Reranker未就绪则跳过）
        3. 终排：importance + 类型权重 + 指数时间衰减（Ebbinghaus简化）

        query_vec：2026-08-15 整体审查性能——调用方已编码过同一 query 时直接复用，
        避免同一消息每回合被 encode 2-3 次（每次 ~4ms CPU）。

        uncertain_only（2026-10-24 不确定性三档）：反选信任门——只取**没有原话佐证**
        的记忆（legacy_unverified/unverified/NULL），供「她模糊记得」那一档使用。
        与 trusted_only 互斥（同时为真由 store 抛 ValueError，这里不重复判定——
        判定只能有一处）。min_importance/min_confidence 是模糊路的额外质量闸门：
        在 SQL 侧收窄，而不是取回一堆碎片再在 Python 里筛（那样 limit 会被噪声吃满）。

        reinforce_after（2026-10-25 第 11 轮）：召回到的记忆回写「提取强度」。

        ⚠ **默认关闭。只有对话路径（记忆块 / 自忆 / `search_memories` 工具）显式传
        True。** 为什么不是默认开——第一版就是默认开，实测**立刻写放大**：bot 启动后
        两分钟内 96 条被 `importance+1`，因为 `recall()` 同时被**后台任务批量调用**
        （启动时的记忆整理、反思、关系合成）。而「被后台扫过」**不等于**「她想起了」，
        把两者混起来会让排序被后台任务污染。

        这和第 9 轮「核了工具存在、没核工具作用域」是同一类错：**核了「接在底层
        能覆盖全部调用点」，没核「全部调用点里有哪些不该覆盖」**。
        所以现在的方向是 **fail-closed**：漏标一个调用点，代价是「不强化」（安全），
        而不是「过度强化」（污染排序）。

        为什么这事值得做：`memories` 有 `last_recalled` / `recall_count` 两列
        （对应 Bjork 双强度模型里的**提取强度**），`memory.reinforce()` 和
        `store.reinforce_memory_by_id()` 都有完整实现、`health_check` 还有监控项。
        查库的真相是：2026-08 有大量回写（8-17 单日 251 条），**9 月起几乎归零**
        ——不是「从未接」，是**接了之后在重构中掉了**。掉了的后果是三件静默失效：
          ① Phase 2 的休眠降权（60 天不用 ×0.5）**不再执行**——新记忆字段全空，
             `if last_rec` 直接挡住；
          ② `cleanup_stale_memories` 判「从未回忆」用的是 `recall_count = 0`，
             而新记忆恒为 0 → 系统把它们当成「从未用过」；
          ③ 「她最近到底想起了什么」无法观测——记忆能力判据 J1/J2 没有测量基础。

        ⚠ **模糊档必须关掉**（`recall_uncertain` 恒传 False）：强化会 `importance +1`，
        让「她不确定的事」一路爬到可信档，等于用「被检索次数」伪造证据等级。

        ⚠ 副作用是**设计意图不是 bug**：回写一开，休眠降权从此真正生效；被用起来的
        记忆 importance 会缓慢上升（30 分钟冷却 + 上限 10 兜着）。
        """

        # grounded_only 是旧调用方兼容名；新模型以关系化证据验证后的
        # trust_level 为唯一真值入口，不能再凭非空 CSV 自证。
        if grounded_only is not None:
            trusted_only = grounded_only
        rows = self.store.query_memories(
            qq_id, target_qq=target_qq, trusted_only=trusted_only,
            uncertain_only=uncertain_only,
            min_importance=min_importance, min_confidence=min_confidence,
            source_group_id=source_group_id,
            # 显式查询必须遍历该主体的完整可信集合；否则旧的 top-200 会让
            # 低 importance 但高度相关的永久记忆永远不可达。
            limit=None if query_text else 200,
        )
        if not rows:
            return []

        now = datetime.now()

        # ── Phase 1: 全量作用域内混合召回（向量 + jieba 完整词）──
        if query_text:
            import jieba as _jieba

            lexical_stop_terms = {
                "什么", "怎么", "为什么", "这个", "那个", "以前", "现在",
                "时候", "事情", "东西", "有没有", "是不是",
            }

            def _terms(text: str) -> set[str]:
                return {
                    token.strip().casefold()
                    for token in _jieba.cut(str(text or ""))
                    if len(token.strip()) >= 2
                    and token.strip().casefold() not in lexical_stop_terms
                    and any(ch.isalnum() or '\u4e00' <= ch <= '\u9fff'
                            for ch in token)
                }

            query_terms = _terms(query_text)
            semantic_ranked = []
            semantic_scores: dict[int, float] = {}
            if embed_engine and embed_engine.ready:
                if query_vec is None:
                    query_vec = embed_engine.encode(query_text)
                if query_vec is not None:
                    embeddings = self.store.batch_get_embeddings(
                        [row["id"] for row in rows]
                    )
                    for row in rows:
                        embedding = embeddings.get(row["id"])
                        if embedding is None:
                            continue
                        similarity = float(
                            embed_engine.similarity(query_vec, embedding)
                        )
                        # 绝对拒绝阈值：top-k 只表示相对名次，不能证明相关。
                        if similarity >= 0.40:
                            semantic_ranked.append((similarity, row))
                            semantic_scores[int(row["id"])] = max(
                                0.0, min(1.0, similarity)
                            )
                    semantic_ranked.sort(key=lambda item: item[0], reverse=True)

            lexical_ranked = []
            lexical_scores: dict[int, float] = {}
            if query_terms:
                folded_query = query_text.casefold()
                for row in rows:
                    value = str(row.get("value") or "")
                    memory_terms = _terms(value)
                    overlap = len(query_terms & memory_terms)
                    exact_bonus = 1 if (
                        folded_query in value.casefold()
                        or value.casefold() in folded_query
                    ) else 0
                    if overlap or exact_bonus:
                        score = overlap / max(1, min(len(query_terms), len(memory_terms)))
                        lexical_score = score + exact_bonus
                        # 一个泛词相同不代表相关；无向量时也要具备绝对拒绝能力。
                        if exact_bonus or score >= 0.50:
                            lexical_ranked.append((lexical_score, row))
                            lexical_scores[int(row["id"])] = min(1.0, lexical_score)
                lexical_ranked.sort(key=lambda item: item[0], reverse=True)

            # Reciprocal Rank Fusion：通道只负责提供候选，不让任一通道用相对
            # top-k 强塞零相关结果。相同 id 合并后保留稳定排序。
            fused: dict[int, tuple[float, dict]] = {}
            for ranked in (semantic_ranked[:50], lexical_ranked[:50]):
                for rank, (_, row) in enumerate(ranked, start=1):
                    memory_id = int(row["id"])
                    old_score, _ = fused.get(memory_id, (0.0, row))
                    fused[memory_id] = (old_score + 1.0 / (60 + rank), row)
            rows = []
            for fusion_score, row in sorted(
                    fused.values(), key=lambda item: (-item[0], -int(item[1]["id"]))):
                candidate = dict(row)
                memory_id = int(candidate["id"])
                candidate["_retrieval_relevance"] = max(
                    semantic_scores.get(memory_id, 0.0),
                    lexical_scores.get(memory_id, 0.0),
                )
                candidate["_fusion_score"] = fusion_score
                rows.append(candidate)
            if not rows:
                return []

        # ── Phase 1.5: Reranker 精排（Cross-Encoder 对 BGE 粗排结果重新打分）──
        if query_text and reranker and reranker.ready and len(rows) > limit:
            try:
                # 取 BGE 粗排 top-30，Reranker 精排到 top-20（或 limit×2，取大者）
                coarse = rows[:30]
                rerank_top = max(limit * 2, 20)
                reranked = reranker.pick_top(query_text, coarse, top_k=min(rerank_top, len(coarse)),
                                             value_getter=lambda r: r.get("value", ""))
                if reranked:
                    rows = reranked
            except Exception as e:
                logger.debug(f"Reranker 精排失败，跳过: {e}")

        # ── Phase 2: importance + 类型权重 + 认知类型 + 差异化衰减 + 休眠 ──
        scored = []
        for r in rows:
            score = float(r["importance"])

            # 2026-08-16 批 5：置信度进入打分——0.55 种子与 1.0 事实不再同权
            # （此前 confidence 只做入库门槛，检索时失效——「特摄仙人」靠
            # importance 强化爬升到 5 的通道）
            try:
                conf = float(r.get("confidence", 0.7) or 0.7)
                score *= 0.5 + conf * 0.5
            except (TypeError, ValueError):
                pass
            # 纠正事实优先级最高——它是对方亲口说的最新真相
            if r.get("origin") == "corrected":
                score *= 1.4

            # 类型权重
            key = r.get("key", "")
            if key == "said":
                score *= 0.5
            elif key == "promise":
                score *= 1.5   # 承诺/自我记忆——糖糖自己说过要做什么
            elif key in ("fact", "like", "hate", "habit"):
                score *= 1.1

            # R2-3: 认知类型差异化衰减
            # episodic（事件）快速淡化，半衰~30天；semantic（事实）长期保留，半衰~120天
            cognitive = r.get("cognitive", "semantic") or "semantic"
            try:
                ts = r.get("event_time", "") or r.get("timestamp", "")
                if ts:
                    age_days = (now - datetime.strptime(ts[:10], "%Y-%m-%d")).days
                    if age_days <= 7:
                        score += 1.0
                    elif age_days > 30:
                        if cognitive == "episodic":
                            # 快速衰减：30天后半衰~30天
                            score *= max(0.15, math.exp(-0.023 * (age_days - 30)))
                        else:
                            # 慢速衰减：半衰~120天
                            score *= max(0.25, math.exp(-0.008 * (age_days - 30)))
            except (ValueError, IndexError):
                pass

            # R2-2: 记忆休眠——长期未被回忆的记忆额外降权
            try:
                last_rec = r.get("last_recalled", "")
                if last_rec:
                    last_dt = datetime.strptime(str(last_rec)[:10], "%Y-%m-%d")
                    dormant_days = (now - last_dt).days
                    if dormant_days > 60:
                        score *= 0.5   # 两个月没被想起——大幅降权
                    elif dormant_days > 30:
                        score *= 0.7   # 一个月没被想起——轻微降权
            except (ValueError, IndexError):
                pass

            # episodic 检索加权：对话中"一起经历过的事"比"知道的事"更有连接感
            if cognitive == "episodic":
                score *= 1.2

            if query_text:
                # 查询场景必须以相关性为主。importance/置信度/时间只做有界调权，
                # 不能让 0.40 临界候选把 1.0 强相关事实挤出最终窗口。
                relevance = float(r.get("_retrieval_relevance", 0.0) or 0.0)
                quality = min(1.0, max(0.0, score) / 15.0)
                score = relevance * 0.90 + quality * 0.10

            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        entries = [
            MemoryEntry(
                id=r["id"], qq_id=r["qq_id"], key=r["key"], value=r["value"],
                cognitive=r.get("cognitive", "semantic") or "semantic",
                timestamp=r["timestamp"], importance=r["importance"],
                confidence=float(r.get("confidence", 0.7) or 0.7),
                origin=r.get("origin", "extracted") or "extracted",
                effective=round(score, 2),
                last_recalled=r.get("last_recalled", "") or "",
                recall_count=r.get("recall_count", 0) or 0,
                target_qq=r.get("target_qq", "") or "",
                source_group_id=r.get("source_group_id", "") or "",
                evidence_ids=r.get("evidence_ids", "") or "",
                trust_level=r.get("trust_level", "legacy_unverified") or "legacy_unverified",
                retention=r.get("retention", "normal") or "normal",
                event_time=r.get("event_time", "") or "",
                ingested_at=r.get("ingested_at", "") or "",
                valid_from=r.get("valid_from", "") or "",
                valid_to=r.get("valid_to", "") or "",
                superseded_by=r.get("superseded_by"),
                idempotency_key=r.get("idempotency_key", "") or "",
            )
            for score, r in scored[:limit]
        ]
        if reinforce_after and entries:
            # 提取强度回写（2026-10-25 第 11 轮，见方法 docstring）。
            # fail-open：写库失败绝不能让召回失败——否则「她想不起来」会被放大成
            # 「她这一轮没说话」。reinforce 内部已有 30 分钟冷却与 summarized 跳过。
            try:
                self.reinforce(qq_id, entries)
            except Exception as e:
                logger.debug(f"🧠 提取强度回写失败（不影响召回）: {e}")
        return entries

    def recall_uncertain(self, qq_id: str, limit: int | None = None,
                         query_text: str = "", embed_engine=None,
                         reranker=None, query_vec=None,
                         source_group_id: str | None = None,
                         ) -> list[MemoryEntry]:
        """模糊档召回（2026-10-24 不确定性三档）——薄封装，不另写一套排序。

        只做三件事：把「反选信任门 + 质量门槛」的参数钉死在一处，然后走**同一个**
        recall。为什么是薄封装而不是复制一份召回逻辑：排序/衰减/Reranker 这些
        规则如果分叉，两档就会用两套尺子量同一件事——下次调参只改一处，另一处
        悄悄腐烂（教训 #4/#14）。

        门槛取 protocols 的常量（UNCERTAIN_MIN_IMPORTANCE / MIN_CONFIDENCE）：
        DB 实测 active 模糊档 10,060 条，加双门槛后剩 943 条（94 人，人均 10 条），
        噪声面收窄一个数量级——不加门槛时「importance=10 的『我觉得可以炒』」这类
        碎片会直接进对话。"""
        return self.recall(
            qq_id,
            limit=int(limit if limit is not None else _protocols.UNCERTAIN_RECALL_LIMIT),
            query_text=query_text, embed_engine=embed_engine,
            reranker=reranker, query_vec=query_vec,
            trusted_only=False, uncertain_only=True,
            min_importance=_protocols.UNCERTAIN_MIN_IMPORTANCE,
            min_confidence=_protocols.UNCERTAIN_MIN_CONFIDENCE,
            source_group_id=source_group_id,
            # 模糊档**绝不**回写提取强度：强化会 importance+1，让「她不确定的事」
            # 一路爬到可信档——那等于用「被检索次数」伪造证据等级（第 11 轮）。
            reinforce_after=False,
        )

    def reinforce(self, qq_id: str, memory_entries: list):
        """
        强化记忆：被 recall 并在对话中用到的记忆，权重回升。
        每次强化 +1 importance（上限10），更新 last_recalled 和 recall_count。
        使用 memory ID 精确匹配，避免 LIKE 字符串误伤。

        冷却：同一条记忆 30 分钟内不重复强化——防止旧记忆越滚越高。
        """
        if not memory_entries:
            return
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        for m in memory_entries:
            mem_id = m.id if hasattr(m, 'id') else None
            if not mem_id:
                continue
            # 2026-08-16 批 5：合成行不强化——「检索→强化→再合成」的循环曾把
            # 错误画像从 importance 4 滚到 10（DB 实证），压过真实事实
            origin = getattr(m, 'origin', '') or ''
            if origin == "summarized":
                continue
            # 30 分钟冷却：同一条记忆不重复强化
            cooldown_key = f"last_reinforce:{mem_id}"
            last = self.store.kv_get(cooldown_key)
            if last:
                try:
                    last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
                    if (now - last_dt).total_seconds() < 1800:
                        continue
                except (ValueError, IndexError):
                    pass
            self.store.reinforce_memory_by_id(mem_id, now_str)
            self.store.kv_set(cooldown_key, now_str)

    def cleanup_stale_memories(self, days: int = 90) -> int:
        """
        删除长期未被回忆的低重要性记忆。
        不再用 Ebbinghaus 衰减曲线——LLM 提取时已标注 importance，系统只需清理明显无用的。
        条件：importance <= 2 且从未被回忆 且 超过 days 天。
        返回删除数量。
        """
        _log = logging.getLogger("糖糖.Memory")
        deleted = self.store.delete_stale_memories(
            days, actor="decay:stale_cleanup",
            reason=f"陈旧清理：importance≤2 且从未回忆且 >{days} 天",
        )
        if deleted:
            _log.info(f"🧹 陈旧记忆清理: {deleted} 条已删除 (importance≤2, 未回忆, >{days}天)")
        return deleted

    def cleanup_stale_buffers(self, max_idle_hours: int = 24):
        """清理长期未使用的短期记忆条目——防止 dict 无限增长。
        建议每天调用一次（由 handler 定时器触发）。"""
        cutoff = datetime.now()
        removed_groups = 0
        for gid in list(self.short_term.keys()):
            buf = self.short_term[gid]
            if not buf:
                del self.short_term[gid]
                removed_groups += 1
            elif buf[-1].get("time", ""):
                try:
                    last_time = datetime.strptime(buf[-1]["time"][:19], "%Y-%m-%d %H:%M:%S")
                    if (cutoff - last_time).total_seconds() > max_idle_hours * 3600:
                        del self.short_term[gid]
                        removed_groups += 1
                except (ValueError, IndexError):
                    pass
        # _last_extracted_id 不裁剪——每个条目仅 ~20 字节，即使 1000 用户也只需 20KB。
        # 之前取 [-50:] 的逻辑会静默丢弃 50+ 用户的提取进度，导致下次提取从消息 ID 0 开始。
        if removed_groups:
            _log = logging.getLogger("糖糖.Memory")
            _log.debug(f"🧹 缓冲清理: {removed_groups} 个过期群")

    def get_memory_stats(self, qq_id: str = "") -> dict:
        """获取记忆统计"""
        rows = self.store.query_memories(qq_id, limit=1000) if qq_id else self.store.query_all_memories()

        if not rows:
            return {"total": 0, "reinforced": 0}

        total = len(rows)
        reinforced = sum(1 for r in rows if (r.get("recall_count", 0) or 0) > 0)

        return {
            "total": total,
            "reinforced": reinforced,
        }

    def recall_formatted(self, qq_id: str, limit: int = 25,
                         source_group_id: str | None = None) -> str:
        """
        分层记忆召回：人物速写 + 核心记忆 + 话题匹配 + 最近互动。
        返回紧凑格式，注入LLM系统提示词。
        """
        person = self.get_or_create_person(qq_id)
        all_memories = self.recall(
            qq_id, limit=limit * 2, source_group_id=source_group_id,
        )  # 多取一些来挑
        intimacy = person.get("intimacy", 0)

        if not all_memories:
            return "（你们好像还不太熟呢...糖糖对你充满好奇！）"

        nickname = person.get("nickname", qq_id)
        total = person.get("total_chats", 0)
        first = person.get("first_met", "")[:10] if person.get("first_met") else "?"
        aliases = self.get_aliases(qq_id)
        grade = self._intimacy_grade_text(intimacy)

        # R2-1: 认知类型配额——确保 episodic 和 semantic 各占一定比例
        # 按 effective 排序，但保证 episodic 至少有 3 条、semantic 至少 5 条进入候选
        episodic_mems = [m for m in all_memories if getattr(m, 'cognitive', 'semantic') == 'episodic']
        semantic_mems = [m for m in all_memories if getattr(m, 'cognitive', 'semantic') == 'semantic']
        quota_episodic = sorted(episodic_mems[:6],
            key=lambda m: getattr(m, 'effective', 0), reverse=True)[:3]
        quota_semantic = sorted(semantic_mems[:12],
            key=lambda m: getattr(m, 'effective', 0), reverse=True)[:5]
        # 配额内的记忆合并去重作为候选（配额外的不会被完全丢弃，只是不占配额外的位置）
        quota_ids = {getattr(m, 'id', id(m)) for m in quota_episodic + quota_semantic}
        all_memories = [m for m in all_memories if getattr(m, 'id', id(m)) in quota_ids] + \
                       [m for m in all_memories if getattr(m, 'id', id(m)) not in quota_ids]
        # 去重保持顺序
        seen_ids = set()
        all_memories = [m for m in all_memories if not (getattr(m, 'id', id(m)) in seen_ids or seen_ids.add(getattr(m, 'id', id(m))))]

        lines = []
        # ---- 人物速写 ----
        alias_str = f"（{'、'.join(aliases[:3])}）" if aliases else ""
        lines.append(f"📋 {nickname}(ID:{self._qq_suffix(qq_id)}){alias_str} | 认识{first} | 聊{total}次 | {grade}")

        # ---- 合成画像（LLM提炼的人物速写） ----
        # people.notes 是全局单值，生成型分域上下文不能自动注入。
        profile_notes = self.active_notes(qq_id) if source_group_id is None else ""
        if profile_notes and len(profile_notes) > 20:
            # 2026-08-16 批 1b：此前全文无 caveat（PM 代发路径裸注入）——统一契约
            lines.append(f"  🖼️ {_protocols.profile_text(profile_notes)}")
            lines.append(f"  {_protocols.PROFILE_CAVEAT_LINE}")

        # ---- 身份/核心事实（profile类记忆 + 最近7天的新记忆优先） ----
        now = datetime.now()
        def _is_newer(m, days=7):
            """检查记忆是否在最近 N 天内创建——新记忆即使importance低也应被看到"""
            try:
                ts = m.timestamp if hasattr(m, 'timestamp') else ""
                if ts:
                    mem_dt = datetime.strptime(ts[:10], "%Y-%m-%d")
                    return (now - mem_dt).days <= days
            except (ValueError, IndexError):
                pass
            return False

        profile_mems = [m for m in all_memories if m.key in ("fact",) and (m.importance >= 6 or _is_newer(m))]
        if profile_mems:
            facts = " | ".join(m.value[:25] for m in profile_mems[:4])
            lines.append(f"  身份：{facts}")

        # ---- 喜好/讨厌 ----
        likes = [m.value for m in all_memories if m.key == "like"]
        hates = [m.value for m in all_memories if m.key == "hate"]
        if likes:
            lines.append(f"  喜欢：{'、'.join(likes[:5])}")
        if hates:
            lines.append(f"  讨厌：{'、'.join(hates[:3])}")

        # ---- 核心记忆（最重要 + 新记忆，去重） ----
        # 2026-08-16 批 1b：合成行（profile_synthesis/fact_synthesis）不进「重要」
        # 频道——它们是 LLM 合成物，不能与真实记忆同挂「重要」名头
        core = [m for m in all_memories
                if (m.importance >= 6 or _is_newer(m))
                and m.key not in ("like", "hate") + _protocols.SYNTHESIS_KEYS]
        shown = set()
        if core:
            core_lines = []
            for m in core[:8]:
                key = m.value[:20]
                if key not in shown:
                    shown.add(key)
                    core_lines.append(m.value[:35])
            if core_lines:
                lines.append(f"  重要：{'；'.join(core_lines[:8])}")


        # ---- 最近互动 ----
        recent = [m for m in all_memories if m.importance < 6 and m.value[:20] not in shown]
        if recent:
            lines.append(f"  最近：{'；'.join(m.value[:30] for m in recent[:5])}")

        # ---- R3-1: Episode 事件摘要 ----
        episodes = self.store.query_episodes(
            qq_id, limit=2, source_group_id=source_group_id,
        )
        if episodes:
            ep_lines = []
            for ep in episodes:
                ts = ep.get("time_start", "")[:10] if ep.get("time_start") else "?"
                ep_lines.append(f"{ts} {ep.get('title','事件')[:40]}：{ep.get('summary','')[:60]}")
            lines.append(f"  📖 往事：{' | '.join(ep_lines)}")

        return "\n".join(lines)

    def _format_self_memories(self, selected: list = None) -> str:
        """格式化糖糖自己的记忆——不是"用户档案"，是自我认知。

        ## 2026-10-25 第十轮：元数据曾吃掉这一块 68% 的预算

        每条原本写成：

            💭 [承诺过 | 2026-08-27 22:22 | 置信度0.70 | 来源 chat_log#236563 私聊] 承诺把「啥子！！！」补传过去

        前缀 50 个字符 ≈ 33t，而 promise 类自忆的正文只有 9~20 字（≈6~14t）。
        实测（拿她真答出来的 5 条真数据）：单条 40~51t、5 条共 **242t**，
        其中 **165t（68%）是元数据**；落到 150t 预算下只装得下 **2 条**。

        其中两个字段是纯噪音，与「她记不记得自己说过什么」无关：

        · `置信度0.70` —— 查库：`bot_qq` 名下 1673 条 active 自忆的 confidence
          **全是 0.70**（写入侧硬编码一个值）。它不区分任何东西，只是占地方。
        · `来源 chat_log#236563` —— 她**没有**能按 chat_log id 取回的工具
          （`search_chat_history` 是关键词检索，不吃 id）。这串数字她既用不上、
          也带不走；30 万条 chat_log 里她一次都没引用过它。而第 9 轮刚证明过：
          「登记一条取回路径」必须核**执行体的检索主体**，否则就是假路径。

        同一功能的另一块（`format_compact_memories`，记忆块）早在第 8 轮就是
        `  💭 {内容}` 的紧凑格式——**同一个能力两套实现，只压过其中一套**。
        现在两边前缀同为个位数 token，5 条自忆在 150t 下能全部落座。

        ## provenance 没有因此丢失

        「有原始消息支撑」由三处保证，每一处都比「逐条打印编号」更强：

        ① **写入侧** `remember_self` 的 `validate_self_memory_source` 强制：
           必须是同一对象、同一作用域、`is_bot_reply=1` 的 chat_log 行——
           bot 行只在确认送达后才写，所以这些行天然是「她真说过」。
        ② **块首全块声明**「有原始消息支持的自忆」——它是针对**整块**的断言，
           逐条再打一遍编号是同一件事的重复表述（也正是 68% 的来源）。
        ③ **读侧** 调用方的 `grounded_only=True` 只取可信档（trusted_only），
           不可信的一条都进不来。

        保留字段的理由：`类型`（说过/承诺过/动作已完成——契约明说三者不能互相
        改写，必须逐条可见）+ `日期`（第 9 轮刚修好「时间块从没进上下文」，
        时间感是宝贝；但秒级精度无用，`MM-DD` 足够）+ `群号`（仅群聊时，
        她要知道这句话是在哪个群说的）。
        """
        if not selected:
            return "📋 你自己说过的话——还没记下什么特别的"
        labels = {
            "said": "说过",
            "promise": "承诺过",
            "action_completed": "动作已完成",
        }
        lines = ["📋 有原始消息支持的自忆（类型不同，不能互相改写）："]
        for m in selected[:5]:
            if hasattr(m, "value"):
                v = m.value
                key = getattr(m, "key", "said")
                timestamp = getattr(m, "timestamp", "") or ""
                group_id = getattr(m, "source_group_id", "") or ""
            elif isinstance(m, dict):
                v = str(m.get("value", ""))
                key = m.get("key", "said")
                timestamp = m.get("timestamp", "") or ""
                group_id = m.get("source_group_id", "") or ""
            else:
                v = str(m)
                key, timestamp, group_id = "said", "", ""
            if len(v) < 4:
                continue
            ts = str(timestamp)
            day = ts[5:10] if len(ts) >= 10 else "日期未知"
            where = f" 群{group_id}" if group_id else ""
            lines.append(f"  💭 {labels.get(key, key)} {day}{where} {v[:80]}")
        return "\n".join(lines) if len(lines) > 1 else "📋 你自己说过的话——还没记下什么特别的"

    def format_compact_memories(self, qq_id: str, selected: list = None,
                                 max_facts: int = 6,
                                 include_profile: bool = True) -> str:
        """紧凑格式：人物速写(≤50字) + 可检索事实(最多max_facts条，每条80字)。
        画像提供性格氛围，💭事实提供对话锚点——LLM从💭中检索，
        从🖼️中感受气质。两个频道不互相干扰。

        2026-10-24 不确定性三档：**按记忆自己的 trust_level 决定标记**，不按
        「来自哪一路召回」——判定依据是数据的真值等级，不是调用方的意图。
        这样即使将来有别的路径把模糊记忆塞进来，也逃不掉 `~`（双向闸门：
        可信的不许带、模糊的必须带）。模糊行出现时块尾补一条行为契约，
        否则 LLM 会把 `~` 当成装饰（方案 3.4）。

        2026-10-25 纠正优先档（主人决策「corrected 优先」）：可信档内部再分层——
        同一件事可能既有 ta 亲口纠正的结论（corrected），又有提取器当时以为对的
        说法（verified）。原来两者注入文本完全一样（都是 💭），冲突时她只能猜。
        现在 corrected 行挂 ✏️ 并**稳定排序到最前**，块尾补一条冲突仲裁契约。
        """
        # 稳定排序：同优先级内保持调用方（BGE/Reranker）给的相关性顺序——
        # 那一层才是「哪条与这句话有关」的判断，不能被信任等级覆盖。
        # 只调整呈现顺序、**不丢弃任何一条**：「哪条是真的」仍由 LLM 判
        # （store.TRUST_INJECT_PRIORITY 的注释写了为什么不能拿它做过滤）。
        if selected:
            selected = sorted(
                selected,
                key=lambda m: TRUST_INJECT_PRIORITY.get(
                    str(getattr(m, "trust_level", "") or ""), 0),
                reverse=True,
            )
            # 观测：这一轮记忆块里到底装了哪几条、什么信任等级。
            # 为什么需要：`turn_traces` 的 `memory_marks` 只能看到**最终 prompt**
            # 里还剩几个标记，分不清「没被选中」和「被背景预算裁掉了」——
            # 这两种情况的修法完全不同（前者调精排、后者调预算）。
            logger.info("🧠 记忆块装入: %s", " | ".join(
                f"#{getattr(m, 'id', '?')}:{getattr(m, 'trust_level', '?')}"
                for m in selected))
        person = self.get_or_create_person(qq_id)
        nickname = person.get("nickname", qq_id)
        total = person.get("total_chats", 0)
        grade = self._intimacy_grade_text(person.get("intimacy", 0))

        lines = [f"📋 {nickname}(ID:{self._qq_suffix(qq_id)}) | 聊{total}次 | {grade}"]

        # 画像：统一契约截断（2026-08-16 批 1b——此前局部截断逻辑与其它出口不一致）
        profile_notes = self.active_notes(qq_id) if include_profile else ""
        has_profile = profile_notes and len(profile_notes) > 20
        has_memories = bool(selected)
        if has_profile:
            lines.append(f"  🖼️ {_protocols.profile_text(profile_notes, max_len=50)}{_protocols.PROFILE_CAVEAT_SHORT}")

        # 事实锚点：排除与画像高度重叠的记忆（避免同一段话出现两次）
        uncertain_lines = 0
        corrected_lines = 0
        if selected:
            profile_prefix = profile_notes[:40] if has_profile else ""
            facts = []
            for m in selected[:max_facts]:
                v = m.value if hasattr(m, 'value') else str(m)
                if len(v) < 6:
                    continue
                # 跳过与画像开头高度重叠的记忆（是画像自身的副本）
                if profile_prefix and len(profile_prefix) > 20:
                    overlap = v[:min(len(v), 30)]
                    if overlap in profile_prefix or profile_prefix[:30] in overlap:
                        continue
                # 2026-08-16 批 1b：合成行分轨——不能挂 💭（真实记录）名头
                if m.key in _protocols.SYNTHESIS_KEYS:
                    facts.append(f"  🖼️ {v[:60]}")
                else:
                    # 2026-10-24：非可信档挂 ~（模糊），可信档保持 💭
                    # 2026-10-25：可信档内 corrected 再挂 ✏️（纠正优先档）
                    if self.is_uncertain(m):
                        facts.append(f"  {_protocols.UNCERTAIN_MARK} {v[:80]}")
                        uncertain_lines += 1
                    elif self.is_corrected(m):
                        facts.append(f"  {_protocols.CORRECTED_MARK} {v[:80]}")
                        corrected_lines += 1
                    else:
                        facts.append(f"  💭 {v[:80]}")
            if facts:
                lines.extend(facts[:max_facts])

        if not has_profile and not has_memories:
            lines.append("  ⚠️ 你对ta还不太了解——ta还没告诉过你关于自己的事。被问到关于ta的问题时，好奇追问，不编造。")
        # 块尾行为契约：只在真的出现了对应行时追加（绝大多数轮次不占 token）
        if corrected_lines:
            lines.append(f"  {_protocols.CORRECTED_MEMORY_CONTRACT}")
        if uncertain_lines:
            lines.append(f"  {_protocols.UNCERTAIN_MEMORY_CONTRACT}")
        return "\n".join(lines)

    @staticmethod
    def is_uncertain(entry) -> bool:
        """这条记忆是不是「没有原话佐证」的模糊档。

        单一判定点：`trust_level` 是否落在唯一放行集合（store.TRUSTED_TRUST_LEVELS）
        之外。缺失/NULL 一律算模糊（口径字典 §二：NULL 等同 legacy）——fail-closed
        的方向是「宁可说得含糊」，不是「宁可说得肯定」。"""
        level = str(getattr(entry, "trust_level", "") or "").strip()
        return level not in TRUSTED_TRUST_LEVELS

    @staticmethod
    def is_corrected(entry) -> bool:
        """这条记忆是不是 ta 亲口纠正过的结论（corrected 档）。

        2026-10-25 主人决策「corrected 优先」的判定入口。只认
        `trust_level == 'corrected'`——**不用 `origin == 'corrected'`**：
        `origin` 记的是「这条怎么来的」，`trust_level` 记的是「它现在有多可信」；
        纠正结论的信任等级可能被后续维护改写，注入层的唯一判定源必须是
        trust_level（与 `is_uncertain` 同一口径，两条判定不能各查各的字段）。

        为什么需要一个独立方法而不是并进 `is_uncertain`：两者是**正交**的。
        `is_uncertain` 决定「要不要挂 🔵~ 表示不确定」，这里决定「要不要挂 ✏️
        表示是纠正结论」——corrected 当然属于可信档（不挂 ~），但它需要额外
        的可见性（冲突时以它为准）。"""
        return str(getattr(entry, "trust_level", "") or "").strip() == "corrected"

    # ---- 短期记忆（群聊上下文） ----

    def add_to_buffer(self, group_id: str, qq_id: str, nickname: str, message: str) -> int:
        """添加消息到短期缓冲，返回该条消息的 `seq` 锚点。

        **为什么返回 seq（2026-10-25）**：调用方在**构造 LLM 上下文之前**就把当前
        消息放进了缓冲，而 `get_recent_context_messages()` 取最近 N 条时必然把它
        一起取出来——随后 `_call_deepseek` 又把它作为本轮 `user_message` 追加一次，
        于是同一条用户消息在 `api_messages` 里出现两遍（主人实测：会客厅里糖糖
        说「你说了两遍」）。修复不由内容比对完成（`user_message` 带背景块前缀、
        图片占位符会被中性化，两者并不总相等），而是由这个 `seq` 精确锚定：
        调用方把返回值传给 `get_recent_context_messages(exclude_seq=...)`。
        """
        if group_id not in self.short_term:
            self.short_term[group_id] = deque(maxlen=self.short_term_size)

        # 自增序号（用于排序）
        seq = getattr(self, '_msg_seq', 0) + 1
        self._msg_seq = seq
        entry = {
            "qq_id": qq_id,
            "nickname": nickname,
            "message": message,
            "time": datetime.now().strftime("%H:%M:%S"),
            "seq": seq,
        }
        self.short_term[group_id].append(entry)
        return seq

    def warm_short_term_from_chat_log(self, bot_qq: str, bot_name: str = "",
                                      active_hours: int = 24,
                                      max_scopes: int = 8) -> dict:
        """启动时从 `chat_log` 重建最近对话窗口（2026-10-25 第 4 轮）。

        ## 为什么

        `short_term` 是纯内存 deque——bot 一重启，所有对话窗口归零，她下一句话
        就「不记得刚才聊到哪」。而 `chat_log` 里这些消息全在。

        取证：`turn_traces#80` 的 `prompt_digest.history_count=0`——那是重启后
        第一轮，她只收到当前这一句话、没有任何上下文；下一轮回升到 2 条，
        证实是内存态被清、不是「会客厅不拼历史」。

        ## 为什么是「重建」而不是「持久化 buffer」

        两者都能让重启后还记得，但**持久化会造出第二份真相**：buffer 存磁盘 +
        chat_log 各一份，两份不一致时没人知道该信谁。`chat_log` 本来就是唯一
        事实源，buffer 只是它的内存投影——投影可以重算，不必存档。

        ## 边界

        - 已有内存数据的会话**不覆盖**（回灌只补空窗，不动活数据）；
        - 只回灌最近 `active_hours` 内活跃的 `max_scopes` 个会话——全量回灌
          会让启动变慢，而几小时前的群聊窗口本来也没有上下文价值；
        - 排除 quarantine 行与框架合成行（14k+ 条「【同时有 N 个人找你…】」
          是提示词不是聊天，回灌进来她会把系统提示当成自己说过的话）。

        返回 `{"scopes": n, "entries": m}` 供启动日志观测。
        """
        stats = {"scopes": 0, "entries": 0}
        store = getattr(self, "store", None)
        if not bot_qq or store is None:
            return stats
        try:
            scopes = store.list_recent_chat_scopes(
                hours=active_hours, limit=max_scopes)
        except Exception:
            logger.debug("对话窗口回灌：取活跃会话失败", exc_info=True)
            return stats

        for scope in (scopes or []):
            kind = str(scope.get("kind") or "")
            sid = str(scope.get("id") or "")
            if not sid:
                continue
            key = f"_private_{sid}" if kind == "private" else sid
            if self.short_term.get(key):
                continue  # 已有活数据，绝不覆盖
            try:
                rows = store.recent_chat_rows_for_scope(
                    kind=kind, scope_id=sid, limit=self.short_term_size)
            except Exception:
                logger.debug("对话窗口回灌：读会话失败 %s", key, exc_info=True)
                continue
            if not rows:
                continue
            # 昵称一次批量取——单人查库 × 30 条会在启动路径上放大成 240 次查询
            speakers = [str(r.get("qq_id") or "") for r in rows if not r.get("is_bot_reply")]
            try:
                nick_map = store.get_nicknames(sorted(set(speakers)))
            except Exception:
                nick_map = {}
            buf = deque(maxlen=self.short_term_size)
            for row in rows:
                is_bot = bool(row.get("is_bot_reply"))
                uid = str(row.get("qq_id") or "")
                self._msg_seq = getattr(self, "_msg_seq", 0) + 1
                buf.append({
                    "qq_id": str(bot_qq) if is_bot else uid,
                    "nickname": (bot_name or "糖糖") if is_bot else (nick_map.get(uid) or uid),
                    "message": str(row.get("message") or ""),
                    # chat_log 存的 "YYYY-MM-DD HH:MM:SS"；buffer 里只需要时分秒
                    "time": str(row.get("timestamp") or "")[11:19],
                    "seq": self._msg_seq,
                })
            self.short_term[key] = buf
            stats["scopes"] += 1
            stats["entries"] += len(buf)
        return stats

    def enrich_image_message_in_buffer(self, group_id: str, qq_id: str, enriched: str):
        """识图写回（2026-08-16 结构性修复）：把识图描述写进 buffer 里最近一条
        图片占位符消息——历史里的「发了张图」从此带着内容（此前描述只存在于
        当轮背景块，下一轮就丢，LLM 从历史只能看到占位符）。"""
        buf = self.short_term.get(group_id)
        if not buf:
            return
        for entry in reversed(buf):
            if str(entry.get("qq_id", "")) == str(qq_id) and (
                    entry.get("message") == "（发了张图片）"
                    or "[图片" in str(entry.get("message", ""))):
                entry["message"] = enriched
                return

    def get_recent_context(self, group_id: str, limit: int = 20, focus_user: str = "",
                           priority_users: list = None) -> str:
        """获取最近的群聊上下文。
        focus_user + priority_users 的消息优先保留，其他人只保留最近 5 条做氛围参考。
        总长度控制在 ~2000 字符以内。

        ⚠️ 已弃用：新代码应使用 get_recent_context_messages()，返回结构化消息列表
        直接喂给 LLM API，让模型区分自己说过的话和别人说的话。
        """
        # 2026-08-16 教训 #19 清理：无记录返回空串——哨兵文案是魔术字符串，
        # 调用方用 truthiness 判断即可
        if group_id not in self.short_term:
            return ""

        all_msgs = list(self.short_term[group_id])
        if not all_msgs:
            return ""

        if focus_user or (priority_users and len(priority_users) > 0):
            # 构建优先用户集合
            prio_set = set(priority_users or [])
            if focus_user:
                prio_set.add(focus_user)

            # 分离优先用户和普通用户
            prio_msgs = [m for m in all_msgs if m.get('qq_id') in prio_set]
            other_msgs = [m for m in all_msgs if m.get('qq_id') not in prio_set]

            # 优先用户最近 15 条 + 其他人最近 10 条（保证昨天的对话也能看到）
            selected = prio_msgs[-15:] + other_msgs[-10:]
            selected.sort(key=lambda m: m.get('seq', 0))
        else:
            selected = all_msgs[-limit:]

        # 生成文本，控制总长度
        lines = []
        total_chars = 0
        max_chars = 3000
        for m in reversed(selected):
            line = f"[{m['time']}] {m['nickname']}: {m['message']}"
            if total_chars + len(line) > max_chars:
                break
            lines.insert(0, line)
            total_chars += len(line)

        return "\n".join(lines)

    def get_recent_context_messages(self, group_id: str, bot_qq: str, limit: int = 30,
                                    focus_user: str = "", priority_users: list = None,
                                    is_private: bool = False,
                                    exclude_seq: int = 0) -> list[dict]:
        """获取最近上下文，返回结构化消息列表——适配 LLM Chat Completions API。

        与 get_recent_context() 使用相同的消息选择逻辑，但输出格式不同：
        - 糖糖自己的消息 → {"role": "assistant", "content": "..."}
        - 群聊其他人 → {"role": "user", "content": "昵称: 消息内容"}
        - 私聊对方 → {"role": "user", "content": "消息内容"}

        这样 LLM 能通过 role 字段区分自己说过的话和别人说的话，
        而非把所有内容当作文本去"阅读理解"。

        `exclude_seq`：排除某条消息（`add_to_buffer` 的返回值）。**主对话入口必须传
        当前消息的 seq**——否则当前消息既出现在这里（历史数组），又被 `_call_deepseek`
        作为本轮 user 追加，同一条话会在请求体里出现两次。不传（0）时行为与历史一致。
        """
        if group_id not in self.short_term:
            return []

        all_msgs = list(self.short_term[group_id])
        if not all_msgs:
            return []

        # 与 get_recent_context 相同选择逻辑
        if focus_user or (priority_users and len(priority_users) > 0):
            prio_set = set(priority_users or [])
            if focus_user:
                prio_set.add(focus_user)
            prio_msgs = [m for m in all_msgs if m.get('qq_id') in prio_set]
            other_msgs = [m for m in all_msgs if m.get('qq_id') not in prio_set]
            selected = prio_msgs[-15:] + other_msgs[-10:]
            selected.sort(key=lambda m: m.get('seq', 0))
        else:
            selected = all_msgs[-limit:]

        # 排除「当前这条」——它由调用方的 user_message 单独提供，不能重复进历史。
        # 只按 seq 精确排除；不做内容比对（理由见 docstring）。
        if exclude_seq:
            selected = [
                m for m in selected
                if int(m.get('seq', 0) or 0) != int(exclude_seq)
            ]

        messages = []
        for m in selected:
            qq_id = str(m.get('qq_id', ''))
            nickname = m.get('nickname', qq_id)
            content = m.get('message', '')
            # 2026-08-15：历史里的引用占位符中性化——引用原文只注入当前消息
            # （quote_prefix）。历史里的占位符会让 LLM 脑补引用内容（现场：
            # 引用内容已正确解析，LLM 却按历史占位符编出「歌单」）。
            content = content.replace("[回复了上面的消息]", "（引用了一条消息）")
            # 2026-08-16：图片占位符中性化——LLM 原样回显过「[图片:[动画表情]]」
            # （管理员表情包占位符被逐字引用进回复）；占位符是客户端渲染物
            content = re.sub(r"\[图片\s*[:：]?\s*[^\]]*\]+", "（发了张图片）", content)

            if qq_id == str(bot_qq):
                messages.append({"role": "assistant", "content": content})
            else:
                if is_private:
                    messages.append({"role": "user", "content": content})
                else:
                    messages.append({"role": "user", "content": f"{nickname}: {content}"})

        return messages

    def get_user_recent_messages(self, qq_id: str, limit: int = 20,
                                 group_id: str | None = None) -> list[str]:
        """从数据库获取某个用户最近的发言（委托给 Store）"""
        return self.store.get_user_recent_messages(qq_id, limit, group_id=group_id)

    # ---- 聊天记录 ----

    def log_chat(self, qq_id: str, message: str, group_id: str = "", is_bot: bool = False,
                 timestamp: str = "", raw_message: str = "", segments: str = "",
                 event_key: str = ""):
        """记录聊天（委托给 Store）——返回 chat_log 行 id（2026-08-16 Codex I5：
        识图写回按精确 id CAS，防止并发下写错图片）。

        2026-08-28 任务A：新增 timestamp/raw_message/segments/event_key 透传——
        批处理逐条落真实事件用；event_key 重复投递时返回 None（幂等）。"""
        return self.store.insert_chat(qq_id, message, group_id, is_bot,
                                      timestamp=timestamp, raw_message=raw_message,
                                      segments=segments, event_key=event_key)

    # ---- 去重辅助 ----

    def _deduped_remember(self, qq_id: str, key: str, value: str, importance: int = 3,
                          embed_engine=None, cognitive: str = "semantic",
                          confidence: float = 0.7, origin: str = "extracted",
                          evidence_ids: str = "", source_group_id: str = "",
                          evidence_quote: str = "", claim_type: str = "stated"):
        """带去重的记忆存储——精确前缀 + BGE语义相似度双重查重。
        BGE 相似度 > 0.85 → 更新旧记忆（替换值、取更高importance）而非新增。"""
        # Phase 1: 精确前缀去重（快、便宜）
        dedup_key = (qq_id, key, value[:40])
        if dedup_key in self._dedup_cache:
            # 同一事实再次出现时，正文可以去重，新的原始证据不能丢。
            # P0-D2：新证据必须被 evidence_quote 原文支持，否则拒绝 union
            # （LLM 幻觉证据不得污染已 verified 记忆）
            if evidence_ids:
                cached = self.store.find_memory_by_value(qq_id, value)
                if cached is not None:
                    self.store.update_memory_evidence(
                        cached["id"], evidence_ids, source_group_id,
                        evidence_quote=evidence_quote,
                        actor="dedup:cache",
                        review_reason="同批次重复出现，补录新证据",
                    )
            return
        self._dedup_cache.add(dedup_key)
        if len(self._dedup_cache) > self._dedup_max:
            half = self._dedup_max // 2
            self._dedup_cache = set(list(self._dedup_cache)[-half:])

        # Phase 2a: 精确查重——同用户同 value 就是重复，跨 key 全库匹配
        # （2026-08-14：之前只走 top-100 相似度且只比较同 key——大用户盲区 +
        # like/said 撞车都从这里漏进去。精确匹配不受 limit/排序影响）
        exact = self.store.find_memory_by_value(qq_id, value)
        if exact is not None:
            new_imp = max(importance, exact["importance"])
            self.store.set_memory_importance(
                exact["id"], new_imp, actor="dedup:exact",
                reason="精确去重：同一事实再次出现，取更高 importance",
            )
            if evidence_ids:
                # P0-D2：新证据必须被 quote 支持（见 update_memory_evidence）
                self.store.update_memory_evidence(
                    exact["id"], evidence_ids, source_group_id,
                    evidence_quote=evidence_quote,
                    actor="dedup:exact",
                    review_reason="精确去重：同一事实再次出现，补录新证据")
            if embed_engine and embed_engine.ready:
                new_vec = embed_engine.encode(value)
                if new_vec is not None:
                    self.store.set_embedding(exact["id"], new_vec)
            logger.debug(f"🧠 记忆精确去重({exact.get('key', key)}): {value[:50]}...")
            return

        # Phase 2b: 语义查重——BGE优先，字符相似度兜底（只比较同 key，
        # 避免语义相近但分类不同的记忆被误合并）
        # 🔧 修复：之前整个 Phase 2 被 `if embed_engine.ready` 包住，
        # BGE未就绪时连DB都不查，导致同批次重复入库。
        existing = self.store.query_memories(qq_id, limit=100)
        if existing:
            new_vec = None
            if embed_engine and embed_engine.ready:
                new_vec = embed_engine.encode(value)
            for old in existing:
                if old.get("key") != key:
                    continue  # 只比较同类型记忆
                old_val = old.get("value", "")
                if new_vec is not None:
                    old_vec = self.store.get_embedding(old["id"])
                    if old_vec is not None:
                        sim = embed_engine.similarity(new_vec, old_vec)
                    else:
                        sim = self._char_similarity(value, old_val)
                else:
                    sim = self._char_similarity(value, old_val)
                if sim > 0.85:
                    new_imp = max(importance, old["importance"])
                    # ⚠️ 这是旧值**永久消失**的写点：update_memory_value 用新表述
                    # 就地覆盖旧的。留痕时把相似度写进 reason，事后才能判断这次
                    # 合并是不是误合并（0.86 和 0.99 的可信度完全不同）。
                    self.store.update_memory_value(
                        old["id"], value, actor="dedup:semantic",
                        reason=f"语义去重合并（相似度 {sim:.2f}）：旧表述被新表述覆盖",
                    )
                    self.store.set_memory_importance(
                        old["id"], new_imp, actor="dedup:semantic",
                        reason="语义去重合并：取更高 importance",
                    )
                    if evidence_ids:
                        # P0-D2：新证据必须被 quote 支持（见 update_memory_evidence）
                        self.store.update_memory_evidence(
                            old["id"], evidence_ids, source_group_id,
                            evidence_quote=evidence_quote,
                            actor="dedup:semantic",
                            review_reason="语义去重合并：补录新证据")
                    # 更新 embedding——合并后的文本应与向量一致
                    if new_vec is not None:
                        self.store.set_embedding(old["id"], new_vec)
                    logger.debug(f"🧠 记忆去重更新({key}): {value[:50]}...")
                    return

        # Phase 3: 新记忆 — 插入 + 存 embedding
        mem_id = self.remember(qq_id, key, value, importance,
                               cognitive=cognitive, confidence=confidence, origin=origin,
                               evidence_ids=evidence_ids, source_group_id=source_group_id,
                               evidence_quote=evidence_quote, claim_type=claim_type)
        if embed_engine and embed_engine.ready and mem_id:
            vec = embed_engine.encode(value)
            if vec is not None:
                self.store.set_embedding(mem_id, vec)

    def ensure_memory_embeddings(self, embed_engine, batch_limit: int = 100) -> int:
        """批量填充所有缺失的 memory embedding。后台任务调用。
        返回填充的数量。"""
        if not embed_engine or not embed_engine.ready:
            return 0
        ids = self.store.get_memories_without_embeddings(
            limit=max(1, int(batch_limit)),
        )
        if not ids:
            return 0
        count = 0
        failed = 0
        for mem_id in ids:
            try:
                mem = self.store.get_memory_by_id(mem_id)
                if mem and mem.get("value"):
                    vec = embed_engine.encode(mem["value"])
                    if vec is not None:
                        self.store.set_embedding(mem_id, vec)
                        count += 1
                        continue
                self.store.mark_embedding_failure(mem_id, "empty_embedding")
                failed += 1
            except Exception as exc:
                self.store.mark_embedding_failure(mem_id, type(exc).__name__)
                failed += 1
        if count > 0:
            logger.info(f"🧠 批量填充 {count} 条记忆 embedding（共{len(ids)}条缺失）")
        if failed > 0:
            logger.warning(
                "🧠 embedding 填充失败 %s 条，已退避且不会阻塞后续 backlog",
                failed,
            )
        return count

    def remember_self(self, bot_qq: str, value: str, key: str = "promise",
                      importance: int = 8, embed_engine=None, target_qq: str = "",
                      source_group_id: str = "", group_id: str = "",
                      evidence_ids: str = "", confirmed_action_id: str = ""):
        """记住已发送的自忆；对象、原始 chat_log 和语义类型缺一不可。"""
        # group_id 是历史调用方使用的参数名；统一落到 source_group_id，避免
        # 自我承诺因兼容性问题重新变成无作用域的全局记忆。
        source_group_id = source_group_id or group_id
        if key not in ("said", "promise", "action_completed"):
            logger.warning(f"🧠 拒绝未知类型的自忆: {key}")
            return 0
        if not str(target_qq or "").strip() or not str(evidence_ids or "").strip():
            logger.warning("🧠 拒绝未绑定对象或原始消息的自忆")
            return 0
        if not self.store.validate_self_memory_source(
                str(target_qq), str(source_group_id), str(evidence_ids)):
            logger.warning(
                "🧠 拒绝无效自忆证据: target=%s group=%s evidence=%s",
                str(target_qq), str(source_group_id), str(evidence_ids),
            )
            return 0
        validate_action = getattr(
            self.store, "validate_self_memory_action_anchor", None,
        )
        # 2026-10-12 主人拍板（方案 B）：文字回复的**回执锚点**。
        #
        # 原来的写法要求 action_completed 必须有冻结动作回执，否则整条拒收。
        # 但动作回执只有语音路径产出（_build_voice_action_envelope 只服务语音；
        # 查库全库 5 条 conversation_reply 事实的 self_memory_eligible 全是 0），
        # 于是文字回复一律被挡在写入之前——取证：action_completed 自忆最后一次
        # 写入停在 2026-08-30，此后一条都没有，这就是 fulfilled 恒为 0 的真凶。
        #
        # 闸门要的是**传输层确认**（"她确实发出了这条回复"）。而上面那段
        # validate_self_memory_source 对**每一条**自忆都校验过：证据必须是同一对象、
        # 同一作用域、is_bot_reply=1 的 chat_log 行——而 chat_log 的 bot 行只在
        # **确认送达**之后才写。所以对没有动作回执的文字回复，传输层证据已经给足。
        #
        # 收窄后的口径（fail-closed，但不再有罪推定）：
        #   · 提供了动作回执 → 必须**真实有效**，无效一律拒（不许降级蒙混）
        #   · 没有动作回执 → 走上面已经通过的证据校验（文字回复路径）
        action_ref = str(confirmed_action_id or "").strip()
        if key == "action_completed" and action_ref:
            if (not callable(validate_action)
                    or not validate_action(
                        action_ref, str(target_qq), str(source_group_id),
                    )):
                logger.warning(
                    "🧠 拒绝动作回执无效的已完成自忆: action_id=%s",
                    action_ref[:80],
                )
                return 0
        mem_id = self.remember(bot_qq, key, value, importance, origin="self",
                               target_qq=target_qq, source_group_id=source_group_id,
                               evidence_ids=evidence_ids)
        if embed_engine and embed_engine.ready and mem_id:
            vec = embed_engine.encode(value)
            if vec is not None:
                self.store.set_embedding(mem_id, vec)
        return mem_id

    def fulfill_self_promise(self, promise_id: int, completion_id: int) -> bool:
        """结算一条有证据链的自我承诺，保留完成动作指针。"""
        return bool(self.store.fulfill_self_promise(promise_id, completion_id))

    def validate_self_memory_action_anchor(self, action_id: str,
                                            target_qq: str,
                                            source_group_id: str) -> bool:
        """只读检查 action_completed 是否有 confirmed action 事实锚点。"""
        return bool(self.store.validate_self_memory_action_anchor(
            action_id, target_qq, source_group_id,
        ))

    # ---- 自动学习 ----
    # ⚠️ 已退役 (2026-07-29)：auto_learn 方法体保留供参考，但不再被任何代码路径调用。
    # 替代方案：handler.py 的 _extract_semantic_memories（LLM 语义提取器，每 12-15 条触发）。
    # 如需重新启用关键词提取，先确认 handler.py:2321/3162 的注释已移除。


    def get_unprocessed_messages(self, qq_id: str, limit: int = 20,
                                 include_bot_replies: bool = False) -> list[dict]:
        """获取某个人尚未被 LLM 提取过的新消息（委托给 Store）。
        include_bot_replies（2026-08-16 批 4）：带糖糖的私聊回复做提取语境。"""
        # SQLite 是唯一游标真值；进程缓存只供健康监控展示，不能决定任务范围。
        last_id = self.store.get_extraction_cursor(qq_id, "forward")
        self._last_extracted_id[qq_id] = last_id
        return self.store.get_unprocessed_messages(qq_id, last_id, limit,
                                                   include_bot_replies=include_bot_replies)

    def mark_extracted(self, qq_id: str, max_msg_id: int):
        """兼容旧调用：把前向进度迁移进 SQLite；新提取必须走事务任务。"""
        self.store.migrate_extraction_cursors(forward={qq_id: max_msg_id})
        self._last_extracted_id[qq_id] = self.store.get_extraction_cursor(
            qq_id, "forward"
        )

    def restore_extraction_progress(self, progress: dict[str, int],
                                    backfill: dict[str, int] | None = None):
        """幂等导入旧 JSON 进度，随后只从 SQLite 恢复运行时镜像。"""
        self.store.migrate_extraction_cursors(progress, backfill)
        self._last_extracted_id = self.store.get_extraction_cursors("forward")
        self._last_backfill_to_id = self.store.get_extraction_cursors("backfill")

    # ---- 统计 ----

    def get_stats(self, qq_id: str) -> dict:
        """获取与某人的互动统计"""
        person = self.get_or_create_person(qq_id)
        # 统计用途不算「想起来」——否则查一次统计就把 recall_count / importance 污染了
        memories = self.recall(qq_id, reinforce_after=False)
        return {
            "nickname": person.get("nickname", ""),
            "intimacy": person.get("intimacy", 0),
            "relationship": person.get("relationship", "stranger"),
            "total_chats": person.get("total_chats", 0),
            "memory_count": len(memories),
            "intimacy_grade": self._intimacy_grade_text(person.get("intimacy", 0))
        }

    def _intimacy_grade_text(self, intimacy: int) -> str:
        if intimacy < 10: return "🌱 路人"
        if intimacy < 25: return "🌿 眼熟了"
        if intimacy < 40: return "🌸 朋友"
        if intimacy < 60: return "💫 知己"
        if intimacy < 80: return "💕 心动"
        if intimacy < 100: return "❤️‍🔥 沦陷"
        return "👑 灵魂绑定"

    # ════════════════════════════════════════════════════════════
    # 🧠 EverOS 风格：LLM 语义记忆提取 + 画像合成 + 反思整合
    # ════════════════════════════════════════════════════════════

    async def extract_semantic_memories(
        self, messages: list[dict], nickname: str, qq_id: str,
        llm_call, existing_summary: str = "",
        alias_candidates: list[str] = None
    ) -> list[dict]:
        """
        用 LLM 从一批消息中语义提取结构化记忆。
        比 auto_learn 的关键词正则覆盖率高得多——能捕捉隐式偏好、性格特征、
        人际关系、生活经历等关键词匹配不到的信息。

        Args:
            messages: [{"message": ..., "timestamp": ...}, ...]
            nickname: 群友昵称
            qq_id: QQ号
            llm_call: async callable(system_prompt, user_message) -> str
            existing_summary: 已有的用户画像摘要（用于增量更新）

        Returns:
            (items, stats) — items 是记忆列表，stats 是提取统计 dict 或 None（LLM失败时）
        """
        self._last_extract_stats = None
        # 2026-08-16 批 4：#id 供证据追溯；带糖糖相邻回复——单句玩笑/引用/测试
        # 语句零语境入库的事故根源就是批次只有用户自己的话
        prompt_messages = messages[-20:]
        chat_text = "\n".join(
            f"[{m.get('timestamp', '?')}] #{m.get('id', '?')} "
            f"{'糖糖' if m.get('is_bot_reply') else nickname}: {m['message'][:150]}"
            for m in prompt_messages
        )
        existing_block = (
            f"\n\n## 你已知的关于{nickname}的信息：\n{existing_summary}\n"
            f"（如发现与已有信息**矛盾**的新事实——比如之前说喜欢猫，现在说讨厌猫——"
            f"请以最新的明确陈述为准，用高重要性提取新事实来覆盖旧信息）"
            if existing_summary else ""
        )

        # ⚠ 2026-10-15（提取积压调查）：这两行原本没有 f 前缀，`{nickname}`
        # 是**字面量**——LLM 收到的 system prompt 里写着「你的群友{nickname}」，
        # 提取器被要求提取一个叫「{nickname}」的人的事实。全仓没有一处
        # `.format()` 修补它，所以从上线起就是这样发的。
        # 这是可证的缺陷；「它是不是 82% 空数组的成因」属待验证假设（另记）。
        system_prompt = (
            f"你是小糖糖，一只猫娘。以下聊天记录是你的群友{nickname}和你们的对话。\n"
            f"你只提取{nickname}关于自己的陈述——不提取关于你的陈述、不提取角色扮演。\n\n"
            "规则：\n"
            "- 只提取用户明确陈述的关于自己的事实\n"
            "- 每条信息标注认知类型(cognitive)、重要性(importance 1-10)、置信度(confidence 0.0-1.0)\n"
            "- 每条信息必须给 evidence_ids：支持该事实的聊天记录 #id 列表；只能填用户明确陈述的消息，不能填糖糖回复。\n"
            "- 每条信息给 evidence_quote：支持该事实的原话摘录（从聊天记录里照抄，一字不改）——系统会校验它确实出现在对应消息里，不匹配的记忆会被拒绝入库。\n"
            "- 每条信息给 claim_type：'stated'=用户明确陈述的（默认）；'inferred'=你从上下文推断的。推断结果永远不会进入可信记忆层。\n"
            "- 如果已有信息与新信息矛盾，以最新明确陈述为准\n"
            "- 如果消息中没有有意义的新信息，返回空数组 []\n\n"
            "🚫 以下内容**绝对不要提取**（返回空数组即可）：\n"
            "- 问句——「怎么突然叫我老公了？」「你觉得我怎么样？」是提问，不是自我陈述\n"
            "- 反问——「我能怎么办？」是反问，不是陈述\n"
            "- 对别人的提问——「你是AI吗？」「你会唱歌吗？」是问别人的，不是用户自述\n"
            "- 自贬/情绪发泄——「我是废物」「我没人关心」「我什么都不行」是情绪症状，不是事实\n"
            "- 指令/命令——「我叫你搜一下」「你给我查」「你去帮我找」是命令，不是自述\n"
            "- 角色扮演——「我是勇敢的勇者」「我就是神」是游戏中扮演的角色，不是真实身份\n"
            "- 玩笑/玩梗——「我是秦始皇」「我住在火星」这类明显夸张的玩笑不是事实\n"
            "- 测试语句——开发者在测试记忆功能的输入（「测试一下」「随便说句话试试」）不是自述\n"
            "- 引用转述——消息里转述/引用别人的话（「他说」「XX说」、引号里的他人言论）不是用户自述\n"
            "- 关于别人的陈述——**主语是第三方**的句子（「他今年高一」「她不喜欢抽烟」\n"
            "  「XX 是我同事」）说的是**那个人**，不是当前用户自述。\n"
            "  尤其当用户让你「修正/记一下 XX 的事」时，那句事实属于 XX —— 记到当前用户名下\n"
            "  就是张冠李戴（2026-10-25 现场：主人让你修正「穷到吃外卖」的记忆，\n"
            "  说「他实际是高一新生」，结果第二天这句话被提取成了主人自己的身份记忆）。\n"
            "  判据**看主语，不看句子里有没有「他/她」**：\n"
            "  ✅「我老婆叫米雪儿」「我妈是医生」——主语是「我」，是自述\n"
            "  🚫「她叫米雪儿」「他今年高一」——主语是第三方，说的是别人\n"
            "- 否定句注意方向——「我不喜欢XX」提取为「不喜欢XX」，绝不能提取成「喜欢XX」\n"
            "- 无上下文单句标签——像「特摄仙人」这种孤零零的称号/名词，没有自述句式证据\n"
            "（我是/我学/我住/我叫/我玩），不提取\n"
            "- 从这些消息中只能提取用户关于自己的明确陈述。宁缺毋滥。\n\n"
            "重要性标准：\n"
            "- 永久/长期不变的（姓名/性别/职业/学历/家乡）= 8-10\n"
            "- 稳定偏好/习惯（喜欢的食物/颜色/音乐/日常习惯）= 4-7\n"
            "- 临时事件/观点（今天吃了什么/刚才做了什么/一时想法）= 1-4\n\n"
            "置信度标准：不要在0.7附近犹豫——明确看到事实就给0.9+，暗示/不确定就给0.4-0.6\n\n"
            "类型定义：\n"
            "- identity: 身份、职业、学校、年龄、所在地等长期稳定信息\n"
            "- preference: 喜欢/讨厌/偏好（食物、音乐、颜色、品牌等）\n"
            "- habit: 习惯、日常行为模式、口头禅\n"
            "- event: **已经发生**的生活事件、经历（去过哪里、做了什么、发生了什么）。\n"
            "  🚫 还没发生的事（明天/下周/周末要做的）不要写成 event——用下面的 open_loop\n"
            "- relationship: 人际关系——不仅限于用户自己，也包括用户提到的其他人的关系。"
            "如「我是她男朋友」「我妈是医生」「张三是李四拉进群的」。\n"
            "- note: 其他值得记住的信息（说过的话、观点、技能等）\n"
            "- alias: 外号/绰号/称呼（2026-08-16）——只在系统提供了候选列表时判断，"
            "value 是外号本身\n"
            "- open_loop: 一件**还没结**的事——对方说将来要做的、承诺的、担心的、有截止日的事。"
            "value 用一句话说清「什么没结」（不要写成「他说要…」，直接写事本身），"
            "另给 kind 和 expected_at。\n"
            "  ⚠ **判据只有一个：这件事发生了没有？** 还没发生 → open_loop；已经发生 → event。\n"
            "  下面这些**都是 open_loop，不要写成 event / note / identity**"
            "（2026-10-25 实测：1791 个提取批次里含将来时词的 158 条只有 2 条被判成 open_loop，"
            "其余 116 条落进了 event——这不是「数据里没有」，是分类没选对）：\n"
            "  ✅「明天去学校拿录取通知书」→ open_loop(kind=plan, expected_at=明天)\n"
            "  ✅「明天要去医院复查」→ open_loop(kind=plan, expected_at=明天)\n"
            "  ✅「明天10点有事，需要人叫醒」→ open_loop(kind=plan, expected_at=明天)\n"
            "  ✅「国庆假期作业还没动笔」→ open_loop(kind=deadline)\n"
            "  ✅ 算：「明天要去面试」「下个月体检」「说了要写但还没写」「想跟某人和好但还没开口」\n"
            "  🚫 不算：已经完成的事——「面试完了」「体检做完了」是普通 event 记忆，不是没结的事\n"
            "  🚫 不算：糖糖自己的承诺——那是糖糖的事（走另一条 remember_self），不是对方的未结事\n"
            "  🚫 不算：无时间指向的空泛愿望——「以后想环游世界」「想变得有钱」没有落点，不是没结的事\n"
            "  **同一件事只输出一条**：几句话说的是同一件事时（「明天要去医院复查」+「八点起来抢复查的号」），"
            "合并成一条 open_loop，细节并进 value；拆成两条会让她对同一件事问两遍、收两次尾。\n"
            "  kind 取值：plan(计划) / promise(承诺) / worry(担忧) / request(请求) / deadline(截止日)\n"
            "  expected_at：预计发生/到期日，格式 YYYY-MM-DD；不知道或没有明确日期就填空字符串 \"\"\n\n"
            "认知类型（cognitive）：\n"
            "- episodic: 有时间锚点的具体事件——「上周去吃了火锅」「昨天说想学吉他」「和张三一起打游戏」\n"
            "  这些是发生过的事，会随时间快速淡化\n"
            "- semantic: 相对稳定的个人信息——「喜欢喝咖啡」「在广州读大学」「室友是李四」\n"
            "  这些是知道的事实，应该长期保留\n"
            "  判定方法：如果你可以用「某年某月某日发生了这件事」来描述，就是 episodic。\n"
            "  如果这个信息在一两年后大概率仍然成立，就是 semantic。\n\n"
            "输出格式（严格的 JSON 数组，不要其他内容）：\n"
            '[{"type":"identity","cognitive":"semantic","value":"在北大读计算机","importance":8,"confidence":0.95,"claim_type":"stated","evidence_quote":"我在北大读计算机","evidence_ids":[12]},'
            '{"type":"preference","cognitive":"semantic","value":"喜欢喝咖啡","importance":3,"confidence":0.8,"claim_type":"inferred","evidence_quote":"每天都要来一杯","evidence_ids":[15]}]'
            "\n\nopen_loop 条目的写法（多 kind / expected_at 两个字段；其他类型不要带这两个字段）：\n"
            '[{"type":"open_loop","kind":"plan","value":"明天要去面试","expected_at":"2026-10-26","cognitive":"episodic","importance":6,"confidence":0.9,"claim_type":"stated","evidence_quote":"我明天要去面试","evidence_ids":[12]}]'
        )

        # 2026-08-16 范式转换（教训 #24）：外号候选由系统在 @ 附近收集（信号收集
        # 不是决策）——是否为外号的判定交给 LLM
        alias_block = ""
        if alias_candidates:
            alias_block = (
                f"\n\n系统在 @{nickname} 附近收集到这些疑似外号候选"
                f"（系统只是收集候选，不做判断）：{'、'.join(alias_candidates[:5])}。\n"
                f"判断哪些是真实的称呼/绰号/外号——是的话输出 type='alias' 条目"
                f"（value=外号，importance=3，confidence=0.8）。"
                f"不是外号的普通词语忽略，不确定的忽略。"
            )

        user_message = (
            f"从 {nickname} 的聊天记录中提取个人信息：\n\n{chat_text}{existing_block}"
            f"{alias_block}\n\n"
            f"请输出 JSON 数组。没有新信息就输出 []。"
        )

        try:
            import json as _json
            raw = await llm_call(system_prompt, user_message)
            raw_length = len(raw) if isinstance(raw, str) else 0
            document = raw.strip() if isinstance(raw, str) else ""
            # 只做一次有界修复：允许“一个 JSON 文档”被 Markdown 代码围栏包裹；
            # 不再用首尾方括号吞掉重复数组或尾随说明。
            if document.startswith("```") and document.endswith("```"):
                first_line_end = document.find("\n")
                if first_line_end != -1:
                    document = document[first_line_end + 1:-3].strip()
            items = _json.loads(document)
            if not isinstance(items, list):
                raise TypeError("top-level JSON must be an array")
            if isinstance(items, list):
                    # 验证每条记录 + 后置校验拦截常见误提取
                    valid = []
                    raw_total = len(items)
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        raw_value = str(item.get("value", "")).strip()
                        if not raw_value:
                            continue
                        # ═══ 后置校验：拦截LLM不遵守提取规则时的常见错误 ═══
                        # 1. 2026-08-16 范式转换：问句过滤已删——提示词已教「问句不提取」，
                        # 代码层重复决策且误伤引用句（「我妈说我怎么怎么」）
                        # 2. 指令/命令不是事实——直接丢弃
                        cmd_markers = ["我是叫你", "我是让你", "你给我", "你耳聋", "你去给", "叫你去"]
                        if any(m in raw_value for m in cmd_markers):
                            continue
                        # 3. 自贬/情绪宣泄不是事实——保留（六节已确认积累：
                        # 「我是废物」是情绪症状不是事实，入库会污染画像）
                        self_neg = ["我是废物", "我没人关心", "我什么都不行", "我是个废物", "我没用",
                                    "我活该", "我不配", "我好没用", "我真没用"]
                        if any(m in raw_value for m in self_neg):
                            continue
                        # 3.5. LLM 推理泄露——「SKIP（前者有...）」「注意：此处...」不是记忆值
                        leak_markers = ["SKIP", "前者有", "后者仅", "注意：", "备注：", "说明：",
                                       "该用户", "这个用户", "此处", "信息不一致"]
                        if any(m in raw_value for m in leak_markers):
                            continue
                        # 4. 长度 < 6 且不是身份类/外号信息 → 碎片（外号天然短：老张/阿强）
                        if len(raw_value) < 6 and item.get("type") not in ("identity", "alias"):
                            continue
                        # 5. 置信度分层（2026-08-16 批 4）：缺失字段与显式 0.7 分开——
                        # 缺失 = LLM 没敢打分 → 拒绝；显式 0.7 = 合法弱证据 → 保留为
                        # 低可信候选（<0.75 不进画像/自动注入，检索带 ~ 标记）。
                        # 旧逻辑把 0.7 一律降权 0.55——恰好压过 0.5 丢弃线，
                        # 「特摄仙人」正是这样入库的；且 0.7 也可能真弱证据，不该一律降
                        conf_raw = item.get("confidence")
                        if conf_raw is None or conf_raw == "":
                            continue
                        try:
                            confidence = min(1.0, max(0.1, float(conf_raw)))
                        except (TypeError, ValueError):
                            continue
                        if confidence < 0.5:
                            continue
                        # 6. 认知类型修正：含事件动词的短记忆→episodic
                        cognitive = item.get("cognitive", "semantic")
                        if cognitive == "semantic" and len(raw_value) < 60:
                            event_verbs = ["去了", "吃了", "说了", "做了", "玩了", "看了", "发了",
                                          "买了", "来了", "写了", "画了", "唱了", "拍了", "送了",
                                          "喝了", "开了", "掉了", "得了", "见过", "去过"]
                            if any(v in raw_value for v in event_verbs):
                                cognitive = "episodic"
                        # 7. importance 合理性检查：LLM 经常默认给 7，实际价值更低
                        imp = item.get("importance", 5)
                        if isinstance(imp, (int, float)):
                            imp = min(10, max(1, int(imp)))
                        else:
                            imp = 5
                        # importance=7 且置信度低 → 降权
                        if imp == 7 and confidence < 0.6:
                            imp = 4
                        # 证据只接受本批次中存在的用户消息；机器人回复不能证明
                        # 用户事实。旧模型偶尔漏字段时仍保留候选，但检索会明确显示
                        # 其只有 memory 来源、没有原始 chat_log 证据。
                        evidence_raw = item.get("evidence_ids") or []
                        if not isinstance(evidence_raw, list):
                            evidence_raw = []
                        user_evidence = {int(m["id"]): m for m in prompt_messages
                                         if m.get("id") is not None and not m.get("is_bot_reply")}
                        evidence_ids = []
                        for ev in evidence_raw:
                            try:
                                ev_id = int(ev)
                            except (TypeError, ValueError):
                                continue
                            if ev_id in user_evidence:
                                evidence_ids.append(ev_id)
                        evidence_ids = sorted(set(evidence_ids))
                        source_group_id = str(
                            (user_evidence.get(evidence_ids[0], {})
                             if evidence_ids else {}).get("group_id", "") or ""
                        )
                        # 一条记忆只能属于一个会话作用域。批处理中偶尔会混入同一用户
                        # 在不同群/私聊的消息，不能把这些证据拼成一条跨作用域事实。
                        evidence_ids = [
                            evidence_id for evidence_id in evidence_ids
                            if str(user_evidence[evidence_id].get("group_id", "") or "")
                            == source_group_id
                        ]
                        payload = {
                            "type": item.get("type", "note"),
                            "cognitive": cognitive,
                            "value": self._truncate_natural(raw_value, 100),
                            "importance": imp,
                            "confidence": confidence,
                            # P0-D2：证据字段必须完整穿过提取器→任务→Store，
                            # 否则下游只能看到 evidence_ids，无法执行原文支持校验。
                            "claim_type": str(item.get("claim_type") or "").strip().lower(),
                            "evidence_quote": str(item.get("evidence_quote") or "").strip(),
                            "evidence_ids": evidence_ids,
                            "source_group_id": source_group_id,
                        }
                        # open_loop 专有字段必须透传：丢了 expected_at，L3 的
                        # 「到期优先」排序永远命中不到；丢了 kind，注入块说不清
                        # 「这是计划还是担忧」。只挂在 open_loop 上——其他类型
                        # 多出这两个字段会污染既有记忆结构（闸门钉住）。
                        if str(payload["type"]) == "open_loop":
                            payload["kind"] = (
                                str(item.get("kind") or "plan").strip().lower() or "plan")
                            payload["expected_at"] = str(
                                item.get("expected_at") or "").strip()
                        valid.append(payload)
                    outcome = (
                        "success_empty" if raw_total == 0
                        else "success_with_items" if valid
                        else "rejected_all"
                    )
                    self._last_extract_stats = {
                        "outcome": outcome,
                        "protocol_ok": True,
                        "raw": raw_total,
                        "valid": len(valid),
                        "rejected": raw_total - len(valid),
                        "raw_length": raw_length,
                    }
                    return valid, self._last_extract_stats
        except (json.JSONDecodeError, TypeError) as e:
            self._last_extract_stats = {
                "outcome": "invalid_json",
                "protocol_ok": False,
                "raw": 0,
                "valid": 0,
                "rejected": 0,
                "raw_length": len(raw) if isinstance(locals().get("raw"), str) else 0,
            }
            logger.warning(
                "🧠 LLM 提取 JSON 无效: %s (raw_length=%s)",
                e, self._last_extract_stats["raw_length"],
            )
        except Exception as e:
            self._last_extract_stats = {
                "outcome": "transport_error",
                "protocol_ok": False,
                "raw": 0,
                "valid": 0,
                "rejected": 0,
                "raw_length": len(raw) if isinstance(locals().get("raw"), str) else 0,
            }
            logger.warning(f"🧠 LLM 提取失败: {type(e).__name__}: {e}")
        return [], self._last_extract_stats

    async def synthesize_profile(
        self, qq_id: str, llm_call,
        memories: list | None = None,
        embed_engine=None,
        bypass_cooldown: bool = False,
    ) -> str | None:
        """
        从多条记忆中综合提炼用户画像（EverOS Profile 概念的简化版）。
        类似 EverOS extract_user_profile strategy —— 从 accumulated facts
        中 INIT/UPDATE 一个可读的人物画像。

        2026-08-16 批 3：
        - 成功冷却 24h（kv 持久化）——三路触发共享；纠正路径 bypass 一次
        - 合成输入排除合成行（旧画像/画像事实不自我复制）
        - 快照替换语义（singleton 画像 + 事实集合事务化替换，不再追加堆积）

        Returns:
            画像文本，或 None（记忆不够 / 冷却中）
        """
        # 批 3：成功冷却——只拦日常触发；纠正路径（bypass_cooldown=True）放行
        if not bypass_cooldown:
            last = await self._run_store_io(
                "kv_get_profile_syn_at", self.store.kv_get,
                f"profile_syn_at:{qq_id}",
            ) or ""
            if last and last > (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"):
                return None

        if memories is None:
            memories = await self._run_store_io(
                "recall_profile_memories", self.recall, qq_id, limit=50,
            )

        # 批 3：输入排除合成行——第二条自我复制通道（第一条是 notes 回喂）
        memories = [m for m in memories
                    if (m.key if hasattr(m, 'key') else '') not in _protocols.SYNTHESIS_KEYS]
        # 批 4：低置信度语义记忆不进画像（0.55 种子洗白成 0.7 画像的事故通道）
        # 高重要度（≥8，纠正/手动）与 episodic 保留
        memories = [m for m in memories
                    if getattr(m, "importance", 0) >= 8
                    or getattr(m, "confidence", 0.7) >= 0.75
                    or getattr(m, "cognitive", "semantic") == "episodic"]

        # people.notes 是单值，不能把群A、群B和私聊混合成一张画像后再当作
        # 任一会话的可信事实。多作用域用户先依赖原子记忆；待有分域画像表再合成。
        source_scopes = {
            str(
                getattr(memory, "source_group_id", "")
                if hasattr(memory, "source_group_id")
                else memory.get("source_group_id", "")
                if isinstance(memory, dict) else ""
            )
            for memory in memories
        }
        if len(source_scopes) != 1:
            return None
        profile_scope = next(iter(source_scopes))

        if len(memories) < 5:
            return None

        person = await self._run_store_io(
            "get_or_create_person", self.get_or_create_person, qq_id,
        )
        nickname = person.get("nickname", qq_id)

        # 分类组织记忆
        by_type: dict[str, list[str]] = {}
        for m in memories:
            key = m.key if hasattr(m, 'key') else 'fact'
            val = m.value if hasattr(m, 'value') else str(m)
            by_type.setdefault(key, []).append(val)

        memory_text = ""
        for t, vals in by_type.items():
            memory_text += f"\n[{t}] " + " | ".join(vals[:8])

        # C1（2026-08-16 Codex 审查）：dirty 的旧画像是已知错误——禁止回喂合成，
        # 否则纠正后的重合成会把刚撤销的错误事实重新写回（自我污染）
        existing = await self._run_store_io(
            "active_notes", self.active_notes, qq_id,
        )
        if existing and person.get("notes_trust_level") == "verified":
            source_ids = [
                int(item) for item in str(person.get("notes_source_ids") or "").split(",")
                if item.strip().isdigit() and int(item) > 0
            ]
            source_rows = []
            for item in source_ids:
                source_rows.append(await self._run_store_io(
                    "get_memory_by_id", self.store.get_memory_by_id, item,
                ))
            source_rows = [row for row in source_rows if row]
            existing_scopes = {
                str(row.get("source_group_id") or "") for row in source_rows
            }
            # people.notes 只有一个槽位。已有可信画像属于别的会话时，拒绝
            # 增量合成和覆盖；当前 scope 继续依赖有证据的原子记忆。
            if not source_ids or len(source_rows) != len(source_ids) or existing_scopes != {profile_scope}:
                return None

        system_prompt = (
            "你是用户画像员。根据零散的记忆碎片，综合提炼出一个自然的人物画像。\n"
            "像写人物简介一样——连贯、自然、有温度，不是列表。\n"
            "包含：身份背景、性格特点、喜好习惯、重要经历、人际关系。\n"
            "100-200字，中文。只说能确定的事，不编造。\n"
            "只输出画像正文——禁止任何开头语/自述/解释/标题/分隔线，第一个字就进入正文。\n\n"
            "画像之后，另起一行写「KEY_FACTS:」，然后逐条列出从画像中提取的关键事实。\n"
            "每条是一个独立的、可检索的事实陈述。2-5条，每条15-40字。\n"
            "这些事实将被单独索引，用于对话中的记忆检索——所以你提炼的每条事实\n"
            "都应该是群聊中提到时可以自然接话的具体信息。\n\n"
            "格式示例：\n"
            "（画像正文100-200字）\n"
            "KEY_FACTS:\n"
            "- 在雷霆服务器当过管理员，高考那年一边备考一边管机房\n"
            "- 自称伞夫，喜欢利他牺牲式的角色关系\n"
            "- 奶奶的睡前故事是奇怪的车牌号，被带歪了童年\n"
        )

        user_message = (
            f"关于{nickname}的记忆碎片：\n{memory_text}\n\n"
            + (f"已有画像：{existing}\n\n请增量更新。" if existing else "请生成初始画像。")
        )

        try:
            response = await llm_call(system_prompt, user_message)
            response = response.strip()
            if not response or len(response) < 20:
                return None

            # 解析 KEY_FACTS 段落
            import re as _re
            profile = response
            key_facts = []
            facts_match = _re.search(r'KEY_FACTS\s*[:：]\s*\n?(.*)', response, _re.DOTALL | _re.IGNORECASE)
            if facts_match:
                profile = response[:facts_match.start()].strip()
                facts_text = facts_match.group(1).strip()
                # 按行解析，只剥行首列表标记（年份是内容不是标记——见 _strip_list_marker）
                for line in facts_text.split('\n'):
                    line = _strip_list_marker(line)
                    if len(line) >= 10:  # 过滤太短的
                        key_facts.append(line)

            # 批 3：元话术前缀兜底剥离——主防线是 prompt 契约（「这是个人群像的
            # 增量更新版」曾实存进 notes 的教训），这里兜底 3 轮
            _meta_re = _re.compile(
                r"^(这是[^。\n]*[。\n]|以下是[^。\n]*[。\n]|根据[^。\n]*[。\n]|"
                r"好的[，,。\n]?|收到[，,。\n]?|用户画像[:：]?|画像[:：]?|#{1,6}\s*)"
            )
            for _ in range(3):
                m = _meta_re.match(profile)
                if m:
                    profile = profile[m.end():].strip()
                else:
                    break

            # 保存画像到 people.notes
            if profile and len(profile) > 20:
                # 2026-08-16 Codex I6：单事务完成 notes 换入 + 清 dirty + 两世代
                # supersede + 新行插入——三次独立事务之间崩溃会产生「新 notes +
                # 旧事实」的半世代（纠正场景下 dirty 提前清零而旧错误仍可见）
                source_memory_ids = [
                    int(getattr(memory, "id", 0) or 0) for memory in memories
                    if int(getattr(memory, "id", 0) or 0) > 0
                ]
                snap = await self._run_store_io(
                    "replace_profile_snapshot", self.store.replace_profile_snapshot,
                    qq_id, profile, key_facts,
                    importance=4, origin="summarized",
                    source_memory_ids=source_memory_ids,
                    source_group_id=profile_scope,
                    actor="synthesis:profile",
                )
                # 新行同步建 embedding（旧行 embedding 已在快照事务内删除）
                if embed_engine and embed_engine.ready:
                    pairs = [(snap["profile_id"], profile)]
                    pairs += list(zip(snap["fact_ids"], key_facts[:len(snap["fact_ids"])]))
                    for mid, txt in pairs:
                        try:
                            vec = await self._encode_embedding(
                                embed_engine, txt, "memory.profile.embedding",
                            )
                            if vec is not None:
                                await self._run_store_io(
                                    "set_embedding", self.store.set_embedding, mid, vec,
                                )
                        except Exception:
                            pass
                # 成功冷却落盘（失败不落——下次仍可触发）
                await self._run_store_io(
                    "kv_set_profile_syn_at", self.store.kv_set,
                    f"profile_syn_at:{qq_id}",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )

            if key_facts:
                logger.info(f"📌 {nickname}: 画像合成 + {len(key_facts)}条关键事实已索引")

            return profile
        except Exception:
            # 2026-10-24：这里原本是裸 `except Exception: pass`——「合成失败」与
            # 「记忆不够」返回同一个 None，调用方与运维都看不出区别（教训 #27：
            # 空结果不许静默）。只加日志，返回值与降级行为一概不变。
            logger.warning("画像合成异常（已返回 None）", exc_info=True)
        return None

    # ════════════════════════════════════════════════════════════
    # 🏗️ 结构化记忆层——事实簇提取
    # ════════════════════════════════════════════════════════════

    _EXTRACT_FACTS_PROMPT = (
        "你是一个信息提取助手。从聊天记录中提取关于用户的真实个人信息，越多越好。\n\n"
        "核心原则：提取用户本人的真实情况，不提取角色扮演或对糖糖说的话。\n\n"
        "规则：\n"
        "- ✅ 提取用户明确陈述的关于自己的事实——身份、经历、喜好、状态、关系\n"
        "- ✅ 保留原话的具体信息——'花10块买了盗版模组'不要写成'有过消费经历'，具体才能检索\n"
        "- 🚫 不提取问句、反问、对别人的描述\n"
        "- 🚫 不提取自贬——「我是废物」「没人关心我」是情绪不是事实\n"
        "- 🚫 不提取角色扮演/游戏设定——「我是猫娘」「被设定为主人」「来测试功能」是RP对话，不是用户的真实信息！\n"
        "- 🚫 不提取用户对糖糖说的话——「糖糖你好可爱」\n"
        "- 🚫 不提取玩笑/玩梗（「我是秦始皇」）、测试语句（开发者测试记忆的输入）、\n"
        "引用转述（「他说」「XX说」、引号里的他人言论）\n"
        "- 🚫 否定句注意方向——「我不喜欢XX」是「不喜欢XX」，绝不能写成「喜欢XX」\n"
        "- evidence_ids：每条 fact 附上支持它的聊天编号列表（取上行首 #编号，如 [5,7]；不确定给 []）\n"
        "- 每条一句话，一个信息点。拆分复合陈述——「有心脏病，喜欢火锅」→ 两条\n"
        "- topic 是2-6字标签，同主题用同一个 topic\n"
        "- importance 1-10：永久事实(疾病/家庭成员)=9-10，长期(职业/学校/居住地/社交状况)=7-8，偏好/习惯=3-5，日常状态=1-2\n"
        "- confidence 0.0-1.0：明确陈述=1.0，暗示=0.7-0.8\n"
        "- 每条消息仔细看，低重要度也提取。宁可多不要漏\n\n"
        "category 定义：\n"
        "- health: 持续性的健康问题——慢性病、过敏、心理疾病、长期失眠。🚫日常状态(今天饿了/今晚不睡/有点累)不是health，归入note\n"
        "- identity: 身份、职业、学校、年龄、性别、所在地、独居/合住\n"
        "- relationship: 人际关系——家庭、朋友(有无/多少)、恋人、社交状态。「没有朋友」「朋友很少」是relationship！\n"
        "- work: 工作、项目、学习、考试\n"
        "- preference: 喜欢/讨厌的具体事物——食物、音乐、颜色、游戏、称呼\n"
        "- experience: 经历过的事——被骗、搬家、旅行、买了什么、做了什么\n"
        "- habit: 习惯、作息、口头禅\n"
        "- note: 其他——临时状态、计划、对糖糖的评价(不是RP)、日常琐事\n\n"
        "输出（严格的 JSON 数组）：\n"
        '[{"topic":"心脏健康","category":"health","fact":"有先天性心脏病","importance":9,"confidence":1.0},'
        '{"topic":"饮食偏好","category":"preference","fact":"喜欢吃火锅","importance":4,"confidence":0.95}]'
    )

    @staticmethod
    def _derive_permanence(category: str, importance: int) -> str:
        """根据类别和重要性自动判定持久性——LLM 不需要输出这个字段。
        原则：health/identity 是核心事实，不衰减；低重要度的 experience 是短期状态。"""
        if category in ("health",):
            # 健康事实永久保留——疾病、过敏、心理健康状态不会"过期"
            return "permanent" if importance >= 6 else "stable"
        if category in ("identity", "relationship"):
            return "stable" if importance >= 5 else "normal"
        if category in ("work",):
            return "stable" if importance >= 6 else "normal"
        if category in ("preference", "habit"):
            return "normal"
        if category in ("experience",):
            if importance >= 8:
                return "stable"  # 重大人生事件——"搬到了北京"
            if importance <= 3:
                return "transient"  # 日常琐事——"昨天去了趟超市"
            return "normal"
        # note / 其他
        if importance <= 2:
            return "transient"
        return "normal"

    @staticmethod
    def _parse_fact_cluster_response(raw) -> list[dict] | None:
        """解析事实簇协议，只接受一个完整 JSON 文档。

        DeepSeek 偶尔会把严格数组包在 Markdown 围栏中，或返回
        ``{"facts": [...]}`` 这类等价 envelope。允许这两种无歧义的包装，
        但拒绝数组前后夹杂自然语言，避免用 substring 截取把截断/重复响应
        误当成合法事实并推进游标。
        """
        if not isinstance(raw, str):
            return None
        document = raw.strip().lstrip("\ufeff")
        if not document:
            return None
        if document.startswith("```"):
            lines = document.splitlines()
            if len(lines) < 3 or not lines[-1].strip().startswith("```"):
                return None
            document = "\n".join(lines[1:-1]).strip()
        try:
            parsed = json.loads(document)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, list):
            return parsed
        if not isinstance(parsed, dict):
            return None
        # 只认单一、明确的事实容器；其它字段可能是模型的解释文本。
        candidates = [
            value for key, value in parsed.items()
            if key in ("facts", "items", "results", "data")
            and isinstance(value, list)
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """事实簇异步路径的统一 Store 门，避免 SQLite 占用事件循环。"""
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="🧠 Memory Store SQLite 调用较慢", **kwargs,
        )

    async def _encode_embedding(self, embed_engine, text: str, operation: str):
        """事实簇/画像异步路径的统一 embedding 门，避免模型编码冻结事件循环。"""
        if not (embed_engine and getattr(embed_engine, "ready", False)):
            return None
        return await run_bounded_blocking(
            operation,
            embed_engine.encode,
            text,
            logger=logger,
            log_prefix="🧠 Memory embedding 编码较慢",
        )

    async def extract_fact_clusters(
        self, qq_id: str, messages: list[dict], nickname: str,
        llm_call, embed_engine
    ) -> dict:
        """
        从聊天记录中提取结构化事实，归入事实簇。
        增量更新：新事实合并到已有簇，或创建新簇。

        Returns:
            {"new_clusters": N, "new_facts": M, "updated_clusters": K}
        """
        chat_text = "\n".join(
            f"[{m.get('timestamp', '?')}] #{m.get('id', '?')} "
            f"{'糖糖' if m.get('is_bot_reply') else nickname}: {m['message'][:150]}"
            for m in messages[-30:]
        )

        # 获取已有簇，供 LLM 参考——鼓励补充新细节，不阻止提取
        existing_clusters = await self._run_store_io(
            "get_fact_clusters", self.store.get_fact_clusters, qq_id,
        )
        existing_block = ""
        # 启动迁移已清空没有可追溯 active evidence 的历史摘要；因此非空摘要
        # 才能进入当前提取回合。无锚点原子行仍保留供人工审计，但不能再合成。
        anchored_clusters = [
            c for c in existing_clusters
            if str(c.get("summary") or "").strip()
            # 缺失该字段的旧适配器按不可信处理，避免旁路把旧摘要送进 LLM。
            and not c.get("has_unanchored_active_facts", True)
        ]
        if anchored_clusters:
            lines = [
                "## 已知的关于此人的信息（供参考，如果消息中有相关的新细节或变化，仍然提取）："
            ]
            for c in anchored_clusters:
                lines.append(f"- [{c['category']}] {c['title']}: {c.get('summary', '')[:80]}")
            lines.append("即使话题已存在，仍可提取——补充新细节、更新状态、增加具体例子。")
            existing_block = "\n".join(lines) + "\n\n"

        user_message = (
            f"从 {nickname} 的聊天记录中提取个人信息：\n\n{chat_text}\n\n"
            f"{existing_block}"
            f"请输出 JSON 数组。没有新信息就输出 []。"
        )

        try:
            raw = await llm_call(self._EXTRACT_FACTS_PROMPT, user_message)
            items = self._parse_fact_cluster_response(raw)
            if items is None:
                # C3（2026-08-16 Codex）：失败与合法空结果必须可区分——
                # ok=False 时调用方绝不推进游标。只记录长度，不记录模型原文，
                # 避免把聊天内容/提示注入写进日志。
                logger.warning(
                    "🧠 事实簇响应协议无效: raw_type=%s raw_length=%s",
                    type(raw).__name__, len(raw) if isinstance(raw, str) else 0,
                )
                return {"ok": False, "new_clusters": 0, "new_facts": 0, "updated_clusters": 0}

            if not items:
                return {"ok": True, "new_clusters": 0, "new_facts": 0, "updated_clusters": 0}

        except Exception:
            return {"ok": False, "new_clusters": 0, "new_facts": 0, "updated_clusters": 0}

        # ── 后处理：按 topic 分组 → 匹配已有簇 → 合并/创建 ──
        import logging as _logging
        _log = _logging.getLogger("糖糖.Memory")

        # 预计算已有簇的向量（title 的 BGE embedding）
        cluster_index = []  # [(cluster_dict, embedding)]
        for c in anchored_clusters:
            emb = await self._encode_embedding(
                embed_engine, c["title"], "memory.fact_cluster.title_embedding",
            )
            cluster_index.append((c, emb))

        new_clusters = 0
        new_facts = 0
        updated_clusters = set()
        modified_cluster_ids = set()
        valid_user_evidence = {int(m["id"]) for m in messages
                               if m.get("id") is not None and not m.get("is_bot_reply")}

        for item in items:
            fact_text = str(item.get("fact", "")).strip()
            if not fact_text or len(fact_text) < 4:
                continue

            # 批 4：缺失 confidence 拒绝（与主提取同一分层口径）
            conf_raw = item.get("confidence")
            if conf_raw is None or conf_raw == "":
                continue
            try:
                confidence = min(1.0, max(0.1, float(conf_raw)))
            except (TypeError, ValueError):
                continue
            if confidence < 0.5:
                continue

            # 批 4：证据 id——纠正/撤销可回溯是哪条聊天支持的
            ev_ids = item.get("evidence_ids") or []
            if not isinstance(ev_ids, list):
                ev_ids = []
            evidence_ids = ",".join(str(int(e)) for e in ev_ids
                                    if isinstance(e, (int, float)) and float(e).is_integer()
                                    and int(e) in valid_user_evidence)
            if not evidence_ids:
                _log.warning(f"🧠 事实簇拒绝无有效证据: {fact_text[:50]}")
                continue

            importance = min(10, max(1, int(item.get("importance", 5))))
            category = str(item.get("category", "note"))
            topic = str(item.get("topic", "")).strip()
            # 系统自动判定 permanence——不让 LLM 输出（它不理解这个概念）
            permanence = self._derive_permanence(category, importance)

            if not topic:
                topic = category  # 兜底：用 category 当 topic

            # 计算这个 topic 的向量
            topic_emb = await self._encode_embedding(
                embed_engine, topic, "memory.fact_cluster.topic_embedding",
            )
            fact_emb = await self._encode_embedding(
                embed_engine, fact_text, "memory.fact_cluster.fact_embedding",
            )

            # 找最匹配的已有簇
            best_cluster = None
            best_sim = 0.0
            if topic_emb is not None:
                for c, c_emb in cluster_index:
                    if c_emb is not None:
                        sim = embed_engine.similarity(topic_emb, c_emb)
                        if sim > best_sim:
                            best_sim = sim
                            best_cluster = c

            MERGE_THRESHOLD = 0.60
            if best_cluster and best_sim > MERGE_THRESHOLD:
                # 合并到已有簇
                fid = await self._run_store_io(
                    "add_cluster_fact", self.store.add_cluster_fact,
                    cluster_id=best_cluster["id"],
                    subject_qq=qq_id,
                    fact=fact_text,
                    source_qq=qq_id,
                    confidence=confidence,
                    importance=importance,
                    embedding=fact_emb,
                    evidence_ids=evidence_ids,
                )
                modified_cluster_ids.add(best_cluster["id"])
                if fid:
                    new_facts += 1  # 批 4：重复 INSERT OR IGNORE 不虚增计数
            else:
                # 新建簇
                cluster_id = await self._run_store_io(
                    "upsert_fact_cluster", self.store.upsert_fact_cluster,
                    subject_qq=qq_id,
                    category=category,
                    title=topic,
                    summary=fact_text,  # 初始摘要 = 第一条事实
                    permanence=permanence,
                    embedding=topic_emb,
                )
                fid = await self._run_store_io(
                    "add_cluster_fact", self.store.add_cluster_fact,
                    cluster_id=cluster_id,
                    subject_qq=qq_id,
                    fact=fact_text,
                    source_qq=qq_id,
                    confidence=confidence,
                    importance=importance,
                    embedding=fact_emb,
                    evidence_ids=evidence_ids,
                )
                # 把新簇加入索引，后续事实可能合并进去
                new_c = await self._run_store_io(
                    "get_fact_clusters", self.store.get_fact_clusters, qq_id,
                )
                for c in new_c:
                    if c["id"] == cluster_id:
                        cluster_index.append((c, topic_emb))
                        break
                new_clusters += 1
                new_facts += 1
                modified_cluster_ids.add(cluster_id)

        # ── 重新生成被修改过的簇的摘要 ──
        for cid in modified_cluster_ids:
            try:
                await self._regenerate_cluster_summary(cid, llm_call, embed_engine)
                updated_clusters.add(cid)
            except Exception:
                pass

        if new_facts > 0:
            _log.info(
                f"🏗️ 事实簇提取: {nickname}({qq_id}) → "
                f"{new_clusters}个新簇, {new_facts}条新事实, "
                f"{len(updated_clusters)}个簇摘要已更新"
            )

        return {
            "ok": True,
            "new_clusters": new_clusters,
            "new_facts": new_facts,
            "updated_clusters": len(updated_clusters),
        }

    async def _regenerate_cluster_summary(self, cluster_id: int, llm_call, embed_engine):
        """为一个事实簇重新生成摘要和向量"""
        facts = await self._run_store_io(
            "get_cluster_facts", self.store.get_cluster_facts, cluster_id,
        )
        # active 不等于可信：历史无证据原子事实保留作审计，但不得再次
        # 参与 LLM 摘要，防止启动迁移后被后台任务重新“洗白”。
        facts = [
            fact for fact in facts
            if str(fact.get("evidence_ids") or "").strip()
        ]
        if not facts:
            return

        # 直接查簇信息（2026-08-10 收口：走 Store 统一连接）
        cluster = await self._run_store_io(
            "get_fact_cluster", self.store.get_fact_cluster, cluster_id,
        )
        if not cluster:
            return

        facts_text = "\n".join(
            f"- [{f['importance']}] {f['fact']} (置信度:{f['confidence']:.0%})"
            for f in sorted(facts, key=lambda x: x["importance"], reverse=True)
        )

        system_prompt = (
            "你是信息摘要助手。将多条同类事实合并为1-2句连贯的摘要。\n"
            "规则：\n"
            "- 包含所有重要事实的关键信息，不遗漏\n"
            "- 自然流畅，像在介绍一个人\n"
            "- 不编造任何不在事实列表中的内容\n"
            "- 中文，30-80字"
        )
        user_message = (
            f"主题：{cluster['title']}\n"
            f"类别：{cluster['category']}\n"
            f"事实列表：\n{facts_text}\n\n"
            f"请生成摘要。"
        )

        try:
            summary = await llm_call(system_prompt, user_message)
            summary = summary.strip()
            if summary and len(summary) > 10:
                new_emb = await self._encode_embedding(
                    embed_engine,
                    f"{cluster['title']} {summary}",
                    "memory.fact_cluster.summary_embedding",
                )
                await self._run_store_io(
                    "update_cluster_summary", self.store.update_cluster_summary,
                    cluster_id, summary, new_emb,
                    actor="synthesis:fact_cluster",
                    reason=f"簇摘要重生成：{cluster['title']}",
                )
        except Exception:
            pass

    def search_fact_clusters(self, subject_qq: str, query: str,
                             embed_engine, top_k: int = 3,
                             source_group_id: str | None = None) -> str:
        """搜索可信原子记忆。

        旧事实簇摘要是 LLM 二次合成物且没有完整证据关系，只可作为离线重建
        线索；面向对话的工具结果统一从 trusted memory 投影生成。
        """
        memories = self.recall(
            subject_qq,
            limit=max(8, top_k * 3),
            query_text=query,
            embed_engine=embed_engine,
            trusted_only=True,
            source_group_id=source_group_id,
        )
        fact_keys = {
            "fact", "fact_correction", "like", "hate", "habit",
            "preference", "birthday", "relationship", "said",
        }
        facts = [memory for memory in memories if memory.key in fact_keys][:max(1, top_k * 2)]
        if not facts:
            return f"[未找到关于 {subject_qq} 的「{query}」可信事实]"

        lines = ["可信事实（回答前仍应结合当前对话核验）："]
        for memory in facts:
            if memory.evidence_ids:
                source = f"chat_log#{memory.evidence_ids}"
            else:
                source = f"{memory.trust_level}#{memory.id}"
            fact_time = _protocols.format_fact_time(memory.event_time or memory.timestamp)
            lines.append(
                f"- [{memory.key} | {fact_time} | 来源 {source}] {memory.value[:160]}"
            )
        return "\n".join(lines)

    async def merge_similar_clusters(self, subject_qq: str, llm_call, embed_engine) -> int:
        """
        全部分析完后，比较同一个人所有簇的 summary 向量，相似度 > 0.7 的让 LLM 判断是否该合并。
        返回合并的次数。
        """
        clusters = await self._run_store_io(
            "get_fact_clusters", self.store.get_fact_clusters, subject_qq,
        )
        # 摘要可能混入历史无证据事实；混合簇/无锚点簇不参与相似合并。
        # 缺少标记的旧适配器 fail-closed，不让维护路径绕过证据闸门。
        clusters = [
            cluster for cluster in clusters
            if not cluster.get("has_unanchored_active_facts", True)
        ]
        if len(clusters) < 2:
            return 0

        # 计算所有簇的 summary 向量
        cluster_vecs = []
        for c in clusters:
            text = f"{c['title']} {c.get('summary', '')}"
            emb = await self._encode_embedding(
                embed_engine, text, "memory.fact_cluster.merge_embedding",
            )
            cluster_vecs.append((c, emb))

        # 找相似对
        pairs = []
        for i in range(len(cluster_vecs)):
            for j in range(i + 1, len(cluster_vecs)):
                ci, vi = cluster_vecs[i]
                cj, vj = cluster_vecs[j]
                if vi is not None and vj is not None:
                    sim = embed_engine.similarity(vi, vj)
                    if sim > 0.7:
                        pairs.append((ci, cj, sim))

        if not pairs:
            return 0

        import logging as _logging
        _log = _logging.getLogger("糖糖.Memory")
        merged_count = 0

        for ci, cj, sim in pairs:
            # 获取双方的事实
            facts_i = await self._run_store_io(
                "get_cluster_facts", self.store.get_cluster_facts, ci["id"],
            )
            facts_j = await self._run_store_io(
                "get_cluster_facts", self.store.get_cluster_facts, cj["id"],
            )
            facts_i = [
                fact for fact in facts_i
                if str(fact.get("evidence_ids") or "").strip()
            ]
            facts_j = [
                fact for fact in facts_j
                if str(fact.get("evidence_ids") or "").strip()
            ]
            if not facts_i or not facts_j:
                continue
            combined = facts_i + facts_j

            facts_text = "\n".join(
                f"[簇A-{ci['title']}] {f['fact']}" for f in facts_i
            ) + "\n" + "\n".join(
                f"[簇B-{cj['title']}] {f['fact']}" for f in facts_j
            )

            system_prompt = (
                "你是记忆整合助手。判断这两组事实是否应该合并为一个主题。\n"
                "输出 YES 或 NO，以及合并后的新 topic（2-6字）。\n"
                "格式：YES|NO, 新topic"
            )
            try:
                result = await llm_call(system_prompt, facts_text)
                result = result.strip()
                if result.upper().startswith("YES"):
                    # 提取新 topic
                    parts = result.split(",")
                    new_topic = parts[1].strip() if len(parts) > 1 else ci["title"]

                    # 把 cj 的所有事实迁移到 ci
                    for f in facts_j:
                        fact_emb = await self._encode_embedding(
                            embed_engine, f["fact"],
                            "memory.fact_cluster.migrated_fact_embedding",
                        )
                        await self._run_store_io(
                            "add_cluster_fact", self.store.add_cluster_fact,
                            cluster_id=ci["id"],
                            subject_qq=subject_qq,
                            fact=f["fact"],
                            source_qq=f.get("source_qq", ""),
                            confidence=f.get("confidence", 1.0),
                            importance=f.get("importance", 5),
                            embedding=fact_emb,
                            evidence_ids=f.get("evidence_ids", ""),
                        )
                    # 更新 ci 的 title
                    await self._run_store_io(
                        "set_cluster_title", self.store.set_cluster_title,
                        ci["id"], new_topic, actor="synthesis:cluster_merge",
                        reason=f"簇合并：'{cj['title']}' 并入 '{ci['title']}'",
                    )
                    # 删除 cj
                    await self._run_store_io(
                        "delete_cluster", self.store.delete_cluster, cj["id"],
                        actor="synthesis:cluster_merge",
                        reason=(f"簇合并：'{cj['title']}' 并入 '{ci['title']}'"
                                f"（相似度 {sim:.0%}），成员事实已迁移后删除原簇"),
                    )
                    # 重新生成摘要
                    await self._regenerate_cluster_summary(ci["id"], llm_call, embed_engine)
                    merged_count += 1
                    _log.info(f"🔀 合并簇: '{cj['title']}' → '{ci['title']}' (新名:{new_topic}, 相似度:{sim:.0%})")
            except Exception:
                pass

        return merged_count

    async def consolidate_memories(
        self, qq_id: str, llm_call, embed_engine=None
    ) -> int:
        """
        兼容旧调度器的安全空操作。

        记忆写入路径已经做作用域内精确去重，召回路径负责相关性与时间衰减。
        旧的后台整理会让 LLM 改写事实、跨会话合并，并物理删除低权重记录；
        这些行为无法满足证据链、永久保留和审计要求，因此不再修改任何记忆。

        Returns: 固定为 0；保留异步接口，避免旧任务/状态文件升级时崩溃。
        """
        return 0

    # ---- 全文记忆搜索 ----

    def search_memories(self, keyword: str, limit: int = 5) -> list[MemoryEntry]:
        """在全部记忆中搜索关键词（委托给 Store）"""
        results = self.store.search_memories_by_keyword(keyword, limit)
        return [
            MemoryEntry(qq_id=r["qq_id"], key=r["key"], value=r["value"],
                        timestamp=r["timestamp"], importance=r["importance"])
            for r in results
        ]

    def get_topic_context(self, message: str, current_qq: str = "") -> str:
        """根据当前消息，搜索所有相关的记忆（包括其他人的），用于话题联想"""
        # 提取可能的关键词（简单分词）
        keywords = []
        for word in message.replace("，", " ").replace("。", " ").split():
            word = word.strip()
            if len(word) >= 2 and word not in ("什么", "怎么", "为什么", "这个", "那个", "就是", "可以", "觉得"):
                keywords.append(word)

        all_memories = []
        seen = set()
        for kw in keywords[:5]:  # 最多取5个关键词
            results = self.search_memories(kw, limit=3)
            for m in results:
                key = f"{m.qq_id}:{m.value}"
                if key not in seen:
                    if m.qq_id != current_qq:  # 不重复当前人的记忆
                        seen.add(key)
                        person = self.get_or_create_person(m.qq_id)
                        m.value = f"[关于{person.get('nickname', m.qq_id)}]{m.value}"
                        all_memories.append(m)

        if not all_memories:
            return ""
        return "糖糖记得的相关事情：\n" + "\n".join(f"- {m.value}" for m in all_memories[:5])

    # ---- 人物计数 ----

    # ════════════════════════════════════════════════════════════
    # R3-1: Episode 结构化聚合
    # ════════════════════════════════════════════════════════════

    EPISODE_GAP_MINUTES = 60    # 相邻 episodic 记忆的时间间隔阈值（分钟）
    EPISODE_MIN_PARAGRAPHS = 3  # 最少需要几条 episodic 记忆才能形成 episode

    async def _aggregate_episodes(self, qq_id: str) -> int:
        """将零散 episodic 记忆聚合为结构化的 Episode。

        纯确定性算法——不调 LLM，不阻塞消息处理。
        同一 qq_id 的 episodic 记忆 → 时间差 < GAP → 成组 → 创建 episode。

        Returns: 新创建的 episode 数量
        """
        created = 0
        rows = await self._run_store_io(
            "query_memories_for_episodes", self.store.query_memories,
            qq_id, limit=None, trusted_only=True,
        )
        by_scope: dict[str, list[dict]] = {}
        for row in rows:
            event_time = str(row.get("event_time") or row.get("timestamp") or "")
            if row.get("cognitive") != "episodic" or len(event_time) < 10:
                continue
            by_scope.setdefault(str(row.get("source_group_id") or ""), []).append(row)

        for scope, episodic in by_scope.items():
            if len(episodic) < self.EPISODE_MIN_PARAGRAPHS:
                continue
            episodic.sort(key=lambda row: row.get("event_time") or row.get("timestamp") or "")
            groups = []
            current = [episodic[0]]
            for row in episodic[1:]:
                try:
                    previous = current[-1].get("event_time") or current[-1].get("timestamp") or ""
                    current_time = row.get("event_time") or row.get("timestamp") or ""
                    gap = abs((
                        datetime.fromisoformat(str(current_time)[:19])
                        - datetime.fromisoformat(str(previous)[:19])
                    ).total_seconds()) / 60
                except (ValueError, IndexError):
                    gap = float("inf")
                if gap <= self.EPISODE_GAP_MINUTES:
                    current.append(row)
                else:
                    if len(current) >= self.EPISODE_MIN_PARAGRAPHS:
                        groups.append(current)
                    current = [row]
            if len(current) >= self.EPISODE_MIN_PARAGRAPHS:
                groups.append(current)

            existing = await self._run_store_io(
                "query_episodes", self.store.query_episodes,
                qq_id, limit=50, source_group_id=scope,
            )
            existing_paragraphs = {item.get("paragraph_ids") for item in existing}
            for group in groups:
                title = str(group[0].get("value") or "")[:48] + "之类的事"
                summary = "；".join(
                    str(item.get("value") or "")[:60] for item in group[:3]
                )[:200]
                para_ids = ",".join(str(item["id"]) for item in group if item.get("id"))
                if para_ids in existing_paragraphs:
                    continue
                await self._run_store_io(
                    "insert_episode", self.store.insert_episode,
                    qq_id=qq_id, title=title, summary=summary,
                    time_start=group[0].get("event_time") or group[0].get("timestamp") or "",
                    time_end=group[-1].get("event_time") or group[-1].get("timestamp") or "",
                    paragraph_ids=para_ids, source_group_id=scope,
                )
                existing_paragraphs.add(para_ids)
                created += 1

        if created:
            logger.info(f"📖 Episode聚合: {qq_id} → {created}个事件")
        return created

    # ════════════════════════════════════════════════════════════
    # R3-2: 社交关系搜索
    # ════════════════════════════════════════════════════════════

    def search_relation_triples(self, name: str, limit: int = 5,
                                subject_qq: str = "",
                                source_group_id: str | None = None) -> list[str]:
        """搜索授权主体的可信关系记忆，不从自由文本切换数据主体。"""
        candidates = []
        seen_values = set()
        authorized_qq = str(subject_qq or "").strip()
        if not authorized_qq:
            exact_matches = {
                qq_id for qq_id, alias in self.store.search_aliases(name)
                if alias == name
            }
            if name.isdigit() and len(name) >= 5:
                exact_matches.add(name)
            if len(exact_matches) != 1:
                return []
            authorized_qq = exact_matches.pop()

        mems = self.recall(
            authorized_qq,
            limit=None,
            trusted_only=True,
            source_group_id=source_group_id,
            # ⚠ limit=None 取回的是**全量**记忆：这里若走提取强度回写，等于把该
            # 主体名下所有记忆一次性 importance+1。关系检索是**筛选**，不是「想起」。
            reinforce_after=False,
        )
        relation_words = (
            "室友", "朋友", "同学", "同事", "对象", "男朋友", "女朋友",
            "闺蜜", "兄弟", "拉了", "一起", "认识的",
        )
        # 关系词必须按完整分词命中，并排除紧邻否定词；简单子串会把
        # “不是朋友/不想一起”误报成正向关系。保留 token 位置以处理
        # “我不是朋友，但他是朋友”这类同句正负并存的情况。
        try:
            import jieba as _jieba
        except Exception:
            _jieba = None
        negations = ("不是", "并非", "不算", "没有", "无", "不想", "拒绝", "否认", "从未")

        def _positive_relation_token(text: str) -> bool:
            if _jieba is None:
                return False
            try:
                tokenized = _jieba.tokenize(text)
            except Exception:
                return False
            for token, start, end in tokenized:
                if token not in relation_words:
                    continue
                prefix = text[max(0, start - 4):start]
                if any(prefix.endswith(negation) for negation in negations):
                    continue
                return True
            return False

        for memory in mems:
            value = memory.value[:80]
            if (
                memory.key in ("fact", "said", "relationship")
                and value not in seen_values
                and _positive_relation_token(value)
            ):
                seen_values.add(value)
                source = (
                    f"chat_log#{memory.evidence_ids}"
                    if memory.evidence_ids
                    else f"{memory.trust_level}#{memory.id}"
                )
                fact_time = _protocols.format_fact_time(memory.event_time or memory.timestamp)
                candidates.append(
                    f"[{memory.key} | {fact_time} | 置信度{float(memory.confidence):.2f} | "
                    f"来源 {source}] {value}"
                )

        return candidates[:limit]

    def count_people(self) -> int:
        return self.store.count_people()

