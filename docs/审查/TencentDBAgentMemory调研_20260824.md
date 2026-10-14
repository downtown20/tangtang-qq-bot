# TencentDB-Agent-Memory 调研（2026-08-24）

> 主人要求调研 https://github.com/TencentCloud/TencentDB-Agent-Memory，判定能否用于 Claude Code（开发侧）或糖糖（bot），或两者。结论：**都不接入主流程，可借鉴「资产反射」思想**。

## 一、项目是什么

腾讯官方「**编码 Agent 团队通用记忆后端**」，MIT 协议，调研时点为 v2.0.0（快速迭代中），Node ≥22.16，docker 部署。

**四组件**：

| 组件 | 端口 | 作用 |
|---|---|---|
| memory-core | 8420 | 记忆读写：L0原始对话 → L1原子事实 → L2场景 → L3人设，异步蒸馏 |
| memory-hub | 8125 | Web 面板：团队/Agent/任务三元组、资产 ACL、版本/所有权管理 |
| knowledge | 8424 | Wiki（文档→结构化页+链接图，源自 Karpathy LLM Wiki 思想）+ CodeGraph（代码符号/调用关系/影响路径，源自 colbymchenry/codegraph） |
| proxy | 8096 | **LLM 请求代理**：Agent 把 base_url 指向它，每轮自动注入 L2/L3+Skill+知识，同时录制对话、后台提取 |

**核心设计哲学**：
- 记忆不常驻提示词，是「Agent 的装备」（loadout）——按 team/agent/task 绑定 + ACL 决定注入什么
- 资产可版本化、可流转（"一人公司的 Agent 团队"：Claude Code + Codex 共享同一记忆空间）
- 零代码接入：支持 Claude Code / Codex / CodeBuddy / dsh / Hermes / OpenClaw，generic 路径可接任意 OpenAI 兼容客户端
- 权限：private / team / restricted / agent 四级 ACL
- 官方基准：PersonaMem 48% → 76%（+59%）
- 部署成本：docker 三服务 + 两套 LLM key（MEMORY_LLM_* 内部提取 + PROXY_UPSTREAM_* 上游转发）

## 二、判定一：糖糖 ❌ 不建议接入

**问题域不同**。它为编码 Agent 团队设计（team/agent/task 三元组、Skill/Wiki/CodeGraph 都是工程资产）；糖糖的记忆为「存在感」服务——关系场、驱动力、真值治理（active/retracted/superseded）、置信度分层、反思循环、情绪三维。

| 它的能力 | 糖糖已有对应 |
|---|---|
| L0→L3 分层蒸馏 | 原始消息 → LLM语义提取 → 画像合成 → 自我叙事 |
| Skill | `@register_skill` 工具体系 |
| Wiki | knowledge/（hybrid+RRF+rerank，2026-08-15 升级） |
| CodeGraph | 糖糖不需要代码知识 |

接入代价：推翻 CLAUDE.md 第六节「已确认有效的积累」；docker 栈 + 双份 LLM 提取消耗；在 DeepSeek 流式/推理模型/tool calling 路径上插代理层（高风险）。

## 三、判定二：Claude Code（我）⚠️ 能用但重复建设 > 增量

| 它的资产 | 已有等价物 | 重叠 |
|---|---|---|
| CodeGraph | GitNexus（29988 符号，impact 纪律已入 CLAUDE.md） | 完全重叠 |
| Wiki | docs/审查/ + knowledge/ + CLAUDE.md | 高度重叠 |
| Skill | ~/.claude/skills + ~/.codex/skills | 重叠 |
| Chat Memory L0-L3 | 文件记忆（MEMORY.md 索引）+ brief 交接 | 部分重叠 |

**真增量只有一个**：跨会话自动记忆 + Claude Code/Codex 共享记忆空间（打中「压缩上下文、brief 交接」痛点）。但代价：

1. 全部流量绕行本地代理——4 个 docker 容器成为每次 API 调用的必经之路，挂一个全挂
2. 每轮注入 L2/L3+Skill——把糖糖项目刚瘦掉的信噪比问题（每轮 5500-9000t 说明书）搬到自己身上
3. 每个新会话都要过 Team/Agent/Task 选择器（Codex 还要先切 Plan 模式）
4. v2.0.0 刚发布，迭代快，追更成本高

## 四、判定三：两者都用 ❌

技术上可行（proxy 多协议 + generic 路径），但糖糖侧无收益、我侧高风险——不接。

## 五、可借鉴（不接入也能抄）

1. **`/analyse` 资产反射**（最有价值）：让 LLM 每轮自评「注入的资产有没有帮上忙」——正是糖糖对话质量瘦身（2026-08-17）缺的**注入效果度量工具**。待办：上下文预算审计时引入类似机制。
2. L2 Scenario 分层思想——糖糖的场景引擎已是对应物，不需动。
3. 「记忆是装备不是全局提示词」——糖糖已在践行（能力数据按需注入、World Info 绿灯原则），互相印证。

## 六、后续行动

- 不接入主流程。
- 若未来真正需要「Claude Code + Codex 共享记忆」：单点试用 Hub 面板 + Skill 提取（手动导入导出资产），**不动 base_url、不穿代理**。
- 「资产反射」纳入糖糖上下文注入优化待办。

取证来源：README.md / INSTALL.md（Claude Code/Codex/generic 接入章节）／官方安装文档 docs.github.com。
