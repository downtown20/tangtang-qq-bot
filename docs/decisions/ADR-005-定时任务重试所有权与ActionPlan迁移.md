# ADR-005：定时任务重试所有权与 ActionPlan 迁移

## 状态

Implemented for Phase 0 + Phase 1 + Phase 2b schema/reducer；Phase 2a/2b 行为 canary 待样本（2026-08-29）

Phase 0 已增量落地 attempt/event、task 当前指针、outbox 归属、状态机触发器和
schema checksum；Phase 1 已将 legacy/typed 纯文本迁移为“冻结计划与 linked outbox
同事务提交，outbox worker 唯一首发并原子结算”。相关 228 项、全量 1354 项测试、
pyflakes undefined=0、compileall 和环境体检通过。boot `3dd84a1f` 的生产 canary 又
证明单文本提醒只发送一次，并以 confirmed/clean、永久 fact/chat 投影和零 linked
outbox 收敛。人工 linked retry 在 Phase 2 的核验/风险确认契约完成前仍 fail-closed。

Phase 2 对抗设计审查为 NEEDS WORK：现有 schema 可接受跨代 payload 漂移，多 child
结算与迟到确认也缺少事实归约边界。实施已拆成 Phase 2a 确定失败单文本 retry 和
Phase 2b confirmation/跨代 reducer。Phase 2a 已完成独立迁移、精确命令、跨代冻结
计划校验、入站事件派生幂等键、回执 actual 闸门和错误边界；receipt v2 会原子替换
旧 `children[0]` guard 并保留 v1 marker。boot `7272978b` 已在功能开关关闭时完成
生产 schema 加载与零错误对账；现无确定失败样本，功能 canary 尚未执行。Phase 2b
源码、迁移与 reducer 已完成，uncertain/partial/cancelled 仍在行为 canary 前禁止重发。

本 ADR 只定义定时任务与发送 outbox 的契约。首个实现切片仍限定为 legacy
text-only 与 typed text；贴图、语音、图片和唱歌沿用 ADR-004 规定的后续顺序。

## 背景与当前取证

### 任务状态和发送入口是两套状态机

* `tasks` 目前只有 `id/owner_qq/description/remind_at/created_at/status`，旧库再
  增加 `group_id/action_payload/idempotency_key`；没有 attempt、plan 或 outbox
  关联字段（`agent/store.py:178-205`）。
* `TaskManager._check_and_send()` 先把 `pending` 原子领取成 `sending`，然后依据
  聚合结果执行 `done`、`uncertain` 或 release 回 `pending`；发送异常直接冻结为
  `uncertain`（`agent/tasks.py:74-116`）。
* `_execute_payload()` 内部直接调用群/私聊 sender。它把所有 child 的结果压成一个
  字符串；无 typed action 时还会在发送前调用 `_compose_message()`
  （`agent/tasks.py:147-207`、`agent/tasks.py:222-244`）。因此当前 legacy
  文本可能在每次重试时再次经过 LLM 改写。
* `ActionPlan` 只冻结有序 child 和父身份，`ActionExecutor` 只在一次执行中按序
  dispatch，并把已有 `confirmed/uncertain/failed` receipt 视为终态而跳过
  （`agent/action_plan.py:67-126`、`agent/action_executor.py:79-113`）。它本身不写
  plan、task 或 outbox。

### NapCat 已经拥有另一位重试所有者

* `NapCatClient._enqueue_retryable_send()` 遇到 `SendResult.retryable` 会写入
  `send_outbox`（`onebot/ws_client.py:288-305`）。群/私聊公开 sender 在网络调用
  返回后都会走该路径（`onebot/ws_client.py:1266-1285`、`1413-1434`）。
* outbox worker 只重放 `pending`，发送结果进入 `confirmed/uncertain/dead`；
  `uncertain/dead` 不盲重放（`onebot/ws_client.py:391-483`）。outbox 的终局会按
  receipt 模板写回执，v2 confirmed 还会提交永久事实（`agent/store.py:2198-2352`）。
* outbox 的已有字段包括 `receipt_template`、`domain_action_id` 和确认快照，但没有
  `task_id/plan_id/attempt_generation`（`agent/store.py:808-849`）。因此只能知道
  child/domain action，无法可靠地推进某一个定时任务。
* 进程重启会把 outbox 的 `sending` 标为 `uncertain`，并为有模板的行生成不确定
  receipt（`agent/store.py:1810-1845`）；TaskManager 则把所有 `sending` task
  标为 `uncertain`（`agent/store.py:4592-4598`）。两个恢复动作没有共同的
  attempt 关联。

### 现有人工 retry 与执行器终态规则不相容

`retry_task()` 只把 task 从 `uncertain` 改回 `pending`，没有改变 action identity
（`agent/store.py:4600-4608`）。如果新执行仍使用旧 action ID，
`ActionExecutor` 会按其“uncertain 是终态、不得静默重试”规则跳过；如果直接生成
新 ID，又没有持久的 generation/plan 关联，旧回执和新任务可能串联。现有测试明确
要求 text uncertain 不得在下一轮重放、全部确定失败才 release
（`tests/test_task_actions.py:206-220`、`309-322`）。

这些事实表明：仅把 `_execute_payload()` 外面包一层 `ActionPlan/ActionExecutor`
不能完成可靠迁移；retry owner、attempt identity 和 task↔outbox 归属必须先定下来。

## 决策

### 1. 自动重试按失败类型划分，但每次发送只有一个 owner

采用 **outbox 负责首次 transport 与可重试 transport，TaskManager 只负责任务编排**
的契约。迁移后的 child 只能有一个网络发送者，TaskManager 不再同步直发。

1. TaskManager claim 后生成并冻结 plan；在同一 SQLite 事务中写入 plan、创建
   task-linked `pending` outbox，并把 attempt 标记为 `outbox_pending`。事务提交前
   禁止任何 QQ/NapCat 网络调用。
2. outbox worker 领取该行后负责**首次发送和后续有限重试**。TaskManager 在该
   generation 存在未决 outbox 时不得调用 sender、不得 release 回 `pending`。
3. payload/资产校验等本地确定失败必须在 outbox 入队前 fail-closed；已经入队后
   的 OneBot 确定拒绝由 outbox 记录 `failed/dead`，不能交回 TaskManager 再发。
4. outbox 达到最大次数后进入 `dead`，不再交给 TaskManager 自动重试；对应 task
   进入 `failed` 并要求主人明确 retry。这样不会出现“outbox dead 后任务下一分钟
   又自动发送”的第二个 owner。
5. `uncertain`（网络响应丢失、进程重启等无法判断是否送达）永不自动重放。
   只有人工核验或显式 retry 才能开启新 generation。

`outbox_pending`/`dead` 是内部执行状态。只有全部 ordinal 明确送达才显示完成；
known-confirmed 的本地归账问题另显示运维告警，不得把“已经送达”改写成“不确定”。

### 2. attempt generation 是重试的身份边界

每个 task 只保存 `current_attempt_id`；每个 attempt 持有该 task 内单调递增的
`generation`，从 0 开始。CAS 以当前 attempt ID 为准，generation 用于展示和 lineage。

* `root_source_id = "task:<task_id>"` 永久标识逻辑任务；同源创建幂等仍由现有
  `idempotency_key` 负责（`agent/store.py:4516-4547`）。
* 一次具体执行使用
  `source_id = "task:<task_id>:attempt:<attempt_generation>"`。该值写入
  ActionPlan 和所有 child，参与 v2 action/plan ID 派生；同一个 generation 的
  action ID 永不改变。
* 发送前如果没有 plan 快照，先把最终文本（包括 legacy 的 LLM 改写结果）冻结到
  该 generation；发送后不得重新调用 `_compose_message()` 或重新选择媒体。
* 进程重启、outbox worker 重试和普通调度循环都复用同一 generation；它们只能读取
  已冻结 plan/receipt，不能递增 generation。
* 主人明确 retry 时，事务内按 child ordinal 检查旧 generation 的终态：只有全部
  预期 child confirmed 才收敛 task 为 done；新 generation 仅复制明确 failed 或经
  人工确认未送达的 ordinal。旧 attempt、plan 和 receipt 永久保留，新 action ID
  使用同一逻辑 ordinal。这样 `ActionExecutor` 的“终态不重放”与人工重试同时成立。

generation 的 CAS 条件是所有终态回写的必需条件：旧 worker、迟到的 outbox 回执或
  重复 webhook 只能写入审计，不能覆盖新 generation。

### 3. TaskManager ↔ outbox 的持久关联

采用向后兼容的规范化 attempt 表，不修改或删除既有 action/receipt 事实。计划不能
直接只存在 `tasks` 当前行：generation 递增会覆盖旧计划，破坏审计和迟到回执核验。

`tasks`：

| 字段 | 约束与用途 |
| --- | --- |
| `current_attempt_id INTEGER DEFAULT NULL` | NULL 表示尚未生成计划；外键指向当前 attempt，只能由 claim/retry CAS 推进。 |

`task_action_attempts`（每个 generation 一行）：

| 字段 | 约束与用途 |
| --- | --- |
| `id INTEGER PRIMARY KEY` | outbox、事件和 tasks 当前指针的唯一引用目标。 |
| `task_id, generation` | task 外键 `ON DELETE RESTRICT`，generation 非负，组合唯一。 |
| `plan_id TEXT NOT NULL UNIQUE` | 当前 generation 的冻结 plan ID。 |
| `plan_json TEXT NOT NULL` | 完整 immutable plan；legacy LLM 改写结果不会因重启/重试改变。 |
| `state TEXT NOT NULL` | `persisted/outbox_pending/sending/confirmed/uncertain/failed/dead/partial/cancelled`，只表达发送/计划状态。 |
| `accounting_state TEXT NOT NULL DEFAULT 'none'` | `none/clean/pending_repair/conflict`；与 delivery 正交，known-confirmed 不降级为 uncertain。 |
| `last_error, created_at, updated_at` | 运维证据；正文只存在 plan，不进入日志。 |

禁止更新 attempt 的 `task_id/generation/plan_id/plan_json`；状态迁移另写
`task_action_events` 审计行。outbox 的 `confirmed_unaccounted/confirmed_conflict` 都
表示该 child 已确认送达，只映射为 accounting `pending_repair/conflict`，绝不能降级
成 delivery `uncertain`。

`task_action_events` 是 append-only 事实表：`attempt_id` 外键、`event_type`、
`from_state/to_state`、`outbox_id/domain_action_id`、`actor/reason/metadata_json`、
`created_at`。attempt 状态 CAS 与 event INSERT 必须同一事务；UPDATE/DELETE 由触发器
禁止。人工核验、retry、迟到回执、回滚和 known-confirmed repair 都必须留事件。

`send_outbox`：

| 字段 | 约束与用途 |
| --- | --- |
| `task_attempt_id INTEGER DEFAULT NULL` | NULL 表示非定时任务旧行；非 NULL 时外键指向唯一 attempt，禁止复制 task/generation/plan 三份真值。 |
| `ordinal INTEGER NOT NULL DEFAULT 0` | child 在 plan 中的位置；与 `domain_action_id` 联合唯一。 |
| `retry_owner TEXT NOT NULL DEFAULT 'outbox'` | 只有 `outbox` 可领取；新 task-linked 行必须显式为 outbox。 |

增加唯一索引 `(task_attempt_id, ordinal)`（仅 `task_attempt_id IS NOT NULL`）和查询
索引 `(task_attempt_id,status)`；worker 领取索引以
`(retry_owner,status,next_retry_at,created_at)` 为序。既有 `domain_action_id` 仍是 child
的唯一动作身份，不能用 attempt ID 代替。所有连接继续开启 SQLite foreign_keys，
孤儿关联 fail-closed。

所有 task/outbox 关联写入必须在同一 SQLite 事务中完成：plan 快照与初始
`pending` outbox 先于任何网络请求共同提交。若事务失败，不得调用 QQ API；禁止
采用“TaskManager 先直发，失败后由 NapCat 另开事务补写 outbox”的双写方式。

现有 `ActionExecutor` 表示“执行后取得 terminal receipt”，不能把“成功入队”伪装成
送达 receipt。定时路径在事务内只持久化 `ActionPlan/ActionEnvelope` 和 outbox；
outbox worker 才是 durable executor，并复用相同的 child identity/receipt 校验。

task-linked outbox 的每个 terminal settle 必须由 Store 在**一个 SQLite 外层事务**中
完成：① outbox 更新/删除或 confirmed snapshot；② receipt/confirmed fact；③ attempt
delivery/accounting；④ task 当前投影与 `current_attempt_id` CAS；⑤ append-only event。
正常 confirmed 投影使用 savepoint；若永久归账失败/冲突，只回滚该 savepoint，并在
同一外层事务保存 confirmed snapshot、accounting repair/conflict、task delivery 累计
和事件后提交，绝不能先删 outbox 再另事务补 task。Phase 1 的旧 attempt 终局不移动
current pointer；Phase 2b 有 confirmation 事实后可重算逻辑 task 状态，但仍不得回拨
或覆盖 current attempt。

### 4. receipt 与父状态映射

`ActionPlan.aggregate_status()` 的结果是父计划事实；TaskManager 再把它投影到旧
任务状态。映射如下：

| child/plan 事实 | TaskManager 投影 | 自动动作 | 说明 |
| --- | --- | --- | --- |
| 所有预期 ordinal 累计 `confirmed` | `task.status=done`, attempt=`confirmed` | 无 | 先完成 ADR-003 永久归账；`on_confirmed` 以 `(task_id,generation,plan_id)` 幂等且只调用一次。 |
| 某 child QQ confirmed，但本地归账失败/冲突 | 该 ordinal 按 confirmed 累计；attempt accounting=`pending_repair/conflict` | 仅 DB repair/审计 | `confirmed_message_ids/confirmed_at` 是送达证据；仅当全部预期 ordinal 均送达时 task 才 done，否则仍 partial。 |
| 任一 child `uncertain` | `task.status=uncertain`, attempt=`uncertain` | 禁止 | 响应丢失或重启；没有 confirmed fact 不代表没有送达。 |
| confirmed + failed/uncertain（`partial`） | `task.status=partial`, attempt=`partial` | 禁止 | 跨 generation 按 ordinal 累计；confirmed ordinal 永不复制。 |
| 全部确定 `failed` / outbox `dead` | `task.status=failed`, attempt=`failed/dead` | 禁止 | failed 是终态；不得回 pending 或复用同 action ID。 |
| plan 已持久化并有 pending/sending outbox | `task.status=sending`, attempt=`outbox_pending/sending` | 仅 outbox worker | outbox 同时负责首次发送；TaskManager 不得直发或 release。 |

缺少 child receipt、receipt 身份不匹配或 plan 快照损坏均按 `uncertain` 处理，不能
  推导成 `confirmed`。

### 5. 崩溃恢复

启动顺序固定为：恢复 outbox → 恢复 task attempt → 启动调度/worker。

1. `send_outbox.status='sending'` 的行先转 `uncertain`，生成
   `PROCESS_RESTART_UNCERTAIN` receipt；关联的 task generation 以 CAS 转为
   `uncertain`。不得重新调用 QQ API。
2. `send_outbox.status='pending'` 的行保留，仍由 outbox owner 领取；关联 task 保持
   `status=sending, attempt.state=outbox_pending`，TaskManager 的 due 查询排除它。
3. task 为 `sending` 但没有关联 outbox 时，先按 attempt/action identity 查询 receipt、
   confirmed fact、outbox confirmed snapshot 和 accounting repair/conflict。存在任何
   送达证据就只做 DB-only 收敛；全部证据均不存在时才转 `uncertain`，不得重新执行
   `_compose_message()` 或发送。
4. confirmed outbox 即使本地投影失败，该 ordinal 仍按“已送达”累计，并把 attempt
   `accounting_state` 设为 `pending_repair/conflict`；只允许 DB-only repair/审计，
   不得回到任何网络发送路径。其余 ordinal 仍独立决定 task 是 done 还是 partial。
5. 恢复和 outbox 回写以 `task_attempt_id` 定位 immutable plan，再以
   `tasks.current_attempt_id` 做 CAS；迟到的旧 generation 回执不得覆盖或回拨 current
   attempt。Phase 2b 可基于追加式 confirmation 跨代重算逻辑 task 状态。

### 6. 人工 retry 语义

`retry_task` 改为显式的新 generation 事务，参数至少包含
`expected_attempt_id`（响应同时展示 generation）和核验结论，而不是简单把同一
action 放回队列：

1. pending/sending outbox、known-confirmed outbox/accounting 告警或 current attempt
   CAS 不匹配时一律拒绝对应 ordinal；已确认送达只能做 DB repair。
2. 所有预期 ordinal 已 confirmed 才收敛 done；partial 不得因“有一张 receipt”就
   把整计划标完成。
3. failed/dead ordinal 可复制到新 generation；uncertain ordinal 只有在
   `verification=verified_not_delivered`，或操作者明确提供 `force_resend_ack` 并接受
   重复风险时才可复制。confirmed ordinal 永不复制。
4. 新 generation 复用冻结业务 payload 和原逻辑 ordinal，生成新 source/action/plan
   ID；默认不重新调用 LLM。旧 attempt/plan/receipt 保持不可变。
5. 新 generation 直接在同一事务建立 attempt + outbox，不先退回裸 `pending`；迟到
   的旧 generation 回执不能覆盖当前指针，但可在 Phase 2b 形成 confirmation 并触发
   task reducer。
6. retry 写审计事件（task、旧/新 generation、操作者、核验结论、是否接受重复风险）；
   CAS/验证失败返回明确错误，不能静默吞掉。

#### Phase 2 接口合同（对抗审查冻结）

Phase 2 拆为两个有独立上线闸门的切片。**Phase 2a 只重试已确定失败的单 child
纯文本**；`uncertain/partial/cancelled` 仍 fail-closed。Phase 2b 在 child 事实账本、
迟到证据和跨 generation reducer 完成后，才开放“已核验未收到/接受重复风险”。不借
人工 retry 顺手迁移语音、贴图或复合计划，也不把重试开放成任意 LLM 可调用工具；
先由精确 `/任务` 命令承接，后续自然语言工具也必须复用同一 Store 合同。

新增接口采用结构化结果，不再让 `bool=False` 同时表示“不存在、版本过期、正在发送、
需要核验或已确认”。结果至少包含 `ok/code/task_id/current_attempt_id/current_generation`，
成功创建下一代时再包含 `new_attempt_id/new_generation`。handler 只按稳定 `code` 映射
中文提示，不向用户暴露 SQLite 异常或内部错误文本。旧 `retry_task(task_id, owner_qq)`
保留为 legacy-only 兼容入口；linked task 一律走带版本条件的新接口。

新 retry 输入至少包含：

* `task_id + owner_qq`：在同一事务校验归属，其他用户只能得到统一的
  `NOT_FOUND_OR_NOT_OWNER`，不能枚举任务；
* `expected_attempt_id`：用户从 `/任务` 列表复制的重试令牌，也是 optimistic CAS；
* `verification_result`：完整合同包含 `VERIFIED_NOT_DELIVERED`、
  `DELIVERY_UNKNOWN` 和 `NOT_REQUIRED`；Phase 2a 只接受 `NOT_REQUIRED`；
* `force_resend_ack`：只有 `DELIVERY_UNKNOWN` 时可为 true，表示操作者明确接受重复
  送达风险；不能由默认值、LLM 猜测或模糊自然语言补齐；
* actor 由已认证的 `owner_qq` 和入口类型派生，调用方不能伪造任意 actor。

允许/拒绝矩阵：

| 当前 attempt / outbox | Phase 2 retry | 结果 |
| --- | --- | --- |
| `failed/dead`，且 error/receipt 证明是确定失败、无 open outbox、accounting 为 `none/clean` | `NOT_REQUIRED` | Phase 2a 允许创建下一 generation |
| `uncertain`，无 open outbox，accounting 为 `none/clean` | 已核验未送达 | Phase 2a 返回 `VERIFICATION_NOT_SUPPORTED`；Phase 2b 才允许 |
| 同上，但仍无法确认 | `DELIVERY_UNKNOWN + force_resend_ack=true` | Phase 2a 拒绝；Phase 2b 才允许并审计重复风险 |
| `persisted/outbox_pending/sending` 或 outbox `pending/sending` | 任意 | `IN_FLIGHT`，拒绝 |
| `confirmed` 或 known-confirmed/accounting repair/conflict | 任意 | `ALREADY_DELIVERED` 或 `ACCOUNTING_REPAIR_REQUIRED`，绝不发网 |
| `partial/cancelled`、损坏/非单文本 plan | 任意 | 首切片 `UNSUPPORTED_STATE/INVALID_FROZEN_PLAN`，拒绝 |
| current pointer 与 `expected_attempt_id` 不同 | 任意 | `STALE_ATTEMPT`，返回当前 generation 供重新查看 |

成功事务固定为：`BEGIN IMMEDIATE` → 校验 owner/current attempt/终态 outbox → 从旧
immutable `plan_json` 读取冻结文本并重新校验 identity → 以
`task.status + current_attempt_id` CAS 暂投影为 `sending` → 插入 generation+1 的新
attempt、plan、action identity 与 pending outbox → 将 current pointer CAS 到新 attempt
→ 在旧、新 attempt 各追加带 actor、核验来源和重复风险的关联事件 → commit。事务内
不得调用 LLM 或 QQ；任一 SQL/identity 校验失败全部回滚，旧 task/attempt/outbox 原样
保留。旧 terminal outbox 不删除、不改 action ID，新 generation 复用业务文本而不是
旧 action identity。

`/任务` 对 linked task 展示“任务号、状态、第 N 代、attempt 令牌”，并纳入需要处理的
`failed/partial`，而不只显示 pending/sending/uncertain。精确命令采用：

* `/任务 重试 <任务号>@<attempt令牌>`：Phase 2a 仅确定失败；
* `/任务 重试 <任务号>@<attempt令牌> 未收到`：Phase 2b 只记录用户观察并返回需要
  风险确认；它不能伪装成权威 `VERIFIED_NOT_DELIVERED`；
* `/任务 重试 <任务号>@<attempt令牌> 接受重复风险`：Phase 2b 确实无法核验且明确
  接受风险。

后两种语法可先解析为明确的“当前版本尚未开放”，但 Phase 2a 不能据此创建 outbox。

旧 `/任务 重试 <任务号>` 只兼容 `current_attempt_id IS NULL` 的 legacy uncertain；对
linked task 返回当前令牌和完整用法，不能暗自补条件。命令是精确副作用确认，不属于
关键词替 LLM 做对话决策。

取消也必须结构化并与 worker claim 原子竞争。linked outbox 仅在
`status=pending AND attempts=0` 时允许按“outbox→cancelled、attempt→cancelled、
current pointer→NULL 且 task→pending、task→cancelled”的同一事务顺序关闭；若 worker
先 claim，则返回 `IN_FLIGHT`，不得把可能已经发出的消息伪装成已取消。对 uncertain 的
“取消”只表示停止后续处理，不代表撤回可能已经送达的消息，linked uncertain 首切片
继续拒绝修改其送达事实。

现有 Phase 0 schema 只能证明**单 child 理想路径**可在一事务建立连续 generation；
它不足以作为完整 Phase 2 的数据库合同。反向探针已证明：数据库本身不能阻止新一代
偷换冻结 payload；多 child 第一条完成时，attempt 级终态会与其余 open outbox 冲突；
旧 generation 进入 `uncertain` 后也无法记录并归约迟到的确认事实。因此开放人工 retry
前必须先完成 Phase 2 schema 迁移：显式 retry lineage、由 immutable plan 自动物化的
预期 ordinal、单调 confirmation 事实、跨 generation reducer，以及可审计的迟到
delivery evidence。无需再复制一张 child 表：generation 0 plan 是预期账本，outbox
表示开放/负向事实，confirmation 表表示正向事实。迁移必须升级 trigger manifest/
checksum，不能绕过 Phase 0 终态触发器。

Phase 2a 新增 append-only `task_action_retry_requests`：保存 request ID、task、expected/
new attempt 与 generation、actor、verification、风险确认、选择/跳过 ordinal、结果码、
证据摘要和时间。accepted 必须关联唯一 new attempt，rejected 不得关联；同 request ID
重放只返回原结果。generation lineage 可由同 task 的 `generation-1` 推导，不再重复存
`retry_of_attempt_id`。同时增加 DB 跨代 business JSON 比较触发器：新旧单文本 child 的
kind/channel/target/ordinal/payload 必须完全一致，仅 source/action/plan identity 与时间
可变化；不能只靠调用方“记得复制旧文本”，也不能接受调用方传入的正文或孤立 hash。

attempt 增加 immutable、唯一的 `retry_request_id`；generation 0 必须为 NULL，后续代
必须非 NULL。request 的 `new_attempt_id` 与 attempt 的 `retry_request_id` 以 deferred
FK 和 insert guard 双向闭合：二者必须互相指向，expected/new generation 连续且属于
同一 task。request 中 `selected_ordinals_json` 必须与新 plan 的 ordinal 集合完全相等；
新 plan 每个 ordinal 再与 generation 0 的 canonical kind/channel/target/scope/payload/
conversation_ref 比较。accepted/rejected request 均不可 UPDATE/DELETE，确保失败请求也
留下 reason code，而不会只有成功历史。

Phase 2b 新增 append-only `task_action_confirmations` 正向送达事实表，按
task/attempt/ordinal/action/outbox 保存 evidence source、message IDs、证据 JSON、确认/
记录时间和 actor。`domain_action_id` 唯一，但 `(task_id, ordinal)` **不能唯一**，因为同一
逻辑 ordinal 的第二次 confirmed 必须被保留为重复送达证据。正常 confirmed、
known-confirmed 和未来 late confirmation 都先进入该事实面，再由跨 generation reducer
结合 generation 0 的预期 ordinal、各代 plan 和 terminal outbox 投影 attempt/task。
`current_attempt_id` 只表示最新 generation，永不回指旧代；若旧代确认覆盖了尚未 claim
的新 outbox，reducer 应在同一事务取消该 ordinal，已 claim 则保留重复风险审计。

linked outbox 增加删除保护：只有同一事务已经插入匹配 attempt/ordinal/action 的
confirmation 才能删除；uncertain/dead/cancelled 不得被静默清理。attempt reducer 只看
本代 plan：有 open child 保持 sending/outbox_pending，全 confirmed 才 confirmed，
confirmed 与 uncertain/dead 混合为 partial，无 confirmed 且有 uncertain 为 uncertain，
全确定失败才 failed/dead。task reducer 则按 generation 0 的全部逻辑 ordinal 跨代
归约：任一代 confirmation 永久胜出；没有 confirmation 才取 lineage 最新 occurrence。
`DELIVERY_UNKNOWN + force` 不抹掉祖代 uncertainty，因此后代尚未 confirmed 时 task 仍
保持 uncertain。attempt 本代状态与 task 跨代状态不得再由同一个 helper 直接赋值。

当前 QQ 发送是一次 30 秒 HTTP POST；read/write timeout、5xx 或协议损坏转为
`NETWORK_UNCERTAIN` 后没有 future/webhook，WebSocket router 也不接 `message_sent`，而
`get_msg` 又需要未知路径恰好缺失的 message ID。因此现在不存在权威自动 late-confirm
通道。“用户没看到”只能算 `DELIVERY_UNKNOWN`；只有未来接入可关联的历史查询/
message-sent echo，或用户明确接受重复风险，Phase 2b 才能创建 uncertain 的下一代。

两次迁移都要使用独立 version/checksum。首次升级先验证 Phase 0 manifest，再在
savepoint 内迁移；后续启动由最高版本验证完整 manifest，不能让 Phase 0 的 exact
trigger 清单把合法新 trigger 误判为漂移。上述结构化 API、命令语法和单事务 CAS
仍作为入口合同保留；`_append_task_action_event_conn()` 同时接收经边界派生的 actor。
在相应迁移与红测完成前，linked retry 继续 fail-closed。

迁移预检必须拒绝已有 generation>0、孤儿 outbox、plan/outbox identity 漂移或无法
重建的 terminal attempt；Phase 2b 只从 `confirmed_action_facts` 和保留的
known-confirmed outbox 无歧义回填 confirmation，uncertain/dead 绝不伪造成确认。建表、
回填、reducer 对账、重建受影响 trigger、完整 manifest/checksum 校验和 migration marker
都在同一 savepoint；marker 已存在时必须先走最高版本校验，不能先执行旧 Phase 0 的
exact-manifest 检查。成功升级后旧二进制应安全拒绝启动，只允许新二进制内 feature-off。

实现必须先写红以下路径：25 路并发 retry 只能创建一个 generation/outbox；重复命令
返回 stale 而不再发；owner 隔离；三种核验组合；confirmed/known-confirmed/open
outbox/损坏 plan 拒绝；每个 SQL 故障点全回滚；cancel 与 claim 竞速只能一方成功；
重启后新 pending generation 只发一次；旧 generation 的迟到 settle 不覆盖或回拨
current attempt，但能重算逻辑 task；跨代 payload 偷换由 DB 拒绝；retry request 重放
幂等；列表稳定展示状态和令牌；
整个 retry 路径 LLM/QQ 调用次数均为零。多 child 还必须证明第一条 settle 不会因其余
open outbox 卡死，`confirmed+failed/uncertain` 聚合为 partial，同 ordinal 多次 confirmed
进入告警而不被唯一约束吞掉。

### 7. 首切片和后续迁移闸门

**Phase 0：schema 与状态机（必须先完成）**

* 增量增加 tasks 当前指针、`task_action_attempts/task_action_events`、outbox 关联字段、
  外键/触发器和索引；旧 task 指针与旧 outbox 关联均为 NULL，不重写历史 receipt。
* 为 plan 不可变、引用完整性、CAS、唯一索引、旧库二次迁移和回滚写 DB 测试；迁移
  失败必须原子回滚。Phase 0 只建 schema，不产生任何 task-linked outbox。

**Phase 1：legacy/typed text plan freeze + 原子入 outbox（首个业务切片）**

* `TaskManager` 仅迁移 legacy text-only 与 typed text；claim 后一次性生成
  `ActionPlan`，文本和 identity 与 task-linked pending outbox 在同一事务落盘。
  TaskManager 不再调用群/私聊 sender；outbox worker 是唯一网络入口。
* ActionPlan child 必须带 group/private scope、target、ordinal=0、source_id 和
  `conversation_ref`（v2）；sender 回执必须能回填 `message_ids` 与 delivery state。
* 贴图、语音和其他 payload 继续旧路径，不能借首切片顺手改动。
* **实现状态（2026-08-28）**：上述纯文本路径、linked claim/settle/restart、
  known-confirmed DB-only repair 和唯一 retry owner 已完成。legacy 媒体仍由
  TaskManager 独占重试，调用 NapCat 时禁止另建普通 outbox；待新 boot canary。

**Phase 2a：确定失败的单文本人工 retry**

* Phase 1 必须同时包含 outbox 首次发送，以及单事务完成 confirmed/uncertain/dead、
  confirmed_unaccounted/conflict 的 outbox+receipt/fact+attempt+task+event 结算；未完成
  终态回写不得启用 task-linked outbox。失败只更新同一行，禁止 sender 再 enqueue。
* 仅允许单 child text 的确定 failed/dead；以 request ID、expected attempt 和 actor 审计，
  从旧 immutable plan 克隆业务 payload，数据库拒绝跨代 payload 漂移。
* `uncertain/partial/cancelled` 与任何 confirmed/known-confirmed/open outbox 全部拒绝。
* **实现状态（2026-08-29）**：独立 version/checksum、append-only retry request、
  deferred 双向 lineage、跨代 business-plan trigger、冻结回执 actual 校验和精确
  `/任务 重试 task@attempt` 已落地。相同平台入站事件派生稳定 request ID；异常返回
  稳定提示且不泄露数据库细节。receipt v2 迁移会保留 v1 marker、替换固定 child0
  guard，并拒绝自洽 checksum 下的旧 DDL。相关 76 项、全量 1407 项、pyflakes
  undefined=0、compileall、体检、生产库只读副本迁移和独立 8 组故障探针通过。
  boot `7272978b` 已完成生产 schema canary，`manual_retry_generation_enabled=false`
  维持默认关闭；真实确定失败样本的功能 canary 通过前不开放。

**Phase 2b：uncertain、迟到证据与跨 generation reducer**

* 建立正向 confirmation 事实面，统一 normal/known/late confirmed；按 generation 0 的
  逻辑 ordinal 跨代累计，confirmed ordinal 永不复制。
* 修复多 child claim/settle 和 partial 聚合；接入权威未送达证据或显式重复风险确认后，
  才开放 uncertain retry。该切片是定时媒体迁移的前置条件。

**实现状态（2026-08-29，代码与生产 schema 已落地，行为待 canary）**：已新增独立 Phase 2b
迁移（逻辑 `task_action_items`、物理 `task_action_children`、append-only
`task_action_confirmations`），迁移从 Phase 1 的 immutable plan 与既有 confirmed fact
幂等回填；linked outbox 删除必须先有 confirmation，confirmation/duplicate 事件保留
来源、时间、message IDs 与 generation。settle 已改为“物理 child 结果 → attempt reducer
→ task reducer”，多 child 首条 confirmed 不再终结整代，第二 child 可继续 claim；新增
内部平台回调入口 `_record_task_confirmation_from_platform()` 摄取迟到证据并在后代尚未
claim 时原子取消；公开 `record_task_confirmation()` 已 fail-closed，未接入可信平台回调
前不得由命令层伪造确认。Phase 2b
扩展 late-confirmed 状态转移并同步 Phase 2a marker，所有 DDL/回填在既有 savepoint
内 fail-closed。confirmation 现在必须与对应 outbox 的发送/终局状态一致；普通发送确认
接受非零 OneBot signed int32；没有 message_id 的 confirmed/known-confirmed 一律降为
uncertain 或保留待审查，禁止生成永久事实；旧 v1 宽松触发器
留下的污染确认在启动校验中拒绝。strict fact-anchor 还要求 mailbox 的 action/status
均为 confirmed，并在启动健康检查中复核同一锚点；已知 pre-guard marker 受控升级，未知
marker 仍 fail-closed。全量 1442
项与生产副本迁移通过；原始 `memory.db` 当前 Phase 2b/anchor marker/checksum 一致且
health 全 0。最新全量回归为 1448 项；已知旧 anchor marker（含 9217…合同）可原子升级；历史空 confirmation 证据会在启动校验中 fail-closed。本轮补齐多 child 合法 `sending+pending`、`confirmed_unaccounted/conflict` 健康状态，聚合 accounting 不会被兄弟动作覆盖，重复投影失败可重启并继续修复。boot `8ecb37a1` 已安全重启上线，生产 health/integrity 全 0。
运行进程仍需真实迟到/重复 canary，uncertain 自动 retry 与媒体迁移不提前宣称完成。

本轮护栏补充：`task_action_children` 的 outbox/状态/删除均为 append-only
且有重启对账；`confirmed_action_facts` 也禁止 UPDATE/DELETE、校验列与快照
一致性，并对带 `ordinal` 的新 v2 快照重算 `action_id` 身份锚点。历史 v2 快照
没有 `ordinal` 时保持兼容，只执行原有列↔快照检查。SQLite 文件若被拥有写权限
者同时改写事实列和快照，仍不能视为可信来源；生产运维应限制数据库写权限，
后续再评估外部签名/审计锚点，数据库管理员权限仍是明确的信任边界。

**投影版本底线（2026-08-29）**：Phase 2b 投影后，plan 的 child 版本不能在重启
时被同步降级来绕过 v2 身份校验。新增只追加表
`task_action_projection_anchors(attempt_id, ordinal, action_id, schema_version)`
及独立 floor marker；迁移按当时 child 版本回填，materialize 与 child 同事务建立
anchor，启动时校验 anchor↔plan↔child 三方一致。历史 legacy v1 仍按 v1 锚定，保留
兼容性；已投影 v2 降级、删除 child/item/outbox 或缺失 anchor 均 fail-closed。该表
只保存版本底线，不改变现有 logical item/physical child/reducer 语义。

**Phase 3：后续媒体与长期观测**

* 观察 orphan outbox、duplicate action、旧 generation 回写、LLM 重写次数。
* text 稳定后再迁 voice/sticker/image/sing；每类 payload 必须先冻结可重放资产，再
  复用同一 owner/attempt 契约。

**Phase 4：删除 legacy 直发**

仅在以下指标连续通过后移除旧入口：无未关联 outbox、无重复 domain action、重启
  演练无重放、confirmed projection 无未处理冲突、TaskManager 与 outbox 的状态对账
  为零。旧 v1 receipt 只读保留，不删除历史。

## 验收断言（实现前先写成测试）

1. **冻结一次**：legacy text 有 LLM 时，计划创建只调用一次 `_compose_message()`；
   重启、outbox 重试和调度循环均发送同一冻结文本。
2. **唯一 owner**：plan 与唯一 outbox 同事务预写；TaskManager 从不调用 sender，
   outbox worker 负责首次发送且尝试次数受上限约束。
3. **身份不串**：两个 task、两个用户、群/私聊或不同 generation 的
   `(attempt_id, plan_id, action_id, scope_id, target)` 均不同；旧 receipt 不能通过
   executor 的 identity/CAS 校验。
4. **状态映射**：全 ordinal 累计 confirmed→done；known-confirmed 本地归账失败仍
   →done+repair；任一 uncertain→uncertain；confirmed+failed/uncertain→partial；
   全 failed/dead→failed，均不自动回 pending。
5. **崩溃窗口**：在“QQ 请求已发出但本地未 settle”、task claim 后、outbox sending
   三个故障点强杀并重启，均不自动重发；产生可追踪 uncertain receipt。
6. **人工 retry**：confirmed ordinal 不复制；failed/dead 可产生新 action ID；
   uncertain 必须携带核验/重复风险确认；并发双 retry 只有一个 CAS 成功。
7. **投影幂等**：重复 outbox confirmed、迟到旧 generation 回执和本地 projection
   异常不会重复写 chat/memory/window，也不会重新调用 QQ API。
8. **兼容性**：无 payload 的旧任务仍按 legacy 文本发送；typed text 原文不走 LLM
   改写；贴图/语音测试路径在首切片中保持原断言。
9. **可观测性**：每个 child 由 `task_attempt_id + ordinal` 反查 task、generation、
   plan、outbox、mailbox 和 confirmed facts；known-confirmed repair 状态单独告警。

## 备选方案与取舍

### A. TaskManager 继续独占重试

发送时不传 receipt template，关闭任务 outbox。改动小，但无法利用已有 outbox 的
重启隔离、永久回执和 DB-only repair；不满足长期可靠性目标，拒绝作为 B 方案。

### C. 仅内存 ActionPlan/ActionExecutor

不增加 schema，短期可通过单元测试，但进程在“网络请求与状态写入之间”崩溃后没有
plan、generation 或关联可恢复，仍会重放/丢任务；拒绝进入生产。

### 复用 uncertain 的旧 action ID 做人工 retry

与 `ActionExecutor` 的终态跳过和 receipt mailbox 的冲突保护矛盾；会把“明确人工
重试”误判为重复事实，拒绝。

### 先直发、retryable 后补 outbox

现有 NapCat 会在 sender 返回后自行打开 Store 事务补写 outbox，而 TaskManager 的
状态更新在另一个事务中；无法把外部 QQ 请求、outbox 和 task 状态原子绑定。真实
探针已复现同一逻辑任务同时处于 `task=pending` 与 `outbox=pending`，两位 owner
都能再次发送。因此拒绝该过渡方案。

## 外部设计依据

* [AWS Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
  要求业务状态与待发送事件在同一数据库事务写入，再由独立 worker 发布；同时提醒
  消费方必须能处理重复。对应本项目即“plan + task-linked outbox 先提交，后发 QQ”。
* [Temporal Activity Definition](https://docs.temporal.io/activity-definition#idempotency)
  明确指出 Activity 可能执行多次，外部副作用需以稳定 idempotency key 去重，并应把
  复合副作用拆为细粒度 activity。QQ 发送端不接受本项目 action_id 作为幂等键，故
  `uncertain` 不能自动重放，只能冻结等待核验。
* [Celery Tasks](https://docs.celeryq.dev/en/main/userguide/tasks.html)
  只建议对幂等任务启用 late acknowledgement。QQ 发消息并非天然幂等，因此不能把
  worker 崩溃后的“未确认”简单退回队列；本 ADR 采用 generation + uncertain 隔离。

## 后果

正面：一次尝试只有一个自动重试者；计划、子回执、outbox 和任务可以按 generation
对账；重启和人工 retry 不会把旧事实误认为新发送；legacy 文本的 LLM 改写结果可
冻结并追踪。

成本：需要一次增量 SQLite schema 迁移、outbox→task 的事务回调、状态统计和新的
故障注入测试；`outbox_pending/dead/partial/known-confirmed repair` 需要在控制台/LLM
工具层明确展示。首切片不应在 Phase 0 和 Phase 1 终态回写全部完成前启用。

## 回滚

Phase 0 尚未产生 task-linked 数据时允许旧二进制回滚。Phase 1 启用后只支持**新
二进制内 feature-off**，不允许直接降级旧二进制：旧恢复逻辑会把 task-linked
`sending` 错判为 uncertain，且不会做终态 CAS。

Phase 1 的紧急 feature-off 是**冻结而非降级**：先设置
`tasks.text_action_outbox_enabled=false`，停止领取新的纯文本任务；需要停止发送时再设
`tasks.send_outbox_claims_enabled=false`。若只关闭后者，启动代码会联动关闭前者，避免
继续制造无人领取的 linked outbox。两个开关都在 claim 前生效，已有 plan/event/
receipt/outbox/confirmed facts 一律不删、不回退、不转交 legacy sender。

恢复 legacy pending、人工 retry 或清理未发送 attempt 属于 Phase 2 的显式核验流程，
Phase 1 不提供自动转换。任何时刻不得同时存在 `task=pending` 与同 task 的
`outbox=pending/sending`；只有所有关联状态完成对账后才允许旧二进制回滚。
