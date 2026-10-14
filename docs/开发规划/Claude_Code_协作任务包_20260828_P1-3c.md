# Claude Code 协作任务包：P1-3c 统一媒体动作

状态：待 P1-3b4 生产 canary 通过并部署后执行。此包只冻结边界，不代表实现已完成。

## 目标

在不破坏单脑原则的前提下，把文本、语音、贴图、CG、图片、唱歌和定时动作收口到 `ActionPlan + ActionExecutor`。LLM 决定是否及做什么，系统只校验、执行、持久化和报告真实状态。

## Claude Code 负责的只读/受限工作

1. 审查 `agent/tasks.py`、`agent/scheduler.py`、`agent/image_share.py`、`agent/songs.py`、`agent/handler_commands.py` 的现有发送状态机，给出逐符号调用图和最小迁移顺序。
2. 为每个媒体 child 设计 typed payload 与冻结字段：`asset_ref/hash/library_version`、`role_id/model_version`、`song_id/section`、`source_id/scope_id/ordinal`。
3. 补充 handler 端到端红测：媒体-only、同回合多动作、`skip_response + media`、部分确认、unknown 不重发、创建定时动作后切角色、坏 JSON/路径穿越。
4. 对照 Nekro Agent、Letta、OpenClaw 的 durable action/receipt 语义，标出可借鉴点和不能照搬点。

## 禁止范围

- 不改 `ActionEnvelope/ActionReceipt`、`Store`、`NapCatClient` 和生产配置；这些由 Codex 主负责人统一修改。
- 不把随机选图、角色当前状态或当前 mood 作为重试时的隐式输入。
- 不使用关键词门控替 LLM 决策，不恢复 `[SING]`/裸 CQ 文本协议。

## 验收证据

- 每个 child 有独立 domain ID、receipt、outbox/confirmed/uncertain/failed 终态；父计划只聚合，不伪造送达。
- 所有测试、pyflakes undefined、compileall 通过；GitNexus impact 在编辑前完成。
- 真实 canary 只在部署后运行；未触发媒体功能必须报告 `INSUFFICIENT`。

## 当前已知证据

- 即时贴图依赖文字成功；语音不确定或 `skip_response` 会静默跳过贴图。
- 定时/复合动作仍是 plain payload；CG 群路径缺失且私聊忽略 `SendResult`。
- 图片 CQ 会被 `ReplyPipeline.clean` 清洗；唱歌 marker 在清洗后检测而不可达。
- 角色切换未同步 TaskManager；`turn_actions` 单值字段会覆盖同回合重复媒体调用。

