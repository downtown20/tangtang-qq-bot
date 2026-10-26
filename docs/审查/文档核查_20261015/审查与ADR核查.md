# 审查与 ADR 核查（2026-10-15）

> 任务：task-6。核查「审查记录 + ADR」——上次审计提的问题**修了没有**，决策记录**还成立吗**。
> 纪律：**只读核查**，本轮未修改任何业务代码、未跑 pytest、未重启 bot。产出仅落在 `docs/审查/文档核查_20261015/`。

## 0. 核查方法与一个重要前提

**前提修正（本轮最重要的一条）**：任务书列的第 1 号必读文档
`docs/开发规划/架构审计执行_复盘与重规划_20261013.md` 自称「执行规划的唯一事实源」，但
**2026-10-14 起出现了更新的单一事实源**：`docs/开发规划/长期优化/任务台账.md`（143 行）+
`docs/开发规划/长期优化/记录/` 下 58 篇执行记录。台账 §阶段目标（`任务台账.md:11-15`）把
S1/S2/S3/S5/S4 五个阶段全部标记为 2026-10-14 完成。

⇒ **2026-10-13 文档里所有 `[×] 未做` 的结论都必须以台账 + 代码重判，不能直接引用。**
下文 A 表第 4 列即为此重判结果。

**取证方法**：`read`/`grep`/`glob` 逐条落到 `file:line`；只有文档没有代码证据的，一律降到 §D「待验证假设」。

---

## A. 审计发现 → 修复状态表

发现编号取原始审计 `docs/审查/全项目架构审计与市场对照_20260923.md` 的原号
（P0 四条在 `:102-163`、P1 六条在 `:168-173`、P2/P3 六条在 `:179-184`）。

### A.1 P0（4 条）

| 发现编号 / 标题 | 声称严重级 | 文档声称的处理结论 | 代码/文档实证判定 |
|---|---|---|---|
| **P0-1** 驱动力没有消化回路：4/6 长期贴顶 | P0 | 10-13 复盘：◐ 代码完成效果未验证；同文档 T0.1 撤回为「预测成立」 | **已修复（有实测序列）**。T0.1：446 采样点 / 7.4h 单 boot，四项饱和驱动力收敛 **0.635~0.689**，落进预测区间 0.66~0.80 ⇒ X1 关闭（`架构审计执行_复盘与重规划_20261013.md:第六部分`）；台账 `任务台账.md:113` 记录 T4.1 **PASS**：`last_tick=2026-10-14 07:31`，六项全 <0.85（social 0.064 / commitment 0.798 / curiosity_info 0.709 / express 0.728 / curiosity_explore 0.673 / avoid 0.35）。序列采样器 `tools/长期优化快照.py --drives-series` 已落地（`任务台账.md:114`） |
| **P0-2** 情绪两头断：11/15 事件零发射、无负向输入 | P0 | 10-13 复盘 2.1：[√] | **已修复**。发射点实测 9 处：`agent/handler_autonomy.py:835` LATE_NIGHT、`:897` BEING_SCOLDED、`:902`/`:909` BEING_IGNORED；`agent/handler.py:3382`/`:3428` BEING_AT、`:3532` INTERJECTED、`:4408` SENT_LONG_REPLY、`:4410` SENT_REPLY；正向白名单 `agent/mood.py:64-68`（BEING_PRAISED/BEING_THANKED/BEING_MISSED/INTIMATE_TALK/HELPED_SOMEONE）。**残留**：`agent/mood.py:46` TALKING_TOO_MUCH 仍零发射（见 A.2 / §C-3） |
| **P0-3** 部分送达前缀不入账 + 多气泡在账本里压成一条 | P0 | 10-13 复盘 2.1：**[×] 未做**；P0P1交接 / P3交接 均列为未做 | **部分修复**。① 部分送达：`agent/reply_pipeline.py:610-617`（T2.2b 注释 + `first_problem = next((r for r in results if not r.delivered), None)`）、`:618` `partial_delivery`、`:627` `"PARTIAL_DELIVERY:"`、`:629/:632` error/retcode 透传；前缀归账 `agent/handler.py:13523` `async def _account_confirmed_prefix`（调用点 `:4244`/`:5291` 回合起点、`:4312`/`:5390` 归账、`:13346`）。② **多气泡「压成一条」未修**：`agent/reply_pipeline.py:484-486` `multi_part = len(parts) > 1` / `transport_template = receipt_template if not multi_part else None`，`:509-513` 显式 warning「多段批次丢弃 receipt 模板（receipt.parts 聚合尚未实现）」⇒ 多段批次的 permanently 归账被整体跳过。见 §C-1 |
| **P0-4** 语音引擎失败静默换嗓（丛雨 → 微软晓伊） | P0 | 10-13 复盘 2.1：[√] | **运行口径已修，代码默认值仍在（分叉）**。`config.yaml:178` `provider: gpt-sovits` ⇒ 生产按 GPT-SoVITS 走。**但**：`agent/handler.py:1438` 取默认值仍写 `voice_cfg.get("provider", "edge-tts")`；`agent/voice.py:294` docstring 仍写「支持 edge-tts (默认) 和 GPT-SoVITS (克隆音色)」、`:309` 形参默认 `provider: str = "edge-tts"`、`:369-371` 仍保留「provider 不是 gpt-sovits → fallback_text」降级分支。而 `CLAUDE.md`「语音与唱歌（2026-10-15 定版）」写「只有 GPT-SoVITS 一个 provider（控制台里是只读标签）」。**后果**：手工删/改 `config.yaml:178` 这一行即静默回落微软晓伊——正是 P0-4 的原始机理，且无启动告警（对照教训 #11「破坏性 boolean 开关必须启动时显式警告」） |

### A.2 P1（6 条）

| 发现编号 / 标题 | 声称严重级 | 文档声称的处理结论 | 代码实证判定 |
|---|---|---|---|
| **P1-1** 承诺只有记账没有闭环（193 active / 0 fulfilled） | P1 | 10-13 复盘 2.1：◐ 代码完成，`fulfilled>0` 从未观测；P0P1交接 第七部分：**已完成可关闭**（reply_commitment 有调用点两处 + 承诺压力写入端） | **机制替换 + 仍未闭环**。`grep fulfilled agent/drives.py` = **0 命中** ⇒ 原两条释放口（`drives.py:320` / `self_state.py:272-279`）已不在 `drives.py` 中；承诺落地改由 LLM 工具 `set_reminder`（`agent/handler.py:6928` 定义、`:7160` 执行分支、`:7158` 注释「承诺的落地路径」）。台账**无** fulfilled>0 的观测记录 ⇒ 判定「**口径已换，生产结算闭环仍无证据**」，与 10-13 的 ◐ 实质相同 |
| **P1-2** 记忆没有事件时间与时间精度（486 条 value 冻结「昨天/上周」） | P1 | 10-13 复盘 2.1：**[×] 未做**（属 M5）；P0P1交接/P3交接 同 | **已修复**。台账 `任务台账.md:116` T3.2 完成（2026-10-14）：`agent/protocols.py:446` 「── 相对时间语义（2026-10-14 台账 T3.2，Stage 3）──」、`:449` 「不改 `event_time` 语义」；新增 `humanize_age` / `format_fact_time` 并接线 `agent/memory.py:2102`、`:2428`，并去掉 `:2430` 的 `fact_time[:16]` 截断；黄金集 **80/80**（8 维度） |
| **P1-3** hard_turn 保护块吃满预算、当前轮证据被静默饿死 | P1（中高） | 10-13 复盘 2.1：[×] 未做 → 同文档 T1.3 已修；P3交接 第六部分仍列未做 | **已修复（三段证据齐全）**。① 代码：`agent/protocols.py:271` `CURRENT_TURN_EVIDENCE = frozenset({"提及","文件","图片"})`、`:272` `EVIDENCE_RESERVE_TOKENS = 400`、`:323/:344` 判定、`:327` `reserve_left`；`:18` `logger = logging.getLogger("糖糖.Protocols")`（原「无 logger」描述已过期）、`:354/:367` 淘汰明细日志。② 台账 `任务台账.md:112` T4 核查记录活体验证：构造「引用×1500 + 协议×2000」后 `输出标签: ['引用','协议','提及','图片']`。③ 日志可见 `🧺 背景块淘汰: 保留flex=2 淘汰=2 … ⚠已动用证据保底` |
| **P1-4** 画像层断线（people.notes 不进主对话路径、83% 存量被拒） | P1 | 10-13 复盘 2.1：[√] | **已修复（私聊注入已恢复）**。`agent/handler.py:4847-4851`：注释「2026-10-12 主人拍板：私聊**注入人物画像**」+ `include_profile=True`（全项目**唯一** True 调用点）；透传链 `agent/handler.py:9705` 签名 → `:9737` 透传 → `agent/memory.py:769/781`（`profile_notes = self.active_notes(qq_id) if include_profile else ""`）。`recall_formatted` 的 `source_group_id=""` 排除保持原状（`handler.py:4841`） |
| **P1-5** 已 verified 的记忆被同 value 弱提取静默降级 | P1（小） | 10-13 复盘 2.1：[√] | **已修复**。`agent/store.py:10918` 注释明确写「危害在下面那条 UPDATE：CASE 只保 manual/corrected，**漏了 verified**」；修复行 `:10939` `"trust_level=CASE WHEN trust_level IN ('manual','corrected','verified') ` ⇒ verified 已被保住 |
| **P1-6** 自检兜底文案已送达却被当成没发出去 | P1（小） | 10-13 复盘 2.1：[√] | **已修复**。`agent/handler.py:13332` `🔍 自检后发送`、`:13340` docstring「True 发送成功；False 被自检拦截或发送失败」、`:13348` 注释「发出『里拆出来——自检拦截后用兜底文案顶上时，用户确实收到了一条消息」⇒ 兜底已计入发送证据 |

### A.3 P2 / P3（6 条）

| 发现编号 / 标题（原文档 `全项目架构审计…20260923.md:179-184`） | 声称严重级 | 文档声称的处理结论 | 代码实证判定 |
|---|---|---|---|
| **P2-a** 状态没参与「要不要开口」（TALKING_TOO_MUCH / LATE_NIGHT 零调用；随机数冒充心情） | P2 | 10-13 复盘 2.1：◐ TALKING_TOO_MUCH 全仓只有定义零发射点；X7 列风险（低中） | **已拍板保留（不再是缺陷）**。`agent/mood.py:46` `TALKING_TOO_MUCH = ("说话太多", -2, -1, -2)  # was: -3, -1, -3`，全 `agent/` 仅此 1 处命中。台账 `任务台账.md:115` T3.3 明确处置理由：「**能力定义不是死代码**——情绪事件词汇表，缺发射点是『还没接』而非『不该有』，删了会窄化可表达的情绪空间」 |
| **P2-b** 延迟与定时的承诺会静默落空 | P2 | 10-13 复盘 2.1：[√] | **已修复**。定时/延迟意图单一决策源归 LLM 工具：`agent/handler.py:4762` 注释「定时提醒/延迟发言一律走 LLM 工具 set_reminder / group_say_later」、`:3358`「定时意图一律由 LLM set_reminder 工具决定」；工具注册 `:6216` `set_reminder`、`:6217` `group_say_later`（P0P1交接第七部分亦记「5 个正向事件有发射点」） |
| **P2-c** 协议标记两条未设防通道（`[不说话]` 无 clean 兜底） | P2 | 10-13 复盘 2.1：[√] | **已修复**。`agent/reply_pipeline.py:183` 注释「洗掉**全部**协议标记：唱歌 [SING:]、色色切换、[分段]、[引用]、[不说话]」、`:185`「strip_protocol_markers()——上一轮正是因为一条条列，漏了 [不说话]」⇒ 已从「逐条列」改为全量剥离 |
| **P2-d** 延迟 outbox 重放的 confirmed 不产生永久归账 | P2 | 10-13 复盘 2.1：**[×] 未做**（属 M4/ADR-003）；P0P1交接/P3交接 同 | **部分修复**。linked 行已接结算：`agent/store.py:7401-7416` `mark_send_outbox_uncertain` 与 `:7418-7427` `fail_send_outbox`，对 `task_attempt_id IS NOT NULL` 的行转调 `settle_send_outbox`（`:9062`）完成 ADR-003:57-68 的 fact/chat/window 原子归账；群/私文字路径亦挂模板（`agent/handler.py:4284-4304`）。**残留**：`agent/store.py:7390-7399` `complete_send_outbox` 对 **legacy（`task_attempt_id IS NULL`）** 行只 `DELETE FROM send_outbox`、**不写任何永久事实** ⇒ 非 linked 的延迟重放 confirmed 仍然不归账 |
| **P2-e** 矛盾仲裁缺位：`existing_summary` 硬编码为空 | P2 | 10-13 复盘 2.1：**[!] 有争议**（当时定位 `handler.py:11668` 仍传 ""） | **未修（重复 ≥3 次）**。`agent/handler.py:11994` `existing_summary=""`；对照同文件 `:12265` `existing_summary=existing[:300] if existing else ""`（另一条路径已正确传值）；签名 `:11887`。见 §C-2 |
| **P3** Episode 是无真值状态的派生层，被撤事实仍可经往事注入 | P3 | 10-13 复盘 2.1：[?] 未逐条取证，不下结论 | **待验证假设**（见 §D-1）。已确认存在的机制：`agent/memory.py:2286` `_aggregate_episodes`、`:2332` `query_episodes`、`:2345` `insert_episode`、`agent/store.py:1041-1060` episodes 表与迁移、`agent/handler.py:1767` 健康检查注册、`:8283-8310` search_episodes 工具。**但本轮未取到「temporal 聚合是否过滤 memories.status」的证据**，不下结论 |

### A.4 里程碑级结论（10-13 vs 台账）

| 项 | 10-13 复盘声称 | 2026-10-14 台账 | 本轮重判 |
|---|---|---|---|
| M1 / M2 | [√] / [√] | 无未修登记 | 维持 |
| M3 内部状态回路 | ◐ | T0.1 关闭 X1；T4.1 PASS（`任务台账.md:113`） | 升级为**已达成** |
| M4 自我记录保真（P0-3 + P2-d） | **[×] 未开始** | T5 **阻塞**（`任务台账.md:38`、§阻塞登记 `:139`） | **仍卡在同一处**：`trg_confirmed_action_fact_strict_anchor` 要求 `send_outbox` 或 `action_receipt_mailbox` 行存在，而成功路径两者都不写 |
| M5 时间感与防复发收口 | ◐（5 项中 1 项） | T3.1/T3.2/T3.3 全部完成（`任务台账.md:116-117`、`:115`） | **已达成**（三项全绿） |

---

## B. ADR 现状核查（四问）

四问定义：① 决策是否仍在生效（代码是否按它实现）② 「迁移待办/尚未实现」章节今天做完了吗 ③ 是否与后来的代码分叉（代码改了 ADR 没跟上）④ 是否有新 ADR 缺失。

### B.1 ADR-001 统一动作信封与真实回执（78 行）

- ① **仍生效**。`ActionEnvelope/ActionReceipt` 已是实体：`agent/action_contract.py`（`_validate_identity_version` `:103`、`ConversationRef` 校验 `:148-181`、domain ID 生成 `:200-214`）、`agent/action_plan.py`、`agent/action_executor.py:31`。ADR-001:7 声明「冲突处以 ADR-002 为准」，与 ADR-002 内容无冲突。
- ② 迁移顺序 4 步（`:52-55`）：第 1 步（纯契约 + 旧文本 receipt 兼容）✅；第 2 步（媒体真实 receipt 按 scope 一次性消费）✅（ADR-002 B1）；第 3 步（即时/定时贴图共用 typed payload）✅（`agent/handler.py:2699`、`agent/image_share.py:369`）；第 4 步（**所有消费者迁移完成后删除重复 bool/stats 口径**）**未完成**——旧口径仍在（`agent/reply_pipeline.py:76` `last_delivered_parts` 等并存）。
- ③ 无实质分叉。
- ④ ADR-001 未记录的重要新决策：**`turn_actions` 之外新增 `confirmed_action_facts` / `conversation_window_events` 两个永久事实表**（`agent/store.py:754/843/865`）——这实质上是「不采用的方案：所有动作一开始就强制进入同一数据库队列」（`:69-71`）的**部分反转**，但没有新的 ADR 记录这一转向；它只散落在 ADR-003 的语境里。

### B.2 ADR-002 持久动作回执邮箱与媒体事实语义（118 行）

- ① **仍生效**。邮箱与 lease/inject/ack 契约在 `agent/store.py`（`action_receipt_mailbox`、`expire_action_receipts`）。
- ② **实施状态章节已过期**：`ADR-002:84-89` 写于 2026-08-28，其中「**延迟 outbox `confirmed` 对聊天记录/窗口的幂等事实归账仍待单独设计**」（`:89`）——这一项**已被 ADR-003 承接并部分落地**（linked 行走 `settle_send_outbox`，`agent/store.py:7401-7427`）。ADR-002 未更新该行，读者会以为仍是悬空项。
- ③ **分叉点**：`ADR-002:64` 要求「部分确认、部分失败的批次整体 `uncertain`，`error_code=PARTIAL_DELIVERY`；已确认项永不重放」——**已实现**（`agent/reply_pipeline.py:618/627`）；但 `ADR-002:66` 的「`actual.text` 与平台可见内容分列保存」与 `ADR-003:23` 的「`receipt.parts` 聚合」**都还没有对应的新版本落地**，而 `ADR-002:39` 的邮箱字段清单里没有 `parts`。
- ④ 缺：ADR-002 未记录 2026-10-13 引入的 **`receipt_template` 单气泡作用域约束**（`agent/reply_pipeline.py:462-466、484-486、509-513`）——这是一个**新的、有明确边界的契约**，只写在代码 docstring 与复盘文档里，没有 ADR。

### B.3 ADR-003 确认送达的永久事实归账（132 行）★重点

- ① **仍生效且实现最完整**。`domain_action_id`：`agent/store.py:754`（`confirmed_action_facts.domain_action_id TEXT PRIMARY KEY`）、`:843`（`conversation_window_events.domain_action_id TEXT NOT NULL UNIQUE`）、`:1377/1392`（`send_outbox` 加列 + 非空唯一索引）；`conversation_ref`/`identity_version`：`agent/action_contract.py:103-106`（`raise ValueError("identity_version is frozen at 1")`）、`:239-276`；锚点触发器 `agent/store.py:813`（`CREATE TRIGGER IF NOT EXISTS trg_confirmed_action_fact_strict_anchor`）与 `:1600`；崩溃语义分支齐备：`confirmed_unaccounted`/`confirmed_conflict` 出现在 `agent/store.py:69-71/146-151/228/821-822/1621-1622/3913-3918/4094/4324-4325/4431-4432` 与 `agent/health_check.py:217-241`。
- ② **迁移待办未完成**：ADR-003:5 状态「生产 canary 后启用」；实施与验收 6 步（`:121-126`）中第 5 步「故障注入和**生产 canary**」未做，因此第 6 步「通过后才进入 ADR-002 B2 贴图统一」的**门禁没有打开过**。
- ③ **分叉（明确的、带代码证据）**：`ADR-003:23` 写「普通 `ReplyPipeline` 会分段发送，部分成功时不能把整段正文归账，**必须等逻辑动作与 `receipt.parts` 聚合完成后再接入**」——代码侧确认「尚未实现」并**主动降级**：`agent/reply_pipeline.py:465` 「那部分**尚未实现**」、`:509-513` warning「多段批次丢弃 receipt 模板（receipt.parts 聚合尚未实现，禁止用单模板冒充一批动作）」。`agent/handler.py:4286-4287` 同口径。⇒ ADR-003 把「多段」列为**前置条件**，而工程上选择了**放弃多段的永久归账**；这个「不修」的决策**没有任何 ADR 记录**（只在 `记录/T32-空行即分条.md` 的「残留代价」段里如实登记）。
- ④ 缺：上述「多段不归账」的显式取舍需要一条 ADR（或 ADR-003 的修正记录），否则后来者会把 `policy = 丢弃模板` 当成待修 bug 反复重提——**事实上已经重提了 9 次**（§C-1）。

### B.4 ADR-004 统一媒体动作计划（99 行）

- ① **仍生效**。`ActionPlan`/`ActionExecutor` 是唯一媒体入口：`agent/handler.py:2699`（Sticker）、`:2839/2906`（Image）、`:2930-2998`（CG）、`:4251`/`:5323`（「唱歌：一律 ActionPlan child receipt」）、`agent/image_share.py:369`（`ActionExecutor(_dispatch).execute(...)`）。
- ② 迁移顺序 6 步（`:46-51`）：1 ActionPlan/child/schema ✅、2 即时贴图+CG+语音组合 ✅、3 TaskManager/Cron/DailyReport 定时与复合 ✅（ADR-004:89-99 定时媒体复核）、4 **image share/gen ✅**（`agent/image_share.py:369`）、5 **sing 工具 ✅**（`agent/handler.py:4251/5323`）、6 删除 CQ 文本协议与随机重选路径 + 重启/故障注入/回滚演练 **未完成**。
- ③ **分叉（状态行过期）**：`ADR-004:5` 状态行仍写「In progress（… **图片/唱歌仍待迁移**）」，而代码里图片与唱歌都已走 ActionPlan child receipt。⇒ **ADR 状态行落后于代码约一个阶段**。
- ④ 缺：ADR-004 未记录 2026-10-14 新增的 **`[分段]`+空行双段边界**对媒体/回执链的边界（`agent/protocols.py` `parse_split_markers`；影响分析见 `记录/T32-空行即分条.md`）。

### B.5 ADR-005 定时任务重试所有权与 ActionPlan 迁移（557 行）

- ① **仍生效**。Phase 0 的 attempt/event/指针/触发器、Phase 1 的 linked outbox 唯一首发与原子结算在 `agent/store.py:9062 settle_send_outbox`、`:7390-7427`、`:1600-1672` 中可见；功能开关 `config.yaml:115-117`（`text_action_outbox_enabled` / `media_action_outbox_enabled` / `send_outbox_claims_enabled`）与 `ADR-004:98` 的 `tasks.media_action_outbox_enabled 默认 false` 对应。
- ② **待办未完成**：`ADR-005:5` 状态「Phase 2a/2b 行为 canary 待样本」；`:19-20` 明确「现无确定失败样本，功能 canary 尚未执行」+「uncertain/partial/cancelled 仍在行为 canary 前禁止重发」。台账也无 Phase 2a/2b canary 完成记录（`任务台账.md` 全文无对应行）⇒ **待办仍在原位，无进展**。
- ③ 无实质分叉；ADR-005 与 ADR-003/004 的边界（`:22-23` 「本 ADR 只定义定时任务与发送 outbox 的契约，首个实现切片仍限定为 legacy text-only 与 typed text」）与代码一致。
- ④ 缺：ADR-005 未记录 `trg_confirmed_action_fact_strict_anchor` 与成功路径的冲突（即台账 T5 阻塞项）——**该冲突正是 ADR-005 与 ADR-003 的交界处**，却只在台账 `任务台账.md:139` 有登记。

### B.6 ADR-006 出站平台回执契约与安全边界（41 行）★任务点名

| 四问 | 结论 | 证据 |
|---|---|---|
| ① 决策是否仍在生效（代码是否按它实现） | **生效**。纯验证层存在且不碰 Store：`agent/platform_receipts.py` —— `:27-32` 常量（`PLATFORM_RECEIPT_SCHEMA_VERSION=1`、`PLATFORM_RECEIPT_EVENT_PREFIX="out:v1:"`、`PLATFORM_RECEIPT_SOURCES={onebot_ws_api, onebot_message_sent}`）、`:58` `build_outgoing_receipt_event_key`、`:77` `class PlatformReceipt`、`:164` `validate_context`、`:184` `to_evidence`、`:202` `validate_platform_receipt`；`:175` `if self.synthetic and not allow_synthetic:` fail-closed；回归 `tests/test_platform_receipts.py`；**ADR-006:33-34 的禁令被遵守**——`grep echo onebot/ws_client.py` = **0 命中**，`onebot/ws_client.py:1437 _call_api` 与 `:881 _event_route` 未被改动 | 上列 file:line |
| ② 「尚未实现」章节今天做完了吗 | **未做，0% 进展**。`grep platform_receipts agent/` = **0**、`grep platform_receipts onebot/` = **0** ⇒ 该模块**没有任何生产消费者**，只有测试 import（`tests/test_platform_receipts.py:7`）。零调用闸门的两处登记证明这一点：`tests/test_integration_contract.py:169-170` `validate_platform_receipt` 进 ALLOWLIST（「平台回执适配器尚未接入生产路由，feature-off 期间保留契约和回归测试」）、`:221` `to_evidence` 进 CLASS_METHOD_BASELINE（「仅测试引用；出站回执/终局入口——主人 2026-10-13 拍板当『待接线』留着」）。ADR-006:3 的状态「**已实现契约、生产接入 feature-off**（2026-08-29）」至今第 **47 天**不变 | 上列 file:line |
| ③ 是否与后来的代码分叉 | **有一处口径分叉**：`ADR-006:39-40` 写「生产接入仍由 **feature flag** 控制」，但 `config.yaml` 里**不存在该 flag**（`grep platform_receipt config.yaml` = 0；`:115-117` 的三个开关属 ADR-004/005 的 outbox，不是回执接入）。当前唯一的 fail-closed 机制是函数形参 `allow_synthetic: bool = False`（`agent/platform_receipts.py:166/205`），**不是**可运维的配置开关。另一个分叉是「`_event_route`/`_call_api` 保持不动」这条**已被 ADR-003/004/005 的成功路径改造绕开**：多段文本走 `receipt_template` → `settle_send_outbox`（`agent/handler.py:4298-4303`），并未经过 `platform_receipts` 的验证层 ⇒ **「谁验证出站回执」这件事事实上存在两条并行路径**，ADR-006 只描述了其中一条 | 上列 file:line |
| ④ 是否有新 ADR 缺失 | **有**。以下均为代码里已成立、但无 ADR 记录的重要决策：① `receipt_template` 单气泡作用域与「多段丢弃认证模板」（`agent/reply_pipeline.py:462-466/484-486/509-513`）；② `confirmed_action_facts` + `conversation_window_events` 两个永久事实表取代「不合并存储」的原判断（`agent/store.py:754/843` vs ADR-001:69-71）；③ ADR-006 验证层与 ADR-003 归账层并存的双路径（本次核查新发现）；④ T3.3 的**类级大写成员零调用闸门**（`tests/test_integration_contract.py` + `记录/T3.3-枚举成员闸门.md`）；⑤ T3.1 的「观察器心跳 + 缺席告警」观测契约（`agent/health_check.py:934-989`） | 上列 file:line |

---

## C. 重复出现的问题清单

判定标准：同一问题在 ≥2 份文档/记录里被独立提出，且**至今未修**。「审计噪音」= 反复提但已被拍板保留或本就不可达；「真实顽疾」= 反复提但确有工程后果且无决策记录。

| # | 问题 | 出现次数与出处 | 性质判定 |
|---|---|---|---|
| **C-1** | **多段/部分送达的 `receipt.parts` 聚合缺失** ⇒ 多气泡批次的永久归账被整体跳过 | **≥9 次**：① `ADR-002:89`（2026-08-28「延迟 outbox confirmed 归账仍待单独设计」）② `ADR-003:23`（08-28 列为 B2 **前置条件**）③ 审计 P0-3（09-23）④ 10-13 复盘 2.1 P0-3 **[×]** ⑤ `架构审计执行_P0P1交接_20261013.md` 第七部分「P2·M4 一点没碰」⑥ `架构审计执行_P3交接_20261013.md` 第六部分「P2·M4」⑦ 10-13 复盘第九部分 T2.4 **❌ 未达完成标准** ⑧ `agent/reply_pipeline.py:465`（10-13 注释「尚未实现」）⑨ `记录/T32-空行即分条.md`「残留代价…不修——要修得实现 ADR-003:23 的 receipt.parts 聚合」⑩ 台账 `任务台账.md:38` T5 **阻塞** / `:139` 阻塞登记 | **真实顽疾**。工程后果具体：带真实动作且回复含空行的私聊，永久归账被跳过（消息照发、少一行 `confirmed_action_facts`）；且阻塞条件（`trg_confirmed_action_fact_strict_anchor` 要求 outbox/mailbox 行，成功路径两者都不写）**已被明确写出却始终没有设计决策**。**不是噪音**——9 次里没有一次把它拍板为「不做」 |
| **C-2** | **矛盾仲裁缺位：`existing_summary` 硬编码为空** | **≥3 次**：① 审计 P2（`全项目架构审计…20260923.md:183`）② 10-13 复盘 2.1「[!] 有争议，`handler.py:11668` 仍传 `""`」③ P0P1/P3 交接未做清单 | **真实顽疾（小而明确）**。现状 `agent/handler.py:11994` `existing_summary=""`，而同一文件 `:12265` 的另一条路径已正确传 `existing[:300]` ⇒ 是**局部漏改**，不是设计取舍。修法面极小（1 行），却没有被任何一轮认领 |
| **C-3** | metrics 五埋点零调用 + `TALKING_TOO_MUCH` 零发射 | **≥3 次**：① 审计 X7 / P2-a（09-23）② 10-13 复盘 3.5 X7 ③ `tests/test_integration_contract.py:212-217`（`record_extract_success/record_extract_empty/record_extract_rejected/record_busy_skip/record_dedup_hit` 五条「指标埋点——写好了但无上报点」） | **半噪音**。TALKING_TOO_MUCH 已由台账 `任务台账.md:115` 明确拍板**保留**（「能力定义不是死代码」）⇒ 该项应从审计清单**移出**，否则每轮都会被重新报一次。但 `agent/metrics.py` 五条仍是「写了没生效家族」，**未被拍板**，属真实待决 |
| **C-4** | ADR-006 验证层零生产消费者 | **≥3 次**：① `ADR-006:3`（08-29 起 feature-off）② `架构审计执行_P3交接_20261013.md:175`「`agent/platform_receipts.py:184 PlatformReceipt.to_evidence` 仅测试引用」③ `tests/test_integration_contract.py:221` 基线条目 | **已知不修（不是遗漏）**：主人 2026-10-13 已拍板「当『待接线』留着（P2 可能要用）」。**但它以「待接线」形态一直挂在基线里，每轮审查都会重报一次** ⇒ 建议在 ADR-006 里补一行「已知不修 + 触发条件」，把它从「疑似遗漏」翻成「已决策」 |
| **C-5** | 文档与代码分家（对照教训 #35「对外文档要当代码测」） | **复发型**：2026-09-19 用户手册审计（14 条里 8 条不符）→ `记录/T10-发布脱敏复核.md`（`tools/发布前检查.py` 与 `准备发布.py` 口径漂移）→ **本轮新发现 3 处**：① `ADR-004:5` 状态行仍称「图片/唱歌仍待迁移」而代码已迁完 ② `tests/test_integration_contract.py:112` 写「72 个」、`:177` 写「79 个」，而 **dict 实际 71 条**（台账 `:13/:117` 记 79→72）③ 台账 `任务台账.md:108` T25 行被原始换行截断，正文残片落在表外的 `:111`（表格结构破损） | **真实顽疾（元问题）**。三处都是「数值/状态没有机器闸门」——与 T10 的修法（`test_both_scanners_share_the_same_path_pattern` 改为真加载比对）同一处方，但 ADR 状态行与台账表格都还没有对应闸门 |

---

## D. 待验证假设（无证据，不得混入 A/B/C）

| # | 假设 | 为什么未验证 | 最小取证方式 |
|---|---|---|---|
| D-1 | **P3「Episode 是无真值状态的派生层」是否仍成立** | 本轮只确认聚合/查询/工具链在场（`agent/memory.py:2286/2332/2345`、`agent/store.py:1041-1060`、`agent/handler.py:8283-8310`），**未取到「temporal 聚合是否过滤 `memories.status`」的证据** | 读 `agent/store.py` 的 `query_memories_for_episodes` 同类查询（本轮 `grep "def query_memories_for_episodes"` = 0，说明实现名不同，需先定位真实函数） |
| D-2 | **`agent/metrics.py` 五条埋点是否真的无人读** | 本轮只读基线的**文字理由**，未逐条 grep 验证读方是否存在（教训 #36：工具报 0 不等于事实是 0） | 对五个方法名各自 grep 全仓 + AST 属性访问 |
| D-3 | **T4.5 FAIL 的「60s/120s 反馈闸门使分母结构性恒 0」** | 引用自 `记录/S4-Stage4判定.md`（台账 `任务台账.md:113`），本轮**未独立复现** | 查 `_apply_mood_report` 注入日志的生产命中数 |
| D-4 | **P1-1 `fulfilled>0` 是否产生过任何生产值** | 台账无对应观测记录，未查库 | 查 `memory.db` 的 tasks/confirmed_action_facts 计数（T4.3 记 `tasks 26 行 done24/failed1/cancelled1`） |
| D-5 | **T8 Stage 4 生产 soak（≥7 天连续）** | 台账 `任务台账.md:75` 自己登记为「待取证」 | 需 ≥7 天 `tools/长期优化快照.py` 连续记录 |
| D-6 | **台账 T30 编号重复**（`:63`/`:100` 已完成「我自己的检查与既有检查矛盾」 vs `:66` 待取证「两个计数器互相矛盾」） | 无法从文档判断哪个 ID 有效；`:141` 阻塞登记表该行还有 6 列错位（行内多出「修复 / 部分完成」两个单元格） | 需人判：为两条中之一重新编号 |
| D-7 | **P0-4 的 edge-tts 默认值是否为活路径** | 只确认 `config.yaml:178` 现值是 `gpt-sovits`；未验证「配置键缺失时 `handler.py:1438` 是否真的回落 edge-tts 并静默」 | 删键跑单测或读 `agent/voice.py:369-371` 的降级分支是否可达 |

---

## E. 给下一轮的 3 条最小建议

1. **C-1 需要一个决策，不是一次修复**：`trg_confirmed_action_fact_strict_anchor` 要求 outbox/mailbox 行，而成功路径不写——二者必有一方让步。建议单独立 ADR（「成功路径的锚点是什么」），否则第 10 次重提只是第 10 次记录。
2. **C-2 是一行修复**：`agent/handler.py:11994: existing_summary=""` → 对齐 `:12265` 的 `existing[:300]`。三轮无人认领，成本最低、收益最明确。
3. **给 ADR 状态行与台账表格加机器闸门**（C-5）：ADR 状态行的「仍待迁移」依赖人工更新，`CLASS_METHOD_BASELINE` 的计数也靠人工同步——两者本轮都被实测打脸（ADR-004 状态行过期；实际 71 vs 文档 72/79）。

---

*核查人：review-auditor（task-6）。全部结论均带 `file:line`；无证据项已单列 §D。本轮未修改任何业务代码、未跑 pytest、未重启 bot。*
