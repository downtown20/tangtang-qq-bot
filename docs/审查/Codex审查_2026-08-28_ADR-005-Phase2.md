# Codex 审查：ADR-005 Phase 2

## 判定

**Request Changes / NEEDS WORK（完整 Phase 2）**。Phase 1 不回退；可先实现受限的
Phase 2a，但当前源码不得直接开放 linked `uncertain` 人工重试。本次为只读设计审查，
没有修改运行逻辑。

## Critical

### 1. 跨 generation 的冻结 payload 没有数据库约束

**位置**：`agent/store.py:1320-1373, 2774-2869`。

**证据**：临时 SQLite 探针把 generation 0 的“冻结原文”改成 generation 1 的
“被改写的新文案”，现有 trigger 全部接受。

**根因**：generation trigger 只验证连续性、旧代终态和 accounting；新 attempt helper
仍接收任意正文。

**最小修复**：新 API 不接收正文，只从旧 immutable plan 克隆；增加跨代 business
JSON 比较 trigger，仅允许 source/action/plan identity 与时间变化；健康检查扫描漂移。

### 2. 多 child 会在第一条结算时卡死

**位置**：`agent/store.py:3069-3077, 3498-3672, 1509-1522`，
`agent/action_plan.py:188-194`。

**证据**：双 child 探针第一条 claim 成功、第二条 claim 失败；第一条 confirmed 抛出
`attempt cannot settle with open outbox`，最终停在 attempt=`sending`、两个 outbox 分别
为 `sending/pending`。

**根因**：claim 只允许 attempt 从 `outbox_pending→sending`；单 child settle 被直接
当成整个 attempt 终态；`confirmed+uncertain` 的现有聚合也没有得到 ADR 约定的 partial。

**最小修复**：child 结算后按全部逻辑 ordinal reducer；存在未决 child 时 attempt 保持
非终态，最后一个 child 闭合后才投影 attempt/task；`confirmed+failed/uncertain→partial`。

### 3. 迟到确认无法合法记录或跨代收敛

**位置**：`agent/store.py:1478-1493, 1614-1689, 1692-1743, 3690-3696`。

**证据**：gen1 建立后，gen0 uncertain 的 confirmed settle 返回 `missing`；重开旧
outbox、`uncertain→confirmed`、取消未发送 gen1 后把 task 投影 done 均被 trigger 拒绝。

**根因**：attempt 是 generation 局部状态，但 task 被强制等同于 current attempt；缺少
跨 generation 的正向确认事实面。

**最小修复**：增加 append-only `task_action_confirmations`，保留同一逻辑 ordinal 的
多次 confirmed 以证明重复送达；current pointer 只前进，task 状态由 generation 0 的
预期 ordinal 跨代归约。新 outbox 未 claim 时可被旧代确认原子抑制，已 claim 则告警。

## Important

### 4. 人工 API、列表与审计无法表达安全重试

**位置**：`agent/store.py:2691-2710, 6124-6147, 6236-6255`，
`agent/handler_commands.py:377-397`。

**证据**：retry/cancel 只返回 bool，命令没有 expected attempt、request ID、核验或风险
确认；事件 actor 固定为 `system`；failed/partial 不在列表中。

**根因**：旧接口只为 legacy uncertain→pending 设计，没有并发 CAS 和审计合同。

**最小修复**：结构化稳定结果码；append-only `task_action_retry_requests` 记录接受与拒绝；
命令携带 `task@attempt`；actor 由可信入口派生；列表展示 generation/attempt 和操作原因。

## 可安全拆出的 Phase 2a

仅允许当前 attempt=`failed/dead` 的单 child text，且 accounting=`none/clean`、没有任何
open outbox、confirmed fact 或 known-confirmed snapshot。失败分类必须证明请求确定未
送达；`NETWORK_UNCERTAIN`、`NETWORK_OR_GATEWAY`、partial 和损坏 receipt 全拒绝。新
generation 只复制旧 plan，不调用 LLM/QQ；request、expected attempt CAS、新 attempt、
current pointer、唯一 outbox 和审计同一事务提交。

`uncertain/partial/cancelled` 留给 Phase 2b。当前 QQ 是一次 HTTP POST，timeout 后没有
future/webhook，router 也不接 `message_sent`；“用户没看到”只是未知，不是权威未送达。

## 数据库闭环（实施前必须同时满足）

1. retry request 与新 attempt 必须双向可核验：attempt 增加唯一、不可变的
   `retry_request_id`；generation 0 为空，后续代非空。accepted request 与 attempt 在
   同一事务互指，rejected request 不得产生 attempt。
2. `selected_ordinals_json` 必须与新 plan 的 ordinal 集合完全一致；每个 child 再与
   generation 0 的 canonical kind/channel/target/scope/payload/conversation_ref 比较，
   禁止少建、多建或偷换业务内容。
3. linked terminal outbox 只有在同一事务已写入匹配 confirmation 时才能删除；
   uncertain/dead/cancelled 不能被清理成“无记录”。
4. attempt reducer 只归约本代 child，task reducer 按 generation 0 的逻辑 ordinal 跨代
   归约；两个投影不得继续由同一个 helper 直接赋值。
5. 迟到 confirmation 可以重算旧 attempt 和当前 task，也可抑制尚未 claim 的后代
   outbox，但绝不能把 `current_attempt_id` 回拨到旧代；已 claim 时只记录重复风险。
6. 迁移 marker 已存在时先验证最高 schema version 的完整 manifest/checksum；首次升级
   才先验证 Phase 0，避免旧 exact-manifest 校验误杀合法的新 trigger。升级后旧二进制
   必须安全拒绝启动，不做降级迁移。

## 上线门槛

1. Phase 1 新 boot、schema 和单文本 canary 先通过。
2. Phase 2a schema/触发器以独立 version/checksum 安装，所有功能开关默认关闭。
3. preview-only 后，仅测试账号/测试群开放 failed/dead text；24 小时且故障注入全过再扩大。
4. `retry_payload_mismatch`、同 expected attempt 双接受、LLM compose、task-action invariant、
   confirmed ordinal 被复制或旧代覆盖 current pointer 任一非零，立即冻结 generation 创建。
5. 回滚只关闭新 generation；已有 outbox 正常收敛。不得删 plan/event/confirmation、回退
   pointer、转 legacy sender 或降级旧二进制。

## 本轮验证

- task-action 当前定向回归：70 项全绿（2026-08-29 合同收口复核）。
- Phase 2 合同相关既有探针：4 项全绿；它们证明现状 fail-closed，不证明完整 Phase 2。
- 2026-08-29 boot `3dd84a1f` 已完成 Phase 1 单文本生产 canary；1/1 confirmed、
  durable_confirmed=1、linked outbox/invariant=0。该证据只放行 Phase 2a 实现，不改变
  完整 Phase 2 的 NEEDS WORK 判定。
