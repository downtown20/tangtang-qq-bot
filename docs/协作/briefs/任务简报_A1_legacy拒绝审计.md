# 任务简报 A1：legacy 复核拒绝的结构化审计（P0-3）

- 派发：2026-09-02 · 主负责人
- 来源：`docs/运行验收/阶段总结_记忆与真实链路验收_20260901.md` P0-3
- 执行方：Codex · 执行后写 `docs/协作/reports/执行报告_A1.md`

## 目标

把 legacy 记忆候选的「批准 / 拒绝 / 延后」决策**全部持久化**为结构化审计记录；已拒绝的候选不再反复进入待审队列。当前拒绝结果只存在于阶段文档——生产 4 个候选逐条复核均因语义不足拒绝，但没有任何落盘 disposition，下次审计它们会原样再出现。

## 现状（主负责人已取证，可信但请复核）

- `agent/store.py:631-650`：`memory_reverification_events` 表已记录 promotion/rollback 事件（memory_id、previous/new trust_level、evidence_ids、evidence_quote、reviewer、reason、reviewed_at）
- `agent/store.py:9974-10058`：`update_memory_evidence(allow_legacy_promotion=True)` 写 promotion 事件
- `agent/store.py:10060-10105`：`rollback_memory_reverification` 写回退事件
- `tools/memory_reverify.py`（94 行）：preview/apply/rollback CLI，**无 reject/defer 命令**
- `tools/memory_truth_audit.py:65-78`：候选清单 = `legacy_unverified` + 有非 bot 可核验证据——**已审已拒的候选会反复出现在这里**，这是要修的根
- 测试模式：`tests/test_memory_truth_model.py`（646 行，21 测试，`store` fixture 用临时库）

## 契约（必须满足）

1. **拒绝/延后持久化**：reviewer、reason、reviewed_at 必填，缺任一即 fail-closed 拒绝写入；记录被审记忆的 evidence 上下文（至少 memory_id，建议 evidence_ids）
2. **拒绝候选退出队列**：`memory_truth_audit.py` 默认候选清单排除已拒项；延后项默认也排除，但在审计输出中单独列出（含延后理由），便于人工决定何时重审
3. **CLI**：`memory_reverify.py` 增加 `--reject`（必须带 --reviewer --reason）；`--defer` 按你的落点设计决定是否本期实现，不实现就写进遗留并说明
4. **不碰 promotion/rollback 语义**：现有提升与回退的 fail-closed 行为是已验证的契约，改动不得削弱
5. **回归测试**：仿 `test_memory_truth_model.py` 模式——拒绝后 audit 候选消失、缺 reviewer/reason 拒写、拒绝后再次 apply 的行为明确（写清你的判定）
6. **生产库回填**：阶段总结记载「生产 4 个可预览候选逐条复核后均因语义不足拒绝」。从阶段文档 + 根目录 `codex_memory_*.log`（align/resume/review 三个日志）找出这 4 条的 id 与拒绝理由，用新机制回填（reviewer 注明来源，如 `codex-review-20260901`，reason 引用真实复核结论）。**回填前先备份 memory.db 到 backups/**。

## 危险区

- **schema 变更纪律**：store.py 有 schema checksum/manifest 机制（见 `agent/store.py:4436` 附近的 manifest 校验模式）。改表结构必须同步 manifest，否则启动即告警。落点选择（events 表加列 vs 新表）先查清迁移机制再定，报告里写清理由。
- **bot 可能正在运行**：memory.db 是 WAL 模式（根目录有 -wal/-shm），生产 bot 可能正持有连接。写操作保持短事务；若发现数据库锁，不得暴力重试——报告里说明，等批次 B 重启窗口再回填。
- **日志极大**：tangtang.log 数十万行，禁止直接 grep 全文，用 python 脚本过滤。
- 改任何符号前跑 GitNexus impact，HIGH/CRITICAL 在报告「影响面」节显式写出。

## 禁入区

- 不新增配置项、不改控制台 UI
- 不动 `update_memory_evidence` / `rollback_memory_reverification` 的现有校验逻辑
- 不删任何历史数据——回填只写 audit 记录，不改 memories 行的 trust_level
- 不做 legacy 批量提升（哪怕某些候选看起来证据充足）——本简报只做审计落盘

## 验收标准

1. `python -m pytest tests/test_memory_truth_model.py -q` 全绿 + 新增测试全绿
2. pyflakes undefined 清零
3. `python tools/memory_truth_audit.py --db memory.db` 的候选清单不再包含已回填拒绝的 4 条；输出含已拒/延后统计
4. 回填前 memory.db 备份存在（backups/ 下可查）
5. 执行报告格式符合《协作协议》第二节
