# DeepSeek 论文学习笔记 — 糖糖对话质量改进

> 2026年7月，阅读 DeepSeek-R1 / V3 论文及最佳实践后整理。每条发现对应糖糖的具体改动。

---

## 一、DeepSeek-R1：推理链（Chain-of-Thought）

**论文**：*DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning* (2025.1)

### 核心发现

1. **`<think>` / `<answer>` 结构**：R1 训练时强制模型先输出推理过程再给答案。推理过程中自发出现自我纠错（"wait, let me try again"）、回溯验证等行为。分类准确率从 15.6% → 77.9% → 86.7%。

2. **冷启动模板**：只约束输出格式（`<think>` → `<answer>`），不约束思考内容。不预设"应该怎么想"。

3. **语言一致性奖励**：RL 训练中加入语言一致性信号，防止推理过程中英混杂。

4. **推理预算控制**：简单问题秒回，复杂问题多想想。"... " 省略号提示可随机触发思考/不思考模式，省 ~52% token。

5. **思考锚点（Thought Anchors）**：DeepSeek-R1 使用**集中式推理策略**——少量高影响力推理步骤（平均概率增量 0.408），而非均匀分布。82.7% 的推理步骤正向影响最终答案。

### 糖糖应用

| 发现 | 落地 |
|------|------|
| CoT 提升决策准确率 | `_llm_intent_recognition` 和 `_classify_persona_intent` 加"先分析…"前缀，让 LLM 在分类前先想一步 |
| 冷启动模板只约束格式 | 提示词从"教你怎么说话"改为"画边界"——铁律 + 红线模式 |
| 语言一致性 | 提醒中加入"不要中英混杂"（待观察效果） |

---

## 二、DeepSeek-V3：系统提示词与指令遵从

**来源**：V3 技术报告、API 文档、社区最佳实践

### 核心发现

1. **User message 遵从度 > System prompt**：DeepSeek V3 对 user message 里的指令反应更强。System prompt 只被 prepend，没有特殊 token 标记（不像 Llama 3 的 Instruct template 有专门标记）。

2. **极端化问题**（GitHub Issue #829）：DeepSeek 会把单一人设特质无限放大——设定"温柔"→ 完全没有脾气；设定"认真"→ 堆砌术语；设定"喜欢布丁"→ 每轮对话都提布丁。

3. **System prompt 缓存**：API 自动缓存相同 system prompt，命中后可省 ~90% 输入 token。动态变量应放 user message。

4. **温度映射**：API `temperature=1.0` → 模型内部 `temperature=0.3`。官方推荐保持默认 1.0。

5. **结构化分隔**：用 XML 标签或 Markdown 分隔不同指令区块，比纯文本段落遵从度更高。

### 糖糖应用

| 发现 | 落地 |
|------|------|
| User message 优先级 | `_call_llm_with_skills` 的行为规则从 system prompt 移到 user message，用 `--- BEGIN BEHAVIOR RULES ---` 结构化分隔 |
| 极端化防御 | 所有行为规则改为 `✅ DO` / `❌ DON'T` 对比格式。角色卡每个性格描述配"但你不是"边界 |
| 缓存优化 | system prompt 静态部分缓存（`_cached_base`），动态部分（时间/情绪/记忆）在 `build_system_prompt` 追加 |

---

## 三、角色扮演与防 OOC

**来源**：DeepSeek 角色扮演最佳实践、CPDC 2025 竞赛方案

### 核心发现

1. **三维建模法**：角色定义 = 身份 + 行为 + 风格。颗粒度过粗无法触发专业技能，过细限制响应范围。

2. **否定约束**：`禁止使用 X` 比 `使用 Y` 更有效，可减少 ~35% 无效输出。必须同时告诉模型"是什么"和"不是什么"。

3. **情商优先级**：DeepSeek 默认偏理性，需要显式告诉它"情感先于逻辑"。避免仅提供事实信息或逻辑推理。

4. **上下文锚定**：多轮对话中每隔几轮就重新锚定角色身份，防止"聊得越久越容易失忆"。

5. **规则式角色提示（RRP）**：CPDC 最优方案——Character-Card 定义合法行为边界，Action-first 规则强制先动作再对话。

### 糖糖应用

| 发现 | 落地 |
|------|------|
| 双向定义 | `role_card.md` 每个核心特质配否定边界（"但你不是…"） |
| 情商优先 | 铁律 #4："情感优先于逻辑——先感受对方的情绪再组织回应" |
| 否定约束 | 提醒和角色卡大量使用 `❌ DON'T` 格式 |
| 上下文锚定 | 结构化 `history_messages`（role: user/assistant）让 LLM 每轮都清楚"我说过什么" |

---

## 四、多轮对话结构化

**来源**：DeepSeek API 多轮对话文档

### 核心发现

Chat Completions API 是无状态的，每次请求需传递完整对话历史。正确使用 `role: user/assistant` 区分对话角色，比把整个聊天记录当文本喂给 LLM 效果好得多。

### 糖糖应用（2026年7月24日）

- `memory.py` 新增 `get_recent_context_messages()`：糖糖的消息 → `role: assistant`，别人的 → `role: user`
- `_call_deepseek` 改为 `[system] + history_messages + [user]` 数组结构
- 群聊/私聊 handler 均改为传递结构化消息

---

## 五、排查出的同类问题（2026年7月25日审计）

全量扫描 `agent/` 目录后修复了 8 处"兜底逻辑默认有相关性"的同类 bug：

| 文件 | 问题 | 修法 |
|------|------|------|
| `handler.py` `_looks_like_question` | 消息≥15字或有"？"+ ≥10字就搜知识库 | 删掉长度兜底，只保留实质性提问词匹配 + 最小长度门槛 |
| `handler.py` `_keyword_pick_memories` | 无匹配时 `return candidates[:3]` | 改为 `return []` |
| `handler.py` 记忆强化 | 每次回复 reinforce top 10，权重滚雪球 | 只加强本次对话真正选中的记忆 |
| `interjection.py` | "呢""吧"当问题标记 +60分 | 移除，只留"吗"+ 实质提问词 |
| `scenario.py` | 私聊空文本（发图）→ 色色模式 | 空文本不改场景 |
| `scenario.py` | 色色触发词含大量日常用语（撒娇/黏人/睡不着等） | 删除 12 个高频误触词 |
| `web_search.py` | `[吗嘛呢啊]` 匹配任何句末语气词 | 移除"呢""啊"，只留"吗""嘛" |
| `knowledge.py` | n-gram 产生"糖你""好呀"等噪音关键词 | STOP_WORDS 加 30+ 招呼/日常高频词 |

---

## 六、文件 URI 兼容性

SnowLuma 不能解析含反斜杠的 `file:///` URI。所有 `[CQ:image` / `[CQ:record` 生成路径统一用 `.as_posix()` 转为正斜杠。涉及 `sticker.py`、`image_share.py`、`handler.py`、`voice.py`。

---

## 七、后续方向（未实施）

- **System prompt 缓存最大化**：当前 `build_system_prompt` 在 system prompt 中追加了动态内容（时间/情绪/关系）。理想做法是将这些移到 user message，让 system prompt 完全静态以触发 API 缓存（省 ~90% 输入成本）
- **推理预算差异化**：简单闲聊用轻量提醒，复杂问题（搜索/计算）追加"请先想清楚再回答"前缀
- **思考链可视化**：如果未来切换到 R1 系列模型，可启用 `<think>` / `<answer>` 标签格式
