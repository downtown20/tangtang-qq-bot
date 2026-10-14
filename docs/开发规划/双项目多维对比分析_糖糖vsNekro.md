# 糖糖 vs Nekro Agent 多维对比分析

> 对比版本：小糖糖 (2026-08-02) vs Nekro Agent 2.3.0
> 目的：不是评判谁好谁坏——是理解两个项目在不同维度上的选择及其代价，
>        找出糖糖在每个维度上可以实质性改进的点。
> 方法：每个维度先分别描述两边的做法，再交叉对比，最后提出改进建议。
> 2026-08-14 更新：吸收《Nekro_Agent_2.3.0_学习报告》精华作为附录（原文已归档至 docs/开发规划/归档/）。
>        正文各维度建议追加「当前状态（2026-08-14）」标注；交叉对比表格仍以 2026-08-02 写作时状态为准。

---

## 维度一：架构与框架设计

### 糖糖的做法

**单体架构**。一个 `handler.py` (7263 行) 是中枢神经，直接或间接 import 了 42 个模块。模块之间通过 handler 实例互相访问——`self.memory`、`self.self_state`、`self.scenarios` 挂在 handler 上，其他模块需要时通过 `self.handler.xxx` 回调。已拆出 handler_commands.py、handler_autonomy.py 两个模块。

```
handler.py (中枢)
  ├── memory.py         → self.memory
  ├── self_state.py     → self.self_state
  ├── personality.py    → self.personality
  ├── context_builder.py → ContextBuilder(self.self_state)
  ├── drives.py         → self.self_state.drives
  ├── mood.py           → self.mood_engine
  ├── skills.py         → @register_skill 全局注册表
  ├── store.py          → self.memory.store (SQLite 唯一入口)
  ├── voice.py, sticker.py, knowledge.py, ...
  └── ... (共 42 个 import)
```

**模块加载**：手动初始化。`handler.__init__` 里按顺序创建各模块实例，顺序很重要（如 MemorySystem 必须在 MoodEngine 之前，因为 MoodEngine 依赖 memory.store）。

**数据流向**：消息到达 → handler 决定如何处理 → 构建 prompt → 调用 LLM → 执行工具 → 构建回复 → 发送。每条消息是一条独立的处理链，自治循环是额外的定时任务。

### Nekro 的做法

**服务层架构**。`services/` 下有 agent、plugin、sandbox、memory、message、chat、config、timer、command 等子目录。各服务之间通过导入通信，不依赖中心调度器。

```
services/
  ├── agent/         # LLM 调用 + prompt 编译 + 代码解析
  │   └── run_agent.py  → 核心循环
  ├── plugin/        # 插件加载 + 生命周期 + 方法收集
  │   ├── collector.py → 扫描/加载/启用/禁用
  │   └── base.py      → NekroPlugin 基类
  ├── sandbox/       # Docker 容器管理 + RPC 代理
  ├── memory/        # 14个文件，记忆全生命周期
  ├── message/       # 消息广播 + 推送
  ├── config/        # 配置管理服务
  ├── timer/         # 定时任务
  └── command/       # 命令系统
```

**插件系统**：所有业务能力通过插件注册。内置插件（`plugins/builtin/`）和外部插件（`plugins/` 目录 + 云端）统一由 `PluginCollector` 管理。每个插件是独立的 `NekroPlugin` 实例，通过装饰器注册沙盒方法、提示词注入、回调等。

**适配器层**：`adapters/` 下 9 个平台适配器，各自实现 `BaseAdapter` 抽象接口。核心引擎不关心消息来自哪个平台——统一为 `AgentCtx` + `ChatMessage`。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 架构风格 | 单体中枢 | 服务层 + 插件 + 适配器 |
| 模块通信 | handler 属性传递 | 服务间直接 import |
| 能力注册 | `@register_skill` 全局表 | `@plugin.mount_sandbox_method()` 插件级 |
| 平台抽象 | 无（只支持 QQ/NapCat） | 9 个适配器，统一接口 |
| 初始化 | 手动顺序创建 | 插件自动扫描 + 生命周期回调 |
| 代码组织 | 平坦（agent/ 下 25+ .py 文件） | 分层（services/agent/plugin/sandbox/memory/...） |

**各自优势**：

糖糖的单体架构——**对于一个特定目标的项目来说，这是对的**。没有插件市场、没有多平台、没有多用户。所有模块可以互相直接访问，不需要抽象层。添加新功能就是在 handler 里加一个方法，在相应的模块里加逻辑。快，直接。

Nekro 的服务层架构——**对于一个通用平台来说，这是必须的**。如果不把插件系统和核心引擎解耦，任何人都加不了自定义能力。如果不把适配器抽象出来，每加一个平台都要改核心代码。

**各自的问题**：

糖糖的问题：handler.py 7263 行（拆分进行中——已拆出 handler_commands.py、handler_autonomy.py，handler_tools.py / handler_groups.py 尚未拆出）。当你想知道"糖糖收到一条群消息后发生了什么"，需要从 handler 的一个方法跳到另一个方法，每个方法再调 5-10 个其他模块的方法。模块之间的依赖是隐式的——通过 `self.handler.xxx` 回调和 `self.memory` 属性访问。**新人（包括 3 个月后的你自己）接手时，需要读完 handler 的初始化顺序才能理解模块之间的关系。**

Nekro 的问题：9 个适配器、完整的插件生命周期、Docker 沙盒、Qdrant 向量库——这些对单人多 bot 场景来说严重过度设计。100KB 的 config.py 定义了几百个配置项，其中大部分对简单场景没有意义。

### 对糖糖的改进建议

**不改架构，但做三件事让架构更清晰**：

**1. 模块清单 + 依赖图（文档级，0 行代码）**

在 CLAUDE.md 里维护一张表：

```
模块            | 依赖              | 被依赖方
memory.py       | store.py          | handler, context_builder, self_state
self_state.py   | 无                | handler, context_builder
drives.py       | self_state        | handler (自治循环 + 内在消化)
personality.py  | 无                | handler, context_builder
context_builder | self_state        | handler
```

这不是给 Python 看的——是给人看的。每次新增模块时更新这张表。

**2. handler.py 拆分——不是拆框架，是拆文件**

当前最大的问题不是架构——是 handler.py 太大。7263 行一个文件，里面混了消息处理、命令处理、工具执行、自治循环、内在消化、群管理。这些逻辑之间没有强耦合——它们只是恰好都在 handler.py 里。

拆法：
```
handler.py          → ~3000 行（核心：消息路由 + LLM 调用 + 生命周期）
handler_commands.py → ~1500 行（所有 /命令 处理器）
handler_tools.py    → ~1500 行（Tool Calling 执行 + 定义构建）
handler_autonomy.py → ~1200 行（自治循环 + 内在消化 + 驱动力释放）
handler_groups.py   → ~1000 行（群管理 + 黑名单 + 欢迎 + 定时任务）
```

Handler 类本身保留——拆分的是方法。每个文件定义 Handler 的一个 mixin 或直接在 Handler 类上 monkey-patch。

**3. 初始化顺序文档化**

当前 handler.__init__ 的顺序是靠人工记忆维护的。加一个简单的检查：每个模块初始化后注册自己的依赖是否已满足，不满足则 warn。

这三件事不改变架构，不增加抽象层，不创建新框架——只让现有的东西更容易理解。

**当前状态（2026-08-14）**：第 2 件部分完成——handler_commands.py、handler_autonomy.py 已拆出，handler_tools.py、handler_groups.py 未拆，handler.py 当前 7263 行；第 1 件（模块清单 + 依赖图文档化）、第 3 件（初始化顺序文档化 + 依赖检查）均未实施。

---

## 维度二：人格/角色系统

### 糖糖的做法

**深度人格化**。糖糖不是"一个友好的 AI 助手"——她是一只特定的猫娘，有自己的三观、自己的记忆、自己对不同人的感觉。

```
role_card.md (~42行)          ← 人格定义的核心
    ↓
personality.py build_system_prompt()  ← 运行时组装
    ├── 场景引擎 (scenario.py)         ← overlay/replace 两种模式
    ├── 情绪引擎 (mood.py)             ← 精力/心情/耐心 三维模型
    ├── 关系层级 (relationship tiers)  ← close/familiar/stranger 三档
    ├── 自我叙事 (self_state.py)       ← "我是谁"的动态认知
    └── 群风适配 (group_style.py)      ← 每个群不同的氛围
```

**关键设计**：
- `_cached_base` / `_cached_minimal` 双缓存——静态人格部分不每次重建
- 场景引擎 overlay（追加敏感度）和 replace（替换人格）两种模式
- 关系层级决定回复的自然度——close 做自己，familiar 更放松，stranger 友好但有距离
- 群风学习自动适配每个群的说话方式
- 自我叙事来自反思循环——不是静态设定，是从互动中提炼

### Nekro 的做法

**通用角色系统**。人格是"预设（Preset）"——一段文本，存在数据库里，不同频道可以绑定不同的预设。

```
DBPreset (数据库模型)
  ├── title: str          # 人设名称
  ├── content: str        # 人设内容（纯文本）
  └── ...

PersonaPrompt(chat_preset=preset.content)  ← Jinja2 模板渲染
```

**关键设计**：
- 人设是一段可替换的文本——换一个频道换一个人设
- 没有"关系层级"的概念——对所有人一样
- 没有"自我叙事"——Agent 不知道"自己是谁"，只执行预设
- 没有情绪/群风——纯静态

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 人格深度 | 🟢 深——三观、关系感、自我叙事 | 🔴 浅——一段可替换文本 |
| 人格稳定性 | 🟢 稳定——role_card 是锚，反思是微调 | 🟢 稳定——静态预设 |
| 对不同人的差异 | 🟢 关系层级 + 关系场数值 | 🔴 所有人一样 |
| 自我认知 | 🟢 自我叙事 + 驱动力 + 反思 | 🔴 Agent 不认知自己 |
| 多角色支持 | 🔴 不支持——糖糖只有一个身份 | 🟢 多角色，工作区隔离 |
| 换人设 | 🔴 需要改 role_card + /人格 重载 | 🟢 WebUI 选一个就行 |

### 分析

**这是糖糖最不需要从 Nekro 学习的维度——因为糖糖在这一维度上的深度远超 Nekro。** 

Nekro 的角色系统是为"一个平台服务多个 Agent"设计的——每个群可以有不同的人设，轻松切换。糖糖的角色系统是为"一个特定的存在持续深化"设计的——不变身份但越来越了解自己。

但这不是说糖糖的人格系统完美。有一个结构性弱点：

**`build_system_prompt()` 参数太多且耦合紧**。看签名：

```python
def build_system_prompt(
    self, relationship, context="", memories="", topic_memories="",
    group_vibe="", knowledge="", intimacy=0, active_members="",
    power_structure="", scenario=None,
) -> str:
```

10 个参数。每次要加一个新的上下文段落（比如 Phase 3 的 episode 摘要），就要加一个新参数。这就是 Nekro 的 Jinja2 模板解决的问题——模板定义"有哪些插槽"，各模块只负责填自己的插槽，不需要修改组装函数签名。

### 对糖糖的改进建议

**不需要动人格系统的核心设计。只改组装方式。**

当前：
```python
# personality.py
base += f"\n\n{self._get_time_context()}"
base += f"\n\n{mood_block}"
base += f"\n\n## 关系\n{tier}"
base += f"\n\n{power_structure}"
base += f"\n\n## 这个群的风格\n{group_vibe}"
```

改为 Jinja2 模板：
```jinja2
## 你现在的状态
{{ time_context }}
{{ mood_context }}

## 你和这个人的关系
{{ relationship_tier }}

{% if power_structure %}
{{ power_structure }}
{% endif %}

{% if group_vibe %}
## 这个群的风格
{{ group_vibe }}
{% endif %}
```

收益：加新段落不需要改 `build_system_prompt` 签名——只在模板里加 `{{ new_thing }}` 和传一个变量。

**但这个问题不紧急**。当前 10 个参数还在可维护范围内。等到参数超过 15 个或每次改 prompt 都要动 3 个文件的时候再做。

---

## 维度三：记忆系统

### 糖糖的做法

详细分析见前文。概括：

**五层记忆模型**（从提取到使用）：

```
短期缓冲 (short_term dict)     → 群聊上下文窗口
    ↓ (extract_semantic_memories, LLM)
长期记忆 (memories 表)         → BGE + Reranker 检索
    ↓ (synthesize_profile, LLM)
合成画像 (people.notes)        → 人物速写
    ↓ (consolidate_memories, LLM)
记忆整合                        → 去重 + 清理
    ↓ (反思循环 Phase D, LLM)
自我叙事 (TangTangSelf)        → 糖糖对自己的认知
```

**关键数据流**：
- 提取：`extract_semantic_memories()` 每 12-15 条消息触发 → `remember()` → SQLite
- 检索：`recall()` 三阶段（BGE → Reranker → importance+衰减）
- 注入：`recall_formatted()` → 注入 handler.py llm_message 前缀
- 画像：`synthesize_profile()` → `people.notes` + `fact_synthesis` 记忆
- 整合：`consolidate_memories()` → 合并类似记忆 + 清理低价值
- 自我：`remember_self()` → bot_qq 的记忆独立一条线

### Nekro 的做法

详细分析见前文。概括：

**四层结构化记忆**：

```
DBMemParagraph (最小单元)
  ├── cognitive_type: EPISODIC | SEMANTIC
  ├── knowledge_type: fact | preference | experience | decision | ...
  ├── 衰减: base_weight × half_life_seconds
  └── 向量: Qdrant embedding_ref

DBMemEntity (实体) + DBMemRelation (关系三元组)
  └── subject_entity - predicate - object_entity

DBMemEpisode (事件聚合)
  ├── 确定性时间+来源聚类
  └── 阶段映射: OPENING→DEVELOPMENT→CLIMAX→RESOLUTION

DBMemReinforcementLog (强化追踪)
  └── 每次检索命中记录 → 用于反向强化
```

### 交叉对比

| 维度 | 糖糖 | Nekro | 谁更好 |
|------|------|------|--------|
| 认知类型区分 | ❌ 无 (key 混用) | ✅ EPISODIC/SEMANTIC | Nekro |
| 知识类型细分 | 🟡 有 (like/hate/fact/event等) | ✅ fact/preference/experience/decision/emotion | Nekro |
| 衰减机制 | 🟡 统一衰减 (7天+1, 30天指数) | ✅ 每记忆独立半衰期 + 冻结 | Nekro |
| 检索精度 | ✅ BGE+Reranker 双阶段 | 🟡 Qdrant 单阶段 | 糖糖 |
| 上下文编排 | 🔴 按分数截断 | ✅ 类型配额制 | Nekro |
| 关系知识 | 🟡 数值向量 (closeness/trust) | ✅ 结构化三元组 | 互补 |
| 事件聚合 | 🔴 无 | ✅ Episode 确定性聚合 | Nekro |
| 记忆出处 | 🔴 置信度提取后丢弃 | ✅ anchor_msg_id 追溯 | Nekro |
| 检索强化 | 🟡 +1 importance | ✅ reinforce(boost=0.3) + 日志 | 相当 |
| 画像合成 | ✅ synthesize_profile + KEY_FACTS | 🔴 无 | 糖糖 |
| 自我记忆 | ✅ remember_self + 独立上下文注入 | 🔴 Agent 不记自己 | 糖糖 |
| 记忆整合 | 🟡 consolidate 去重 | 🟡 scheduler 衰减+失活+重建 | 各有侧重 |
| 存储引擎 | 🟡 SQLite (一人用足够) | ✅ PostgreSQL + Qdrant (多租户) | Nekro (但不适用于糖糖) |

### 分析

**糖糖的记忆系统在"关系性"维度上更好**——画像合成、自我记忆、关系场数值向量。这些 Nekro 没有。

**Nekro 的记忆系统在"结构性"维度上更好**——认知类型区分、知识类型细分、Episode 聚合、配额编排。这些糖糖没有。

**为什么这个差距存在？** 因为两个项目的记忆目标不同：
- 糖糖的记忆是为了"让糖糖和这个人的关系更真实"——所以有画像合成（理解这个人）、自我记忆（知道自己说过什么）、关系场（感知和每个人的距离）
- Nekro 的记忆是为了"让 Agent 准确检索信息"——所以有认知类型区分（衰减不同）、知识类型细分（检索过滤）、Episode 聚合（事件级检索）、配额编排（注入平衡）

**两种目标都是合理的。糖糖同时需要两者**——她既要有关系感，也要能准确检索。这就解释了为什么记忆系统是这次学习中最值得投入的维度。

### 对糖糖的改进建议

见前文《记忆系统升级计划》。此处不重复，只强调一点：

**记忆系统的改进是这次学习中回报最高的投入**。原因是：
1. 它直接提升糖糖"像人"的程度——事件记忆让她能接话，类型配额让对话不干瘪
2. 改动都在现有架构内部——不需要新框架、不需要新数据库
3. 每一步独立可验证——不依赖其他维度的改动

**当前状态（2026-08-14）**：认知类型配额（memory.py R2-1）、Episode 聚合（memory.py `_aggregate_episodes` + store.py episodes 表）、cognitive_type 区分（memory.py cognitive 字段 episodic/semantic）、记忆休眠/衰减降权（memory.py R2-2）均已实现；社交关系三元组未实现（store.py 无 relations 表）——关系知识仍为数值向量（closeness/trust）。

---

## 维度四：工具/能力系统

### 糖糖的做法

**`@register_skill` 全局注册表** ([skills.py](agent/skills.py)，92 行)。

```python
@register_skill("web_search", "搜索互联网", {"query": "搜索关键词"})
async def web_search(query: str): ...

# 自动转为 Tool Calling 定义
def build_tool_definitions() -> list[dict]:
    # → OpenAI function calling 格式
```

**注册的技能**：`calculate`、`convert`、`get_time`、`get_weather`、`web_search`、`translate`、`draw`、`draw_divination_lot`、`draw_tarot`、`post_moment`、`share_image`

**额外工具**（在 handler.py 中手工构建）：
- 记忆工具：`search_facts`、`search_memories`、`get_profile`
- 语音工具：`send_voice`
- 唱歌：`sing`
- 表情包：`send_sticker`
- 群管理工具

**两套系统并存**：`skills.py` 注册的通过 `build_tool_definitions()` 转为 Tool Calling 定义，handler.py 手工构建的通过 `_build_memory_tools()` 等函数追加。最终合并到一个 tools 列表传给 LLM。

### Nekro 的做法

**沙盒方法四分类**。插件中的 `@plugin.mount_sandbox_method(type, name)` 注册的方法在沙盒里被 AI 生成的代码调用。方法类型决定调用后的行为。

```python
class SandboxMethodType(str, Enum):
    TOOL = "tool"            # 纯工具，返回值 LLM 直接用
    AGENT = "agent"          # 返回值注入上下文，触发 LLM 再思考
    BEHAVIOR = "behavior"    # 副作用操作，不触发 LLM 再思考
    MULTIMODAL_AGENT = ...   # 多模态返回，触发再思考
```

方法通过 RPC（pickle + HTTP）在沙盒和主进程之间传递。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 注册方式 | `@register_skill` 装饰器 | `@plugin.mount_sandbox_method(type)` |
| 工具定义构建 | 全局遍历注册表 → OpenAI format | 插件级收集 → 注入沙盒代码 |
| 工具调用方式 | LLM → Tool Calling → handler 执行 → 返回 | LLM 生成代码 → 沙盒执行 → RPC → 返回 |
| 返回值处理 | 统一注入上下文 | 按方法类型 (TOOL/AGENT/BEHAVIOR) 区分 |
| 工具来源 | 全局注册表 + handler 手工构建 | 插件注册 + 内置沙盒方法 |
| 工具数量 | ~15 个 | 取决于加载的插件（~30+） |

### 分析

**两边的工具注册方式本质相同**——装饰器注册 + 自动生成描述。糖糖的 `@register_skill` ≈ Nekro 的 `@plugin.mount_sandbox_method`。

**核心差异在两个地方**：

**1. 方法类型区分**。糖糖不区分——所有 Tool Calling 返回值一视同仁地注入上下文。Nekro 用四种类型控制 LLM 是否再思考。

这导致了一个实际浪费：糖糖调用 `send_voice` 发完语音后，LLM 会收到返回值并再想一轮"发完了，现在该说什么"。而实际上发语音是副作用操作，不需要再思考。

**2. 两套系统并存**。糖糖的 `skills.py` 和 `handler.py` 各维护一半的工具定义。`search_facts`、`send_voice` 等由于需要访问 handler 的属性，无法用 `@register_skill` 注册——只能在 handler 里手工构建。如果一个工具被加了两次，LLM 看到两个定义，行为不可预测。

### 对糖糖的改进建议

**1. 工具方法类型区分**（P0，~30 行）

`@register_skill` 加 `method_type` 参数：

```python
@register_skill("send_voice", "发送语音", {"text": "..."}, method_type="behavior")
@register_skill("web_search", "搜索互联网", {"query": "..."}, method_type="agent")
@register_skill("calculate", "计算数学表达式", {"expr": "..."}, method_type="tool")
```

handler 执行工具后根据 method_type 决定是否触发 LLM 再处理。

**当前状态（2026-08-14）**：已实现——skills.py `Skill.method_type`（tool | agent | behavior，默认 agent）。

**2. 统一工具注册入口**（P1，~50 行）

让 handler.py 的工具也能走 `@register_skill`：

```python
# handler.py 中
def _register_handler_tools(self):
    """把需要访问 handler 属性的工具也注册到 skills 系统"""
    @register_skill("search_facts", ..., method_type="agent")
    async def search_facts(query: str):
        return self.memory.search_facts(...)  # 闭包捕获 self
```

两套系统合并为一套。`build_tool_definitions()` 就是唯一的 Tool Calling 定义来源。

---

## 维度五：消息处理流程

### 糖糖的做法

```
QQ消息
  → NapCat HTTP → handler.py _handle_message()
    ├── 过滤（黑名单、testing_mode、机器人识别）
    ├── 预处理（@提及检测、命令检测、去重）
    ├── 构建上下文
    │     ├── memory.recall_formatted() → 记忆
    │     ├── knowledge.search() → 知识库
    │     ├── self_state → 关系感 + 存在状态 + 驱动力
    │     ├── personality.build_system_prompt() → 人格
    │     └── context_builder.build() → 组装+预算控制
    ├── LLM 调用（Tool Calling）
    │     ├── 工具执行（LLM 自主选择）
    │     └── 返回最终回复
    ├── 回复后处理（reply_pipeline.py）
    │     ├── 清洗（截断/重复检测）
    │     ├── @解析（把 ID:xxxx 转成 @昵称）
    │     ├── 贴图替换（[贴图:xxx] → 实际路径）
    │     └── 分句发送（长回复分段）
    └── 反馈收集（perception.py） + 记忆提取计数
```

### Nekro 的做法

```
平台消息
  → Adapter → MessageService.push_message()
    ├── 过滤（检查触发词、禁用词、随机通过率）
    ├── 配额检查（日限/时限）
    ├── 防抖（debounce）+ 合并（pending消息）
    ├── 构建 AgentCtx
    ├── 通知记忆调度器
    └── 启动 run_agent()
          ├── PromptCompiler 组装 prompt
          ├── LLM 生成代码
          ├── 解析代码 → Docker 沙盒执行
          ├── 根据退出码迭代（最多 N 次）
          └── 沙盒输出 → 最终回复
              → MessageService.push_message() 发送
```

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 消息入口 | NapCat HTTP → handler | Adapter → MessageService |
| 预处理 | handler 直接处理 | MessageService 统一管道 |
| 防抖 | ✅ message_batcher（2 秒批处理 + 1.5 秒 @防抖） | ✅ debounce + pending 合并 |
| 配额 | 无 | ✅ 日限 + 时限 + 白名单 |
| 上下文组装 | handler + personality + context_builder 三文件协作 | PromptCompiler 统一编译 |
| LLM 调用 | 单轮 Tool Calling | 多轮代码生成+沙盒执行 |
| 回复处理 | reply_pipeline 后处理 | 沙盒输出即回复 |

### 分析

**糖糖的流程对单 Bot 场景是合适的**。消息到达后一条线处理，逻辑清晰。

**防抖已补上，配额仍缺**。Nekro 的 MessageService 有 `debounce_timers` 和 `pending_messages`——短时间内同一频道的多条消息合并为一次处理。糖糖此前没有，2026-08-14 已实现（message_batcher.py：2 秒批处理合并 + 1.5 秒 @防抖）。日限/时限类配额仍然没有——但在单用户场景下不严重。

### 对糖糖的改进建议

**不需要动消息处理流程的架构。轻量防抖已实现**：message_batcher.py 将同一群的连续消息做 2 秒批处理合并为一次 LLM 请求，并对 @提及做 1.5 秒防抖避免重复回复。**当前状态（2026-08-14）**：防抖已实现；剩余可选的是配额（日限/时限），单用户场景下优先级低。

---

## 维度六：状态管理与持久化

### 糖糖的做法

**TangTangSelf** ([self_state.py](agent/self_state.py)) 是核心状态容器：

```python
class TangTangSelf:
    existence: ExistenceState       # 精力/心情/活力 + 自主节律
    self_narrative: SelfNarrative   # 自我叙事 + 价值观
    relationships: dict             # 关系场 {qq_id: {closeness, trust, ...}}
    drives: DriveSystem             # 六种驱动力
    experience_buffer: list         # 经验缓冲（反思消费）
    group_atmospheres: dict         # 群氛围
```

**持久化**：`_save()` / `_load()` 写 JSON 文件。每 10 次关系更新自动写磁盘。反思循环触发时写自我叙事。

### Nekro 的做法

**数据库驱动的状态**。没有中心状态对象。状态散布在：
- `DBChatChannel`：频道配置 + 状态
- `DBChatMessage`：消息历史
- `DBMemParagraph/Entity/Relation/Episode`：记忆状态
- `DBPluginData`：插件级 K-V 存储
- `DBChatChannel.data`：JSON 字段存额外状态

**生命周期**：数据库 = 持久化。没有"保存/加载"的概念——所有状态都在数据库里，启动即恢复。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 状态组织 | 中心对象 TangTangSelf | 分散在多个数据库表 |
| 持久化方式 | JSON 文件 (_save/_load) | PostgreSQL 表 |
| 持久化时机 | 定时 + 手动触发 | 实时（数据库写入） |
| 重启恢复 | _load() 从 JSON | 启动时数据库读取 |
| 历史错误 | ✅ 漏存 relationships, _last_tick_time (已修复) | 🟡 多表一致性依赖事务 |

### 分析

**糖糖的状态管理比 Nekro 更"有机"**——因为糖糖有"自我状态"这个概念。Nekro 的 Agent 没有"存在感"——它是被调用时存在的函数，状态是纯数据。

**但 JSON 文件持久化是脆弱的**：
- 并发写入不安全（虽然当前单线程不会触发）
- 文件损坏时静默失败
- 没有 schema 迁移（加字段要手动兼容）

**升级到 SQLite 统一存储**（Phase 1 建议之一）是合理的——不是换数据库引擎，是把 JSON 文件替换为 SQLite 表，利用事务和 schema 保证的一致性。

**当前状态（2026-08-14）**：未实施——self_state.py 仍写 JSON 文件。

---

## 维度七：配置管理

### 糖糖的做法

**YAML 配置文件** ([config.yaml](config.yaml))。扁平结构，一次加载，全局访问。群级别特殊设置通过 `groups.xxx` 下的零散字段实现。

```yaml
behavior:
  active_interjection: true
  autonomous_speech: true

groups:
  '88888888':
    owner: '10001'
    admins: [...]
    scenario: ''
    scenario_targets: {}
```

### Nekro 的做法

**Pydantic 模型 + 三级覆盖**。

```python
class CoreConfig(ConfigBase):  # 104KB, 几百个字段
    AI_CHAT_xxx: ...
    MEMORY_xxx: ...
    SANDBOX_xxx: ...

# overridable_config.py (54行)
OverridableConfig = create_overridable_config_model("OverridableConfig", CoreConfig)
# → 自动为每个 overridable 字段生成 enable_xxx + xxx 覆盖对

# DBChatChannel.get_effective_config()
# → 系统配置 → 适配器覆盖 → 频道覆盖（优先级递增）
```

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 配置格式 | YAML 文件 | Pydantic + YAML |
| 类型安全 | 🔴 无（字典访问） | ✅ Pydantic 类型校验 |
| 配置覆盖 | 🔴 零散字段 (groups.xxx) | ✅ 自动生成 enable_xxx 覆盖对 |
| 配置文档 | 🟡 config.yaml 注释 | ✅ Pydantic Field description |
| WebUI | Qt 控制台（手动同步） | ✅ 自动生成 WebUI |
| 配置项数量 | ~80 个 | ~300+ 个 |

### 分析

**Nekro 的配置系统有两个设计值得学**。

**1. 标记 → 自动生成覆盖**。`overridable_config.py` 的 54 行逻辑：遍历所有标记为 `overridable` 的字段，自动生成一对 `enable_xxx` 开关 + 覆盖值。糖糖如果要让任意配置项可被群级别覆盖，不需要为每个配置项写代码。

**2. 类型安全**。糖糖的 `config.yaml` 是字典访问：`config["behavior"]["active_interjection"]`。拼写错误在运行时才发现。如果换成 Pydantic（或至少 dataclass），IDE 可以自动补全 + 类型检查。

### 对糖糖的改进建议

**不需要迁移到 Pydantic**（300+ 行的配置模型定义，收益不明显）。但可以做两件小事：

**1. 加一个 `ConfigProxy`**（~30 行）——包装字典访问，提供属性访问 + 默认值：

```python
class ConfigProxy:
    def __init__(self, data: dict):
        self._data = data
    def __getattr__(self, key):
        return self._data.get(key, {})
```

这样 `config.behavior.active_interjection` 替代 `config["behavior"]["active_interjection"]`。IDE 不补全，但至少读起来更清晰。

**2. 群配置覆盖系统化**（~60 行）——借鉴 `overridable_config.py` 的思路。不是为每个字段写覆盖代码，而是设计一个通用的覆盖机制：`{group_id: {override_key: value}}`，读取时自动合并。

**当前状态（2026-08-14）**：未实施——ConfigProxy 与群配置覆盖系统化均未落地。

---

## 维度八：Prompt 工程

### 糖糖的做法

**字符串拼接**。`personality.py build_system_prompt()` 用 `base += f"\n\n{xxx}"` 逐段拼接。`context_builder.py build()` 用 `[(name, text), ...]` 的 section 列表 + 预算控制组装。

```python
base = self._cached_base
base += f"\n\n{self._get_time_context()}"
base += f"\n\n{mood_block}"
base += f"\n\n## 回复前先想\n{CoT_text}"
base += f"\n\n## 关系\n{tier}"
# ... 继续
```

### Nekro 的做法

**Jinja2 模板**。四个独立模板文件编译为 `PromptSegments`：

```python
class PolicyKernelPrompt(PromptTemplate): pass         # policy_kernel.j2
class PersonaPrompt(PromptTemplate):                   # persona.j2
    chat_preset: str
class RuntimeContractPrompt(PromptTemplate):           # runtime_contract.j2
    platform_name, bot_platform_id, enable_cot, ...
class SystemPrompt(PromptTemplate):                    # system.j2
    stable_static, channel_static, runtime_dynamic, plugins_prompt
```

`PromptCompiler` 用 Jinja2 `Environment.render()` 组合四层。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 组装方式 | 字符串拼接 (base +=) | Jinja2 模板渲染 |
| 分层 | 🟡 ContextBuilder 四层 | ✅ PromptCompiler 四层 |
| 可读性 | 🔴 需要追踪多个文件 | ✅ 模板文件即文档 |
| 加新段落 | 🔴 改函数签名 + 加拼接行 | ✅ 模板加 {{var}} + 传变量 |
| 安全性 | 🔴 手动注意转义 | ✅ Jinja2 自动转义 |
| 依赖 | 0（Python 原生） | 1（Jinja2） |

### 分析

这个问题之前讨论过。我收回"字符串拼接已经足够"的说法。

**如果糖糖的 prompt 结构不再变化**——字符串拼接完全够用。但 Phase 1-3 每个都要往 prompt 里加新内容（episode 摘要、社交关系、候选话题）。每次加新内容：
1. 改 `build_system_prompt` 签名（加参数）
2. 改 `context_builder.build` 签名（加参数）
3. 改 handler 调用处（传新参数）
4. 确保没有重复注入（靠人工记忆和注释）

**四步变成一步**的可能性：模板里加 `{{ episode_summaries }}`，Python 传一个变量。

### 对糖糖的改进建议

**在 Phase 1 之前做 Jinja2 迁移**。理由：
- 迁移本身 ~80 行
- Phase 1-3 每次加新 prompt 段落省 15 行 × 3-4 次 = 省 ~50 行
- 净收益：更容易的维护 + 更清晰的 prompt 结构

**但不需要像 Nekro 那样分四个模板文件**。糖糖只有一个 prompt，一个 `.j2` 文件就够了。

**当前状态（2026-08-14）**：**已迁移**——context_builder.py 用 Jinja2 渲染 `prompts/system.j2`（模板缺失时回退旧式拼接）。注意：学习报告曾给出「不推荐迁移到 Jinja2」的结论，已被实际演进推翻——以本维度（对比分析）的结论为准。

---

## 维度九：时间与自治

### 糖糖的做法

**双时间系统**：

```
被动时间感（每次消息注入）
  └── personality._get_time_context() → "现在是周二下午3点..."

主动自治循环（每 10 分钟）
  └── handler._check_autonomous_action()
        ├── 检查驱动力是否超过阈值 (>0.7)
        ├── 时间检查（8:00-23:00 活跃、30分钟冷却）
        ├── LLM 基于内心状态决定：说 / 不说 / 写日记
        ├── 选群（最近有活动的群）
        └── 内在消化（不说话时写体验日记 + 微释放）
```

此外还有：
- 反思循环（每 30 分钟检查）→ 自我叙事更新
- 定时任务（早安/晚安/生日提醒/每日点赞）
- 驱动力随时间自然积累（`tick()` 每 10 分钟）

### Nekro 的做法

**纯被动 + 定时器**。没有自治循环。Agent 只在收到消息时响应。

定时器系统（`services/timer/`）用于插件定时任务（如整点报时、定时推送），不是 Agent 的自主行为。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 被动响应 | ✅ | ✅ |
| 自主发起 | ✅ 驱动力驱动的自治循环 | 🔴 无 |
| 时间感 | ✅ 时间上下文注入 + 自主节律 | 🔴 纯工具 (get_time) |
| 反思 | ✅ Phase D 反思循环 | 🟡 记忆维护调度（非自我反思） |
| 生命周期 | ✅ 内在消化（不说话的替代释放） | 🔴 无 |
| 定时任务 | ✅ 早安/晚安/生日/点赞 | ✅ 插件定时器 |

### 分析

**这是糖糖最独特的维度——Nekro 完全没有、糖糖自成一体的东西。** 驱动力 + 自治循环 + 内在消化 + 反思 = 糖糖的"自我生命感"来自这套系统。

**但这套系统有一个未开发的交叉点**：驱动力释放 × 记忆。当前自治循环选群是机械的（最近有活动），释放驱动力的内容是 LLM 自由发挥的。如果加上"候选话题"的提示——基于档案记忆和 episodic 记忆——主动性会从"不知道该说什么就随便说说"变成"想到你了所以想跟你说这个"。

见维度三的记忆改进建议 Phase 3c。

**当前状态（2026-08-14）**：已实现——handler_autonomy.py `_build_initiative_topics()` 构建驱动力 × 记忆候选话题，主动性从"随便说说"变为"想到你了所以想跟你说这个"。

---

## 维度十：安全与边界

### 糖糖的做法

**Tool Calling 白名单**。LLM 只能调用系统注册的工具，不能执行任意代码。回复内容有 `self_check.py` 做硬约束（截断、重复检测）+ 柔性检查（去重、超长）。

**角色扮演边界**：通过 role_card + can_do/cannot_do 规则 + CoT 自我检查。不依赖沙盒——依赖 LLM 遵守规则。

**测试模式**：`testing_mode: true` 会丢弃所有非主人的消息。曾被遗忘在生产环境——已通过启动时显式警告修复。

### Nekro 的做法

**Docker 沙盒隔离**。LLM 生成的代码在容器里跑，通过 RPC 调外部能力。退出码被严格检查。安全由容器边界保证——不依赖 LLM 的自律。

**禁用词检测**：`check_forbidden_message()` 过滤敏感内容。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 安全模型 | 白名单 + 角色约束 | Docker 容器隔离 |
| 代码执行 | ❌ 不允许 | ✅ 沙盒内全 Python |
| 敏感内容 | 🟡 role_card 规则 + self_check | ✅ 禁用词检测 |
| 边界强度 | 🟡 依赖 LLM 遵守规则 | ✅ 容器边界强制 |
| 灵活性代价 | 🟢 低——LLM 本来就只回复文本 | 🔴 高——需要管理 Docker + RPC |

### 分析

**Nekro 的安全模型是它架构的结果，不是它架构的目标**。Nekro 需要沙盒是因为 LLM 要执行任意代码。糖糖不需要沙盒是因为 LLM 只调白名单工具 + 回复文本。

**糖糖目前的安全模型对当前场景足够**。但有一个细节：
- `self_check.py` 的硬约束和柔性检查都在回复生成之后——是事后过滤
- 如果 LLM 输出了不当内容，过滤能截住，但 LLM 已经"想过"了
- Nekro 的禁用词检测在消息进入处理管道之前——是事前拦截

**对糖糖的改进建议**：不需要安全模型的大改。但可以把敏感词检测移到消息预处理阶段（在构建 prompt 之前），而不是只在回复后做 self_check。这可以防止敏感内容进入 LLM 的上下文窗口。

**当前状态（2026-08-14）**：未实施——事后过滤 self_check.py 仍在，敏感词预处理未落地。

---

## 维度十一：扩展性

### 糖糖的做法

**添加新能力** = 在对应的模块里写一个 async 函数 + `@register_skill` 装饰器（纯工具）或在 handler.py 里加方法 + 手工构建 Tool Calling 定义（需要访问 handler 属性的工具）。

**添加新群管理功能** = 在 handler.py 里加 `/命令` 处理器。

**模块扩展** = 新建一个 `agent/xxx.py`，在 handler.__init__ 里初始化，在需要的地方调用。

### Nekro 的做法

**添加新能力** = 写一个插件。插件可以有沙盒方法、提示词注入、消息回调、异步任务、命令、配置类、Web 路由。所有能力通过装饰器注册，由 `PluginCollector` 统一管理。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 加能力 | 改现有模块或新建 .py + handler 初始化 | 新建插件（独立目录）|
| 能力注册 | `@register_skill` 或 handler 手工 | `@plugin.mount_xxx` 统一 |
| 能力隔离 | 🔴 无——所有能力共用 handler 命名空间 | ✅ 插件级命名空间 + 存储隔离 |
| 插件市场 | 🔴 无 | ✅ 云端插件 + 社区贡献 |
| 对外 Web API | 🔴 无 | ✅ 插件可挂载 FastAPI 路由 |

### 分析

**糖糖不需要插件市场、不需要云端分发、不需要第三方开发插件。** 但"能力隔离"这个概念值得提取——不仅是安全隔离，也是代码组织隔离。

当前糖糖的所有能力（唱歌、搜图、算卦、语音、表情包、知识库）的函数定义散落在 handler.py 和 skills 注册表中。如果每个能力模块自包含——有自己的注册、自己的状态、自己的清理逻辑——代码组织会更清晰。

**不需要做成完整插件系统**。但可以让每个能力模块有一个统一的接口约定：

```python
class SkillModule:
    name: str
    def register(self, skill_registry): ...   # 注册 Tool Calling
    def init(self, handler): ...              # 初始化（需要 handler 属性的在这里拿）
    def cleanup(self): ...                    # 清理
```

这个约定不强制——现有的 `@register_skill` 仍然能用。只是给大模块（voice、sticker、knowledge）一个更清晰的结构。

**当前状态（2026-08-14）**：未实施——SkillModule 接口约定与生命周期回调注册均未落地。

---

## 维度十二：部署与运维

### 糖糖的做法

**Python 脚本 + Qt 控制台**。启动一个 Python 进程，Qt 控制台提供可视化管理。配置文件 `.env` + `config.yaml`。重启 = 关掉进程再开。

**日志**：Python logging，输出到控制台 + 文件。

### Nekro 的做法

**Docker Compose 一键部署**。多容器编排（API 服务 + PostgreSQL + Qdrant + Sandbox 镜像）。WebUI 管理。配置热更新（不用重启）。

### 交叉对比

| | 糖糖 | Nekro |
|------|------|------|
| 部署复杂度 | 🟢 单 Python 进程 | 🔴 Docker Compose 多容器 |
| 管理界面 | 🟡 Qt 控制台（手动同步 config） | ✅ WebUI（自动同步） |
| 配置热更新 | 🔴 需要重启或 /人格 重载 | ✅ 热更新 |
| 数据库 | 🟢 SQLite（零配置） | 🔴 PostgreSQL（需要维护） |
| 日志 | 🟡 Python logging | 🟡 结构化日志 + 前端查看 |

### 分析

**糖糖的部署复杂度远低于 Nekro——这对一个人用的项目来说是正确的选择。**

唯一的运维痛点：config.yaml 和 Qt 控制台不同步。这个问题 CLAUDE.md 已经记录了（错误四）。

---

## 总结矩阵

| 维度 | 糖糖优势 | Nekro 优势 | 改进优先级 |
|------|---------|-----------|-----------|
| 架构 | 🟢 单体简洁 | 🟡 但过度设计 | P1 - handler 拆分 + 模块清单 |
| 人格 | 🟢 **远胜** | 🔴 浅 | 不改核心，只改组装方式 (Jinja2) |
| 记忆 | 🟡 关系维度好 | 🟢 **结构维度好** | **P0** - 认知类型+衰减+配额+Episode |
| 工具 | 🟢 简单够用 | 🟡 类型区分好 | P0 - 方法类型区分 |
| 消息处理 | 🟢 直线流程 | 🟡 防抖+配额 | 防抖已实现（message_batcher.py）；配额未做 |
| 状态管理 | 🟢 **"存在"感** | 🟡 持久化机制好 | P1 - SQLite 替代 JSON |
| 配置 | 🟢 简单 | 🟡 类型安全+覆盖机制 | P2 - ConfigProxy + 群覆盖 |
| Prompt | 🟢 灵活 | 🟡 **模板清晰** | P0 - Jinja2 迁移 |
| 时间与自治 | 🟢 **独有** | — | P1 - 驱动力×记忆交叉 |
| 安全 | 🟢 对当前场景够用 | 🟡 但不需要 | P3 - 事前敏感词过滤 |
| 扩展性 | 🟡 够用 | 🟢 但过度 | P2 - 能力模块接口约定 |
| 部署 | 🟢 简单 | 🔴 重 | 不改 |

---

## 附录：Nekro 学习报告精华

> 来源：《Nekro_Agent_2.3.0_学习报告.md》（2026-08-02，阅读源码 v2.3.0）
> 并入日期：2026-08-14。原文已归档至 `docs/开发规划/归档/`。
> 本附录保留学习报告中对糖糖后续演进仍有参考价值的细节：核心数据流与 exit code 控制流、沙盒代理行为、记忆权重公式与配额数值、设计哲学、优先级建议表及落地状态。与正文各维度重复的分析不再重复。

### 附录 A. 核心数据流与 exit code 控制流

```
外部消息（QQ/Discord/...）
    │
    ▼
[适配器层] — 统一为内部消息格式
    │
    ▼
[MessageService] — 写入 DB，触发 Agent
    │
    ▼
[run_agent()] — 核心循环入口
    │
    ├─ 1. PromptCompiler 组装系统提示词
    │     ├── PolicyKernelPrompt     (硬行为准则)
    │     ├── PersonaPrompt          (角色人设)
    │     ├── RuntimeContractPrompt  (平台/能力/格式要求)
    │     └── PluginsPrompt          (插件动态注入)
    │
    ├─ 2. LLM 生成 → Python 代码文本
    │
    ├─ 3. parse_chat_response() → 从文本提取代码块
    │
    ├─ 4. limited_run_code() → Docker 沙盒执行
    │     ├── 代码中的函数调用 → __extension_method_proxy
    │     ├── pickle 序列化参数 → HTTP POST → 主进程 RPC 端点
    │     ├── 主进程执行真正的插件方法
    │     └── pickle 序列化返回值 → HTTP Response → 沙盒
    │
    └─ 5. 根据退出码决定下一步：
          exit 0  → 结束，沙盒输出为最终回复
          exit 8  → AGENT 方法被调用，返回值注入上下文，LLM 再跑一轮
          exit 11 → MULTIMODAL_AGENT，多模态内容注入，LLM 再跑
          exit 1  → 错误，错误信息注入上下文，LLM 修正代码
          (最多重试 N 次)
```

**核心洞察**：Nekro 的 LLM 不直接生产"回复"——它生产"执行计划"（Python 代码）。真正的"回复"是代码在沙盒里跑完后的副作用（调用 `send_message` 等）和输出。LLM 可以写多步骤逻辑；执行有完整的 Python 运行时；安全由 Docker 容器保证，不需要白名单。对糖糖来说这个模式太重了——糖糖不需要 AI 写代码。但 **exit code 作为控制流信号** 的设计思路，可以轻量化借鉴到 Tool Calling 返回值处理中（糖糖已落地为 skills.py `Skill.method_type`，见正文维度四）。

### 附录 B. 沙盒方法四分类——代理代码行为细节

```python
class SandboxMethodType(str, Enum):
    TOOL = "tool"
    AGENT = "agent"
    BEHAVIOR = "behavior"
    MULTIMODAL_AGENT = "multimodal_agent"
```

| 类型 | 返回值 | 是否注入上下文 | 是否触发 LLM 再思考 | 典型场景 |
|------|--------|:---:|:---:|------|
| TOOL | 任意可序列化 | 否（LLM 在沙盒内直接拿到返回值继续执行代码） | 否 | 计算、文件读写 |
| AGENT | str | 是 | **是** | 搜索、知识库查询 |
| BEHAVIOR | str | 是（记录到系统消息） | **否** | 发消息、设定时器 |
| MULTIMODAL_AGENT | 多模态消息段 | 是 | **是** | 图片理解 |

这个分类只在**沙盒内**生效。沙盒内的 RPC 代理函数根据方法类型决定行为（ext_caller_code.py）：

```python
def __extension_method_proxy(method):
    def acutely_call_method(*args, **kwargs):
        # 通过 RPC 调主进程执行真正的插件方法
        response = requests.post(RPC_URL, data=pickle.dumps(body), ...)

        if response.headers.get("Method-Type") == "agent":
            # AGENT: 打印返回值 → exit(8) → 外层循环捕获 → 注入上下文 → LLM 再跑
            print(f"The agent method returned:\n{ret_data}\n[result end]")
            exit(8)

        if response.headers.get("Method-Type") == "multimodal_agent":
            # MULTIMODAL: 打印返回值 → exit(11) → 同上
            print(f"The multimodal agent method returned:\n{ret_data}\n[result end]")
            exit(11)

        # TOOL / BEHAVIOR: 正常 return，沙盒代码继续执行
        return ret_data
    return acutely_call_method
```

外层循环捕获退出码（run_agent.py）：

```python
if stop_type == ExecStopType.AGENT:
    # 把返回值当作用户消息注入，触发新一轮 LLM 生成
    msg = msg.extend(OpenAIChatMessage.from_text(
        "user",
        f"[Agent Method Response] {sandbox_output}\n"
        "Please continue based on this agent response."
    ))
    # → messages 扩展后，下一轮循环 LLM 继续生成
```

对糖糖的启发：糖糖不需要沙盒和 exit code，但可以在 Tool Calling 定义层区分「AGENT（返回值触发再思考）」和「BEHAVIOR（副作用操作，不触发）」——消除 `send_voice` 之后"我已经发完语音了，现在该说什么"的无效 LLM 轮次。**已实现**：skills.py `Skill.method_type`。

### 附录 C. 记忆四层数据模型与权重公式

**四层模型**：

- `DBMemParagraph`（最小编程单元）：content / summary / cognitive_type（EPISODIC 发生过的事 | SEMANTIC 知道的事）/ knowledge_type（fact | preference | experience | decision | task | skill | relation）/ episode_id / base_weight / decay_rate / event_time / is_inactive / embedding（Qdrant）
- `DBMemEpisode`（情节）：title / narrative_summary / time_start-time_end / participant_entity_ids / paragraph_ids / phase_mapping（OPENING/DEVELOPMENT/CLIMAX/RESOLUTION 段落分组）/ base_weight
- `DBMemEntity`（实体）：canonical_name / name / entity_type（PERSON/ORGANIZATION/LOCATION/...）
- `DBMemRelation`（关系三元组）：subject_entity - predicate - object_entity / 来源段落 / base_weight / decay_rate

**检索权重公式**（retriever.py）：

```
effective_weight = base_weight × similarity × episodic_boost × recent_boost
├── base_weight:    衰减后的基础权重 (paragraph.compute_effective_weight)
├── similarity:     向量相似度
├── episodic_boost: 情景记忆 1.3x 加成（更具体，更有价值）
└── recent_boost:   近期记忆（24h 内）1.15x 加成
```

关系图谱补充检索的**关系匹配分数 = 0.55 + 0.2（主体实体命中）+ 0.2（客体实体命中）+ 0.15（谓词语义匹配）**。

**上下文编排配额**（CORE_PLUS_EVIDENCE 模式）：

```python
TYPE_QUOTA = {
    "paragraph": 4,   # 最多 4 条段落记忆
    "episode":   2,   # 最多 2 条情节记忆
    "relation":  2,   # 最多 2 条关系记忆
}

CATEGORY_QUOTA = {
    "DECISION":    3,   # 决策类最多 3 条
    "FACT":        2,   # 事实类最多 2 条
    "PREFERENCE":  1,   # 偏好类最多 1 条
    "EXPERIENCE":  2,   # 经验类最多 2 条
    "CONVERSATION": 2,  # 对话类最多 2 条
    "RELATION":    2,   # 关系类最多 2 条
    "EMOTION":     0,   # 情感类不注入
}
```

**Episode 聚合**——确定性算法，不调 LLM：时间差 < 阈值且来源频道相同 → 加入当前组；组内条数 >= 最小值 → 创建 Episode。阶段按比例自动映射：前 25% → OPENING，25-65% → DEVELOPMENT，65-85% → CLIMAX，后 15% → RESOLUTION。标题/摘要也由确定性算法生成（首段落摘要截断 + 参与实体名称拼接）——与 LLM 提取解耦，避免"为聚合再跑一次 LLM"。

**记忆维护**（scheduler.py）：按 decay_rate 和距上次访问时间衰减 base_weight → 低于阈值 `is_inactive = True`（不删除，不参与检索）→ 检索命中记入 DBMemReinforcementLog → 反向重建强化/失活。

糖糖落地状态（2026-08-14）：认知类型配额（memory.py R2-1）、Episode 聚合（memory.py `_aggregate_episodes` + store.py episodes 表）、cognitive 字段（episodic/semantic）、休眠降权（memory.py R2-2）均已实现；关系三元组未实现（store.py 无 relations 表）。

### 附录 D. 设计哲学五条提炼

1. **安全隔离 = 能力边界**：不信任 LLM——容器能做什么由 Docker capabilities 控制，LLM 想访问数据库必须走 RPC。对糖糖的启示不是"加 Docker"，而是**能力边界应该显式化**：Tool Calling 本质也是边界控制，但糖糖的边界是隐式的——函数定义散落在 skills.py 和 handler.py 中。
2. **确定性算法优先于 LLM**：Episode 聚合（时间聚类）、权重计算（数值公式）、阶段分配（比例映射）、失活判断（阈值）都不调 LLM。LLM 只用在真正有价值处——非结构化文本提取。糖糖已在践行（auto_learn 正则退役 → LLM 语义提取器；关键词门控 → LLM 自主 Tool Calling）。
3. **装饰器优于配置文件**：所有能力注册都用装饰器，没有 XML/YAML 注册表。代码即配置。糖糖的 `@register_skill` 方向相同。
4. **每个模块自带存储**：插件通过 PluginStore 自带 K-V 数据库，不需要协调谁建表。存储是模块的私有实现细节。糖糖的存储正碎片化（TangTangSelf 写 JSON、perception 写 JSON、reflection buffer 在内存）——对应附录 E 建议 3（ModuleStore，未实施）。
5. **配额制 > 截断**：先按类型配额分配名额，再在各类型内按分数排序——确保不同类型记忆都被代表，而不是被同一类型高分记忆占满。糖糖已在记忆检索层落地（R2-1），context_builder 层截断前配额未做。

### 附录 E. 三档优先级建议表（10 条）与当前状态（2026-08-14）

**优先级 1：低投入、高收益**

| # | 建议 | 改动量 | 收益 | 当前状态（2026-08-14） |
|---|------|--------|------|----------------------|
| 1 | Tool Calling 加 `method_type`——区分 AGENT（触发再思考）和 BEHAVIOR（不触发），消除 `send_voice` 等操作后的无效 LLM 轮次 | ~30 行 | 减少不必要的 API 调用 | **已实现**（skills.py Skill.method_type，默认 agent） |
| 2 | 上下文编排加类型配额——按类型分配名额（facts/profiles/relationships 各 N 条） | ~50 行 | 记忆注入多样性提升 | **部分实现**：配额已落在记忆检索层（memory.py R2-1 认知类型配额）；context_builder 截断层配额未实现 |
| 3 | 统一模块 K-V 存储——`ModuleStore(module_name).get/set/delete` 替代各模块手写 JSON I/O | ~80 行 | 消除存储碎片化 | 未实施 |

**优先级 2：中投入、中收益**

| # | 建议 | 改动量 | 收益 | 当前状态（2026-08-14） |
|---|------|--------|------|----------------------|
| 4 | 反思循环加 Episode 聚合——时间+来源聚类先分组，再让 LLM 写摘要 | ~100 行 | 反思产出结构化 | **已实现**（memory.py `_aggregate_episodes` + store.py episodes 表） |
| 5 | 记忆加 cognitive_type——EPISODIC（发生过的事）vs SEMANTIC（知道的事），检索时区别对待 | ~150 行 | 记忆检索质量提升 | **已实现**（memory.py cognitive 字段 episodic/semantic） |
| 6 | 配置层级覆盖系统化——借鉴 `overridable_config.py` 的 54 行逻辑，群级别覆盖统一化 | ~120 行 | 消除零散的群配置字段 | 未实施（ConfigProxy 也未做） |

**优先级 3：保持关注、暂不实施**

| # | 建议 | 原因 | 当前状态（2026-08-14） |
|---|------|------|----------------------|
| 7 | 异步任务系统 | 糖糖没有耗时超过 LLM 调用的操作 | 保持关注（未实施） |
| 8 | Jinja2 模板化 Prompt | 单人单语言，字符串拼接够用 | **已迁移**（context_builder.py 渲染 prompts/system.j2，缺失时回退旧式拼接）——学习报告「不推荐迁移」结论已被实际演进推翻，以正文维度八为准 |
| 9 | Docker 沙盒 | 安全需求不至此，Tool Calling 白名单足够 | 保持不实施 |
| 10 | 多适配器架构 | 糖糖就是 QQ 猫娘，不需要跨平台 | 保持不实施 |

### 附录 F. 关键源码文件索引（Nekro 侧，源码根目录下）

| 关注点 | 文件路径 |
|--------|---------|
| 核心执行循环 | `nekro_agent/services/agent/run_agent.py` |
| 沙盒方法类型 | `nekro_agent/services/plugin/schema.py` |
| 插件基类 | `nekro_agent/services/plugin/base.py` |
| 沙盒 RPC 代理 | `sandbox/nekro_agent_sandbox/ext_caller_code.py` |
| AgentCtx | `nekro_agent/schemas/agent_ctx.py` |
| 记忆检索 | `nekro_agent/services/memory/retriever.py` |
| 记忆提取 | `nekro_agent/services/memory/consolidator.py` |
| Episode 聚合 | `nekro_agent/services/memory/episode_aggregator.py` |
| 记忆维护 | `nekro_agent/services/memory/scheduler.py` |
| 配置覆盖 | `nekro_agent/core/overridable_config.py` |
| Prompt 编译 | `nekro_agent/services/agent/templates/compiler.py` |
| 沙盒执行 | `nekro_agent/services/sandbox/runner.py` |
