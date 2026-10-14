# Codex 审查：Phase 2b 多 child 归账与健康收口

日期：2026-08-29

判定：**Request Changes → 已修复，等待真实行为 canary**

## 取证与修复

### Important：多 child 合法中间态被健康检查误报

- 位置：`agent/store.py:25-175`（`_TASK_ACTION_INVARIANT_COUNT_SQL`）。
- 证据：独立 SQLite 探针与新增回归 `tests/test_task_action_phase2b.py:222`、`:297`。两 child 中 ordinal 0 已确认、ordinal 1 仍 pending 时，attempt 合法为 `sending`；原健康计数仍报 `linked_invariant_violations=1`。冲突冻结态同样被重复计数。
- 根因：旧 SQL 只接受 `pending↔outbox_pending`、`sending↔sending`，没有表达“已确认/有事实锚点的兄弟 + 待发送 child”，且 confirmation delivery 对 `outbox_settle` 只放行 `sending`。
- 最小修复：允许有 confirmed fact 锚点的 `sending+pending`；允许 `outbox_settle` confirmation 关联 `sending/confirmed_unaccounted/confirmed_conflict`。修复后 claim-first、late-repair、冲突+兄弟三条回归均健康无误报。

### Critical：投影失败后的重启与重复修复不安全

- 位置：`agent/store.py:5170-5245`、`:7109-7140`。
- 证据：新增 `tests/test_task_action_phase2b.py:262`。注入 confirmed projection failure 后，原实现直接 `Store(db)` 会因 `confirmation delivery evidence is invalid` 拒绝启动；再次 repair 会因重复写入 immutable confirmation 抛 `sqlite3.IntegrityError`。
- 根因：合法 `confirmed_unaccounted` 状态未被启动校验接受；repair fallback 对已有同一 confirmation 再走 `outbox_settle` insert，触发终态 guard。
- 最小修复：放行合法冻结状态；复用 identity/message_ids 完全一致的既有 confirmation，若证据不一致则冻结为 `confirmed_conflict`，不静默覆盖。新增重启、连续失败与最终 repair 回归。

### Important：兄弟动作完成覆盖 attempt 归账状态

- 位置：`agent/store.py:5170-5245`。
- 证据：新增 `tests/test_task_action_phase2b.py:222`。ordinal 0 late-confirmed 未 repair，ordinal 1 随后正常 confirmed；原实现把 attempt `accounting_state` 从 `pending_repair` 错误清为 `clean`。
- 根因：`_settle_linked_task_attempt_conn` 按当前 child 的 `clean` 结果覆盖 attempt 聚合状态。
- 最小修复：按整个 attempt 的 outbox/confirmation/fact 聚合，`conflict` 优先、任一待修复事实保留 `pending_repair`。repair 完成后才转 `clean`。

## 验收证据

- `python -m pytest tests/ -q`：**1448 passed, 3 warnings**。
- `python -m compileall -q agent napcat main.py 糖糖控制台_qt.py`：通过。
- pyflakes `undefined`：0（项目已有非 undefined 风格告警未扩大本轮范围）。
- `python tools/体检.py`：通过，依赖、模型、端口、SQLite 完整性均正常。
- 安全重启后 boot `8ecb37a1` 正常上线；日志出现配置加载、SnowLuma 连接、QQ 在线、GPT-SoVITS 就绪，无启动 ERROR/CRITICAL/Traceback。
- 生产 `memory.db`：`PRAGMA integrity_check=ok`；`open/stale_sending/linked_invariant_violations/confirmed_fact_integrity_violations/bare_sending_tasks/needs_review=0`；确认记录 1 条且空 message_id 为 0。

## 未完成边界

可信平台迟到回执尚未接入 NapCat/OneBot 事件路由，`_record_task_confirmation_from_platform` 仍是 feature-off 的内部契约；尚无真实 uncertain/迟到/重复送达样本。因此 Phase 2b 的 schema、reducer 与故障恢复已具备回归证据，但不能宣称线上行为 canary 或完整生产认证通过。
