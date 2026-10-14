# Claude Code 协作任务包（2026-08-28）

这是“小糖糖生产窗口全链路审查”的执行拆分。Codex 是主负责人；Claude Code 是协助实现者。任务之间共享工作区，禁止回滚或覆盖他人改动。每项任务开始前：

1. 先读 `CLAUDE.md`、`AGENTS.md` 和 `docs/审查/Codex审查_2026-08-28_生产窗口全链路.md`。
2. 对将要编辑的函数/类逐一运行 GitNexus `impact(direction=upstream)`，HIGH/CRITICAL 必须先回报。
3. 先写红测或最小回归，再改实现；不得用关键词门控替 LLM 做语义决定。
4. 完成后运行 `python -m pytest tests/ -q` 与 pyflakes undefined 检查，提交文件清单、测试输出、未决风险。

## 任务 A：原始事件与批处理隔离（最高优先级）

负责范围：`agent/message_batcher.py`、`agent/store.py` 的必要接口、事件落库测试。

目标：群/私批处理先逐条保存 `event_id/message_id/user_id/chat_id/raw_message/segments`，合并文本只作为 LLM view；任何记忆、反馈、权限和历史查询都使用真实 source IDs。要求同批两个用户的消息不会出现在同一个用户的 `chat_log` 事实行中，重复事件幂等。生产库约有 13,912 条历史 synthetic rows、至少 57 条记忆引用它们：只做可回滚的标记/quarantine 与降信任迁移，禁止直接删除唯一历史。

禁止：修改人格 prompt、用昵称/关键词猜主体、以合并文本替代原始事件。

## 任务 B：群/私统一媒体规范化

负责范围：入站适配、`agent/handler.py` 的媒体解析、`agent/asr.py` 调用边界和测试。

目标：群语音进入与私聊相同的 ASR；图片/语音在 batching 前转为 typed segments，保留原始 CQ；短 TTL `PendingMediaRef` 支持同一 scope 的自然追问，过期或跨用户必须拒绝。

验收：群 `[CQ:record]` 有 transcript/source；图片后“帮我看看刚才那张”调用 `analyze_image` 且回执带 source；无引用或过期不调用。

## 任务 C：ActionEnvelope、贴图/语音与定时任务

负责范围：`agent/tasks.py`、动作执行器、贴图/语音提交边界、任务 Store 接口与测试。

目标：动作可 text-only、sticker-only、voice-only 或组合；定时任务持久化 typed payload + idempotency key；confirmed 后产生统一 proactive event 开窗；uncertain 不重放、不写成功式记忆。

## 任务 D：统一传话与严格历史查询

负责范围：`agent/handler_commands.py`、`agent/handler.py` 的工具 schema/适配、历史查询测试。

目标：一个 `send_message` 支持 `verbatim/natural/relay` 和归因字段；返回 requested/actual/message_id/status receipt。新增 `history_query(scope,speaker,from,to,order,limit)`，原始时间线优先，工具目录不能描述不可调用的 deferred 工具。补 memory claim 的 quote/span 支持校验，低置信/推断不得直接写 verified。

## 任务 E：窗口自治与可观测性

负责范围：`agent/conversation_tracker.py`、窗口决策统计和轻量 LLM 指标。

目标：结构化记录 `observe/respond`、direct/continuation、skip/fade、candidate/routed；连续沉默只推进渐变。轻量 LLM 空回复区分预算耗尽、无效和传输错误。修复自忆缓冲静默丢失和私聊窗口 last_active/summary 死链路；统一 set_reminder/group_say_later 的 behavior 分类。

## 主负责人整合顺序

先合并 A/B（防止新动作继续丢媒体和主体），再合并 C/D（统一副作用与证据），最后合并 E 并做真实回放/soak。任何任务若需要改共享 handler 大段代码，先停下提交 impact 和最小接口设计，避免并行冲突。
