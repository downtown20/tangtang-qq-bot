# 糖糖记忆系统升级计划

> 学习来源：Nekro Agent 2.3.0 源码
> 制定日期：2026-08-02
> 核心原则：不改架构，在现有代码上填三个维度的坑——认知类型、生命周期、上下文质量

---

## 前置——现有数据流全貌

改之前必须清楚数据的完整路径。每一条记忆在糖糖系统里经过以下环节：

```
┌─ 写入端 ─────────────────────────────────────────────┐
│                                                        │
│  群聊消息 ──→ extract_semantic_memories() [LLM提取]      │
│             ──→ remember() → store.insert_memory()      │
│             ──→ 可选: set_embedding() [BGE向量化]       │
│                                                        │
│  画像合成 ──→ synthesize_profile()                     │
│             ──→ remember(type=profile_synthesis)        │
│             ──→ remember(type=fact_synthesis) × N       │
│                                                        │
│  自我记忆 ──→ remember_self()                          │
│             ──→ remember(qq_id=bot_qq, type=promise等) │
│                                                        │
│  整合     ──→ consolidate_memories() [去重+清理]       │
│                                                        │
└────────────────────────────────────────────────────────┘

┌─ 读取端 ─────────────────────────────────────────────┐
│                                                        │
│  群聊/私聊消息到达                                      │
│    └─→ recall_formatted(qq_id)                        │
│          ├─→ recall(qq_id, limit)                     │
│          │     ├─ BGE 语义粗筛 (top-50)                │
│          │     ├─ Reranker 精排 (top-20)               │
│          │     └─ importance + 时间衰减 终排            │
│          ├─→ get_or_create_person() → 人物速写          │
│          └─→ 按章节组装: 速写 + 画像 + 身份 + 核心 + 相关│
│                                                        │
│    └─→ 注入 handler.py llm_message 前缀                 │
│          "【关于xxx的记忆——自然地提及】\n{formatted}"     │
│                                                        │
│  自治循环                                              │
│    └─→ _check_autonomous_action()                     │
│          └─→ recall_formatted(bot_qq) → 自我记忆       │
│                                                        │
└────────────────────────────────────────────────────────┘
```

**关键发现**：写入端在 `extract_semantic_memories` 产出了 `type`/`importance`/`confidence`/`value` 四个字段，但 `remember()` 只存了 `key=type`、`value=value`、`importance=importance`。`confidence` 丢了。阅读端 `recall()` 只看 `key` 和 `importance`——不知道一条记忆的认知类型（是"发生过的事"还是"知道的事"），也不知道置信度。

**Nekro 的记忆流**：

```
DBChatMessage (原始消息，有ID，可追溯)
    │
    ▼
Consolidator (LLM提取) → DBMemParagraph
    ├── cognitive_type: EPISODIC | SEMANTIC
    ├── knowledge_type: fact | preference | experience | decision | ...
    ├── base_weight + half_life_seconds (可独立衰减)
    ├── anchor_msg_id (可追溯原始消息)
    ├── episode_id (可聚合为事件)
    └── embedding_ref → Qdrant (向量检索)

    ▼
EpisodeAggregator (确定性算法) → DBMemEpisode
    ├── 时间+来源聚类
    └── 阶段映射 (OPENING→DEVELOPMENT→CLIMAX→RESOLUTION)

    ▼
Retriever (语义检索+权重+配额)
    ├── Qdrant 向量搜索
    ├── DBMemRelation 关系图谱补充
    ├── DBMemEpisode 事件级补充
    ├── compute_effective_weight (读时衰减)
    └── _select_context_memories (类型配额)
```

---

## 行动计划——三阶段推进

```
Phase 1 [基础]: 认知类型 + 置信度传递 + 身份锚定
    ↓ (Phase 1 是后续所有改动的依赖)
Phase 2 [增强]: 差异化衰减 + 上下文配额 + 记忆休眠/苏醒
    ↓ (Phase 2 依赖 Phase 1 的 cognitive_type)
Phase 3 [进阶]: Episode 聚合 + 社交关系 + 驱动力×记忆交叉
    ↓ (Phase 3 依赖 Phase 1 的 episodic 数据和 Phase 2 的配额)
```

---

## Phase 1：认知类型体系 + 置信度传递 + 身份锚定

**目标**：给每条记忆打上"这是发生过的事"还是"这是知道的事实"的标签，把 LLM 提取时标注的置信度传递到存储和检索，修复提取时可能把糖糖的事记成别人的事的风险。

**依赖**：无。

### 1a. 提取端——加 cognitive_type 和身份锚定

**文件**：[agent/memory.py](agent/memory.py) `extract_semantic_memories()` (L762-853)

**改动内容**：

1. **身份锚定**：system prompt 开头加身份锚定句，替代当前的泛泛"精确的信息提取助手"：

```
你是小糖糖，一只猫娘。以下聊天记录是你的群友{nickname}和你们的对话。
你只提取{nickname}关于自己的陈述——不提取关于你的陈述、不提取角色扮演。
```

2. **认知类型字段**：在输出格式中加 `cognitive` 字段：

```json
{"type":"identity","cognitive":"semantic","value":"在北大读计算机","importance":8,"confidence":0.95}
{"type":"event","cognitive":"episodic","value":"上周去爬了香山","importance":5,"confidence":1.0}
```

3. **类型判定规则**（加到 prompt 里）：

```
cognitive 类型：
- episodic: 有时间锚点的具体事件、说过的话、做过的事。这些会随时间快速淡化
- semantic: 相对稳定的个人信息、偏好、身份、习惯。这些长期保留

示例：
- "上周去吃了火锅" → episodic（具体时间地点的事件）
- "喜欢喝咖啡" → semantic（稳定的偏好）
- "昨天说想学吉他" → episodic（具体某天的发言）
- "在广州读大学" → semantic（长期身份信息）
- "和张三一起打游戏" → episodic（具体事件）
- "室友是李四" → semantic（持续的关系信息）
```

4. **confidence 字段保留**：当前 confidence 在过滤后用完就扔了——提取函数返回 `{"type": ..., "value": ..., "importance": ..., "confidence": ...}`，但 `remember()` 调用时没传 confidence。需要在 `MemoryEntry` 和 `store.insert_memory()` 链路中传递。

**改动量**：~30 行 prompt 修改 + 字段透传逻辑

### 1b. 存储端——MemoryEntry 加字段 + store 适配

**文件**：[agent/memory.py](agent/memory.py) `MemoryEntry` dataclass (L18-29)

**改动内容**：

```python
@dataclass
class MemoryEntry:
    id: int = 0
    qq_id: str = ""
    key: str = ""              # 保持兼容，等于 type
    cognitive: str = "semantic" # 新增：episodic | semantic
    value: str = ""
    timestamp: str = ""
    importance: int = 3
    confidence: float = 0.7    # 新增：LLM 提取时的置信度
    origin: str = "extracted"  # 新增：explicit | inferred | summarized | manual
    effective: float = 0.0
    last_recalled: str = ""
    recall_count: int = 0
```

**数据库改动**：

`memories` 表加三列（SQLite ALTER TABLE，兼容已有数据）：

```sql
ALTER TABLE memories ADD COLUMN cognitive TEXT DEFAULT 'semantic';
ALTER TABLE memories ADD COLUMN confidence REAL DEFAULT 0.7;
ALTER TABLE memories ADD COLUMN origin TEXT DEFAULT 'extracted';
```

**文件**：[agent/store.py](agent/store.py) `insert_memory()` 及相关查询方法

**改动内容**：

- `insert_memory()` 签名加 `cognitive=""`, `confidence=0.7`, `origin="extracted"` 参数
- `query_memories()` 返回结果加这三个字段
- `search_memories_by_keyword()` 同
- `recall()` 中 `MemoryEntry` 构造时读取这三个字段

**⚠️**：先 `Read` store.py 的 `insert_memory()` 和 `query_memories()` 再精确改。

**改动量**：~50 行（MemoryEntry + store 读写 + recall 透传）

### 1c. 写入链路——从提取到存储的完整透传

**文件**：[agent/handler.py](agent/handler.py) 中调用 `extract_semantic_memories()` 和 `remember()` 的位置

**需要找到**：`_extract_semantic_memories` 被调用的代码路径，以及 `remember()` 的调用方式。

**改动内容**：

确保提取结果中的 `cognitive`、`confidence`、`origin` 字段被传递到 `remember()`：

```python
# 提取结果示例
items = await self.memory.extract_semantic_memories(...)
# items[i] = {"type": "preference", "cognitive": "semantic",
#             "value": "喜欢喝咖啡", "importance": 3, "confidence": 0.8}

for item in items:
    self.memory.remember(
        qq_id, item["type"], item["value"],
        importance=item["importance"],
        cognitive=item.get("cognitive", "semantic"),
        confidence=item.get("confidence", 0.7),
        origin="extracted",
    )
```

同时标记现有记忆来源：
- `extract_semantic_memories` 产出 → `origin="extracted"`
- `synthesize_profile` 产出 → `origin="summarized"`
- `/记忆 添加` 手动添加 → `origin="manual"`
- `remember_self` 产出 → `origin="self"`

**改动量**：~30 行（handler 调用处透传 + remember 签名适配）

### Phase 1 验证

```
pytest: 跑全部测试确认无回归

手动验证:
1. 启动糖糖
2. 群里发 "我喜欢喝咖啡" 和 "昨天去爬山了"
3. 等 ~15 条消息触发提取
4. 查看 memory.db → memories 表：
   - "喜欢喝咖啡" 的 cognitive = semantic
   - "昨天去爬山了" 的 cognitive = episodic
   - 两条的 confidence 和 origin 字段有值
5. /记忆 搜索 "咖啡" → 能查到
6. /记忆 搜索 "爬山" → 能查到（cognitive = episodic）
```

---

## Phase 2：差异化衰减 + 上下文配额 + 记忆休眠

**目标**：让记忆像人的记忆一样运作——事件快速淡化、事实长期保留；经常被提起的强化、从不提及的休眠；不同类型的记忆在上下文中都有代表。

**依赖**：Phase 1 完成（需要 cognitive_type 字段）。

### 2a. 差异化衰减

**文件**：[agent/memory.py](agent/memory.py) `recall()` (L92-175)，Phase 2 衰减计算部分 (L137-161)

**当前衰减**（统一）：

```python
# 7天内+1，30天后指数衰减，半衰期~70天，所有记忆一样
if age_days <= 7:
    score += 1.0
elif age_days > 30:
    score *= max(0.25, math.exp(-0.01 * (age_days - 30)))
```

**改动为**（按 cognitive 类型差异化）：

```python
# episodic 记忆——快速衰减（半衰 ~30天）
# semantic 记忆——慢速衰减（半衰 ~120天）
cognitive = r.get("cognitive", "semantic")

if age_days <= 7:
    score += 1.0
elif age_days > 30:
    if cognitive == "episodic":
        # 快速衰减：30天后开始衰减，半衰~30天
        score *= max(0.15, math.exp(-0.023 * (age_days - 30)))
    else:
        # 慢速衰减：保持现有参数，半衰~70天
        score *= max(0.25, math.exp(-0.01 * (age_days - 30)))

# episodic 检索加权：被检索到时额外加分
# (对话中"我们经历过的事"比"知道的事"更有连接感)
if cognitive == "episodic":
    score *= 1.2
```

**reinforce()** 也按类型差异化：

```python
def reinforce(self, qq_id: str, memory_entries: list):
    for m in memory_entries:
        # episodic 记忆命中后强化更多（反复回忆的事件不容易忘）
        boost = 0.3 if getattr(m, 'cognitive', 'semantic') == 'episodic' else 0.15
        ...
```

**改动量**：~25 行（recall 衰减分支 + reinforce 差异化）

### 2b. 上下文注入配额制

**问题**：当前 `recall_formatted()` 按分数排序后取前 N 条。如果一个人的 facts 类记忆特别多，注入的全是 facts。events（尤其是 episodic 记忆）被挤掉。

**文件**：[agent/memory.py](agent/memory.py) `recall_formatted()` (L244-334)

**改动内容**：

在现有按章节组织的结构上，加入认知类型配额：

```python
# 在 recall_formatted() 的最前面，all_memories 拿到后：

# 按 cognitive 类型分桶
episodic_mems = [m for m in all_memories if getattr(m, 'cognitive', 'semantic') == 'episodic']
semantic_mems = [m for m in all_memories if getattr(m, 'cognitive', 'semantic') == 'semantic']

# 配额：最多 3 条 episodic + 5 条 semantic + 2 条 profile/profile_synthesis
episodic_for_context = sorted(episodic_mems[:6],
    key=lambda m: getattr(m, 'effective', 0), reverse=True)[:3]
semantic_for_context = sorted(semantic_mems[:10],
    key=lambda m: getattr(m, 'effective', 0), reverse=True)[:5]

# 然后按现有逻辑分类到各章节
all_memories = episodic_for_context + semantic_for_context
# 保持 effective 排序，后续逻辑不变
all_memories.sort(key=lambda m: getattr(m, 'effective', 0), reverse=True)
```

**配额设计**：

| 类型 | 最多 | 原因 |
|------|------|------|
| episodic | 3 | "我们经历过的事"有连接感但不宜多——多了变成罗列 |
| semantic (fact/identity) | 3 | 身份和事实提供话题基础 |
| semantic (preference/habit) | 2 | 偏好是话题钩子 |
| profile_synthesis | 不限（已有去重） | 合成画像帮助 LLM 理解整体 |
| self (bot_qq 的记忆) | 3 | 糖糖自己的承诺/观点 |

**文件**：[agent/handler.py](agent/handler.py) 群聊记忆注入 (L2788-2794) 和私聊记忆注入 (L3464-3470)

**改动**：不需要改 handler。`recall_formatted` 返回的文本已经包含了配额控制——handler 只负责注入。

**改动量**：~40 行（recall_formatted 开头加配额逻辑）

### 2c. 记忆休眠与苏醒

**问题**：有些记忆存了之后再也不被检索到（不是不重要——只是没有被"触发"过）。但目前的衰减是一视同仁的——30 天没被 recall 的记忆和 30 天内被 recall 了 5 次的记忆衰减速度一样。

**Nekro 的做法**：`is_inactive` 标志——权重低于阈值的不参与检索。`reinforce()` 在每次检索命中时提升权重。

**文件**：[agent/memory.py](agent/memory.py) `recall()` (L92-175) 和 `reinforce()` (L177-189)

**改动内容**：

1. 在 `recall()` 的终排中加入"休眠检测"：

```python
# 长期未被回忆的记忆额外降权
if hasattr(r, 'last_recalled') and r.get('last_recalled'):
    try:
        last = datetime.strptime(str(r['last_recalled'])[:10], "%Y-%m-%d")
        dormant_days = (now - last).days
        if dormant_days > 60:
            score *= 0.5   # 两个月没被想起——降权
        elif dormant_days > 30:
            score *= 0.7   # 一个月没被想起——轻微降权
    except:
        pass
```

2. `reinforce()` 加苏醒逻辑：

```python
# 被 recall 且用到的记忆——苏醒
# +1 importance（已实现）
# 更新 last_recalled（已实现）
# 如果之前是 dormant，这里自动被唤醒
```

3. 可选：`cleanup_stale_memories()` 加休眠条件：

```python
# 现有: importance <= 2 且从未被回忆 且 >90天 → 删除
# 改进: importance <= 2 且 dormant >180天 → 删除
#       importance <= 1 且 dormant >90天 → 删除
```

**改动量**：~30 行（recall 休眠降权 + cleanup 调整）

### Phase 2 验证

```
手动验证:
1. 启动糖糖（Phase 1 已运行，有 cognitive 字段的记忆积累）
2. 查一个熟人 → /记忆 → 看返回是否包含 episodic 和 semantic 两类
3. 群聊中提之前经历过的具体事件 → 糖糖能否接话
4. 检查一条 60 天未被 recall 的 episodic 记忆权重是否低于同级 semantic 记忆
5. 反复聊同一个话题 → 相关记忆 importance 是否逐步上升（强化生效）
```

---

## Phase 3：Episode 聚合 + 社交关系 + 驱动力×记忆

**目标**：让糖糖的记忆不只是散落的句子——有事件级的结构、有人际间的关联、能驱动她的主动行为。

**依赖**：Phase 1 完成（episodic 数据可用），Phase 2 完成（配额保证了 episodic 能被注入）。

### 3a. Episode 结构化记忆

**设计**：借鉴 Nekro 的 EpisodeAggregator——确定性算法（不调 LLM），利用 `conversation_tracker` 已有的对话窗口作为 episode 的自然边界。

**文件**：[agent/memory.py](agent/memory.py) 新增 `_aggregate_episodes()` 方法

**数据表**（新增，轻量）：

```sql
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qq_id TEXT NOT NULL,           -- 主角 QQ
    title TEXT DEFAULT '',         -- "和张三聊到火锅的那次"
    summary TEXT DEFAULT '',       -- 简要概括
    time_start TEXT NOT NULL,      -- 开始时间
    time_end TEXT NOT NULL,        -- 结束时间
    paragraph_ids TEXT DEFAULT '', -- 关联的 memories.id（逗号分隔）
    feeling TEXT DEFAULT ''        -- 糖糖的整体感受（可选）
);
```

**算法**：

```
输入：qq_id 的 episodic 记忆（按 event_time 排序）
      以 conversation_tracker 的对话窗口为分组依据

算法：
1. 取 qq_id 的所有 episodic 记忆，按 timestamp 排序
2. 相邻记忆时间差 < EPISODE_GAP_MINUTES(60分钟) → 划入同一 episode 候选组
3. 每组 >= EPISODE_MIN_PARAGRAPHS(3)条 → 创建 episode
4. 标题 = 第一段记忆截断 + "之类的事"
5. 摘要 = 拼接前3段记忆（每条≤40字）
6. 关联 paragraph_ids

复杂度：O(n log n)，纯 Python，不调 LLM
```

**检索端集成**：

`recall_formatted()` 加一节：

```python
# ---- 相关事件 ----
episodes = self.store.query_episodes(qq_id, limit=2)
if episodes:
    episode_lines = []
    for ep in episodes:
        episode_lines.append(f"📖 {ep['title']}（{ep['time_start'][:10]}）：{ep['summary'][:60]}")
    lines.append("  事件：" + " | ".join(episode_lines))
```

**改动量**：~120 行（新表 + 聚合算法 + recall_formatted 集成）

### 3b. 社交关系三元组

**设计**：在 `extract_semantic_memories` 的 `relationship` 类型提取中，显式输出三元组格式。存储到 `fact_clusters` 表（已有 `subject_qq` 字段）。

**文件**：[agent/memory.py](agent/memory.py) `extract_semantic_memories()` prompt

**prompt 改动**：在 relationship 类型的定义中加三元组格式：

```
- relationship: 人际关系。不仅是用户和谁的关系——也包括用户提到的其他人的关系
  格式：{"type":"relationship","cognitive":"semantic",
         "value":"张三是李四的室友","importance":6,"confidence":0.9,
         "triple":{"subject":"张三","predicate":"室友","object":"李四"}}
```

**检索端**：新增 `search_relation_triples(qq_id_a, qq_id_b)` 方法——查询两个人之间是否有已知关系。在 handler 中，当对话中提到另一个人时，调用此方法。

**改动量**：~60 行（prompt + triple 解析 + 检索方法）

### 3c. 驱动力 × 记忆交叉

**设计**：当前自治循环 `_check_autonomous_action()` 选群的标准是"最近有活动的群"——纯机械。加上记忆驱动的定向选择。

**文件**：[agent/handler.py](agent/handler.py) `_check_autonomous_action()` 和 `_pick_initiative_group()`

**改动内容**：

在决定主动说话时，不只是看"哪个群最近活跃"，而是扫描记忆找到"有什么可以说的"：

```python
async def _find_initiative_topics(self) -> list[dict]:
    """扫描所有活跃群友的记忆，找到适合主动提起的话题。
    
    返回: [{"qq_id": "...", "group_id": "...", "topic": "承诺未完成", "detail": "..."}, ...]
    """
    candidates = []
    
    # 1. 未完成的承诺（承诺压力高的驱动优先消解）
    unkept_promises = self.memory.recall(self.bot_qq, limit=5)
    for m in unkept_promises:
        if getattr(m, 'key', '') == 'promise':
            # 这条承诺是给谁的？去对应的群说
            ...
    
    # 2. 最近认识的人的 episodic 记忆——适合自然接话
    #    "李四上次说想学钢琴，问问学得怎么样了"
    recent_episodes = [m for m in self.memory.recall(...) 
                      if getattr(m, 'cognitive', '') == 'episodic']
    
    # 3. 情绪低落的人——社交渴望 + 关心驱动
    ...
```

**但实际上**——这个改动的最简单形式不是改 `_pick_initiative_group`。最简单的是：给自治循环的 LLM prompt 里注入**候选话题**，让 LLM 决定。就像现在的驱动力数值和存在状态被注入 prompt 一样——加一个"你可能想聊的事"列表。

```python
# 在 _check_autonomous_action 构建 prompt 时：
topics_context = await self._build_initiative_topics_context()
# → "你可能想聊的事：
#   - 张三(群833475319)上次说想去爬山——可以问ta去了没
#   - 李四(群1085662710)答应帮你查资料——可以问查到了吗
#   （你不需要每条都提，选你最有感觉的说）"

system_prompt = f"{base_prompt}\n\n{topics_context}"
```

**改动量**：~80 行（话题扫描 + prompt 注入 + 选群逻辑微调）

### Phase 3 验证

```
Episode:
1. 和一个群友连续聊同一个话题（3轮以上）
2. 等 ~60 分钟后检查 episodes 表是否有新 episode 生成
3. /记忆 → 看返回是否包含"📖 事件"章节

社交关系：
4. 群里有人说"张三是李四的室友"
5. 新消息中提到"张三"时，相关记忆是否包含与李四的关系

驱动力×记忆：
6. 糖糖主动说话时，是否选择了有"可聊话题"的群
7. 糖糖主动说话的内容是否自然地提到了之前的互动
```

---

## 改动总览

| 阶段 | 改动项 | 文件 | 行数 | 风险 |
|------|--------|------|------|------|
| **P1a** | 提取端加 cognitive_type + 身份锚定 | memory.py | ~30 | 低（prompt 修改） |
| **P1b** | MemoryEntry + store + recall 加字段 | memory.py, store.py | ~50 | 中（改数据结构） |
| **P1c** | 写入链路透传 cognitive/confidence/origin | handler.py, memory.py | ~30 | 低（参数透传） |
| **P2a** | 差异化衰减 | memory.py | ~25 | 低（改衰减公式） |
| **P2b** | 上下文配额制 | memory.py | ~40 | 低（改 recall_formatted） |
| **P2c** | 记忆休眠与苏醒 | memory.py | ~30 | 低（改 recall + reinforce） |
| **P3a** | Episode 聚合 | memory.py (+ store) | ~120 | 中（新表+新逻辑） |
| **P3b** | 社交关系三元组 | memory.py, handler.py | ~60 | 低（prompt + 轻量索引） |
| **P3c** | 驱动力×记忆交叉 | handler.py | ~80 | 中（涉及自治循环） |
| **合计** | | | **~465** | |

---

## 实施顺序与分批

**第一批**（Phase 1 全部，~110 行）：
- 1a + 1b + 1c 可以一起做——它们是同一数据流的相邻环节
- 做完后认知类型和置信度开始正常流转
- 建议在一个会话内完成，跑 pytest 验证

**第二批**（Phase 2 全部，~95 行）：
- 依赖第一批的 cognitive 字段有数据
- 2a、2b、2c 互不依赖，可以一起做
- 建议在第一批上线运行至少 1 天后做（积累足够的 cognitive 标签数据）

**第三批**（Phase 3 全部，~260 行）：
- 3a 依赖 episodic 数据积累
- 3c 依赖 3a 的 episode 存在
- 建议在前两批上线运行至少 3 天后做

---

## 不做的事——以及为什么

| 不做的 | 原因 |
|--------|------|
| 引入 Qdrant/PostgreSQL | 糖糖只有一个人用，SQLite + BGE 向量够用。Nekro 需要 Qdrant 是因为多租户+大数据量 |
| 完整的 Entity 表 | 糖糖的"人物"已经在 `people` 表里有完整追踪（昵称、属性、关系）。不需要另起 Entity 模型 |
| reinforcement_log 表 | 糖糖的 `recall_count` + `last_recalled` 字段已经够追踪使用频率。不需要单独的日志表 |
| 完整的记忆重建（rebuild） | Nekro 需要重建是因为记忆可能跨多个维度变化。糖糖的记忆总量小，consolidate_memories 够用 |
| Jinja2 模板化 Prompt | 糖糖只有中文、只有一种人格。字符串拼接的灵活性已经足够 |
| half_life_seconds 字段级衰减控制 | 糖糖只有两种认知类型——用 if 分支就够了，不需要每条记忆存一个独立的半衰期 |
| Docker 沙盒 | 糖糖的 LLM 不写代码，不需要隔离执行环境 |
