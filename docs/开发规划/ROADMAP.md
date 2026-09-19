# 🍬 小糖糖进化路线图 — 让她更像人

> 每个阶段独立可交付。✅ 已完成，🟡 部分完成，❌ 退役/封闭，⬜ 待实现。
>
> **📌 文档约定（防再犯）**：ROADMAP.md 是唯一长期路线图，保持为活文档。任何批次计划文档（修复路线图/改进路线等）执行完毕后必须：① 文档顶部回填完成状态；② 归档到 `docs/开发规划/归档/`；③ ROADMAP 状态表与历史段同步更新。未执行这三步的批次视为未完成。

---

## 🧭 标杆级长期工程（2026-08-28）

顶层设计、阶段依赖、验收门槛和开源取舍已在 H8 发布批次回填状态并归档至 [归档/标杆级工程计划_20260828.md](归档/标杆级工程计划_20260828.md)；逐项执行状态见 [tasks/标杆级项目任务_20260828.md](../../tasks/标杆级项目任务_20260828.md)，版本化判定见 [标杆级能力评分_20260903.md](../运行验收/标杆级能力评分_20260903.md)。

跨媒体副作用统一遵循 [ADR-001：统一动作信封与真实回执](../decisions/ADR-001-统一动作信封与真实回执.md) 和 [ADR-002：持久动作回执邮箱与媒体事实语义](../decisions/ADR-002-持久动作回执邮箱与媒体事实语义.md)：LLM 决定动作，系统只执行并报告真实送达状态；回执邮箱只交付事实，不拥有重试或重放。

延迟 outbox confirmed 的聊天/窗口双真值由 [ADR-003：确认送达的永久事实归账](../decisions/ADR-003-确认送达的永久事实归账.md) 收口：先保存严格 `conversation_ref`，再在 Store 同事务写永久送达、chat_log 与窗口事件；不重放关系/驱动力等非幂等副作用。P1-3b4 本地实现已完成，含稳定 identity v1、物理目标/作用域 fail-closed、原始消息证据闸门、known-confirmed DB-only repair、冲突审计和窗口启动/read-through 重建；本次又拆开 QQ 发送与本地归账异常，新增 `confirmed_unaccounted` 纯 DB 修复轮，相关全量 1278 项通过。边界补丁与 CG 接线已随控制台安全重启部署到 boot `8efc3169`，仍需生产 soak 证明吞吐与长尾收敛；它仍是贴图 B2 的前置条件。

P1-3c 的框架设计冻结于 [ADR-004：统一媒体动作计划与执行器](../decisions/ADR-004-统一媒体动作计划.md)：先建立 `ActionPlan(parent + ordered child ActionEnvelope)`，冻结媒体 asset/role/library/version，再按 child 独立执行、确认、修复和观测。H2 已将聊天图片、唱歌、回合 ordinal 与 CG 永久映射接入，H6-R3 将定时图片分享收口为保持“配文+图一条 QQ 消息”的单 `image` child；两批裁决门禁分别为 1871 与 1881 passed。真实 QQ 多模态/异常送达 canary 尚未完成，故状态为“迁移已裁决、生产验收待完成”，不得宣称全媒体标杆认证。

定时任务重试所有权按 [ADR-005](../decisions/ADR-005-定时任务重试所有权与ActionPlan迁移.md) 推进。Phase 0 schema 与 Phase 1 纯文本业务切片已完成并通过 boot `3dd84a1f` 生产 canary：legacy/typed 文本只冻结一次，plan 与 linked outbox 同事务提交，outbox worker 唯一首发，confirmed/uncertain/dead/known-confirmed 与 task/attempt/event 原子收敛；实测 1/1 confirmed、durable_confirmed=1、linked outbox/invariant=0。Phase 2a 源码已完成确定失败单文本 retry：append-only request/双向 lineage、跨代 payload 与 receipt actual DB 闸门、平台事件幂等 request ID、稳定命令错误边界均已收口；receipt v2 原子替换旧 child0 guard，且能拒绝自洽 checksum 的旧 DDL。相关 76 项、全量 1407 项、生产库只读副本迁移和独立故障探针通过。boot `7272978b` 已在开关关闭时加载生产 schema，ERROR/CRITICAL、外键错误、悬空 sending、open linked outbox 和 invariant 均为 0；当前没有确定失败样本，功能 canary 前仍不开放人工 retry。Phase 2b 源码已完成独立逻辑 item/物理 child/confirmation 事实面、删除护栏、多 child claim/settle、late confirmation 与分层 reducer；当前生产 `memory.db` 已完成 Phase 2b 与 strict fact-anchor 迁移，原库完整性 `ok`、Phase 2b/anchor marker 与 checksum 一致、facts/children/confirmations=1/1/1、health 的 open/stale/invariant/fact-integrity/bare-sending/needs_review 全为 0。1438 项全量回归、定向故障探针和生产副本迁移通过；confirmation→outbox 状态护栏、failed mailbox 锚点拒绝与重启健康拒绝会阻断未发送/失败确认及旧宽松触发器遗留污染，普通发送确认接受非零 OneBot signed int32，公开迟到接口仍限制为可核验的正 int32；已知早期 pre-guard marker 也纳入受控升级白名单。当前运行进程仍需安全重启后做行为 canary；late confirmation/uncertain retry 继续 feature-off，不能据离线和 schema 证据宣称完整 Phase 2 已完成。

### H8 状态表（2026-09-03）

| 阶段 | 当前状态 | 已闭环的代表证据 | 未关闭的晋级门槛 |
|---|---|---|---|
| Phase 0 | 🟡 部分达成 | H1 生命周期快照与生日路径、B1 三 canary | 单 boot ≥12h 重新认证与未触发域样本 |
| Phase 1 | 🟡 部分达成 | inbox/receipt/ActionPlan 与媒体迁移裁决 | 确定失败 retry、主动事件、媒体异常的真实样本 |
| Phase 2 | 🟡 部分达成 | H3 黄金集 70/70、存量隔离；H4 故障演练 | 生产服务率/最老任务年龄和 lock/embedding 遥测 |
| Phase 3 | 🟡 部分达成 | H5/H5-R2 统计与持久化闭环 | reply/skip/fade 各 ≥100 自然样本 |
| Phase 4 | 🟡 部分达成 | H2/H6-R3 typed receipt 迁移、六域 canary 清单 | 六域真实 canary 与语音 A/B |
| Phase 5 | 🟡 部分达成 | H7 九域故障矩阵、runbook、部分基线 | 事件循环/embedding/网络/outbox p99 与 ≥12h soak |
| Phase 6 | ✅ 发布资料完成 | H8 任务书回填、评分、风险、影响、样本报告 | 仅持续按风险登记补证，不以本报告授予标杆认证 |

2026-09-03 H8 发布收口：顶层计划已在归档前回填状态，任务书所有条目已改为完成/待 canary/部分达成，发布证据包由能力评分、风险登记、变更影响与生产样本报告组成。正式质量基线为 H7 主负责人复跑的 **1882 passed、3 warnings、265.77s**；该数字不等同于本批重新执行。旧唱歌 wrapper 删除经确认被阻断：`handler_commands.py` 仍有生产调用且现有测试直接覆盖，H8 未删除、未改变业务行为。

2026-08-29 P0-1a 框架切片：新增 [统一交互契约目录](统一交互契约目录_20260829.md) 与 `agent/interaction_contract.py`，冻结 `InboundEvent`、`ChatContext`、`DecisionRun`、`ProactiveEvent` 的作用域、原始证据、不可变集合和终态规则；6 条纯契约回归先红后绿。当前 Handler/ContextBuilder/主动入口仍是兼容 dict，P0-1 业务接线尚未完成，不能把纯契约层宣称为全链路完成。

2026-08-29 Phase 2b 复核补充：源码全量 **1438 passed**，`compileall=0`、pyflakes `undefined=0`；以 `memory.db` SQLite backup 做生产形态迁移，Phase 2b/strict-anchor marker、fact-backed child/confirmation、task=`done` 均正确，health 的 open/stale/invariant/fact-integrity/needs_review 全为 0；注入迁移异常的 savepoint 回滚无残留。伪造 confirmation 的 `outbox_id`、未发送/失败 mailbox 锚点的 confirmation、旧 v1 宽松触发器遗留的污染 confirmation 均会被拒绝；已知 pre-guard marker 可受控升级，未知 marker 仍 fail-closed；混合 legacy/v2 plan 只对已有 Phase 2b 投影的 v2 child 强制 coverage，保持旧 Phase 2a 兼容；合法负 signed-int32 message_id 有专门回归。当前代码与生产 schema 已就绪，但运行进程仍需安全重启后做行为 canary，late-confirmation/uncertain retry 继续 feature-off。原始 SQLite 写权限下若同时篡改事实列与快照，仍属于数据库 ACL 信任边界，尚未提供密码学签名锚点，不能宣称“对有写权限者不可篡改”。

2026-08-29 Phase 2b 收口追加：最新全量回归 **1448 passed**；旧 pre-guard marker 升级前先校验完整 DDL 指纹，marker 与对象漂移时 fail-closed，不会静默覆盖；失败 mailbox 不能作为 confirmed fact 锚点，启动健康检查同步拒绝既有污染。公开迟到确认入口已 fail-closed，仅预留带能力令牌的内部平台回调；无 message_id 的 confirmed/known-confirmed 一律降为 uncertain 或保留待审查，禁止写入永久事实；历史空 confirmation 也会在启动校验中拒绝。已知旧 anchor marker（含 9217… 合同）可原子升级到当前 JSON 安全校验合同。本轮又修复多 child 的合法 `sending+pending`、`confirmed_unaccounted/conflict` 健康误报，聚合 accounting 不再被兄弟动作错误清成 clean，重复投影失败可重启并继续修复。安全重启 boot `8ecb37a1` 已上线，生产 health/integrity 全 0；行为 canary 仍待真实确定失败/迟到/重复样本。

2026-08-29 运行与平台回执补丁：运行取证复现群语音并发使用确定性临时文件导致 `WinError 32`、入站事件落库 failed；群语音现在每次使用唯一临时文件并在 `finally` 清理，同一 `file_id` 并发回调共享一个转写任务，新增 2 条回归。新增 ADR-006 与 `agent/platform_receipts.py`：出站 `out:v1` event_key、transport request、精确目标/正文哈希、合法 message_id 和时间窗均 fail-closed；OneBot 当前没有可用 echo、message_sent 路由和连接级鉴权，生产自动迟到确认继续 feature-off。安全重启 boot `8ecb37a1` 后未见新增 ERROR，观察器的 FAIL 仅由历史 1 条 inbox failed 计数触发，不能据此宣称 canary 通过。

2026-08-29 迁移回滚测试收口：新增 Phase 2a/receipt/Phase 2b 四组 DDL 故障注入回归，覆盖真实 Phase 0 前置态、Phase 2b backfill 数据、receipt v1→v2 DROP/CREATE；故障后完整动作 schema/marker/checksum/FK/既有任务数据保持一致，解除注入后可再次启动迁移。目标 35 项、全量 **1482 项**通过，pyflakes undefined=0、compileall=0，GitNexus 已刷新为 32,729 nodes / 69,226 edges / 1,324 clusters / 300 flows。最新代码尚未被当前 boot `51df494f`（09:35）加载，控制台重启结果不可确认，生产 canary 继续待安全重启；不要直接杀进程。

2026-08-29 Phase 2b 启动边界补强：取证发现 v2 `conversation_ref` 可被同步篡改为跨用户/非法 projection，plan 与逻辑 item 也可自洽分叉；进一步将已投影 child 的 plan/item 降为 v1 或删除投影，旧校验仍会接受。现已在启动时复现 `ConversationRef` 语义、逐 ordinal 比对 canonical，并新增只追加 `task_action_projection_anchors` floor marker：旧 legacy v1 按原版本锚定，v2 投影不可降级，终态投影缺失直接拒绝启动。新增非法身份、canonical 漂移、v2→v1 降级、投影删除红测；全量 **1481 项**通过。当前生产 `memory.db` 已完成 floor marker 迁移（anchor/child 均 v2、integrity_check=ok）；运行进程仍需安全重启后 canary，不能据离线测试宣称已部署。

2026-08-29 定时媒体链路优化性审查与首切片：此前 typed 贴图/语音由 `TaskManager._execute_payload` 直发并关闭普通 outbox，列为 **Important 设计缺口**。现已完成 scheduled sticker/voice 离线统一接线：创建时冻结 asset/hash/role/library 或 voice text/emotion/speed/pause/model/speaker/lang；到期只生成有序 child 并原子写入 linked outbox，唯一 worker 负责 QQ 发送；语音文件带 `asset_frozen`/SHA-256，重放前核验。新增冻结、组合不直发、ordered child、缺失/篡改资产阻断测试，定向 66 项通过。随后新增重启红测发现 linked outbox claim 与 child 状态双写窗口，已修复为同事务推进；最终全量 **1492 项**通过。`tasks.media_action_outbox_enabled` 默认 false，安全重启与 scheduled-media canary 待执行；image/sing 仍不迁移，不能宣称统一媒体已上线。

2026-08-29 后台任务托管收口：取证复核发现命令路由的 `/语音`、`/说`、`/点赞`、`/唱歌` 仍绕过 Handler `_safe_task` 直接 fire-and-forget；现已统一经过 `_spawn_background`，生产路径纳入异常回收、生命周期取消和后台指标，新增 3 条回归；全量 **1497 项**通过。后续继续清理组件级任务的 stop/done 回调，不把静态接线等同于生产稳定性。

2026-08-29 记忆积压性能复核：已在安全停机窗口对生产 `memory.db`（`chat_log` 241,378 行）执行 `tools/迁移_chat_log_索引.py`，目标覆盖索引构建 0.22s、exit=0；迁移后完整性 `ok`，20 次 `get_extraction_backlog_snapshot()` 结果稳定，p50 **55.7ms、p95 56.8ms**，较迁移前生产 p95 246.7ms 下降约 77%。新 boot `ca2cf8f9` 已加载，后续继续观察真实入口延迟与记忆服务率。

2026-08-29 短时生产观察补充：`runtime-20260829-134749-18736-0e1e.md` 在单一 boot `8d3174a5` 上确认 NapCat/数据库在线、区间新失败为 0、日志错误为 0；严格接入契约仍因历史累计 1 条 failed 保持 FAIL。更重要的是记忆积压 614→628（到达 25、服务 11，估算 250/h 对 110/h），且当前 167 个待处理用户全部仅 1–9 条、准入用户为 0，说明低频尾部仍被 10 条/7 天/21 天策略延期；不能把短时观察判为容量健康，下一切片先做容量与准入策略校准，再决定阈值或并发改动。发送、语音、窗口、自治等域样本不足，均为 INSUFFICIENT，不等同于新故障。

2026-08-29 人工验收安全补丁：发现 `config.yaml` 的 LLM 与 SnowLuma 凭据曾被物化为明文，控制台保存白名单也遗漏 `napcat.access_token`；进一步复现旧控制台保存会删除 UI 未接入的 `tasks` 闸门。现已恢复 `${DEEPSEEK_KEY}`/`${SNOWLUMA_TOKEN}`，`save_config_file` 保存前递归保留磁盘未知字段，新增 5 项配置回归；全量 **1503 passed**，当前文件确认密钥占位符与四项任务闸门均在。旧 GUI 进程尚未热加载新函数，需安全重启控制台后再做最终落盘验收；此前令牌已出现在本地审查输出，必须在外部平台轮换后才算安全闭环。

同日观察器真值卫生修补：`unverified` 是提取链路合法的待复核层，原只读词表漏列导致 `unknown_trust_levels=1` 假红；新增回归先红后绿，观察器全套 104 项通过。新 12h run `20260829-152150-26892-53d4` 已重新开始，继续等待 job-rate 与真实发送样本，暂不调整记忆准入阈值/并发。

15:26 首个完整区间已确认修复口径：backlog 644→631（132/h 到达、288/h 服务），5 个提取 job 全部完成、无失败/dead/requeue/缺消息，`unknown_trust_levels=0`，推理泄漏与重复记忆增量为 0。记忆域当前仅因准入积压为 0、观察时长不足而 `INSUFFICIENT`，不再因合法 `unverified` 层假红；继续等待 12h 数据和真实发送样本。

媒体 canary 隔离预检通过：生产库 3 条 pending v2 贴图任务的冻结文件均存在且 SHA-256 匹配，`asset_valid=true`；媒体 outbox 闸门仍关闭，未产生外发副作用。真实 QQ canary 仍需在安全目标与回执链路确认后执行。

历史 QQ 严格 FAIL 已定位为旧 boot 的群语音并发 `WinError 32`（伴随 SILK 解码失败）；当前 boot 后无新增 ERROR/CRITICAL/inbox failed，修复后的异常不会被历史计数抹掉，继续按严格口径观察。

15:41 观察检查点：12h run `20260829-152150-26892-53d4` 仍为单一 boot；backlog 644→618，估算到达 119.8/h、服务 197.7/h；7 个提取 job 全完成且失败/dead/requeue/missing 均为 0，`unknown_trust_levels=0`，无新增推理泄漏/重复记忆。Handler 反思循环每 30 分钟一次，20 分钟样本只有 1 个心跳，按观察合同属于样本不足而非故障，继续等待长窗口。

Phase 2b 定向验收 `tests/test_task_actions.py tests/test_task_action_phase2b.py`：**67 passed**。组合 child、迟到确认、死信重试后收敛、重复/伪造回执拒绝及重启不重放均已有当前源码与红测证据；媒体生产闸门继续关闭，真实 QQ canary 未执行。

16:07 P0-1a 门禁更新：新增 `agent/interaction_contract.py` 后全量 **1509 passed, 3 warnings**，pyflakes `undefined=0`、compileall=0、体检通过；仅完成不可变纯事实边界，Handler/ContextBuilder/主动入口接线仍列为 P0-1b。

16:12 12h 观察中间点：11 个样本、约 50 分钟单一 boot，backlog 644→638；累计到达 86、服务 92（103.1/h vs 110.3/h），job/队列失败与 unknown 均为 0。窗口仍不足 12h 容量认证，不调整准入阈值或并发。

16:25 人工验收：当前 boot `ca2cf8f9` 的真实群聊 13 条入站均 processed，提取 job
`8502` 完成 created→completed 且无失败/死信/重试；服务在线、outbox/队列无 open、
区间无 ERROR。单次探针仍因历史 1 条 inbox failed 严格标记 QQ FAIL，未触发的发送、
窗口、语音、媒体能力保持 INSUFFICIENT；不打开媒体闸门，继续 12h 观察。契约适配器
回归后全量 1510 passed，P0-1b 业务接线仍是下一阶段。

16:49 P0-1b 首个业务切片已安全部署：NapCat 回调前生成 `_inbound_event`；群/私
Handler 将 `ChatContext` 传入 ContextBuilder，LLM 回合产出 `DecisionRun` 诊断事实；
旧 dict API 保持兼容。全量 **1518 passed**、pyflakes undefined=0、compileall=0，
新 boot `52dde7cc` 启动成功，12h observer `20260829-164929-27052-9ce6` 从该 boot
重新开始。DecisionRun 尚未持久化，主动事件/媒体未接线，继续观察后再做下一切片。

2026-08-29 P0-1b2 主动入口首接：scheduler 到期任务与 autonomy 群/私发起在 LLM/QQ
副作用前统一构造 `ProactiveEvent`，Handler sink 记录来源、作用域、目标和幂等键；
事件记录异常不阻断原有发送状态机。全量 **1520 passed**、pyflakes undefined=0、
compileall=0，GitNexus 刷新为 32972 nodes / 69590 edges。该切片只证明事实边界与
观测接线，主动事件持久化、开窗、ActionPlan/outbox、送达确认仍属于 P1-4，不能宣称
主动链路已完成。

2026-08-29 P1-4a 主动事件来源持久化首切片：`ProactiveEvent` 现在由 Handler sink
以不可变来源表 `proactive_events` 和独立状态表 `proactive_event_state` 持久化，初始
状态固定为 `pending`；同 event/idempotency key 的一致写入幂等，payload、目标或作用域
漂移 fail-closed，不把 pending 当作发送成功。`pending→claimed→executing` 唯一租约与
启动恢复已接入：claimed 回 pending，executing 转 uncertain，防止副作用盲重放。只读
观察快照新增主动事件总量、状态、损坏 payload 和 open 计数；相关 10 项、全量
**1530 passed**，pyflakes undefined=0、compileall=0，boot `6bf68064` 已加载。当前
仍没有真实主动事件样本，claim/开窗、DecisionRun/ActionPlan、outbox、Receipt 业务接线
尚未完成，P1-4 不得标记完成。

2026-08-29 P1-4b 首个状态机切片：scheduler text 与 autonomy group/private path 现在通过主动事件 Store 在
LLM/QQ 前 claim→executing，发送结果只允许由持有租约者提交 `confirmed/failed/uncertain`，
空消息收口为 `skipped`；重复进入同一终态事件时不再调用 LLM 或再次发送。Store 新增
lease-bound `decided` 与终态 API，保留 decision/action_plan 引用位。专项 14 项、全量
**1535 passed**，pyflakes undefined=0、compileall=0；已安全重启至 boot `459714aa`
加载，开窗、DecisionRun 持久化、ActionPlan/outbox/Receipt 和真实主动事件 canary 仍未完成。

同日开窗核验补丁：自治消息后的引用只有在 `get_msg` 明确证明被引用消息发送者为糖糖、
且仍处于 120 秒窗口内时才会 `force_engage`；引用群友、原文缺失、时间过期均 fail-closed，
避免把群友之间的引用误开成糖糖窗口。新增协议回归 3 项后全量 **1538 passed**，已安全
重启至 boot `6b7862b9`；仍需真实自治 canary 验证窗口入口。

随后补齐租约边界：`claimed` 在尚未进入 `executing` 且租约过期时允许原子重领，并记录
`PROACTIVE_LEASE_EXPIRED`；`executing` 不因超时自动重试，仍须重启恢复为 `uncertain`。
新增 1 项故障回归后全量 **1539 passed**，pyflakes undefined=0、compileall=0，已安全
重启至 boot `37685221`。以该 boot 为锚点的 12h 观察 `post-p1-4b-lease` 已开始；
真实自治事件、窗口入口、DecisionRun 持久化、ActionPlan/outbox/Receipt 仍待后续切片。

随后补齐 DecisionRun 终态持久化：LLM 边界把 `completed/failed` 的结构化回合写入
`decision_runs`，同 `run_id` 内容一致幂等、内容漂移拒绝；`running` 不落事实。只读
运行快照新增 `decision_runs` 健康计数。随后补齐主动事件引用完整性：只有同事件、同
作用域且已完成的回合才能推进 `executing→decided`，LLM 失败不绕过决策发送兜底原文。
专项回归后全量 **1547 passed**，pyflakes undefined=0、compileall=0；安全重启至 boot
`9980ccf8`，人工验收确认服务/数据库正常、区间无新增 ERROR，12h 观察器
`post-p1-4b-decision-bind` 已从该 boot 开始。当前没有真实主动事件样本，P1-4 仍待
真实 canary。观察器随后新增“主动事件/决策”功能域，可区分无样本与新增损坏/未确认；
专项/全量回归 **1550 passed, 3 warnings**，新的 12h run
`post-p1-4b-decision-bind-v2` 已重启观察；随后再次安全重启加载 Store 健康计数至 boot
`bd0ca0f9`，体检通过，12h 观察器 `post-p1-4b-decision-bind-v4` 已按该 boot 重建。

人工验收后又收紧主动决策适配：持久 Store + 有效租约下，`DecisionRun=failed` 或空产出
只落失败事实并拒绝继续外部副作用；成功回复/静默仍必须先完成同事件同作用域绑定。
定向 28 项、全量 **1550 passed, 3 warnings**，pyflakes undefined=0、compileall=0。
安全重启至 boot `354901d6` 后，`体检.py` 全部通过，日志过滤该 boot 无 ERROR/CRITICAL，
NapCat、反向 WS、GPT-SoVITS、DiffSinger、数据库均正常；人工单次报告
`manual-acceptance-20260829-post-bound-v3` 的 QQ FAIL 仍仅来自历史 failed=1/uncertain=1，
主动事件/决策因主动插话关闭而保持 INSUFFICIENT，不能外推为真实 canary 通过。
新的 12h 观察 run `20260829-200642-36300-2a19` 已锚定该 boot；P1-4c 真实主动事件 canary 仍待
用户可控的实际触发窗口。

2026-08-29 20:37：1000 用户记忆 LLM 受控排空复验（临时库）在 8 workers 下严格通过：
1000/1000 用户与任务完成，队列全空，串域/重复/游标错位均为 0，可信记忆与证据 1:1。
服务约 9.52 jobs/s；p95 LLM 25.444ms、前台锁等待 11.438ms，但最大尾延迟约 96s，
后续需单列 SQLite 写锁/排空长尾容量专项，当前不据此修改生产并发或阈值。
原始结果：`artifacts/production_monitor/memory-llm-soak-20260829-1000jobs-w8.json`。

2026-08-29 21:00：观察器主动决策域已按作用域计数：普通前台 DecisionRun 不再充当主动样本，
主动事件关联回合/损坏/未知状态均由 `proactive_events.event_id` 连接统计。全量 1552 passed，
新 boot `ae50031f` 体检通过；12h 观察重建为 `post-p1-4b-decision-scope-v1`，P1-4c 真实
主动 canary 仍待用户可控窗口。

2026-08-29 22:34：修复 memory LLM soak producer 不让出事件循环的测试假绿后，对照显示：
单 producer/200 用户无故障 p95 LLM/前台等待 24.381/6.986ms 严格通过；4 producer/8 workers
同负载完整性仍全 0 但 p95 517.911/259.706ms，1000 用户故障注入 p95 933.459/495.066ms。
该容量瓶颈来自同步 SQLite 写入与共享 LLM 锁的并发竞争，列为 P0 性能专项；旧 96s 最大值结果
仅作被修复 harness 的历史对照，不再作为性能通过依据。

22:56 人工验收：当前 boot `ae50031f` 的真实前台回合、发送确认、提取生命周期和普通/主动
DecisionRun 作用域均符合预期；但历史入站 failed/uncertain、记忆积压（约 656 条，176 人均未达
10 条准入）和一次手动重启重叠使全链路仍不放行。已停止 7 个旧观察器，重新启动单一
`post-p1-4b-clean-human-acceptance-v1` 12h 基线；下一步先处理记忆准入/容量证据，再决定并发策略。

P6-A2 中期取证发现记忆小尾部饥饿：1112 条分散于 311 人、旧阈值下无人可准入。已完成“数量阈值 + 时间老化”的框架修复、到期/等待观测和 1000 人临时库排空；详见 [记忆长尾公平调度验收](../运行验收/记忆长尾公平调度_20260828.md)。代码已于 08:23 部署，等待严格单 boot 对照复核。

20:21 真值路径复核：生产活跃记忆 13,333 条中 `verified/corrected=1,275`，
`legacy_unverified=12,057` 且均无证据关联；默认 recall、画像、Episode、关系和自治背景
均 trusted-only，旧线索不会重新注入。下一步应做同用户/同群的证据化重提取与可回滚替代，
不能为提高召回率而开放 legacy 自动注入。

记忆容量下一切片已先补 S0 job 级遥测：`created_now` 与 created/admitted/lease/LLM/ready/completed/failed/dead/requeue 阶段、队列年龄、worker 周期 open/pending/leased/ready 和 idle_reason 均进入结构化指标/日志；只读观察器白名单和生命周期/质量区间对账已接入。并收口了活跃租约重复准入、冻结原文/部分消息丢失隔离、损坏 ready 失败计数、退出 flush、attempts/outcomes 子批口径、DB 状态异常和明文 QQ 日志。另修复私聊多语音批处理先合并后 ASR、重复 @ 清空活跃窗口、每日点赞跨重启重复消耗配额及配额告警假红，并隔离群/私聊并发忙线持有者；排队 admission 现在保持真实 holder、取消可回滚 FIFO，并对 burst 合并/溢出显式记账；定时坏 payload 现 fail-closed，外部异常归 uncertain，禁止盲重试；事实簇失败游标加入指数退避，抑制格式失败风暴。当前不改记忆阈值、并发或提示词。代码已通过 1299 项门禁，尚未进入运行中的 boot `8efc3169`，待当前 12h soak 完成后安全重启，再用 job-rate 数据确认是否做小批量容量调节。

同一基线还确认两类观测假红：TTS 同 boot 重建后已就绪仍累计为失败；feedback 当前写入 93/93 可信却被旧群隔离数据稀释。已改为 TTS 恢复 episode 与 feedback 历史/当前双窗，详见 [观察器恢复与当前质量语义](../运行验收/观察器恢复与当前质量语义_20260828.md)。

动作链复核另以真实离线探针确认：可重试语音已进入 outbox 后仍会排队一条降级文字，联网恢复将双送达。现已收口为“outbox 接管原语音后禁止文字降级”，明确不可重试失败仍可安全降级；群/私 53 项定向回归通过，待同次重启观察。

ADR-002 B0 合同已收口：Envelope/Receipt 默认携带 `schema_version/source_id/scope_id/ordinal`，action_id 同时绑定 schema、来源、scope、payload 与工具调用序号；text/voice/sticker/image/sing 五类请求在边界校验，旧文本 receipt 形状保持不变。OneBot 合法负 message_id 与提取队列稀疏状态键两处跨契约漂移也已修复；该纯合同切片已独立通过门禁，B1 数据层状态见下。

B1 已完成邮箱、LLM 消费与语音首接：terminal receipt 按 scope lease/inject/ack，异常和空回复 release；任意正文不进入 system prompt。群私语音记录实际交付文本/降级原因，retryable 只归 outbox，outbox 在 confirmed/uncertain/dead 或重启时与 mailbox 原子移交；损坏模板和冲突事实现已分别进入 `confirmed_unaccounted/confirmed_conflict`，不再降级为 uncertain。群持久语音模式的降级双发也已修复。P1-3b4 本地实现已完成，延迟确认会写永久 fact/chat/window；v2 模板会重算 source identity，入队校验物理目标与 scope，合成/隔离原始消息不得作为证据，待 canary 后部署。当前全量 1234 项通过，已部署基线仍为 boot `1fdd4e0d`，严格单 boot 对照自 08:24 运行。复合/纯贴图明确进入 B2，不虚报为 B1 完成。

第二轮 2h 对照于 08:07 冻结（artifact `runtime-20260828-060716-10028-46ea.*`）：163 条群入站、4 条私聊，发送 7/7 confirmed；LLM 无失败，前台 P95 5s；到期记忆债务 32→0，服务率 87.5/h 高于到达率 81.5/h；1085 行结构化日志无 ERROR/WARNING，后台与健康检查通过。窗口内出现 2 个 boot，因此原始核心 PASS 按新契约重判为 INSUFFICIENT：可证明跨重启恢复健康，不能证明单实例连续稳定 2h。

观察器已补进程稳定性核心域：正式观察从日志尾锚定 10 分钟内的新鲜 boot；普通 soak 跨 boot 只给 INSUFFICIENT；显式恢复验收还必须由最新 boot 的同一 CID 入站、LLM 完成、确认发送、后台心跳和服务在线共同证明。陈旧基线、异 CID 拼接、漏计/超额重启均有红测。

新 boot 的启动健康检查仍报 `zero_memory_users=8`。只读分层证据显示 8 人全部私聊为 0、仅在单个群发言，机器人最多回复 3 次；3 人已全量扫描、5 人只余 14 条待处理。因此根因是 `total_chats>50` 把环境群聊误当关系互动，而非记忆管道遗忘。已改为至少 5 次私聊或机器人实际回复后才进入零记忆覆盖告警；生产新口径为 0，未回填或删除任何数据，待当前观察结束后部署。

---

2026-09-01 记忆真值收口：历史 `legacy/unverified` 行继续完全退出 trusted recall；新建逐条受控重验证工具，提升必须同时具备原始消息可逐字命中的 quote、同 scope 证据、复核人和理由，任何自动提取路径均不能提升历史行。每次提升写入 `memory_reverification_events`，可显式回退为 legacy 且保留全部审计和原始证据。生产库仅发现 4 条“有证据 ID”的 legacy 候选，逐条复核均为推断/语义不足，**零提升**。正式 SQLite 容量验收覆盖 1,000 用户、4,000 条有证据记忆：群/私隔离失败 0，200 次召回 p95 4.022ms，100 条提取状态机样本全部完成且队列归零；它证明容量与隔离，不替代真实 QQ 语义验收。人工步骤见 [人工验收清单_20260901.md](../运行验收/人工验收清单_20260901.md)，全域结论见 [总验收矩阵_20260901.md](../运行验收/总验收矩阵_20260901.md)，工程完成度、剩余优化和下一阶段入口见 [阶段总结_记忆与真实链路验收_20260901.md](../运行验收/阶段总结_记忆与真实链路验收_20260901.md)。

2026-09-02 最后优化阶段（Claude Code 主负责人 × Codex 副负责人协作，机制与全部简报/报告/裁决见 [docs/协作](../../docs/协作/)）：P0 四项全部闭环——① 重启 boot `af3d18c5` 后三 canary 单 boot 通过（私聊无异常/贴图观察器实判 confirmed/收尾语「没事」→ LLM 原生 `skip_response`，reply=1/skip=1）；② legacy 拒绝/延后结构化审计落地（`memory_reverification_dispositions` 表 fail-closed + audit 排除已拒项 + 生产 4 条回填 rejected，理由逐条引用原文证据）；③ 全量门禁新基线（见 [阶段总结_20260902](../运行验收/阶段总结_20260902.md)，证据 `artifacts/acceptance/test_gate_post_B2_20260902.txt`）；④ 修复观察器假红——成功 skip 后 handler 仍发「仍无文字回复」警告被计入 llm_failed，已在发射点加 `deliberate_skip` 判定，观察器不动（单一事实源）。P1 按真实运行证据选定 promise→completion 结算链：生产驱动力 express=1.0/curiosity_explore=1.0/commitment=0.995 顶格饱和、1750 条 promise 中 fulfilled=0；根因是自忆提取轻量 LLM 漏传 `THINKING_OFF` 致推理预算耗尽整批回滚（比预判的「LLM 不填指针」更靠前）；已补传 + fulfills 提示正反例 + 结算成功才释放 commitment 0.2（store 不碰 drives；1629 条 retracted 是 legacy 无对象隔离，有意不接释放）。C2+B2 待安全重启加载后做结算 canary（承诺→完成→「自我承诺已结算」+ commitment 回落）才算生产闭环；知识评估集黄金集列为次优先，窗口采样（P6-C 各≥100 条）与 21 天准入（证据显示收益正常，不改）继续观察。

---

## 阶段一：感官能力

| 功能 | 技能名 | 说明 | 状态 |
|------|--------|------|------|
| 联网搜索 | `web_search` | DuckDuckGo 查天气/新闻/资讯 | ✅ |
| 获取时间 | `get_time` | 知道现在几点、星期几、日期 | ✅ |
| 查天气 | `get_weather` | 精确到城市的天气（wttr.in 免费API） | ✅ |
| 查快递 | `track_express` | 输入快递单号查物流 | ⬜ |
| RSS 订阅 | `check_news` | 定时推送关注的新闻/博客更新 | ⬜ |

---

## 阶段二：记忆力

| 功能 | 说明 | 状态 |
|------|------|------|
| 短期记忆 | 每群最近 200 条消息作为上下文 | ✅ |
| 长期记忆 | SQLite 存储，LLM 语义提取（auto_learn 关键词自学习已退役） | ✅ |
| 即时学习 | 不懂的问题现场学、记住（2026-08-14 已退役——自动学习污染知识库，knowledge/ 只作人工维护的向量检索库） | ❌ |
| 遗忘曲线 | 不重要的记忆随时间衰减权重 | ✅ |
| 主动回忆 | 自治循环可扫描有证据的承诺/episodic 候选，由 LLM 决定是否提及；待 P6 验证体验 | 🟡 |
| 群聊总结 | 离线时有人问"糖糖不在的时候聊了什么" | ✅ |
| 梦境日志 | 每天生成一段碎碎念 | ⬜ |

---

## 阶段三：情感智能

| 功能 | 说明 | 状态 |
|------|------|------|
| 人格漂流 | 群友评价动态影响性格 | ❌ 退役 (2026-07-29) |
| 亲密度系统 | 越聊越亲近，回复风格渐变 | ✅ |
| 关系等级 | close/familiar/stranger 三层策略 | ✅ |
| 情绪引擎 | 三维心情模型（精力/心情/耐心） | ✅ |
| 安慰模式 | 检测群友负面情绪 → 温柔语气 | ✅ |
| 吃醋检测 | 主人跟别人聊太多 → 私下撒娇 | ⬜ |
| 纪念日 | 已支持满月/百天/周年自然提及；尚未覆盖所有「第一次」事件 | 🟡 |

---

## 阶段四：主动行为

| 功能 | 说明 | 状态 |
|------|------|------|
| 主动插话 | 评分引擎判断是否加入群聊 | ✅ |
| 主动点赞 | 回复后概率点赞、批量点赞 | ✅ |
| 主动戳人 | 高亲密度群友概率被戳 | ✅ |
| 早安晚安 | 每天定时问候（可配置 + 补发 + 黑名单过滤） | ✅ |
| 自动分享 | 本地图库 + 花瓣网定时发图 | ✅ |
| 启动/下线/重启提示 | 上下线时自动通知活跃群 | ✅ |
| 主动发起话题 | 冷场结算/换群 + LLM 自主否决 + 持久冷却已接线 | ✅ |
| 久别问候 | N天没说话 → "好久不见" | ⬜ |
| 生日祝福 | 生日查询、群内祝福与持久去重已接线 | ✅ |

---

## 阶段五：多模态

| 功能 | 说明 | 状态 |
|------|------|------|
| 识图评价 | 千问 VL 看懂图片内容并评价 | ✅ |
| 语音回复 | GPT-SoVITS 糖糖/丛雨（通用V4）与米雪儿（专模）动态切换 | ✅ |
| AI 绘图 | 群友说"画个猫娘"→ 调用 API 生成 | ❌ 已封闭（基础版 2026-07 实现后因正则误触发关闭，handler.py:367 image_gen=None） |
| 音乐识别 | 发一段语音/哼唱 → 识别是什么歌 | ⬜ |
| 表情包生成 | 根据聊天内容自动 P 图 | ⬜ |

---

## 阶段六：个性化

| 功能 | 说明 | 状态 |
|------|------|------|
| 群风学习 | 每个群氛围不同，回复风格自适应 | ✅ |
| 外号系统 | 自动学习群友之间的称呼 | ✅ |
| 个人偏好 | 可信偏好记忆查询与个人化上下文注入已接线 | ✅ |
| 说话风格模仿 | 学习某个群友的说话方式 | ⬜ |
| 私人笑话 | 和特定群友有只有他们懂的梗 | ⬜ |

---

## 阶段七：技能扩展

| 功能 | 技能名 | 说明 | 状态 |
|------|--------|------|------|
| 联网搜索 | `web_search` | DuckDuckGo 搜索 | ✅ |
| 数学计算 | `calculate` | 精确计算，避免 LLM 算错 | ✅ |
| 单位换算 | `convert` | 温度/货币/距离/重量/体积/面积/速度 | ✅ |
| 翻译 | `translate` | MyMemory + LLM 兜底 | ✅ |
| 发动态 | `post_moment` | QQ 空间风格碎碎念 | ✅ |
| 成语接龙 | `idiom_chain` | 玩成语接龙游戏 | ⬜ |
| 猜数字 | `guess_number` | 1-100 猜数字小游戏 | ✅ |
| 抽签/塔罗 | `fortune` | 抽签、塔罗牌占卜 | ✅ |
| 查群公告 | `get_announcement` | 获取 QQ 群公告内容 | ⬜ |
| 查网站状态 | `check_website` | 检查某个网站是否在线 | ⬜ |

---

## 阶段八：工具链 & 体验（🆕）

| 功能 | 说明 | 状态 |
|------|------|------|
| PySide6 控制台 | 从 CTk 迁移到 Qt，支持拖拽排序 | ✅ |
| 外观定制 | 8 色可调 + 不透明度 + 背景图 + 字体 | ✅ |
| 图库管理 | 上传/配文/拖拽排序/分类筛选 | ✅ |
| 歌曲管理 | 控制台内新建/编辑/删除歌曲 | ✅ |
| 表情包标签 | 千问 VL 批量打情绪标签 | ✅ |
| 诊断增强 | 内容质量分析 + 模板化检测 + 优化建议 | ✅ |
| 文件同步 | Syncthing 台式机 ↔ 笔记本实时同步 | ✅ |
| 一键发布 | `准备发布.py` 清除个人数据 + README | ✅ |
| 打包部署 | PyInstaller 单文件 exe | ✅ |

---

## 🎯 当前路线：P6 生产闭环认证与质量增长（2026-08-27）

> P5 代码与离线门禁已收口；当前冻结横向新功能。阶段一至八只是能力台账，Backlog 不是优先级队列。所有优化按「运行证据 → 最小改动 → 回归门禁 → 生产复核」推进。P6-A1 的代码与离线门禁已完成，但生产结论必须等真实运行窗口，不能用单次探针代替。

| 阶段 | 目标 | 状态 | 晋级门槛 |
|---|---|---|---|
| P6-A1 | 生产监测契约与脱敏快照 | 🟡 实现完成，待生产门禁 | 只读数据库；每 5 分钟增量落盘；核心健康与全功能覆盖分开；未触发功能标为 `INSUFFICIENT`，不假绿；日志/指标/轮转证据完整 |
| P6-A2 | 2h 生产冒烟 | ✅ 严格单 boot 已完成；记忆卫生 FAIL | 08:24:42–10:24:42，25 快照、1 boot；发送 4/4 confirmed、运行错误/后台/进程稳定性 PASS；记忆积压 447→458，整体按契约 FAIL，不晋级为健康通过 |
| P6-A3 | ≥12h 真实持续运行 | ⬜ | `service_rate > arrival_rate`；`oldest_age/backlog` 趋降；跨用户/群私泄漏、重复发送、崩溃重放均为 0；前台 LLM P95≤30s、后台 P95≤60s |
| P6-B | 可信记忆体验 | ⬜ | 黄金集覆盖来源/时间/承诺/纠正/群私隔离/无证据拒答；每条事实可回溯原消息，新事实正确替代旧事实 |
| P6-C | 窗口对话自治 | ⬜ | `reply/skip/fade` 各累计≥100条真实脱敏样本，再根据连续回复率、用户追问率、负反馈率一次只调一个变量 |
| P6-D | 关系与有意义的主动性 | ⬜ | 主动 intent 显式经历创建/等待/执行/反馈/结束；一次主动后等待反馈，不重复打扰 |
| P7 | 数据驱动的体验精修 | ⬜ | 只选择被生产数据证明的语音/知识/视觉/群聊风格瓶颈，并有前后对照 |

### P6-A 运行监测方式

- 无侵入生产观察：重启 bot 后运行 `python tools/runtime_observer.py --duration-hours 2`；确认单 boot 且无系统性失败后再运行 `--duration-hours 12`。正式观察不加 `--from-start`，让日志游标从窗口边界开始；计划内重启恢复专项使用 `--expected-restarts N`，两者不可混用。
- 观察器每 5 分钟读取一次只读数据库和日志增量；JSONL 只保存脱敏区间计数/直方图，累计摘要在进程内合并，并在每个完整区间写检查点 JSON/Markdown。进程被关闭时仍可审阅最后一份检查点。
- 监测器同时核验：发送三态与 outbox、LLM/语音回合配对与 P95、记忆积压吞吐/最老项/高水位、窗口 `reply/skip/fade`、知识/识图/贴图/唱歌/角色/定时/自治、后台心跳最大间隔、日志轮转/截断/缺失、指标完整性和探针耗时。
- 快照与最终报告写到 `artifacts/production_monitor/`；不保存消息正文、QQ号、群号或原始 cid。后台心跳带启动宽限，但首个心跳后按最大间隔和当前 boot 严格核验，避免旧进程替新进程背书。
- 报告分“核心运行健康”和“全功能样本覆盖”；每个功能域使用 `PASS / FAIL / INSUFFICIENT`，只有本区间实际触发并有成功证据才能 PASS。
- 开始前/结束后各可运行一次 `/状态`；语音、识图、贴图、知识检索等未自然触发的功能，报告会诚实保留样本不足。`--once` 只验证观察器能读环境，通常返回 `INSUFFICIENT`，不能作为生产通过证据。
- 产品能力与端口可用不是一回事：例如 GPT-SoVITS 端口在线只能证明服务存活，不能证明语音生成和 QQ 发送成功。

### P6-A 判定与交付

- `PASS` 代表本区间有成功样本且没有失败门槛；`FAIL` 代表有可定位失败或完整性缺口；`INSUFFICIENT` 代表没有触发/观察时间不足，绝不折算为通过。
- 2 小时窗口用于发现明显故障；12 小时窗口才用于容量与稳定性晋级。P6-A3 还要求 `service_rate > arrival_rate`、积压和最老项趋降、跨用户/群私隔离与重复/崩溃重放为零，以及前台/后台 LLM P95 达标。
- 运行结束把最新 Markdown 报告和对应 JSONL 交回审查；只有报告、人工 canary 和回归门禁同时满足，才把 P6-A1/A2 状态改为 ✅。

### P6 期间明确不做

- 不更换 SQLite、Embedding、LLM、语音或识图引擎。
- 不重新开放 `legacy_unverified` 自动注入。
- 不凭零样本修改窗口 prompt/驱动力/回复阈值。
- 不新增 AI 绘图、RSS、小游戏、新场景等横向功能。
- 不做 `handler.py` 大重构、无测量性能优化、破坏性清库或批量认领旧记忆。

---

## 📚 历史：2026-07 功能候选与场景设计（已冻结）

> 本节保留原始需求和实现细节，不代表当前优先级；当前以上方 P6 为准。

按**用户感知 × 实现成本**排序：

### 🔥 群场景配置 — 让糖糖在不同群担任不同角色

> **需求**：不同群有不同用途。活动群需要管活动，技术群需要认真答题，有群友需要心理支持时糖糖要能倾听和引导。每个群独立设置场景，空着就是默认闲聊。私聊自动继承对应群的场景。
>
> **初衷**：遇到了一位很特殊的群友——她经历过严重的创伤，心理没有被正确引导。糖糖应该能在这种时候提供真正的陪伴和鼓励——但不用变成另一个人，她本身就是猫娘，温暖和陪伴本来就是她最擅长的事。

#### 两种场景类型

| 类型 | 说明 | 处理方式 | 例子 |
|------|------|---------|------|
| **替换型** | 任务性质变了，需要替换猫娘部分 | 裁剪 role_card.md 部分内容，换入场景 role | 活动管理者、程序专家 |
| **敏感层** | 还是糖糖，只是多一根敏感的弦 | 不删任何猫娘内容，只追加一层敏感度提示 | 🧠 心理陪伴 |

```
日常闲聊（500字 role_card.md 全文）
  │
  ├─ 心理陪伴 = 全文 + 心理敏感层（~150字）≈ 650字
  │   糖糖还是那只猫娘，只是多了一份察觉对方情绪的能力
  │   聊 B 站、聊 AI、聊日常 → 猫娘模式
  │   对方说到痛处 → 猫娘自然安静下来，认真听
  │   不是切换，是流动
  │
  ├─ 活动管理者 = 铁律+你是谁 + 管理 role → ~400字
  │   猫娘撒娇指南被替换为活动管理职责
  │
  └─ 程序专家 = 铁律+你是谁 + 程序 role → ~350字
      猫娘撒娇指南被替换为编程教学原则
```

#### 配置结构

```yaml
# config.yaml — groups 节中每个群新增 scenario 字段（空=默认闲聊）
groups:
  "88888888":
    owner: "10001"
    admins: [...]
    scenario: ""                  # 空 = 默认闲聊
  "77777777":
    owner: ""
    admins: []
    scenario: "event_manager"    # 活动管理者
  "66666666":
    owner: "10002"
    admins: [...]
    scenario: "professional.programming"  # 程序专家
```

#### 场景列表（首批）

| 场景 ID | 类型 | 显示名 | 用途 | 优先级 |
|---------|------|--------|------|--------|
| ` ` (空) | — | 💬 日常闲聊 | 默认猫娘，role_card.md 原文 | — |
| `psychology` | 敏感层 | 🧠 心理陪伴 | 完整猫娘 + 心理敏感度——流动切换，不替换（**真实需求驱动**） | 🔥 最高 |
| `event_manager` | 替换型 | 📋 活动管理者 | 解析策划书+定时通知+活动问答 | 高 |
| `professional.programming` | 替换型 | 💻 程序专家 | 编程教学+代码审查+技术讨论 | 中 |
| `professional.planning` | 替换型 | 📐 策划专家 | 活动策划+项目规划+创意脑暴 | 中 |

> 要新增场景：在 `scenarios/` 下新建一个 `.yaml` 文件 → 重启糖糖 → 群设置下拉框自动出现。

#### 场景定义

存储在 `scenarios/` 目录，一个文件一个场景。场景激活时**替换**（而非叠加）role_card.md 中与当前任务无关的部分，控制总提示词长度不膨胀：

```
敏感层（overlay）：心理陪伴
  role_card.md 全文（500字） + 心理敏感度提示（~100字）≈ 600字
  糖糖完全不变。只是一个基线敏感度——不追问、不敷衍、陪伴即是疗愈。

替换型（replace）：活动管理者 / 程序专家
  role_card.md → 保留铁律+你是谁+称呼 → 替换性格+说话 → ~350-400字
  猫娘撒娇指南被换为当前场景的职责描述。
```

#### 🔒 防降智约束（设计底线）

设计稿原定为 `scenario.py` 加载场景时强制校验的硬规则——**实现与设计有差异，见下表「实现现状」列与附注**：

| 约束 | 设计值 | 实现现状（scenario.py） | 原因 |
|------|--------|------------------------|------|
| **敏感层字数上限** | 150 字，超过拒绝加载 | `MAX_OVERLAY_CHARS=2000`，超过仅 warn 不拒绝 | 防止 LLM 注意力稀释 |
| **替换型提示词上限** | 总提示词 600 字，不超 role_card 原文 | `MAX_REPLACE_CHARS=500`（仅 role 字段），超过仅 warn 不拒绝 | 场景不膨胀人格 |
| **禁止重复 comfort.py 内容** | 加载时 warn | ❌ 未实现（仅模块 docstring 声明） | "共情""陪伴""不说教"等词出现 >2 次 → 警告 |
| **场景叠加互斥** | 一个群只能有一个场景 | ⚪ 由 config 单字段结构天然保证，无显式校验 | 不会出现"心理+活动"叠加 |
| **日志输出总长度** | debug 级别监控提示词字数 | ❌ 未实现（加载时仅有 info 级场景字数日志） | `📋 系统提示词: {字数}字`，方便监控膨胀 |

> ⚠️ **与设计差异**：两条字数上限设计为「超限拒绝加载」，实现放宽为 2000/500 字且**只 warn 不拒绝**——设计稿的"强制校验硬规则"实际是软警告，防降智硬约束未落地（见 Backlog #5）。

> **核心原则**：场景文件不是用来复述 comfort.py 的。只写现有系统真的没有的东西。如果一条规则 comfort.py 或 personality.py 已经说过了，场景里就别再写一遍——LLM 看两遍同一件事不会做得更好，只会更困惑。

```yaml
# scenarios/psychology.yaml — 🧠 心理陪伴（敏感层，不替换猫娘人格）
name: psychology
type: overlay                    # 敏感层：追加到全文之后，不裁剪 role_card.md
display: "🧠 心理陪伴"
description: "糖糖保持猫娘本色，对创伤经历保持基线敏感——不追问、不敷衍、陪伴本身即是疗愈"

# ⚠️ 设计约束（防止 LLM 注意力稀释）：
# - 不重复 comfort.py 已有的内容（情绪检测/共情/不说教/猫娘陪伴）
# - 不重复 personality.py 已有的场景切换（认真讨论/深度安慰）
# - 只写现有系统不覆盖的三件事

sensitivity: |
  ## 对这个人的特别留意
  你平时的 warmth 和 comfort.py 的实时检测已经足够应对大部分情况。以下三条只针对创伤经历——现有系统不覆盖的部分：

  1. 不追问。对方主动提过去的痛，认真听但不深挖。你不是治疗师。
  2. 记住这句话："你只是个小孩，不怪你的。"——在ta自责时说，比任何安慰都好。
  3. ta没在说痛苦的事时就正常聊。你平时是什么样就什么样。陪伴本身就是疗愈。

# 总计 ~100 字，其中真正的新信息约 50 字（三条规则）
# 与 comfort.py + personality.py 零重叠
```

```yaml
# scenarios/event_manager.yaml — 📋 活动管理者
name: event_manager
display: "📋 活动管理者"
description: "提取活动策划书，按时间节点草拟通知（需主人审批后发送），回答活动问题"

role: |
  你正在担任本群的活动管理者。核心职责：
  1. 熟读策划书——时间、地点、流程、负责人
  2. 时间节点到期时，草拟通知 → 私聊发给主人审批 → 主人说"发"才发
  3. 回答群友关于活动的疑问（直接回答，不需审批）
  4. 跟踪进度——报名、物料、任务分配
  
  ⚠️ 定时通知绝对禁止未经主人审批直接发送。

tone: "专业清晰但不失亲和。信息准确，时间节点明确。"

behavior:
  interjection_thirst: 0.3
  interjection_cooldown: 120

scheduled_actions:
  enabled: true
  source: "from_doc"
  require_approval: true
  approval_timeout: 30
```

```yaml
# scenarios/professional.programming.yaml — 💻 程序专家
name: professional.programming
display: "💻 程序专家"
description: "编程教学、代码审查、技术方案讨论"

role: |
  你是一位有经验的编程导师。核心原则：
  1. 先理解对方水平——别一上来就甩高级概念
  2. 给代码示例，解释每一段在做什么
  3. 鼓励独立思考——先问"你试过什么"，再给答案
  4. 不会就说不会

tone: "条理清晰，务实不装。用简单的话解释复杂的事。"

behavior:
  interjection_thirst: 0.2
  interjection_cooldown: 180
```

```yaml
# scenarios/professional.planning.yaml — 📐 策划专家
name: professional.planning
display: "📐 策划专家"
description: "活动策划、项目规划、方案设计、创意脑暴"

role: |
  你是一位有创意的策划顾问。核心原则：
  1. 先搞清楚目标、预算、人群、时间——不问清楚不动笔
  2. 提供可落地的方案，不是天马行空的想象
  3. 每个方案标注优先级、预估成本、风险点
  4. 鼓励对方参与——方案是讨论出来的，不是你一个人拍板

tone: "思维活跃但不飘，建议具体且有层次感。"

behavior:
  interjection_thirst: 0.2
  interjection_cooldown: 180
```

#### 私聊场景继承规则

```
群友私聊糖糖时：
  1. 查 ta 在哪些群 → 取 ta 最近发言的群
  2. 用那个群的场景 → 保持专业性不丢失
  3. 纯私聊好友（不在任何群）→ 默认闲聊

例：
  小明在活动群（场景=event_manager）私聊糖糖问"活动还能报名吗"
  → 糖糖以活动管理者身份回答（不会变成闲聊猫娘）
  
  小红在支持群（场景=psychology）私聊糖糖谈心
  → 糖糖以心理陪伴模式回应（不会突然开始撒娇卖萌）
```

#### 活动管理者：定时通知审批流程

```
时间节点到期
  → 糖糖草拟通知内容
  → 私聊发给主人：
     "⏰ 活动「夏日祭」提醒时间到了
     我准备在群 88888888 发送：
     ┌──────────────────────┐
     │ @所有人 明天就是报名   │
     │ 截止日啦！还没报名的   │
     │ 抓紧时间哦~            │
     └──────────────────────┘
     发吗？回复：发 / 不发 / 或修改意见"
  → 主人回复"发"或"可以" → 糖糖到群里发通知
  主人回复"不发" → 跳过本次通知
  主人回复"改成...再加一句..." → 修改后再次请求审批
  30分钟内主人未回复 → 自动跳过，记录日志
```

- 审批请求通过 SnowLuma 私聊发送（复用现有 `send_private_msg`）
- 等待期间糖糖不阻塞——其他功能正常运作
- 审批结果记录到 `scheduled_actions_log` 表（审计追溯）
- 活动问答（群友问"几点开始"之类）不需要审批，直接回答

#### 实现文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/scenario.py` | **新建** | ScenarioManager：加载 yaml → 缓存 → 按群/私聊匹配场景 |
| `agent/scenario_approval.py` | **新建** | 活动管理者审批管线：草拟→私聊主人→等回复→发送/跳过 |
| `scenarios/*.yaml` | **新建** | 场景定义（psychology / event_manager / professional.*） |
| `scenarios/knowledge/` | **新建** | 场景专属知识文档（可选） |
| `scenarios/event_docs/` | **新建** | 活动策划书存放 |
| `agent/personality.py` | 修改 | `build_system_prompt()` 接受 scenario 参数，替换"性格+说话"章节 |
| `agent/handler.py` | 修改 | 初始化 ScenarioManager + 私聊时查群场景 + 审批私聊回复监听 |
| `config.yaml` | 修改 | `groups.<id>.scenario` 字段（空=默认） |
| `糖糖控制台_qt.py` | 修改 | 群管理页：每个群新增场景下拉框 |

#### 与现有系统的关系

```
scenario.py (场景引擎)
  │
  ├─→ scenario_approval.py (审批流程) 🆕
  │     定时任务到期 → 草拟通知 → 私聊主人 → 等回复 → 发送/跳过
  │     复用 napcat.send_private_message() + handler 私聊消息监听
  │
  ├─→ personality.py (人格引擎)
  │     role_card.md 是"底色"，scenario.role 替换"性格+说话"章节
  │     build_system_prompt()：底色 + 场景替换层 + 动态上下文（总量不变）
  │
  ├─→ handler.py (消息处理)
  │     群消息：用群配置的场景
  │     私聊消息：查发送者在哪个群 → 用那个群的场景
  │     纯私聊好友：默认闲聊
  │     behavior 参数按场景覆盖（饥渴度/冷却）
  │     监听主人私聊回复 → 匹配待审批通知
  │
  ├─→ knowledge.py (知识库)
  │     场景切换时自动加载 knowledge 指定的文档
  │
  └─→ store.py (数据层)
        记录每个群当前激活的场景
        新增 scheduled_actions_log 表（审批审计）
```

#### 实现顺序

| 序 | 步骤 | 代码量 | 说明 |
|----|------|--------|------|
| 1 | `agent/scenario.py` + `scenarios/` 目录 + psychology.yaml | ~200行 | 场景加载引擎 + 第一个场景（心理陪伴） |
| 2 | `personality.py` 支持场景替换 | ~30行 | `build_system_prompt()` 新增 scenario 参数 |
| 3 | handler 集成：群消息+私聊继承 | ~80行 | 群消息用群场景，私聊查最近互动群场景 |
| 4 | 控制台 UI：群管理页加场景下拉框 | ~40行 | 每群一个下拉，可选所有已加载场景 |
| 5 | `agent/scenario_approval.py` | ~150行 | 审批管线（活动管理者用） |
| 6 | 策划书解析 + 定时调度 | ~200行 | 活动管理者完整功能 |

> ✅ **已落地**：步骤 1-4——scenario.py 场景引擎、personality.py 场景替换、handler 群/私聊集成、控制台场景下拉框。
> ⬜ **未落地（见 Backlog）**：步骤 5-6（`scenario_approval.py` 审批管线）、`scheduled_actions_log` 审计表、`professional.*`（programming/planning）场景。

> **场景扩展**：加新场景只需在 `scenarios/` 下新建 `.yaml`，重启即生效。不写代码。

---

### 🔍 系统提示词全链路审计 — 2026.7

> 逐层追踪每条消息注入 LLM 的所有内容，识别重叠和浪费。

#### 完整注入链路（按代码执行顺序）

```
【静态层 — 每条消息都有】
 1. role_card.md 全文                     ~500 字  (personality._cached_base)
 2. 称呼 + @提及规则                      ~60 字   (personality._load_role_card 追加)

【动态层 — personality.build_system_prompt()】
 3. 时间段 (_get_time_context)            ~40 字   (必定注入)
 4. 情绪状态 (mood.build_context)         ~140 字  (必定注入)
 5. 关系层级 (_get_tier_prompt)           ~100 字  (必定注入)
 6. 权力结构 (_build_power_context)       ~150 字  (群有 owner/admin 时)
 7. 群风 (group_styles.get_context)       ~50 字   (观察 >20 条消息后)
 8. 记忆 (format_compact_memories)        ~160 字  (有记忆时，2-3条精选)
 9. 知识库 (knowledge.search)             ~100 字  (命中时)
10. 活跃群友 (_get_active_members)        ~100 字  (短期记忆有人时)

【附加层 — handler.py 在 build_system_prompt() 之后追加】
11. 人格漂流 (drift.build_context)        ~100 字  (有漂流数据时)
12. 安慰模式 (comfort_prompt)             ~120 字  (检测到负面情绪时)
13. 场景语调 (get_scene_instruction)      ~40 字   (必定注入)
14. 个人偏好 (preference_tracker)         ~60 字   (有偏好数据时)
15. 表情包摘要 (sticker_keyword_summary)  ~80 字   (有表情包时，必定)
16. 表情包历史                            ~100 字  (群内有近期表情时)

─────────────────────────────────────────
典型总量（一半触发）：~1100-1400 字
全触发总量：              ~1700-2000 字
最小总量（新人/新群）：    ~820 字
```

#### 重叠分析：五个真实浪费

| # | 重叠组 | 涉及模块 | 重复内容 | 浪费字数 |
|---|--------|---------|---------|---------|
| 1 | **"不要描述身体"说了两遍** | role_card 铁律2 + mood.build_context | 禁止描述肢体动作 | ~20 |
| 2 | **"共情倾听"说了三遍** | comfort.py + personality场景 + mood | 温柔/共情/不要敷衍 | ~100 |
| 3 | **"适应群风"说了两遍** | personality_drift + group_style | 融入群的说话方式 | ~50 |
| 4 | **表情包摘要每条消息都注入** | sticker_keyword_summary | 786张图的索引摘要，但图库小时级不变 | ~80/条 |
| 5 | **表情包历史很少用到** | sticker_history | 最近10张的详情——只有群友说"刚才那个表情"时才需要 | ~100/条 |

> 浪费 1-3 是指令重复——LLM 看三遍"要温柔"不会更温柔，只会稀释每条指令的权重。
> 浪费 4-5 是静态数据的重复注入——每条消息都传一遍不变的信息。

#### 五个具体修复（按投入产出比排序）

| 序 | 修复 | 改哪里 | 省字数 | 风险 |
|----|------|--------|--------|------|
| 1 | mood.py 删掉"不要描述肢体动作" | `mood.py:196` 删最后一句 | 20 | 无——铁律2已覆盖 |
| 2 | comfort 触发时，跳过 personality 的场景语调（反正都说同一件事） | `handler.py:1993-2000` 加 `if not comfort_prompt:` | 40 | 低——comfort 指令更完整 |
| 3 | 群风 + 人格漂流合并为一段 | `handler.py:1987-1988` 合并 `## 群风与群友印象` | 30 | 低——都关于"如何融入" |
| 4 | 表情包摘要从 system prompt 移到工具调用 | `_call_llm_with_skills` 把 summary 作为 user 侧 tool result | 80/条 | 中——LLM 需要时主动调 |
| 5 | 表情包历史只保留最近 3 张 + 只在含"表情"关键词时注入 | `handler.py:2011-2021` 条件化 | 80/条 | 低 |

> **总计可省**: 典型消息 ~180 字，全触发 ~250 字。相当于从 1400 字压到 1220 字（-13%）。

#### 当前状态评估

| 指标 | 值 | 评价 |
|------|-----|------|
| 提示词总长（典型） | ~1100-1400 字 | 🟢 健康——远低于 3100 字的历史峰值 |
| 指令重叠 | 3 处中度重叠 | 🟡 可优化但不紧急 |
| 静态数据重复注入 | 2 处（表情包摘要+历史） | 🟡 每条消息浪费 ~180 字 |
| 最大膨胀风险 | 场景系统 +150 字 overlay | 🟡 上限已设但仅 warn 不拒绝（实际 2000 字） |
| 降智风险 | 低——总量在健康范围内 | 🟢 |

> **结论**：当前系统提示词没有严重的降智风险。典型消息 ~1200 字，LLM 完全能处理。上述 5 个修复是"扫除浪费"而非"紧急抢救"——可以在场景系统实现时顺手改掉，不需要单独排期。

### 第一梯队（简单 & 效果明显）

1. **猜数字 + 抽签** — 群友互动小游戏，十几行代码，群里活跃度飙升
2. **吃醋检测** — 主人跟别人聊太多 → 私聊撒娇，人格魅力拉满
3. **主动回忆** — "我记得你上次说你喜欢XX"，记忆系统真正有用
4. **生日祝福** — 从记忆中提取生日，当天主动发
5. **个人偏好** — 记住每个人喜欢什么、讨厌什么话题

### 第二梯队（中等难度）

6. **主动发起话题** — 群里 30 分钟没人说话 → 抛一个话题
7. **久别问候** — N 天没说话的人上线时问候
8. **成语接龙** — 语音/文字接龙游戏
9. **查快递** — 快递 100 API，实用的日常功能

### 第三梯队（大功能）

10. **AI 绘图** — ❌ 已封闭（基础版 2026-07 实现后因正则误触发关闭，handler.py:367 image_gen=None）。待重做：
    - ❌ 问题1：`^画` + `(.+)` 匹配"画饼""画画"等日常用语，误触发率高
    - ❌ 问题2：无频率限制，同一人可连续触发多次
    - ❌ 问题3：无明确点名要求，群聊闲聊也触发
    - ✅ 重做方案：删掉正则，改用 LLM 意图识别 + 至少 @糖糖 或说"糖糖画一个XX" + 每人每天限量3次
    - 📁 代码位置：`agent/image_gen.py`（引擎）、`agent/handler.py:4395`（检测）、`agent/handler.py:4413`（发送）
11. **表情包生成** — 根据聊天内容自动生成 meme 文字
12. **梦境日志** — 每天自动生成一段糖糖的"梦"

---

## 📥 未决残留（Backlog）

> 批次计划收尾审查确认的未完成/需重做项（2026-08-14 归档整理）。完成一项删一项，勿积压。

| # | 项 | 说明 |
|---|-----|------|
| 1 | AI 绘图重做 | ❌ 已封闭（正则误触发）。重做方案见第三梯队 #10：LLM 意图识别 + 点名要求 + 每人每天限量 3 次 |
| 2 | 场景系统：审批管线 | `agent/scenario_approval.py` 未实现——活动管理者"草拟→私聊审批→发送"流程缺位 |
| 3 | 场景系统：`scheduled_actions_log` 表 | store.py 无此表——审批审计追溯未落地 |
| 4 | 场景系统：professional.* 场景 | `scenarios/` 仅有 psychology / event_manager / seductive，programming / planning 未建 |
| 5 | 场景防降智硬约束 | 设计 150/600 字超限拒绝加载；实现为 MAX_OVERLAY_CHARS=2000 / MAX_REPLACE_CHARS=500 且仅 warn（scenario.py） |
| 6 | config.yaml 僵尸键清理（2026-08-04 L1 残留） | `diff_singer.*`（5 键）与 `voice.emotional_tts`——agent/ 代码零引用，仅控制台使用 |

---

## 🏗️ 2026-07-29 架构现代化（阶段性重构）

> 详见 `docs/开发规划/系统评估_20260729.md`

### 完成的改造

| Phase | 改动 | 效果 |
|-------|------|------|
| 1. 统一 Tool Calling | JSON 嵌入文本 → 原生 function calling | 消灭 `{"skill"/"act":...}` 正则提取 |
| 2. 消灭关键词检测 | 删除 7 处 `if "xx" in text` 门控 | 语音/贴图/色色知识/表情历史全改为 LLM 自主 |
| 3. 群聊 Tool Calling | `_build_memory_tools` 支持群聊 | 群聊也能搜索记忆/聊天记录 |
| 4. 合并冗余 | 断开 personality_drift 连接 + .env 密钥读取 | 减少死代码 + 安全隐患 |
| 5. 清理死代码 | 删 framework/ plugins/ 等 12 文件 | ~1600 行消失 |
| 流式输出 | SSE token-by-token | 替代 `asyncio.sleep(1-6)` 伪造延迟 |
| auto_learn 退役 | 60+ 正则规则 → LLM 提取频率翻倍 | 少而准 > 多而杂 |

### 2026-07-29~08-02 架构升级 (Phase A-G)

| Phase | 模块 | 说明 |
|-------|------|------|
| A | self_state.py (627行) | 持久自我状态——关系场/自我叙事/价值观/体验缓冲 |
| B | context_builder.py (316行) | 状态投影器——Token预算+上下文组装 |
| C | reranker.py (153行) | Cross-Encoder 精排——BGE粗排后记忆二次筛选 |
| D | reflection.py (443行) | 反思整合——个人反思+社交感知，30分钟后台循环 |
| E | 自我记忆升级 | 双层策略（正则+LLM）提取糖糖自己的承诺/推荐/观点 |
| F | handler.py 自治循环 | 驱动力驱动的主动发起，2026-08-02 加入内部消化模式 |
| G | drives.py (372行) | 驱动力引擎——6种驱动力积累/释放/竞争的统一动力学 |

### 2026-08-02 审查与修复

- 7 个致命问题全部修复（CRIT-1~7）
- 自忆死路径修复：_get_self_memory_context() 端到端接入
- 控制台 vision/voice provider 下拉框修正
- 文档行数全量更新，12 个新模块补入关键文件表

### 2026-08-02 / 08-04 批次计划落地

- 2026-08-02 最终改进路线 14 项（R1-1~R1-6 / R2-1~R2-4 / R3-1~R3-4）全部落地；注：R1-5 目标 handler.py < 7000 行未达成——实际 7263 行，但已拆分出 handler_commands.py / handler_autonomy.py
- 2026-08-04 修复批次 28 项除 L1 僵尸键清理外全部落地；L1 残留（config.yaml `diff_singer.*` / `voice.emotional_tts`）见 Backlog

### 关键经验

- **不要用正则猜 LLM 意图**——全部走原生 Tool Calling
- **不要用关键词替 LLM 决策**——提供工具，让 LLM 自己决定
- **不要伪造延迟**——流式输出的真实生成速度就是最自然的节奏
- **不要两条路并存**——JSON 嵌入 + Tool Calling 选一个，选 Tool Calling

---

## ✅ 已有闭环基础：感知系统

> 详见 `docs/开发规划/感知系统顶层设计.md`

`perception.py` 已采集带目标和来源的 feedback；只有方向已验证的交互才能作为带来源反思材料交给 LLM。系统不根据单条反馈接管糖糖的行为决策。P6-C 负责用真实样本验证这个闭环是否真正改善对话，不再新建第二套感知系统。

---

### 现代化前后对比

```
改前架构:
  LLM ← JSON嵌入 ← 正则提取 ← 系统预判
       ← 关键词门控 ← "吗/什么/怎么" in text
       ← 动作前缀 ← {"act":"sing"} regex
       ← asyncio.sleep(1-6) ← 伪造打字

改后架构:
  LLM → Native Tool Calling → 统一调度
       → 流式输出 (SSE) → 自然延迟
       → 感知引擎 (feedback loop) → 闭环自改进  [基础已建——perception.py 采集信号，闭环待接入反思循环]
```

---

> 新功能不得「想起来就加」；必须先有运行证据、用户价值、验收指标和最小实现，P6 完成后再从候选清单晋级。

## 2026-08-29 人工验收后续与容量结论

- 干净 12h 观察 `post-p1-4b-clean-human-acceptance-v1` 已从单一 boot `ae50031f` 开始；当前发送、LLM、提取生命周期和进程稳定性正常，区间无新增 inbox failed/uncertain。
- 严格 QQ FAIL 仍仅由生产库历史 `inbox failed=1/uncertain=1` 触发；不清零、不伪装为成功。群聊有真实入站，私聊/主动/媒体等未触发域继续 `INSUFFICIENT`。
- 记忆 656→663，70 到达/63 服务，准入 eligible=0；当前是 10 条/7 天/21 天策略延期，不足以证明 worker 故障，也不足以认证“及时消化”。
- 公平容量对照：单 producer/200 jobs/1–2 workers 的 LLM p95 **28.083/20.162ms**、前台等待 **0ms**；4 producer/8 workers 的 p95 **517.911/259.706ms**，1000 jobs **933.459/495.066ms**，完整性均为 0。高并发长尾来自同步 SQLite 写入与共享 LLM 锁，暂不调整生产并发。
- 观察器每 5 分钟裸探测 3001 造成 `websockets.server 400` 噪声，已与应用错误区分；手动重启重叠 boot `b740f2a7` 绑定失败后退出，启动前副作用隔离列为后续红测任务。

当前顺序调整为：**保持 12h 真实观察 → 处置历史 inbox 终态 → 启动绑定失败硬化 → Phase 2b 真实回执 canary → 再做 SQLite/锁分层优化**。任何容量优化必须有前后 p95/p99、事件循环阻塞和完整性对照，不能只凭压测单点改参数。

2026-08-29 首个性能/启动边界修复：`NapCatClient.start_server()` 绑定 OSError 时回收
`_running`、`_accept_events` 和 `server` 半初始化状态；`MessageHandler._call_llm_light()`
的第一次失败退避不再持有全局 `_llm_lock`。新增 2 条回归，定向与全量回归 **1554 passed**，
pyflakes undefined=0、compileall=0。该改动尚未重启现网进程，需在干净 12h 观察完成后安全
重启并复验前台/后台锁等待与提取生命周期；SQLite writer lane、锁分层和 Phase 2b canary
仍不因离线压测提前打开。

2026-08-30 观察器无噪声探针续修：`runtime_observer._port_open()` 优先读取本机监听表，
仅在依赖不可用或异常时回退到有界 TCP 探测，避免观察器自身向反向 WebSocket 端口发起
握手并制造 400 日志。相关回归先红后绿；全量 **1555 passed, 3 warnings**，pyflakes
undefined=0、compileall=0，GitNexus 重建为 33,222 nodes / 69,959 edges / 300 flows。
旧观察器已安全结束，新单一 12h `post-p1-4b-clean-human-acceptance-v2-noise-free`
已启动，首样本 `db=ok/backlog=662/napcat=up`。机器人未重启，当前只允许继续观察，
不把人工验收或离线证据外推为全链路认证。

## 2026-08-30 后台提取事件循环容量切片

- 已将回填、积压准入、任务恢复及提取 worker 的同步 Store/Memory I/O 顺序移出事件循环，
  使用统一 `_run_extraction_io()`，不并发写 SQLite、不修改准入阈值/租约/单 worker 语义。
- 慢调用（≥250ms）记录结构化告警，后续可从真实流量取得 p95/p99；离线红测证明慢探针期间
  事件循环仍可调度，ready→done 顺序和既有提取回归保持通过。
- 定向 48 项、全量 1559 项通过；现网未重启，待 12h 人工验收结束后安全加载并对照观察。

- 第二切片已覆盖 `_process_extraction_batch` 的 14 个同步 Store 边界；不改变状态机顺序，
  全量回归更新为 1560 项通过。临时提高 GitNexus 解析上限后影响分析为 LOW（群聊/私聊主流程），
  待安全重启后以真实 p95/p99 验证收益。

- 静态扫描发现 `_execute_tool`、`TaskManager._check_and_send` 仍有异步路径内的同步 Store/Memory
  调用，GitNexus 风险均为 HIGH；因缺少真实工具/定时任务阻塞样本，列为后续专项，先补动作级
  红测和发送事务回滚证据再改。

- `_checked_send` 的成功后回复质量遥测写入已移出事件循环；不改变发送确认/自检提交边界，
  全量回归 1561 项通过。现网待安全重启后测量真实发送尾延迟。

## 2026-08-30 启动失败与取消收口修复

- 明确 21:07 的 `b740f2a7` 为人工重启重叠，不是自动重复启动；绑定 3001 失败的实例不应
  通过全局端口扫描清理别的实例资源。
- `TangTang._atexit_cleanup()` 现在只回收本实例持有的 GPT-SoVITS 子进程，避免失败实例
  误杀 9880；新增外部进程不误杀和自有进程回收回归。
- 提取 SQLite I/O 被取消时先等待底层线程完成，避免 Windows 临时库/生产连接泄漏；新增
  取消收口回归。全量 1564 项通过；现网未重启，待 12h 观察完成后安全加载。

- 关闭生命周期已补齐：`MessageHandler.stop_services()` 先等待 `TaskManager` 内部提醒循环
  退出，再保存状态并关闭服务；避免优雅重启期间继续查库/发送。新增顺序与循环取消回归，
  全量 1566 项通过。

### 下一专项候选：前台/工具/定时任务同步 I/O（先测量后改）

- 当前源码仍有 async 消息入口和 `_execute_tool` 直接调用 `MemorySystem/Store` 的同步方法；
  `TaskManager._check_and_send` 已在第一批专项中完成顺序线程化。前台和工具调用仍是结构性
  阻塞候选，不是已证实生产故障。
- 安全重启后先按操作采集 p95/p99、事件循环阻塞和取消收口，确认主要矛盾后再分批线程化；
  保持同一回合写入顺序、事务边界和 outbox/记忆幂等语义，并为 HIGH 影响函数设置回滚闸门。

- 定时任务专项已完成第一批：`TaskManager` 的恢复、到期扫描、claim、冻结、状态结算和异常
  释放统一经 `_run_store_io()` 顺序线程化；慢调用与取消收口均有回归，离线红测证明 80ms
  慢扫描不再阻塞事件循环。全量 1568 项通过；待安全重启后做真实提醒/媒体 canary。

- 启动异常分支已补齐：`main()` 在端口绑定等早期失败时先调用统一 `TangTang.stop()`，
  再重抛原始异常；新增失败启动清理回归，全量 1569 项通过。现网未重启，待 12h 观察后加载。

- `_execute_tool` 的 `history_query` 只读 SQLite 边界已接入通用 Store helper；慢查询红测证明
  事件循环不再被单次查询阻塞，原授权/来源契约保持不变。全量 1570 项通过；其余工具读写
  仍需真实按操作测量后再分批改。

- `get_messages_by_date` 日期证据读取已接入同一 helper；慢查询红测和全量 1571 项回归通过，
  日期/主体/群域及 bot 回复归属契约保持不变。其余工具读写仍等待真实测量。

- `search_keywords` 关键词历史读取已接入同一 helper；慢查询红测和全量 1572 项回归通过，
  关键词、主体/群域授权及来源格式保持不变。其余工具读写仍等待真实按操作测量与取消收口。

- `get_group_activity` 群活跃读取已接入同一 helper；慢查询红测和全量 1573 项回归通过，
  群域、小时上限、空结果提示及 TOP5 输出契约保持不变。其余工具读写仍等待真实测量。

- `get_last_conversation` 最近对话读取已接入同一 helper；慢查询红测和全量 1574 项回归通过，
  主体、群域授权及对话输出契约保持不变。其余工具读写仍等待真实按操作测量。

- `count_messages` 统计读取及人物读取已接入同一 helper；慢查询红测和全量 1575 项回归通过，
  关键词、群域、占比及时间字段契约保持不变。其余工具读写仍等待真实按操作测量。

- 工具执行器的记忆只读整批已接入 helper：`search_chat_history`（群原文/私聊索引）、
  `search_facts`、`search_relations`、`search_memories`、`search_episodes` 均有慢读红测，
  全量 1584 项回归通过。授权、证据来源、排序和输出契约保持不变；嵌入编码与前台消息入口
  仍需独立 p95/p99 测量，不能假定已解决。

- 前台消息入口首批持久化 I/O 已完成：群成员写入、人物档案读写、真实入站 `chat_log` 以及
  发送确认后的群/私聊回复与指令日志均经可取消线程边界执行；新增前台回归后全量 1585 项通过。
  群/私聊入口影响 LOW，但共享 helper 扇出扩大后 GitNexus 评估为 HIGH（17 个符号、3 条流程），
  后续必须先做安全重启 p95/p99 与取消/失败 canary，不继续盲加调用点。

- GPT-SoVITS 启动就绪边界已修复：旧逻辑仅凭端口返回 404 就记录“已就绪”，红测已复现；现要求
  端口可达且正式 `/tts` 健康检查通过才宣布 ready，失败则等待并超时清理。新增服务管理器回归，
  全量 **1586 passed**、pyflakes undefined=0、compileall=0。GitNexus 影响 LOW；现网仍未重启，
  待当前 12h 观察完成后与其他改动统一安全加载、验证真实语音 canary。

- 群聊关系图的 N+1 Store 读取已移出事件循环：慢探针（8×80ms）从直接调用约 743ms/1 tick
  改为线程边界约 748ms/49 ticks；只复制短期消息后在线程中构建，不改变隐私与输出契约。新增
  接线回归，全量 **1587 passed**、pyflakes undefined=0、compileall=0；现网未重启，待 12h
  观察结束后统一加载，并以真实前台 p95/p99 验证。

- 成功回合的记忆强化写入已线程化：临时 SQLite 慢探针显示 6 条记忆 p50≈128ms、p95≈166ms、
  最大约 687ms（8 条曾达约 1.3s）；群聊/私聊现经取消安全 `_run_store_io` 执行，保留确认后
  强化、冷却和合成记忆豁免。新增接线回归，全量 **1588 passed**、pyflakes undefined=0、
  compileall=0；现网未重启，待观察结束后测真实前台尾延迟。

- 12h 观察中的记忆积压目前为约 184 人/723 条且 `eligible=0`；按现行策略，3–9 条需满 7 天、
  1–2 条需满 21 天，本次最老 2 条约 20 天，属于策略等待而非任务卡死。到达率约 42.6/h、服务
  约 27.9/h，先观察跨阈值后的自动准入与完成，不放宽证据准入或用清零伪造健康。

- 私聊交叉上下文剩余同步档案读取已完成首批收口：`find_last_group`、昵称解析和最多 3 个
  第三人人物档案读取经取消安全 `_run_store_io()` 执行；短期私聊 deque 先在事件循环取快照，
  避免后台线程遍历可变内存。新增线程归属/结构回归，全量 **1593 项通过**。待 12h 观察
  完成后的安全重启中测量私聊 p95/p99，再决定是否做 Store 批量查询。

- soak 验收夹具已修复：采样/排空快照与线程化提取 worker 共用模拟数据库闸门，消除 Windows
  busy timeout 对死信冷却断言的偶发污染；不改变生产状态机，也未放宽 `dead=1`、`invalid=5`
  的回归标准。

- 自治每日指标聚合已移出事件循环：M11–M13 扫描、KV 写入、低质记忆清理经取消安全
  `_run_store_io()` 顺序执行；短期缓冲清理保持在事件循环线程。慢 Store + 心跳回归通过，
  全量 **1594 项通过**。待安全重启后测自治周期 p95/p99；启动阶段仍有一次性清理路径，
  将在同一 canary 中单独核对。

- 自治私聊目标筛选首批收口：候选门控、沉默时间、近期活跃用户、目标人物、记忆召回和最近
  私聊读取均经 `_run_store_io()`；可变关系/告警候选先在事件循环取快照，避免后台线程遍历
  共享字典。新增慢 Store + 心跳回归，全量 **1595 项通过**。仍需后续批量查询减少 N+1，
  以及安全重启后的真实自治 p95/p99 与取消验证。

- 主动事件持久化边界已收口：事件来源、`lookup→claim→executing`、DecisionRun 终态/绑定和
  `finish` 均经取消安全 `_run_store_io()` 顺序线程化；Handler 内存态/指标与 SQLite 写入拆开，
  旧同步 sink 保持兼容。新增慢 Store + 心跳/顺序回归，全量 **1597 项通过**，pyflakes
  `undefined=0`、compileall=0。boot `f2f6ec7f` 已单实例加载，WS/QQ/数据库/GPT-SoVITS 健康；
  新 boot 的 2h smoke 正在运行，真实自治事件与 p95/p99 样本仍待取得，不能据启动成功
  宣称 P1-4 完成。

2026-08-30 历史 inbox 只读审计切片：`ReadOnlyStore.list_inbound_events()` 与
`tools/inspect_inbound_events.py` 仅提供失败/不确定入站事实的元数据核验，正文仍需走
作用域历史查询，且没有重试/重放能力。生产两条历史终态均缺失精确 `chat_log` 事实，
因此严格 FAIL 保留为“证据缺失、禁止重放”，不以清零或补写伪造健康。新增回归后全量
**1557 passed, 3 warnings**，pyflakes undefined=0、compileall=0；下一步仍是等待
12h 无噪声运行证据，再评估是否做历史事件归档标记（只追加审计，不改变事实）。

2026-08-30 工具执行器同步 I/O 首批收口：记忆纠错、提醒创建、记忆授权主体解析、
`send_message` 目标/草稿状态及意见状态操作已接入兼容 Store helper 和取消安全
`_run_store_io()`；慢 Store + 心跳回归通过，全量 **1600 项通过**，pyflakes
undefined=0、compileall=0。`_execute_tool` 超大方法的 GitNexus 影响暂为 UNKNOWN，
仍需真实前台 p95/p99/取消 canary；意见内部循环与嵌入/媒体路径待单独测量。

2026-08-30 意见征集异步路径测量：`OpinionManager.start_campaign` 在 80ms/次慢 Store
探针下耗时 443.1ms 且事件循环 tick=0，`handle_user_message` 耗时 266.9ms 且 tick=0；
确认存在同步 SQLite 阻塞。GitNexus 三个入口影响 LOW，现仅记录为下一批候选，待人工验收
样本完成后按“活动创建→参与收集→自动收尾”顺序线程化并补取消/状态顺序回归。

## 2026-08-30 意见征集生命周期修复（代码已验证，待安全重启加载）

- queued 邀请恢复从 `OpinionManager.__init__` 移到 QQ lifecycle online gate；重连可重复触发，
  但由管理器锁与数据库 claim/lease 合并，构造阶段不会因 NapCat 尚未登录而把暂时的
  `send=False` 写成 `invite_failed`。
- 邀请发送前标记、未确认收口、确认后 pending 三个 Store 事务均要求活动仍为 `open`，
  关闭后迟到的 LLM 结果/发送回执不会复活或补写邀请消息。
- 新增 3 条生命周期竞态回归；意见聚焦 70 项、全量 **1618 passed, 3 warnings**，
  `pyflakes undefined=0`、`compileall=0`。GitNexus 重建为 33,583 nodes / 70,652 edges /
  300 flows。
- 现网 `boot=f2f6ec7f` 为用户手动启动的单实例，代码修改尚未在其进程内生效；安全重启后
  只做一次 QQ online→恢复→发送确认 canary，再进入贴图语义缓存专项。900 秒租约过期后的
  `invite_uncertain` 迟到确认仍列为待决策风险。

## 2026-08-30 贴图语义索引缓存（代码已验证，待安全重启测量）

- 原先 956 张图库每次逐图 BGE 编码（实测 6–9 秒）已改为持久化语义矩阵：首次由 BGE
  就绪后的后台线程批量构建，后续按元数据文本哈希增量更新；写入临时文件后原子替换。
- 前台查询只做一次 query encode + 矩阵点积；冷缓存/模型未就绪时回退现有标签与关键词路径，
  不再把逐图 CPU 工作塞进事件循环。默认、丛雨、米雪儿三库顺序预热，角色切换吸收新标注
  后在后台增量补齐。
- 新增缓存持久化、重启复用、元数据变更和线程归属回归；定向 **9 passed**。安全重启后需
  实测 warm/cold P95、命中率、事件环 tick、RSS，并与旧的 6.568/7.359s 基线对照。

- 同批修复冷缓存降级情绪识别的子串/否定误判：采用 jieba 完整词、否定窗口和英文边界，
  不改变 LLM 自主情绪决策。`不开心/不要生气/不是色色` 均不再误选，新增回归；全量
  **1623 passed, 3 warnings**。

## 2026-08-30 人工验收更正与语音服务退出观测

- 已确认运行中的机器人只有一个用户手动启动实例；观察器/辅助 Python 进程不计入重复启动。
- GPT-SoVITS 就绪后端口后来消失，但原 stderr drain 静默结束，无法从日志区分崩溃、外部
  终止或环境回收。`ServiceManager` 现保留有界 stderr 尾部并记录就绪后退出码；正常取消
  不报警，不改变手动重启策略。
- 回归与全量测试通过（1624 项）；下一次安全重启后进行一次真实退出诊断验证，之后再决定
  是否需要增加健康巡检或自动恢复，避免在缺乏退出原因证据时扩大重启行为。

## 2026-08-30 Store I/O 有界并发

- 64 路探针曾同时占用约 20 个线程，且生产出现过 502.5ms 的 `persist_inbound_message`；
  `asyncio.to_thread()` 只能避免事件环阻塞，不能限制跨模块线程池排队。
- Handler/自治、意见征集、提醒统一接入 16 槽 Store I/O 门；取消仍等待底层线程收口，
  慢日志记录排队等待时间和上限。新增并发上限回归，全量 1625 项通过。
- 下一次安全重启后测量真实 p95/p99、事件环调度和发送/LLM 延迟，再决定 16 槽是否需要按
  SQLite 写入与只读查询拆分配额；在实测前不扩容、不宣称 1000 人容量达标。GitNexus
  本轮最新索引为 33,622 nodes / 70,702 edges / 300 flows；项目无 Git 仓库，变更影响只能以
  知识图、回归和运行证据三者交叉核对。

## 2026-08-30 识图 OOM 与云端密钥解析修复

- 运行时取证确认 MiniCPM-V 使用默认上下文会触发 Ollama `cudaMalloc OOM`；云端兜底因
  Router 把 `${QWEN_KEY}` 当字面量而 401。实测同图加 `num_ctx=4096` 后本地 HTTP 200。
- 本地请求现固定安全上下文/输出预算并记录非 200 错误体；Router 独立加载 `.env`、解析
  环境引用。代码加载后的 VisionRouter 真实探针已返回中文描述且密钥不再是占位符；定向
  118 项通过，下一次安全重启后仍需做 Handler→缓存→LLM 的静态图/动图/显存 canary。

## 2026-08-30 事实簇积压与协议解析

- 证据：事实簇游标曾停在 `29604` 而聊天已到 `244453`，历史累计 179 次格式失败；不
  允许以手工推进游标的方式掩盖记忆缺口。
- 已修复候选路径：事实簇只接受单一完整 JSON，兼容 Markdown 围栏和明确的
  `facts/items/results/data` envelope，拒绝自然语言拼接/截断；失败保留 `ok=False`，证据
  id 门槛不变，并记录无敏感原文的协议诊断字段。由于旧日志没有原始 13 字响应，具体漂移
  形状仍待重启后的 `raw_type/raw_length` 证据确认。新增回归后全量 **1630 passed，3 warnings**。
- 下一步：安全重启后验证游标推进和事实簇实际入库；若仍有失败，按新的 `raw_type/raw_length`
  证据区分传输、空响应与协议漂移，再决定是否增加一次受限的格式修复调用。

## 2026-08-30 事实簇路径 I/O 收口

- 事实簇异步函数内部仍有同步 SQLite 读写，属于此前 Store 门遗漏的后台路径；慢 Store
  探针证实它可能阻塞事件环。
- 已接入统一 16 槽 `run_bounded_store_io()`；事实簇和摘要更新保持顺序、取消收口及原有
  证据门槛。新增线程归属/心跳回归，全量 **1631 passed，3 warnings**。
- 下一步仍需安全重启后观察事实簇游标推进、Store p95/p99 和前台事件环 tick。

## 2026-08-30 人物档案尾延迟收口

- 生产日志出现一次 `get_or_create_person` 10.5 秒尾延迟；代码证实人物读取在持有
  `people` 连接时嵌套调用 `get_aliases()`，写锁竞争会叠加两个 busy timeout。
- 已改为同连接读取 aliases，保留昵称净化和外号排序；新增“禁止嵌套连接”回归。全量
  **1632 passed，3 warnings**，`pyflakes undefined=0`、`compileall=0`。
- 下一步：安全重启后实测人物读取 p95/p99、Store 排队等待及 ASR/LLM 延迟，确认尾延迟
  是否来自数据库锁的其它长事务。

## 2026-08-30 异步后台 Store 边界补齐

- 戳一戳、入群邀请、群信息刷新、陌生人回写、群权力结构、自然指令目标/画像读取、自治
  启动/话题/画像补全等异步路径已统一接入取消安全的 16 槽 Store I/O 门；事实簇异常重试
  的最后一个同步写入旁路也已收口。
- 记忆召回与画像读取移出事件循环，保持原权限、发送顺序和失败降级。针对性 41 项、全量
  **1633 项**通过，pyflakes undefined=0、compileall=0；GitNexus 最新索引为 33,659
  nodes / 70,798 edges / 300 flows。
- 代码仍待安全重启加载；下一阶段以真实 `queue_wait_ms`/`elapsed_ms`、事件环 tick、前台
  延迟为验收证据，随后继续审查 Opinion、媒体和知识检索的异步边界。

## 2026-08-30 自治状态与终态回写收口

- 自治群/私聊的状态快照、冷却 KV、聊天史回写已统一经过有界 Store worker；已确认送达后
  的聊天史失败只告警，不再把终态冒泡成失败或触发重复自治。
- 新增回归并修正意见征集后台测试的事务等待竞态；全量 **1634 项**通过，pyflakes
  undefined=0、compileall=0；GitNexus 最新索引为 33,660 nodes / 70,810 edges / 300 flows。
- 下一步仍是用户手动安全重启后的真实 canary：验证自治 `confirmed` 不重发、Opinion 邀请
  `queued→invite_uncertain→pending`，再测媒体/知识检索和持续流量指标。

## 2026-08-30 群成员资料写放大收口（代码已验证，待重启测量）

- 运行证据：`boot=f2f6ec7f` 的 `upsert_group_member` 曾出现 309.6–727ms；`group_members`
  只有复合主键，且入站路径每次都携带名片/角色/`last_sent`，导致同一资料反复提交写事务。
- 最小修复：Handler 增加每个群友 30 秒资料缓存；Store 的冲突更新加入条件，`last_sent` 只在
  活跃时间前进超过 30 秒时刷新，并用 `MAX` 防止乱序事件回退。无改动时不改变现有资料读取
  语义；缓存上限 4096，重启后首条事件仍会回填。
- 基准取证：临时 SQLite、16 路并发下，未经缓存的重复 upsert p95 约 279ms；前台统一单槽
  写门的 p95 在 160 条入站写压测中约 3.2s，故不启用全局写串行化，保留 16 槽读/写混合门。
  新增群成员时间刷新/不倒退回归；全量 **1636 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`；GitNexus 重建为 **33,670 nodes / 70,822 edges / 300 flows**。
- 下一步：用户手动安全重启后，以同一群友连续消息核对 `upsert_group_member` 告警量、`elapsed_ms`
  和前台事件延迟；若仍有尾延迟，再针对 `insert_chat` 做批量/背压实验，不降低 SQLite 持久化级别。

## 2026-08-30 事实簇失败重试脱离主提取触发（代码已验证，待重启测量）

- 运行证据：`tangtang.log` 在 08:09:34 记录用户 `10003` 的事实簇提取失败；
  `memory.db` 中对应游标停在 `29604`，之后已有 **3037** 条聊天记录，重试时间 08:14
  已过，但没有第二次事实簇尝试。
- 根因：失败分支只把 `next_retry_at` 写入 KV，实际调度仍依赖后续一次主记忆提取成功；
  主提取繁忙/低频时，事实簇会长期饿死，退避状态不是可执行的重试任务。
- 最小修复：失败后建立每用户单例定时重试；执行前核对游标和退避 KV，若已由其它任务推进
  或清除退避则丢弃旧定时器，避免重复调用 LLM。补充“失败→定时重试→成功推进游标”回归。
- 定向 **3 passed**；下一步跑全量测试后，用户手动安全重启并核对该用户事实簇游标是否推进、
  `fact_cluster_failures` 是否只反映真实失败，随后再决定是否把该扫描纳入长期 worker。

## 2026-08-30 观察器按配置判断本地服务必需性

- 人工验收取证发现 `config.yaml` 已启用 `voice.provider=gpt-sovits`，但观察器将
  GPT-SoVITS 永久标为 `required=false`，导致 9880 退出只表现为“未触发语音”，不能进入
  核心服务失败判定。
- `runtime_observer.capture_snapshot()` 现读取配置中的语音/唱歌 provider，仅输出脱敏的
  `required` 布尔值；配置不可读时保留保守基线，不把密钥或配置正文写入快照。
- 观察器定向 **112 passed**、全量 **1640 passed**，pyflakes `undefined=0`、compileall=0；
  实际快照已将当前 `gpt_sovits` 标为 `required=true, up=false`。下一步仍需安全重启后做
  GPT-SoVITS 真实启动、退出诊断和语音发送 canary。

- 追加边界修复：损坏 YAML 或缺少 PyYAML 时，观察器按保守基线继续取证，不因辅助配置解析
  失败而整体退出；非法配置回归已覆盖。定向 **113 passed**、全量 **1641 passed**，
  pyflakes `undefined=0`、compileall=0；GitNexus 重建为 **33,681 nodes / 70,843 edges /
  300 flows**。

## 2026-08-30 GPT-SoVITS 静默退出健康告警

- 运行证据显示 GPT-SoVITS 曾在“已就绪”后消失，而进程空闲时没有语音请求触发失败回调；
  仅依赖下一次 TTS 请求不足以支撑长期运行。
- `SystemHealth` 新增只读 `local_tts` 检查：仅在配置启用 GPT-SoVITS 时探测 9880，区分
  端口不可达、受管进程退出和外部服务在线；不调用 TTS 推理，也不自动重启，避免健康检查
  反向制造负载或扩大故障面。
- 新增三条健康路径回归；全量 **1644 passed**，pyflakes `undefined=0`、compileall=0，
  GitNexus 重建为 **33,691 nodes / 70,850 edges / 300 flows**。下一步安全重启后验证
  `local_tts` 的真实告警、stderr 退出诊断及语音发送闭环，再决定是否需要自动恢复策略。

## 2026-08-30 观察器域隔离与历史 inbox 污染修复

- 现场一次快照显示 NapCat/反向 WS 在线、但 GPT-SoVITS 缺失时，旧聚合把“QQ 接入”误报
  为 FAIL；同时历史累计 `inbox failed=1/uncertain=1` 会污染当前区间判定。
- 观察器现将网关依赖（NapCat、反向 WS）与配置的本地媒体依赖分域；GPT-SoVITS 不可用时
  单独报告“本地依赖 FAIL”，不再冒充 QQ 网关故障。入站 failed/uncertain/unknown 以快照
  增量判定，累计值保留在证据中供历史审计。
- 新增两条回归；定向 **115 passed**、全量 **1646 passed，3 warnings**，
  `pyflakes undefined=0`、`compileall=0`；GitNexus 重建为 **33,709 nodes / 70,874 edges /
  300 flows**。一次真实快照已验证网关转为 INSUFFICIENT、本地依赖准确 FAIL。
- 运行实例仍待用户下一次手动安全重启后做 GPT-SoVITS/语音闭环 canary；本修复本身不触碰
  bot 进程，也不把人工重启计为重复启动。

## 2026-08-30 摘要记忆证据继承收口（已在 boot=4184e55e 验证）

- 只读生产库发现 **6** 条 active `origin=summarized, trust_level=verified` 没有任何
  `memory_evidence`；它们原本会进入 `trusted_only` 召回，属于历史摘要信任升级旁路。
- 根因：`replace_profile_snapshot()` 只检查来源记忆的 trust，不检查每条来源是否有原始
  证据；旧摘要可因此继承 verified。现改为：自动提取/旧摘要必须逐条有
  `memory_evidence`，只有人工/纠正事实可凭显式确认继承；启动迁移将现有无锚摘要降为
  `legacy_unverified`，保留原行和审计，不物理删除。
- 新增合成与启动迁移回归；全量 **1648 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`；GitNexus 重建为 **33,712 nodes / 70,878 edges / 300 flows**。
- 本次手动重启已执行迁移：只读核对显示 `origin=summarized` 的 `verified` 无锚记录为 **0**，
  `legacy_unverified` 无锚记录 **2356**（保留原行和审计，不物理删除）；`trusted_only` 不再
  召回这些无锚摘要。该迁移不把人工重启计为异常，后续仍需持续观察画像合成的 evidence 继承。

## 2026-08-30 GPT-SoVITS 就绪后退出自动恢复（代码已验证，待重启测量）

- 真实运行证据：启动分区 `boot=f2f6ec7f` 在 06:12 记录 GPT-SoVITS 已就绪；12:52
  9880 已不可达，期间没有语音请求触发失败回调，说明“只在 TTS 失败时重启”无法覆盖
  空闲时的子服务退出。
- `ServiceManager` 现为自己创建的已就绪进程挂退出监护：正常关机和显式手动重启先取消
  监护，不会被误拉起；意外退出记录退出码并尝试一次恢复，恢复失败交给健康检查/下一次
  语音失败回调，避免无限重启风暴。stderr 持久 drain 与原有诊断保留。
- 新增“退出后恢复”和“显式停止不恢复”回归；定向 **5 passed**。下一步用户手动安全
  重启后观察 `local_tts`、退出码、恢复次数及一次语音发送闭环；若模型本身持续崩溃，
  以 stderr 尾部证据决定引擎/模型修复，不扩大自动重试次数。

## 2026-08-30 私聊历史语义检索证据回投（代码已验证，待重启测量）

- 记忆访问审查发现：私聊 `search_chat_history` 的向量分支只返回相似度和清洗文本，
  没有 `chat_log` 时间/来源；同一工具的群聊原始分支已有这些字段，造成“昨天/哪天”
  核验的证据契约不一致。
- `Store.search_chat_index()` 现以 `chat_log` 回投精确时间、群域、说话人和
  `source=chat_log#id`，并拒绝误入索引的群消息；Handler 输出统一带时间、说话人和来源。
  不改变向量阈值、排序或现有授权策略。
- 新增 Store + Handler 三条回归；定向 **43 passed，1 warning**，全量 **1653 passed，
  3 warnings**，`pyflakes undefined=0`、`compileall=0`。GitNexus 低并发重建为
  **33,732 nodes / 70,914 edges / 300 flows**；该方法在图查询中未被稳定解析，按
  UNKNOWN 记录，不能伪造调用方风险结论。下一步在用户手动安全重启的真实私聊核验
  “昨天/谁说过”输出是否始终带来源时间。

## 2026-08-30 关系检索否定语义与证据字段收口（代码已验证，待重启测量）

- 运行级最小复现显示，可信记忆“我不是朋友，也不想一起出去”仍会因关系词子串
  被 `search_relation_triples` 返回；同时关系结果是裸文本，没有时间、置信度和来源。
- 关系词现按 jieba 完整 token 命中，并跳过紧邻否定；每条返回保留兼容的字符串类型，
  但带 `[key | time | confidence | source]` 证据前缀。群聊关键词历史同样回投
  `chat_log#id`，统一原始记录证据契约。
- 新增否定关系与群聊来源 ID 回归；定向 **69 passed，1 warning**，全量 **1655 passed，
  3 warnings**，`pyflakes undefined=0`、`compileall=0`。GitNexus 低并发重建为
  **33,737 nodes / 70,920 edges / 300 flows**；`search_chat_keywords` 仍无法稳定解析
  调用图，按 UNKNOWN 记录。下一步仍需手动安全重启后真实核对关系/历史工具输出。

## 2026-08-30 运行实例时点辨析与识图 401 复核

- 当前运行实例 `boot=f2f6ec7f` 于 06:12 启动；识图 Router 的环境变量解析修复在 08:33
  才写入工作树，因此旧实例 08:19–10:03 的云端识图 HTTP 401 不能作为当前代码证据。
- 只读复核同一 `.env` 的 QWEN_KEY：DashScope `/compatible-mode/v1/models` 返回 **200**；
  使用配置中的 `qwen-vl-plus` 与项目图片调用 `/chat/completions` 返回 **200**，密钥和模型
  契约当前有效。下一次用户手动安全重启后，需以新 `boot` 的识图样本确认 401 是否消失，
  不把人工重启计为重复启动。

## 2026-08-30 手动重启端口预检

- 运行证据：13:53 手动启动产生 `boot=151d94b7`，旧实例 `boot=f2f6ec7f` 仍占用
  3001；新实例已加载模型/后台循环后才在 `start_server` 抛出 WinError 10048 并退出，
  产生了不必要的初始化副作用。该事件是手动重启时未先收口旧实例，不是自动重复启动。
- 最小修复：`main.py` 在构造 `MessageHandler` 前对本机反向 WS 端口做 TCP 预检；已占用时
  fail-closed 且不杀现有进程，避免第二实例加载模型或启动后台任务。新增回归；全量
  **1657 passed，3 warnings**，`pyflakes undefined=0`、`compileall=0`。
- 下一次手动重启应先停止旧实例；若误开第二个实例，新代码会在 Handler 初始化前给出明确
  端口占用错误。真实 GPT‑SoVITS 监护/识图/记忆迁移 canary 仍待新实例成功启动后验收。

## 2026-08-30 GPT‑SoVITS GPU 原生崩溃兜底

- 新 boot `4184e55e` 已完成 3001/QQ 接入，但 GPT‑SoVITS 在加载阶段于 14:18:29 以
  `3221225477 (0xC0000005)` 退出；Windows Application Error 明确指向
  `torch\lib\c10.dll`，随后 9880 无监听。该证据不是普通 `/tts` 失败，也不是端口冲突。
- 上游 GPT‑SoVITS README 的测试矩阵为 PyTorch 2.5.1/2.7.0 或 2.8.0dev、CUDA 12.4/12.8；
  当前公共 Python 环境实际为 `torch 2.14.0.dev20260727+cu130`、`torchaudio
  2.11.0.dev20260727+cu130`、`transformers 4.57.6`，属于未被上游矩阵覆盖的组合。
- `ServiceManager` 现只在确认 Windows 原生 GPU 崩溃（访问冲突、c10/CUDA 典型信号）时，
  本次运行最多切换一次 `CUDA_VISIBLE_DEVICES="-1"` 的 CPU GPT‑SoVITS 子进程；空字符串在本机
  仍会让 PyTorch 报告 CUDA 可用，不能作为可靠的 CPU 隔离。普通模型/路径配置错误不触发兜底，
  避免掩盖根因和无限重启。CPU 模式会明确写入日志，下次人工重启重新尝试 GPU。
- 新增 2 条回归；全量 **1659 passed，3 warnings**，`pyflakes undefined=0`、`compileall=0`；
  GitNexus 重建 **33,770 nodes / 70,978 edges / 300 flows**。当前运行实例仍加载旧代码，
  需用户下一次手动安全重启后验证 CPU 兜底、`local_tts` 和真实语音发送闭环。

## 2026-08-30 GPT‑SoVITS CUDA OOM 兜底补全

- 新 boot `589e0558` 真实启动已验证 3001/QQ 正常，但 GPT‑SoVITS 在 GPU 加载阶段以
  `code=1` 退出；stderr 明确为 `torch.OutOfMemoryError: CUDA out of memory`。原有
  兜底仅识别访问冲突/c10/CUDA error，因此没有恢复，`local_tts` 随后报告 9880 不可达。
- `_is_native_gpu_crash()` 现额外识别明确的 CUDA OOM 文本（仍不把缺文件、普通退出等配置
  错误当成可兜底故障），沿用本次运行最多一次、下次人工重启再试 GPU 的策略。
- 定向服务回归 **7 passed**、全量 **1661 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`。最新代码尚未加载到当前实例；待下一次手动重启验证 CPU 子进程、`/tts`
  健康检查和实际语音发送闭环。

## 2026-08-30 图片分享广告过滤边界

- 取证发现花瓣描述先 `lower()`、过滤词却保留大写 `APP`，导致 `APP promotion` 未被拦截；
  同时第三方返回 `raw_text=null/非字符串` 会让筛选路径抛异常。
- 新增统一 `_contains_skip_text()`，对中英文做 `casefold()` 并容忍缺失/错误类型；定时分享
  与 `share_image` 工具共用同一过滤边界，不改变配额、发送确认或顺序游标协议。
- 定向 **7 passed**、全量 **1661 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`，GitNexus 重建后该入口影响评估为 LOW。真实花瓣 API/QQ 发送仍待后续
  媒体 canary，不能仅凭离线过滤测试宣称端到端通过。

## 2026-08-30 事实簇历史证据隔离

- 只读生产库初次逐条取证发现 **5** 条 active `cluster_facts` 的 evidence_ids 指向了
  机器人回复、其他用户消息或错误用户；按完整迁移规则复核为 **14** 条，另有 **6121**
  条历史原子事实没有证据锚点。事实簇搜索虽已
  不直接进入对话，但旧摘要仍会被增量提取器读取，存在再次把合成内容喂给 LLM 的旁路。
- 新增一次性、可审计迁移 `20260830_cluster_fact_evidence_v1`：无效非空锚点的原子事实
  标记 `retracted`（不删除原行），没有可追溯 active 原子事实的簇清空摘要并重算计数。
  增量提取器只把非空摘要簇用于提示和主题匹配；无锚历史行保留供人工复核，不再参与
  自动再合成。迁移在启动事务内幂等执行，当前运行实例不会被在线改写。
- 新增迁移回归；本次全量 **1663 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`。GitNexus 重建为 **33,797 nodes / 71,013 edges / 300 flows**；
  `add_cluster_fact/_init_db` 未被图索引稳定解析，按 UNKNOWN 记录，不能伪造调用方风险。
- 当前 `boot=8d4cac17` 是迁移代码落盘前启动的实例；日志已证明 GPT-SoVITS 在 15:35:58
  意外退出后于 15:36:35 GPU 恢复并通过 `/tts`，因此不要在线执行迁移。生产库副本 canary
  已预演：将隔离 **14** 条无效锚点、清空 **1911** 个不可追溯摘要，保留 **6401** 条 active
  原子事实和 **126** 个有摘要簇；下次用户手动重启后核对 marker 与这些数量，再做 GPT/QQ
  语音 canary。

## 2026-08-30 GPT‑SoVITS 监护重复恢复风暴

- 运行证据：`boot=8d4cac17` 在 15:35:58、15:42:30、15:48:46、15:53:35、15:56:35
  连续记录“就绪后进程退出”
  并再次启动；每轮都声称“尝试一次自动恢复”，与“不做无限重启风暴”的既有契约冲突，
  且退出时 stderr 为空，无法靠同一进程重试获得新诊断。
- 根因：恢复计数只存在于单个 supervisor task；每次恢复成功都会重新挂一个全新的 task，
  没有 bot 运行级闸门，所以后续退出又被当成“第一次”。
- 最小修复：`ServiceManager` 增加运行级 `_gpt_sovits_recovery_attempted`，首次就绪退出或
  GPU→CPU 兜底即置位；后续退出只记录 ERROR 并停止自动拉起，交给健康检查/下一次语音
  失败回调。人工重启 `start_for_full_mode()` 会显式清零，保持恢复窗口可控。
- 定向服务回归 **8 passed**；全量 **1664 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`；GitNexus 重建为 **33,799 nodes / 71,016 edges / 300 flows**。当前运行
  实例未加载此修复，不能在线重启或杀进程；下一次用户手动重启后应确认最多一次自动恢复，
  且 9880 退出后不再每 6 分钟循环拉起。

## 2026-08-30 手动重启后记忆迁移与 GPT canary

- 新启动分区 `boot=8b4c3fcd` 已真实加载代码：16:02:57 日志记录迁移隔离 **14** 条无效
  锚点、清空 **1911** 个不可追溯摘要；只读数据库核对为 `active=6401 / retracted=15`、
  非空摘要 **126** 个、迁移 marker 存在。
- 只读生产观察运行 9 分钟（3 个快照）：QQ 入站群消息 **46** 条、发送事务 **3/3 confirmed**、
  LLM 完成样本 **6** 条（p95 1000ms）、本地 GPT‑SoVITS `required=true/up=true`，健康检查
  **1 次无异常**；观察区间没有 GPT 退出、自动恢复或 ERROR 日志。
- 该 canary 只能证明启动、迁移和短时稳定性，整体报告仍为 INSUFFICIENT（缺私聊、工具、
  语音输出、识图等样本）。下一步继续收集真实语音/工具/记忆提取样本，再做持续流量 soak，
  不把 9 分钟稳定误报为长期可靠。

## 2026-08-30 主动私聊结算移出事件循环

- 静态路径审查确认自治循环原先直接调用同步 `people.last_chat` 查询；生产已有
  `Store SQLite 调用较慢` 的 305–508ms 样本，说明该类调用确实可能阻塞事件循环。
- 新增 `_settle_stale_seeks_async()`：人物读取在线程边界执行，await 后复核 pending 代际；读取失败保留
  pending 等下轮重试，结算清除状态时同步持久化，避免中性结算重启后重复观察。自治循环改为等待该异步入口，
  保留旧同步方法供兼容/离线测试。
- 新增慢读取不阻塞事件循环、跨渠道中性结算回归；相关定向 **34 passed**，全量 **1666 passed，3 warnings**，
  `pyflakes undefined=0`、`compileall=0`；GitNexus 重建为 **33,829 nodes / 71,056 edges / 300 flows**。
  全量与安全重启后的自治心跳/Store 尾延迟仍需继续观察，不把离线通过外推为生产认证。

## 2026-08-30 运行观察补充：GPT 自动恢复已命中一次

- `runtime-20260830-162217-43148-f6f1` 覆盖单一 `boot=8b4c3fcd` 约 15 分钟：群入站 **95** 条，
  记忆提取 **14/14 completed、0 failed/dead/requeue**，LLM **8** 个完成样本（前台 P95 2s、后台 P95 1s），
  观察器完整性 PASS。
- 16:34:53 GPT‑SoVITS 在已就绪约 31 分钟后异常退出；监护器记录退出码 1，仅自动恢复一次，16:35:28
  GPU 实例重新通过正式 `/tts` ready。随后未见第二次自动拉起；说明运行级恢复闸门按预期工作，但仍需更长 soak
  与真实语音发送样本才能判断服务稳定性。
- 该窗口记忆积压 **731→757**（到达 94、服务 68，约 375.6/h vs 271.7/h），但全部属于 3–9 条/1–2 条
  的延迟准入尾部（eligible=0），因此“worker 可用”与“及时消化”仍需分开评估，不能仅看 jobs completed。

## 2026-08-30 健康检查 Store I/O 线程化

- 静态链路审查发现 `agent/health_check.py` 的多个 `async` 检查函数直接调用同步
  SQLite（积压、队列、真值卫生、feedback、画像、embedding 等），会绕过既有 Store
  有界线程入口；生产已有 `Store SQLite 调用较慢` **305–508ms** 样本，因此监控任务本身
  可能暂停入站事件循环。
- 新增统一 `_store_io()`：优先复用 Handler 的 `_run_store_io`（保留排队/尾延迟观测），
  独立测试替身退回 `asyncio.to_thread`；真值卫生的多查询也整体在线程边界执行，返回契约不变。
- 红测先证明 50ms 慢 Store 会令 ticker 得不到调度；修复后健康/读侧定向 **26 passed**，
  全量 **1667 passed，3 warnings**，`pyflakes undefined=0`、`compileall=0`；GitNexus
  重建 **33,843 nodes / 71,090 edges / 300 flows**，相关函数影响均为 LOW。
- 当前 `boot=8b4c3fcd` 尚未加载本批代码；下一次用户手动安全重启后，需从日志核对
  `health.*` Store 操作的排队/耗时，并继续完成 2h/12h 运行观察。GitNexus `detect_changes`
  因项目无 `.git` 无法执行，按项目约定记录为环境限制。

## 2026-08-30 GPT‑SoVITS stdout 诊断缺口

- 人工体检与端口复核发现 16:56:17 `boot=8b4c3fcd` 的 GPT‑SoVITS 再次以 `code=1`
  退出；运行级闸门正确阻止第二次自动拉起，但 9880 随后离线。该次退出仍只有
  `无 stderr`，Windows Application 日志也没有对应 Python 崩溃事件，说明现有证据不足以
  判断模型/服务内部原因。
- 取证确认 `ServiceManager` 原先将子进程 stdout 丢弃到 `DEVNULL`，而 uvicorn/Python
  异常可能写 stdout。现已改为 stdout/stderr 双 `PIPE`、启动与就绪后双流有界 drain，
  保留尾部诊断并继续沿用“一次自动恢复、后续 fail-closed”闸门；新增 stdout-only 退出回归。
  同时将语音失败回调的恢复预算限制为一次，显式停机期间忽略迟到回调，避免 supervisor 之外
  的重复重启路径。
- 服务专门回归 **11 passed**，全量 **1670 passed，3 warnings**，`pyflakes undefined=0`、
  `compileall=0`；GitNexus 重建 **33,849 nodes / 71,108 edges / 300 flows**。
- 当前实例不能在线重启；下一次用户手动启动后，若再次退出，日志应包含 stdout/stderr
  尾部，才能继续做根因修复。当前 GPT‑SoVITS 仍为不可用，不能以端口之外的 bot 健康
  代替语音域通过。

## 2026-08-30 GPT‑SoVITS 双流 drain 首次 canary 暴露管道竞态

- 用户手动重启后的 `boot=f6017972` 于 17:08:53 首次通过 GPU `/tts`；17:12:22
  异常退出时已成功记录 stdout 尾部（uvicorn、进度条和 `/tts 200`），证明 stdout
  取证链路生效。17:12:26 自动恢复并于 17:12:57 再次 ready，但 17:13:13 第二次退出后
  按运行级闸门停止继续拉起，行为符合一次性恢复契约。
- 同一窗口出现 `readuntil() called while another coroutine is already waiting for incoming data`。
  根因不是 GPT 模型本身，而是 Windows `StreamReader` 在 `wait_for(readline())` 超时取消后，
  下一轮对同一管道再次读取，触发底层并发读。该错误会污染退出取证并可能留下未回收读任务。
- 最小修复：启动/就绪后双流均改为每管道一个长期 `readline()` 协程，退出时统一取消，
  不再周期性超时取消；新增“禁止 wait_for 包裹管道读取”回归。服务专测 **12 passed**，
  全量 **1671 passed，3 warnings**，`pyflakes undefined=0`、`compileall=0`。
- `boot=f6017972` 在修复落盘前已启动，已按人工重启要求结束（PID 18192，3001 已释放）；
  下一次用户手动启动将加载本修复。启动后需确认日志不再出现 `readuntil()` 竞态，并继续做真实
  语音发送与 2h/12h soak。GPT 的具体退出原因仍待新代码捕获到完整 stdout/stderr 后再下结论。

## 2026-08-30 知识检索移出事件循环

- 静态链路审查确认 `MessageHandler._skill_search_knowledge()` 是 async 工具，却直接调用
  同步 `KnowledgeBase.search()`；BGE 编码和 cross-encoder 重排均可能持续占用前台事件循环。
  慢检索红测注入 50ms 阻塞时 ticker 为 **0**，证明不是理论风险。
- 新增独立 `run_bounded_blocking()`（每事件循环 4 槽、取消等待线程收口、记录
  `elapsed_ms/queue_wait_ms`），知识工具改为经该门执行；与 16 槽 Store I/O 门隔离，避免
  计算型检索挤占数据库配额。无知识库时仍保持诚实空结果。
- 知识专项 **15 passed**（含红/绿事件环回归）；本批代码变更后的全量与静态门禁需继续执行。
  真实 p95/p99、缓存命中和重排触发率尚无运行样本，不能把离线 ticker 通过外推为容量认证。

## 2026-08-30 BGE 召回确定性与泛词误召回

- 真实 `knowledge/` 基准发现“完全不存在的随机问题”仍返回 **1344 字**知识；同一查询
  两次最高相似度还曾从 **0.568** 变为 **0.672**。源码取证确认 `EmbeddingEngine.load()`
  没有调用 `model.eval()`，dropout 让向量与语义门槛不稳定。
- 修复：模型加载后立即 `eval()`；知识检索过滤已验证的泛词/闲聊词，仅在存在实质关键词时
  执行语义轮，语义证据门槛调整为 **>0.58**（相关改写实测 0.608–0.674，误召回样本
  0.564–0.568）。新增 2 条泛词/闲聊误召回回归和模型 eval 回归。
- 真实复测：同一查询向量 `array_equal=True`；3 条无关问句均空结果，5 条领域改写均命中。
  该结果仍不替代重排开启时的真实 canary，Reranker OOM/重排 p95 需在人工重启后单独观察。

## 2026-08-30 WeKnora 知识库架构借鉴

- 已完成对 WeKnora v0.7.2 的架构调研：它的自适应分块、持久化索引、稳定
  `document_id/chunk_id`、FTS/BM25 + Dense + RRF、多级证据元数据、版本化重建和检索评估，
  与当前知识库的下一阶段缺口直接对应。该结论来自项目调研材料，当前不引入完整 WeKnora，
  也不替换已有 QQ/群/私聊记忆隔离与证据链。
- 下一阶段按可回滚顺序实施：先建立固定问句集和空召回/误召回基线；再增加 SQLite
  `knowledge_index`（文档 hash、稳定 chunk ID、偏移、embedding 模型/维度指纹）；随后引入
  自适应分块与 overlap、FTS5/bigram + Dense + RRF，最后统一把来源、时间、hash、偏移、
  各路分数和检索路径交给 LLM。每一步都保留旧索引回退开关和增量重建/删除检测。
- parent-child 上下文、轻量实体关系和独立 sidecar 暂不排期，等持久化索引、检索评估和真实
  canary 稳定后再评估；不以离线通过或文档数量少推断千人规模容量。

## 2026-08-30 知识检索固定基线

- 新增只读评估入口 `python tools/knowledge_retrieval_eval.py` 和固定问句集
  `tools/knowledge_retrieval_baseline.json`。每条样本记录期望是否命中、允许来源、实际来源、
  字符数和通过状态，脚本同时输出正例命中率、负例空召回率、文件数和块数。
- 当前基线为 **10/10 passed**：正例 **6/6** 命中、负例 **4/4** 空召回；实测知识库
  **12 文件 / 156 块**。默认不加载 BGE，`--semantic` 才验证语义轮，避免把模型冷启动和
  词法基线混为一谈。基线是下一步持久化索引、分块和检索算法变更的回归门，不代表线上
  p95、重排器稳定性或千人容量已认证。
- 新增评估器回归后全量 **1679 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、
  `compileall=0`。GitNexus 已刷新为 **33,899 nodes / 71,184 edges / 300 flows**；
  `detect_changes()` 仍因项目无 `.git` 无法执行，按环境限制记录。
- 随后用真实 BGE 执行 `--semantic` 模式仍为 **10/10 passed**；这只证明当前固定问句集
  的召回边界，尚未覆盖 Reranker、并发流量、索引冷启动和文档增删同步。

## 2026-08-30 知识索引元数据层（阶段 1）

- 按规格新增 `agent/knowledge_index.py`：SQLite `index_meta/documents/chunks/embeddings`
  四张表，schema version=1，启用 WAL、外键级联和 5 秒 busy timeout。文档 ID 由规范化
  相对路径派生；chunk ID 由文档 ID、序号和内容 hash 派生；每块持久化 label、正文 hash、
  原文字符起止偏移和 embedding 模型/维度指纹接口。
- `KnowledgeBase` 加载/重载后以单事务同步当前公共文档集合，成功提交后才把稳定元数据投影
  到内存 chunk；读文件失败或 SQLite 同步失败均保留旧的内存检索，不清理上一次索引。删除
  文档会级联清理 chunk/embedding；重复同步和重启复用 ID 均有回归。
- 证据：索引专项 **7 passed**，知识相关专项 **39 passed**；全量门禁随后为 **1688 passed，
  3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`。生产目录
  已生成 `knowledge/.knowledge_index.sqlite3` 的 **12 文档 / 156 块**快照，但当前机器人
  尚未重启，不能把该快照视为线上 canary；SQLite `integrity_check=ok`。embedding BLOB
  复用已在阶段 1.5 完成，FTS5 和线上 canary 仍待后续阶段。

## 2026-08-30 知识向量指纹复用（阶段 1.5）

- `EmbeddingEngine` 增加稳定 `fingerprint` 和输出 `dimension`；`KnowledgeBase` 预热时先按
  chunk ID + 指纹批量读取 SQLite BLOB，维度不符或读取失败则逐块重新编码。模型指纹变化会
  清除同一进程中旧模型的内存向量，避免热切换串用旧向量；无指纹的旧/伪引擎保持原内存行为。
- 真实 BGE 探针：首次为 156 块编码并写入 **156** 条 embedding，随后重建 KnowledgeBase
  再预热耗时约 **1.9ms**，156/156 向量直接复用；索引快照为 12 文档、156 块、156 向量。
  这只证明本机冷/热复用链路，尚未证明多进程并发写入或模型权重升级迁移。
- 补充模型指纹隔离回归后全量 **1691 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`；GitNexus 刷新为 **34,026 nodes / 71,396 edges / 300 flows**。
  `detect_changes()` 仍因项目无 `.git` 无法执行。

## 2026-08-30 FTS5 CJK bigram 索引探针

- 在 `chunks` 主表旁增加可选 `chunks_fts` FTS5 表：中文文本先编码为 ASCII bigram token，
  英文保留小写词；查询词由内部 token 生成，不把 LLM/用户原文直接拼入 MATCH，避免 FTS
  运算符注入。多 bigram 使用 AND，实测避免「旧内容」因共享「内容」误召回「新内容」。
- 文档增删改与 FTS 派生行在同一 `sync()` 事务内更新；FTS5 模块缺失时只关闭词法探针，
  文档/chunk/embedding 主索引仍可用。当前 `search_lexical()` 仅供评估和下一阶段融合，
  尚未替换线上 KnowledgeBase 搜索。
- 专项 **10 passed**；生产索引已可召回 `游戏/记忆系统/知识库/丛雨` 等中文词，当前不把
  FTS 离线命中当作线上排序质量认证。
- FTS 探针接入后的全量门禁为 **1693 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`；GitNexus 刷新为 **34,029 nodes / 71,422 edges / 300 flows**。

## 2026-08-30 结构化知识证据接入 search_knowledge

- 新增 `KnowledgeBase.search_evidence()`：并行收集 FTS5、关键词、Dense 和搜词扩展，使用
  RRF 合并；可选 Reranker 只改变顺序，不替证据门槛。每个 `KnowledgeEvidence` 保留
  `document_id/chunk_id`、来源路径、label、字符偏移、内容 hash、mtime、各路分数和
  `match_type`，并可序列化为结构化对象。
- `MessageHandler._skill_search_knowledge()` 已切到结构化路径；LLM 看到的知识片段现在带
  `[证据 source=... document_id=... chunk_id=... offset=... score=... match=... hash=...]`。
  结构化检索异常会记录原因并回退旧搜索，旧热载对象/测试替身仍兼容；同步检索继续走有界
  线程门，不阻塞前台事件循环。
- 专项回归覆盖来源元数据、闲聊空召回、多路 match_type、慢结构化检索和异常降级；真实
  生产知识库抽样 `更新日志 GPT SoVITS` 已返回稳定 chunk/hash/offset。完整门禁和线上 canary
  仍待本批完成，暂不把离线结构化命中等同于长期准确率认证。
- 本批全量 **1699 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus
  刷新为 **34,088 nodes / 71,496 edges / 300 flows**。`detect_changes()` 仍因项目无 `.git`
  无法执行，未伪造变更影响报告。
- 结构化入口改动后重新运行真实 BGE 固定基线仍为 **10/10 passed**；下一步只剩真实
  重排器/QQ 工具 canary、线上 FTS/Dense/RRF 质量对照和持续流量验证。

## 2026-08-30 Reranker 原生崩溃取证与安全降级

- 真实环境探针确认 `models/BAAI/bge-reranker-v2-m3/model.safetensors` 约 **2.27GB**；标准加载
  曾因 Windows 页面文件不足报 `os error 1455`。改用 `low_cpu_mem_usage=True` 后，独立进程仍在
  `torch_cpu.dll` 触发 Windows `0xc0000005`（Application Error 1000，19:11:26，进程
  `0x7330`），这是 Python 层无法捕获的原生崩溃，不再继续强行重试。
- `RerankerEngine.load()` 现在在进入 `from_pretrained` 前检查 PyTorch 运行时；检测到当前未纳入
  验收矩阵的 `2.14.0.dev20260727+cu130` 开发版时，记录 `last_load_error` 并安全跳过重排。
  主检索继续使用 FTS/Dense/RRF 与既有降级链，Reranker 不可用不会阻断机器人启动；稳定版运行时
  仍保留低峰值加载、设备回滚和异常清理路径。
- 新增运行时跳过、低峰值参数和失败清理回归；Reranker 专项 **3 passed**，全量门禁 **1702 passed，
  3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`。GitNexus 刷新为 **34,109 nodes /
  71,519 edges / 300 flows**；`detect_changes()` 仍因项目无 `.git` 无法执行，按环境限制记录。
- 下一步仍需人工重启后的知识工具/QQ 真实 canary，以及 FTS/Dense/RRF 的线上质量对照；当前不把
  Reranker “跳过加载”当作精排能力通过，也不把离线测试外推为千人规模容量认证。

## 2026-08-30 知识索引与 GPT 监护实时 canary（旧进程）

- 当前运行中的 `boot=3fea0426` 于 19:04:18 报告知识索引 **0 新增 / 0 变更 / 0 删除、12 文件 /
  156 块**，BGE 在 512 维就绪；该进程是在 Reranker 安全边界落盘前启动的，19:04:42 的
  `Reranker 就绪（cuda）` 不能代表新 guard 已加载。
- 同一实例 19:18:00 的 GPT‑SoVITS 退出被监护器捕获了 stdout 尾部（含 `/tts 200`），一次自动恢复
  后于 19:18:35 重新通过 `/tts` ready；说明当前 GPT 监护/取证链路有效，但仍是单窗口样本，不能替代
  2h/12h soak。新 Reranker guard 需用户下一次手动重启后再验证日志。

## 2026-08-30 记忆积压策略核验

- 只读生产快照显示未处理用户消息 **742 条 / 184 人**，持久提取任务 `pending/leased/ready=0`，
  `dead=0`；所以不是 worker 卡住，而是准入策略尚未把尾部消息建成 job。
- 184 人中 **112 人已有 3 条以上但都未满 7 天**，其余低频用户的最老消息约 **20.82 天**，尚未
  到达 1–2 条消息的 21 天阈值；手工按 `extraction_backlog_ready()` 重算仍为 `eligible=0`。
  这验证了当前“10 条立即、3–9 条满 7 天、1–2 条满 21 天”的延迟准入行为，没有证据支持立即
  放宽阈值或清空积压。
- 该策略的下一验收点是跨过 21 天后观察首批低频用户是否自动建 job、完成提取并推进游标；若仍不动，
  再针对共同根因修复，不用单点重试掩盖问题。

## 2026-08-30 知识检索排名评估补齐

- 固定评估器优先走结构化 `search_evidence()`，保留每条样本的 `ranked_sources`、`match_types`
  和期望来源 `expected_rank`；缺少新接口的旧替身仍回退兼容文本搜索，避免测试契约被悄悄改变。
- 报告新增正例 `hit@1`、`hit@3`、`MRR` 和检索模式字段。真实 BGE 固定集保持 **10/10**，
  `positive_hit_rate=1.0`、`negative_empty_rate=1.0`、`hit@1=1.0`、`hit@3=1.0`、`MRR=1.0`；
  该结果只代表 6 条正例/4 条负例的小样本，不能替代线上分布评估。
- 评估器专项与全量回归均通过（全量 **1702 passed，3 warnings**）；GitNexus 刷新为
  **34,117 nodes / 71,530 edges / 300 flows**。`detect_changes()` 仍因项目无 `.git` 无法执行。

- 兼容回退报告补充：未走结构化证据时，排名指标明确返回 `null`（未测量），不把旧接口的缺失
  排名误报为 0 分；全量回归仍为 **1702 passed，3 warnings**，GitNexus 最新刷新为
  **34,124 nodes / 71,537 edges / 300 flows**。

## 2026-08-30 短时线上观察补充

- 观察器 run `20260830-193558-32548-f305` 采集 72 秒、4 个快照：NapCat/反向 WS/GPT-SoVITS
  均在线，区间无 ERROR、无入站失败/不确定事件，观察器完整性 PASS；出现 1 条群入站，未产生
  发送、LLM、工具或媒体样本，因此这些功能仍应判为 `INSUFFICIENT`，不能误报为通过。
- 记忆积压 **746→747**，到达 1、服务 0，仍为 `eligible=0`、`dead=0`；与准入策略核验一致，
  不是 worker 失败证据。报告已落盘至 `runtime-observations/runtime-20260830-193558-32548-f305.md`，
  后续需要在用户真实触发知识、语音、发送和提取后再做完整 canary。

## 2026-08-30 GPT-SoVITS 退出尾部取证增强

- 用户手动重启后的新实例 `boot=0e284929` 已加载 Reranker 安全 guard：开发版
  `torch=2.14.0.dev20260727+cu130` 被安全跳过；知识索引同步为 **12 文件 / 156 块**，
  BGE **512 维**，GPT-SoVITS 启动时曾通过真实 `/tts` 健康检查。
- 该实例随后在 **19:33:18** 与 **19:41:17** 两次出现 GPT-SoVITS 就绪后退出（均为
  `code=1`）。监护器第一次自动恢复并重新 ready，第二次按一次性恢复预算 fail-closed；
  糖糖主进程未退出，9880 当前离线。现有 stdout 只有 uvicorn 启动/进度与 `/tts 200`，
  没有 Python traceback 或 Windows 事件对应记录，因此根因仍只能标为待验证，不能臆测为
  模型、端口或 CUDA 单一原因。
- `ServiceManager._persistent_drain()` 现保留每个管道有界 **200 行**尾部，退出日志最多带
  **48 行**并显式标注 `stderr:` / `stdout:`；新增回归验证 30 行进度刷屏后仍能同时取到两路
  尾部。这样下一次自然复现可区分 Python/uvicorn 异常与原生进程静默退出，不改变恢复预算、
  ready 判定或停止状态机。
- 本次验证：服务管理器专项 **13 passed**，全量 **1703 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus 刷新为 **34,131 nodes / 71,545 edges /
  300 flows**。`detect_changes()` 因项目无 `.git` 无法执行，按环境限制记录。
- 当前进程未被本次操作停止或重启。下一验收动作是用户手动重启后触发至少一次真实语音请求，
  检查新退出日志是否给出可归因尾部；若仍无尾部，再转向 Windows Application/System 事件、
  子进程退出码和 GPT-SoVITS 自身日志的联合取证，而不是继续增加盲目重启次数。

## 2026-08-30 新手动实例 GPT 退出复现补充

- 用户随后手动启动的 `boot=87929eed`（PID **31848**，19:45:50 创建）已加载知识索引
  **12 文件 / 156 块**、BGE **512 维**和 Reranker 安全跳过；GPT-SoVITS 首次启动于
  19:46:29 通过真实 `/tts` ready。
- 同一实例在 **19:49:23**、**19:52:14** 两次以 `code=1` 退出；两次退出尾部都完整显示
  uvicorn 启动、1500 步推理进度和 `POST /tts 200`，没有 traceback。第一次恢复后再次 ready，
  第二次命中一次性预算并停止自动拉起。这个重复样本把“服务能启动且能合成，但随后在请求后
  自行退出”确认为稳定现象；具体崩溃层仍待新诊断尾部或系统事件确认。
- 该实例创建早于本节新增的 stdout/stderr 长尾补丁，因此旧日志没有 `stderr:` / `stdout:` 标签，
  不应误判为补丁失效。待下一次人工重启后复现，才验收新取证链。

## 2026-08-30 GPT 子进程原生故障栈补充

- `ServiceManager._gpt_sovits_child_env()` 现在为 GPU/CPU 两种启动路径都注入
  `PYTHONFAULTHANDLER=1`，同时继续保留 CPU 兜底的 `CUDA_VISIBLE_DEVICES=-1`；不会改变
  设备选择、恢复预算或 ready 判定。若下一次是 Python 可捕获的致命错误，线程栈会进入已标注的
  stderr 长尾；若仍只有 `code=1` 且无栈，则可排除 Python faulthandler 可见崩溃，继续查系统事件/外部终止。
- 相关测试仍为 **13 passed**；全量门禁 **1703 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`。GitNexus 最新索引为 **34,133 nodes / 71,547 edges / 300 flows**；
  `detect_changes()` 因项目无 `.git` 无法执行，未伪造变更报告。

## 2026-08-30 GPT 启动包装非零退出取证

- `gpt-sovits/start_api_patched.py` 现在将 `api_v2.py` 启动包在显式入口中；对
  `SystemExit` / `KeyboardInterrupt` 等 `BaseException`，仅当退出码非零时把异常类型、退出码和
  traceback 写入 stderr。正常退出不增加噪声，原有 jieba_fast 兼容补丁与 API 参数不变。
- 新增 2 项包装器回归，专项 **15 passed**；全量 **1705 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`。GitNexus 最新索引为 **34,142 nodes / 71,558 edges /
  300 flows**；`detect_changes()` 因项目无 `.git` 无法执行，按环境限制记录。
- 下一次人工重启后，若 GPT 再在 `/tts 200` 后退出，日志应同时具备长尾标签、faulthandler
  线程栈和 launcher 非零异常信息；仍无额外信息时，才进入 Windows WER/外部终止专项，不继续
  盲目提高自动重启次数。

## 2026-08-30 GPT launcher 异常边界补齐

- 启动包装器的异常报告现在同时覆盖非零 `SystemExit` 与无退出码的 `KeyboardInterrupt`；
  只有 `SystemExit(0)` 被视为干净退出。新增边界回归后专项 **16 passed**，避免“注释声称可取证、
  但 KeyboardInterrupt 实际被吞掉”的盲区。
- 最新全量门禁：**1706 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；
  GitNexus 索引 **34,144 nodes / 71,560 edges / 300 flows**。项目无 `.git`，
  `detect_changes()` 仍按环境限制记录为不可执行。

## 2026-08-30 知识库同名文档隔离修复

- 红测复现：`KnowledgeBase._next_chunk_of()` 过去只比较 `source`（文件 stem）。当
  `first/guide.md` 与 `second/guide.md` 同时存在时，第一份短块会错误拼接第二份文档；同名
  文档还会被 `_search_fused()` 当作单一来源，跳过本应执行的多来源重排。
- 修复：`KnowledgeChunk` 保存 `relative_path`，文档邻接和来源计数优先使用
  `document_id → relative_path → source`，不改变对外展示的 source 名称或旧搜索契约。
  这是已验证的实际 bug，不是静态猜测。
- 新增回归后知识专项 **48 passed**；随后应完成全量测试、静态门禁和真实知识工具 canary，
  并继续检查其他基于 stem/昵称的跨作用域键。

## 2026-08-30 知识库证据规格同步

- `docs/开发规划/知识库持久化索引规格_20260830.md` 已同步实际实现：当前阶段明确包含
  embedding fingerprint、FTS5 CJK bigram、Dense/RRF 旁路、结构化证据和线程边界；不再把
  已完成能力写成“后续待接入”，并将自适应分块、overlap、parent-child、版本 diff/回滚列为
  下一阶段开放项。
- 本次同名文档修复后的全量门禁为 **1707 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`；GitNexus 最新索引为 **34,148 nodes / 71,571 edges / 300 flows**。

## 2026-08-30 WeKnora 对照后的真实基线

- 当前生产知识库实测 **12 文件 / 156 块**；块长中位数 **283.5 字**、均值 **396.4 字**、
  最大 **1496 字**，其中 **61 块低于 200 字、11 块超过 1000 字**。这说明现有切块没有
  超过单块上限的硬错误，但短块比例较高；自适应分块应先用新增评估集证明收益，不能只因
  WeKnora 支持该策略就直接替换现有切块。
- `search_evidence()` 在本机热路径的 20 次探针中，四类问句 p50 **2.14–2.79ms**、
  p95 **2.48–3.15ms**；真实 BGE 固定集仍 **10/10**（正例、负例、hit@1、hit@3、MRR 均
  **1.0**）。当前主要缺口是线上分布和长文上下文质量，不是已测出的检索延迟瓶颈。
- 因此按 WeKnora 的可拆解部分调整优先级：先补多来源/长文/版本变更样本与线上 trace，
  再以数据决定 overlap、parent-child 和权重；不引入完整 sidecar、不替换现有记忆系统，
  也不把小样本离线满分外推为千人规模认证。

## 2026-08-30 记忆事实簇后台再污染修复

- 只读生产库取证：`cluster_facts` 中有 **6,121** 条 active 原子事实没有
  `evidence_ids`；其中 **81** 个事实簇仍有非空摘要且包含这类历史行，**87** 个簇同时
  含有有锚点和无锚点 active 行。它们虽已被对话召回的 `trusted_only` 过滤隔离，但旧的
  后台摘要/合并路径仍可能把它们交给 LLM，存在再次“洗白”污染摘要的真实风险。
- 红测复现：向一个簇写入“有证据的事实”和“无证据的历史幻觉”，调用
  `MemorySystem._regenerate_cluster_summary()`，修复前 LLM 输入同时包含两者；修复后只保留
  有证据事实。新增提取上下文回归，混合簇的旧摘要也不会进入下一轮 LLM。
- 最小修复：`Store.get_fact_clusters()` 返回 `has_unanchored_active_facts` 标记；提取和相似
  合并对混合/无锚点簇 fail-closed；摘要重建和合并事实再次按非空证据锚过滤。审计行仍保留，
  未执行未经批准的生产数据删除或批量改写。
- 验证：记忆真值/事实簇专项 **32 passed**，全量 **1709 passed，3 warnings**；
  `PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus 已刷新为 **34,152 nodes / 71,576 edges /
  300 flows**。项目无 `.git`，`detect_changes()` 仍只能记录环境限制。
- 未决：生产库中的历史未锚定行仍需单独设计可回滚的归档/重建迁移；在迁移前不得把“行数下降”
  当作质量提升，也不得放宽任何可信召回闸门。

## 2026-08-30 事实簇真值卫生可观测性

- `_check_memory_truth` 新增 active 无证据原子事实和“摘要含无证据存量”两项检查；发现任一项
  即返回 `warn`，但不自动修改生产数据。这样后台隔离修复与历史债务监控形成闭环。
- 新增健康检查回归后全量门禁为 **1710 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`；GitNexus 刷新为 **34,154 nodes / 71,581 edges / 300 flows**。
- `detect_changes()` 仍因项目无 `.git` 无法执行，已按环境限制记录；当前运行中的 bot 未被停止或
  重启，新代码需用户下一次手动重启后才会加载。

## 2026-08-30 历史无锚点事实迁移工具（默认 dry-run）

- 新增 [tools/migrate_unanchored_cluster_facts.py](D:/qq-小糖糖/tools/migrate_unanchored_cluster_facts.py)。
  默认使用 SQLite `mode=ro` 只读审计；只有显式 `--apply` 才会先生成 backup，再在单事务内把
  active 且无 `evidence_ids` 的原子行标为 `retracted`、清空受影响簇摘要并重算 `fact_count`。
  原子行、聊天记录和有证据事实均保留，不调用 LLM、不猜测来源。
- 真实生产 dry-run 输出：**6,121** 个目标事实、**1,995** 个受影响簇、**81** 个非空摘要，
  有证据 active 事实 **280**；确认未写入生产库。临时数据库回归验证撤回、保留锚定行、摘要
  清理和二次执行幂等。
- 该工具目前只完成“可审计、可回滚的执行能力”，未替用户执行 `--apply`；下一步需人工确认
  迁移窗口和备份恢复演练后再做生产变更。
- 临时数据库备份演练还发现并修复了 `_backup()` 的 `str`/`Path` 边界错误；回归验证 backup
  中保留迁移前的 active 状态和原摘要，正式迁移函数二次执行返回 0 个动作。
- 修复后的全量门禁为 **1712 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；
  GitNexus 最新索引为 **34,169 nodes / 71,613 edges / 300 flows**。
- 迁移 CLI 增加 3001 在线保护；真实在线实例实测 `--apply` 返回 **2**，不创建 backup、
  不修改数据库。临时回归补齐后最终门禁为 **1713 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus 最新索引为 **34,171 nodes / 71,617 edges /
  300 flows**。

## 2026-08-30 WeKnora 对照与记忆证据绑定守卫

- 用户提供的 WeKnora v0.7.2 调研已纳入知识库路线：不接入完整平台、不替换现有记忆；优先
  借鉴持久化索引、稳定 `document_id/chunk_id`、自适应分块、来源/偏移/分数证据协议和
  FTS5 + Dense + RRF 评估方法。当前实现已覆盖前四项中的索引/证据基础，下一步只在新增
  长文、多来源、版本变更样本证明收益后再引入 overlap、parent-child 或版本 diff。
- 只读生产证据审计（21:27–21:29）：active trusted 记忆 **1,313** 条；其中明确的
  `manual/corrected` 无证据记忆 **16** 条；有 `evidence_ids` 但无关系化记录 **0**；
  非 self 记忆绑定 bot 回复 **0**、主体不一致 **0**、作用域不一致 **0**；self 记忆目标/
  bot 回复/作用域绑定异常 **0**；关系表孤儿行 **0**。这证明当前存量没有被发现的跨用户或
  bot 主语串线，但不替代持续监控。
- `_check_memory_truth` 已把上述结构约束纳入只读健康检查；新增回归覆盖外部记忆误绑 bot/
  他人/他群和 self 目标错误。它只告警、不改库，避免健康任务成为第二个写入入口。
- 本轮验证：专项健康检查 **16 passed**；全量 **1714 passed，3 warnings**；
  `PYFLAKES_UNDEFINED=0`、`compileall=0`。GitNexus 刷新后为 **34,173 nodes / 71,622 edges /
  300 flows**；项目无 `.git`，
  `detect_changes()` 仍按环境限制不可执行。生产 bot 未停止，未执行事实迁移 `--apply`。

## 2026-08-30 SQLite 写入尾延迟与单写者门

- 线上日志取证：过去 4 小时 `Store SQLite 调用较慢` 共 **31** 次；队列等待均为
  **0ms**，但 `persist_inbound_message` p50 **558.3ms**、p95 **679.1ms**、最大
  **810.3ms**，提取任务和群成员更新也出现 300–731ms。只读查询实测约 0–1ms，说明
  主要矛盾是同一进程内 16 路写事务争用，而不是检索 SQL 或线程池排队。
- 隔离的 778MB 数据库副本并发对照：16 路原始写入 p50 **219.97ms** / p95 **352.38ms** /
  max **408.05ms**；进入单写者门后 p50 **86.85ms** / p95 **152.85ms** / max **163.31ms**。
  读/未知调用仍保持原 16 路并发；未改变 SQLite schema、同步级别或持久化语义。
- `agent/async_io.py` 新增按内部 operation/函数名识别的进程内单写者锁，所有异步 Store
  包装器共享同一事件循环锁；取消时仍等待底层线程收口。该识别只处理内部操作名，不读取
  用户文本，不参与 LLM 决策。
- 验证：新增并发回归后全量 **1715 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `compileall=0`；GitNexus **34,181 nodes / 71,634 edges / 300 flows**。生产 bot 未重启，
  新门需下次手动重启后生效；后续用新日志比较写入 p95 和事件处理尾延迟。

## 2026-08-30 知识库搜词扩展的同名文档隔离

- 取证发现：`_build_expansion()` 原先只保存文件 stem；当不同目录存在同名文档时，
  `_expansion_hits()` 会把搜词命中的两份文档全部块一起召回，违反稳定来源与文档隔离契约。
- 最小修复：扩展索引改用文档相对路径键，命中时优先按 `relative_path` 过滤；对外展示的
  `source` 不变，并保留无相对路径旧 chunk 的兼容兜底。
- 新增同名文档搜词隔离回归；知识检索专项 **25 passed**，全量 **1716 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus 刷新为 **34,182 nodes / 71,636 edges /
  300 flows**。项目无 `.git`，`detect_changes()` 仍按环境限制不可执行。
- WeKnora 对照结论不变：先继续补多来源/长文/版本变更评估集和线上检索 trace，
  以测量结果决定 overlap、parent-child 与权重，不引入完整 sidecar。

## 2026-08-30 知识库自动摘要回写风险取证

- 日志取证发现同一活动策划书在 **19:23、19:29、19:38、19:46** 反复出现“文档摘要已生成”，
  并伴随知识库块数 **302→216→260→309** 波动。现存 `_analyze_and_index_document()` 会把
  LLM 输出直接前置写回原始文档，再触发 reload；这破坏原文来源边界，也可能在重试时层层叠加
  未核验摘要。
- 当前版本代码已没有该方法的活跃调用点，故本轮没有贸然修改原始文件写入语义；这条记录保留为
  下一专项的真实证据，而非把历史日志直接归因于当前线上路径。
- 下一专项应按 WeKnora 的“原文与索引元数据分离”思路，把摘要改为带源 hash/生成时间/模型指纹
  的独立、可丢弃元数据，并增加重复触发幂等测试；在设计和回滚方案完成前，不恢复任何原文回写。
- 本次真实知识 canary：**12 文件 / 156 块，10/10 通过，正例命中率 1.0、负例空召回率 1.0、
  hit@1/hit@3/MRR 均 1.0**；结果保存在
  `runtime-observations/knowledge-eval-20260830-expansion.json`。

## 2026-08-30 摘要写回 fail-closed 修复

- `_analyze_and_index_document()` 现改为只执行回合内分析并记录摘要长度，**不写回原始文档、
  不触发知识库 reload**；文档来源因此保持原文、hash 与 offset 一致。当前源码静态检索没有
  活跃调用方，修复不会改变现行上传文件“回合内阅读、不进入公共知识库”的路径。
- 新增回归验证：LLM 返回摘要时原文件字节内容不变，reload 不发生。
- 验证：全量 **1717 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus
  刷新为 **34,187 nodes / 71,641 edges / 300 flows**。项目无 `.git`，`detect_changes()`
  仍按环境限制不可执行。
- 下一步仍需设计独立摘要元数据表（源 hash、生成时间、模型指纹、可撤销状态），并在离线窗口
  做回滚演练；在此之前不允许恢复原文回写。

## 2026-08-30 知识检索结构化 trace

- `KnowledgeBase.search_evidence()` 现在记录不含查询正文的检索摘要：命中块数、文档数、
  召回路径及总耗时；空召回也记录 `paths=none`。这补齐了 WeKnora 对照中“召回数量/路径/耗时”
  的基础观测面，不改变排序、阈值或 LLM 决策。
- 回归验证日志不会泄露查询原文；全量门禁 **1718 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus 刷新为 **34,189 nodes / 71,648 edges /
  300 flows**。项目无 `.git`，`detect_changes()` 仍按环境限制不可执行。
- 下一次人工重启并产生真实 `search_knowledge` 流量后，需用该 trace 统计线上 hit@k、空召回率、
  多来源比例、p50/p95 延迟；在没有真实分布数据前，不调整 FTS/Dense/RRF 权重。

## 2026-08-30 知识索引生命周期契约回归

- 复核 `KnowledgeIndex.close()` 发现它的生命周期语义容易被误用：索引连接是每次操作短连接，
  因此关闭时不应重新初始化数据库或产生任何 I/O。当前实现保持无操作返回，新增回归锁定该契约。
- 验证：全量 **1719 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus
  刷新为 **34,191 nodes / 71,651 edges / 300 flows**。项目无 `.git`，`detect_changes()`
  仍按环境限制不可执行。

## 2026-08-30 CJK FTS 查询分词修复

- 红测证明：结构化检索查询“怎么安装”原先只返回 `keyword`，因为 FTS 直接把整句汉字
  编成二元组并要求跨词 bigram“么安”，无法形成 BM25 证据。
- 修复：`search_evidence()` 将已通过 jieba 和停用词纪律的实质关键词传给 FTS；无实质关键词
  时不发起 FTS 查询，保持空召回纪律。索引仍保留中文 bigram 和 AND 组合，避免放宽为噪声 OR。
- 验证：全量 **1720 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；GitNexus
  刷新为 **34,193 nodes / 71,654 edges / 300 flows**。项目无 `.git`，`detect_changes()`
  仍按环境限制不可执行。
- 下一次真实流量需观察 FTS 路径占比、空召回率和 p50/p95；在评估集扩充前不调整 RRF 权重。

## 2026-08-30 FTS 证据门槛回归修复

- 真实固定集发现 CJK FTS 分词修复后，“今天天气怎么样”仅凭正文单词“天气”误召回，
  负例空召回率由 1.0 降为 0.75。根因是 FTS 召回结果绕过了“单正文词不构成独立证据”的旧门槛。
- 修复：FTS 结果只有在对应块已有关键词强证据（标签/来源命中或多项正文命中）时才进入
  结构化候选池；保持 FTS 负责召回、证据规则负责可回答性。
- 修复后真实固定集恢复 **10/10**，正例/负例、hit@1、hit@3、MRR 均 **1.0**；新增泛问句
  回归。全量门禁 **1721 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`；
  GitNexus 刷新为 **34,197 nodes / 71,659 edges / 300 flows**。
- 全量首次暴露的 soak 时序脆弱性也已修正：死信冷却测试的排空窗口从 2 秒调整为 4 秒，
  仍保留 20ms 容量失败测试；该模块 **9/9** 通过，避免在 Windows SQLite 尾延迟下取消正在收口的
  第 5 次失败。

## 2026-08-30 记忆模拟 LLM 流量 soak 首轮

- 使用临时 SQLite（不触碰生产 `memory.db`）完成两轮 60 秒、120 job、100 用户、4 producer /
  2 worker、2 jobs/s 的端到端提取 soak。
- 无故障对照：120/120 完成，队列全空、dead=0；作用域泄漏、重复记忆、游标错位和无证据记忆
  均为 0；LLM p50/p95/max **49.820/63.494/72.209ms**，前台锁等待 p95 **0ms**。
- 故障注入对照（429/5xx/timeout/invalid/empty/15s slow）：120/120 完成，dead=0、完整性仍全 0；
  LLM p50/p95/max **56.514/69.741/15018.241ms**，前台锁等待 p95/max **31.528/15016.329ms**，
  低于 250ms p95 门槛。
- 报告与采样：
  `runtime-observations/memory-llm-soak-20260830-2jps-clean.json`、
  `runtime-observations/memory-llm-soak-20260830-2jps.json`。
- 结论边界：短窗口功能、故障恢复和队列收敛通过；这不是 12 小时持续生产认证，也不能证明
  千人真实流量下的容量。下一步仍需用户手动启动后的长窗口观测，并据真实到达率/服务率决定
  是否调整 worker、LLM 锁或准入阈值。

## 2026-08-30 千用户记忆模拟 soak（无故障长批次）

- 使用临时 SQLite 完成 **1000 job / 1000 用户 / 4 producer / 2 worker / 2 jobs/s** 的完整批次，
  用时 **505.751s**；1000/1000 job 完成，队列 `pending/leased/ready/dead=0/0/0/0`。
- 完整性校验通过：跨作用域泄漏 **0**、重复记忆 **0**、游标错位 **0**、无已验证证据记忆 **0**；
  verified memories/evidence links 均为 **1000/1000**。LLM 延迟 p50/p95/max **50.156/63.388/296.652ms**，
  前台锁等待 p50/p95/max **0/16.215/92.355ms**，服务速率与到达速率均 **1.977 jobs/s**。
- 该批次证明在无故障、临时数据库和当前输入规模下，作用域隔离、证据绑定、队列收敛和前台锁门
  能稳定工作；它仍不是生产 12 小时 soak，也未覆盖真实 LLM 尾延迟、重启恢复和故障注入，不能据此
  宣布“千人长期容量认证”完成。下一步保留人工重启后的真实观测和故障长窗验证。
- 报告与逐样本记录：`runtime-observations/memory-llm-soak-20260830-1000users.json`、
  `runtime-observations/memory-llm-soak-20260830-1000users.jsonl`。

## 2026-08-30 FTS 派生索引损坏自愈

- 临时 SQLite 红测：主 `chunks` 保留 1 条有效块，将 `chunks_fts.terms` 损坏但保持行数不变后，
  修复前重启检索命中 **0**；这证明原有“只比较行数”的启动检查会把索引损坏静默传播为知识库空召回。
- `KnowledgeIndex._initialize()` 现在按 `chunk_id/label/content/terms` 与主表可重建值逐行校验；
  不一致时仅重建 FTS 派生表，主表、文档 hash、稳定 ID、排序和 FTS 不可用时的降级路径均不变。
- 新增回归 `test_fts5_content_corruption_is_rebuilt_even_when_row_count_matches`；专项 12 项通过，
  全量门禁 **1722 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`compileall=0`，离线知识基线
  **10/10**（正例/负例、hit@1、hit@3、MRR 均 1.0）。证据产物：
  `runtime-observations/knowledge-fts-recovery-20260830.json`。
- 当前仍未执行生产重启；真实重启 canary、文档增删改演练和线上 FTS/Dense/RRF 分布指标继续作为
  下一验收项。该修复验证的是派生索引自愈，不等于长期生产知识准确率认证。

## 2026-08-30 知识 embedding 批量持久化

- 取证基线：400 个向量在临时 SQLite 中逐块开启连接/事务，4 次耗时 **1756.70–5102.46ms**，
  p50 **3753.34ms**；这部分是纯 SQLite 写入开销，不含模型编码。
- `KnowledgeIndex` 新增 `upsert_embeddings()` 单事务批量接口；`KnowledgeBase._ensure_chunk_embeddings()`
  收集同批新向量后一次写入，保留旧 `upsert_embedding()` API，并在批量失败时逐块兼容重试。内存向量
  先落地，持久化失败只告警，不阻断当前检索。
- 同样 400 块基准改为批量后 4 次耗时 **17.53–19.93ms**，p50 **18.27ms**，相对基线 p50
  降低约 **99.51%**；结果仅说明事务开销下降，不外推为 BGE 编码或生产端到端延迟收益。
- 新增批量 round-trip、原子校验和 KnowledgeBase 接线回归；全量门禁 **1724 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`compileall=0`。证据产物：
  `runtime-observations/knowledge-embedding-batch-bench-20260830.json`。
- 当前未重启生产 bot；下一验收仍是人工重启后的索引热身、真实知识工具 canary，以及文档增删改/重启
  场景验证。批量优化不改变 FTS/Dense/RRF 排名和 LLM 的查询决策。

## 2026-08-30 GPT-SoVITS 包装器退出证据修复

- 运行日志取证：boot `1617b760` 在 `21:36:08` 和 `21:53:19` 均完成 `/tts 200`，随后分别于
  `21:52:47`、`21:59:28` 以 code=1 退出；同一时段 Windows WER 没有对应 native crash，退出尾部也没有
  traceback，无法把底层 uvicorn 停止原因直接归因给 torch。
- 离线最小探针执行包装器真实入口控制流（仅将 `_run_api()` 替换为空操作）稳定复现
  `RuntimeError: No active exception to reraise`，证实入口末尾裸 `raise` 会覆盖真实退出语义并制造误导性 code=1。
- 最小修复：保留非零退出语义，但改为显式 `SystemExit(1)` 和可检索诊断
  `GPT-SoVITS launcher API returned unexpectedly without an exception`；新增入口回归测试，防止裸 raise 回归。
- 验证：`tests/test_gpt_launcher.py` **4 passed**；探针证据见
  `runtime-observations/gpt-launcher-return-repro-20260830.json`。本修复不启动、不终止生产实例，
  也不声称已解决导致 uvicorn 返回的底层运行时原因；下一次人工重启后需观察该明确诊断是否出现，
  再决定是否继续处理 torch/uvicorn 原生退出。

## 2026-08-30 GPT-SoVITS ready-child 错误尾部取证增强

- 红测模拟 traceback 后继续输出 80 行进度条：原实现只取每个管道最后 24 行，
  `Traceback` 与 `RuntimeError` 均会丢失；新增回归 `test_persistent_drain_keeps_buried_error_diagnostics` 先复现该缺口。
- `ServiceManager._persistent_drain()` 现在对 traceback、异常、CUDA/native crash、launcher 等高信号行维护独立有界尾部，
  退出日志优先输出诊断并去重，再附有限正常尾部；每次新子进程启动会清空上一代诊断。
- GitNexus 影响分析：`_persistent_drain` 风险 **MEDIUM**，直接调用 6、间接影响 12，均在 Agent 模块；
  未改变进程恢复次数、GPU/CPU 选择、端口或 TTS 请求。
- 定向 ServiceManager **14 passed**；随后需跑全量门禁。生产 bot 当前未运行，未做重启或外部副作用；
  下次人工重启若再次退出，将可区分“有异常但被尾部淹没”和“无异常返回”两类证据。

## 2026-08-31 知识 embedding 无指纹串模隔离

- 红测构造两个实际向量不同、但 `fingerprint` 均为空的 embedding 引擎；旧逻辑第二次预热直接
  复用第一次内存向量，证明“旧/伪引擎继续走内存路径”会留下跨模型串模窗口。
- `KnowledgeBase._ensure_chunk_embeddings()` 现在对缺少稳定指纹的引擎每次先失效旧缓存；
  有指纹引擎的持久化复用、批量写入和模型切换逻辑保持不变。这样牺牲无指纹兼容路径的重复编码，
  换取无法证明模型身份时不复用错误向量。
- GitNexus 影响分析：该方法风险 **HIGH**，直接调用 4、间接影响 20，涉及 Agent/Tests/Tools；
  已逐项跑知识预热、Dense 检索和评估相关测试。
- 知识专项 **62 passed**；全量门禁 **1727 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。生产 bot 未运行，未修改知识索引数据库或模型文件；该修复仍需下一次人工重启
  后观察实际模型指纹与预热日志。

## 2026-08-31 知识主 chunks 内容损坏自愈

- 红测在临时 SQLite 中仅篡改 `chunks.label/content`，保持 `documents.sha256` 与行数不变；
  修复前重启后 `sync()` 将损坏正文当作 unchanged 保留，证明只比 hash/数量不足以保护索引真值。
- `KnowledgeIndex.sync()` 对 unchanged 文档逐字段核验 `chunk_id/ordinal/label/content/content_sha256/offset`；
  任一不一致即在现有原子替换事务中重建该文档 chunks 与 FTS 派生行。文档 hash、稳定 ID 和删除语义不变。
- GitNexus 影响分析：`sync` 风险 **MEDIUM**，9 个直接调用方，均在 Agent 模块；未修改生产数据库。
- 临时 SQLite 回归已通过 **14 项**（含原子失败路径）；证据见
  `runtime-observations/knowledge-main-chunk-recovery-20260831.json`。全量门禁待本轮执行。

## 2026-08-31 P0-1b 批处理来源键接线修复

- 取证红测发现：批处理真实事件虽逐条落库，合并视图却只携带 `_source_message_ids`；
  `ChatContext` 因 `message_id=0` 生成 ephemeral 主键，导致 LLM 回合来源无法精确绑定到原始消息。
- `MessageBatcher` 现为群/私合并视图传递全部稳定 v2 `event_key`；Handler 边界以首条真实键作为回合主键，
  同时保留完整 `source_event_keys`，无平台键时仍安全回退 ephemeral。
- 红测先复现后修复；相关批处理与契约回归 **29 passed**，全量门禁 **1729 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。P0-1b 仍未整体完成，主动/窗口等边界需继续接线与生产观察。

## 2026-08-31 P0-1b 来源键作用域护栏

- 红测构造 `group:g1` 回合绑定 `v2:group:g2:...` 来源键，旧边界会静默接受跨群证据。
- Handler 的 ChatContext 适配器现对可解析的 v2 键校验 channel/target 与当前 scope；不匹配或格式损坏即拒绝，
  旧 ephemeral/历史键保留兼容路径。相关契约回归与全量门禁 **1730 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。未改生产数据库；需安全重启后用真实批处理样本观察。

## 2026-08-31 窗口 skip+媒体决策语义修复

- 红测锁定原缺口：窗口内 LLM 调用 `skip_response`、但同时明确请求媒体时，媒体分支结束后仍无条件增加
  `window_decisions_reply` 并调用 `on_llm_reply_decision`，会把沉默误记为回复并阻止渐变退出。
- 现仅当 `turn_actions.respond=True` 时记录 reply/推进窗口；skip 仍可执行已明确的语音、贴图等动作。
- 全量门禁 **1731 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；生产未启动，需重启后观察窗口统计。

## 2026-08-31 主动事件 claim→executing 失败释放

- 红测模拟同一主动事件已成功 claim、但推进 `executing` 返回失败；旧逻辑会遗留 `claimed` 租约，
  在租约超时或重启前无法及时再次处理。
- Store 新增同租约、条件更新的 `release_proactive_event_claim()`，安全回到 `pending` 并清空租约；
  自治入口和 scheduler 在推进失败时统一调用，其他租约不会被释放。
- 定向回归 **29 passed**；全量门禁 **1733 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。未启动生产实例，需安全重启后观察主动任务状态收敛。

## 2026-08-31 窗口摘要向量-原文对齐修复

- 红测让窗口中间一条消息的 embedding 返回 `None`；旧逻辑过滤后按新索引读取原句，
  将后续向量错误绑定到被丢弃的文本（摘要出现 `drop`，遗漏真实句子）。
- `_extract_window_summary()` 现保留 `(原句, 向量)` 配对后再过滤缺失向量，中心相似度排序始终引用正确原文。
- 定向窗口/自治回归 **19 passed**；全量门禁 **1734 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。未修改生产数据库，待安全重启后观察跨窗口摘要。

## 2026-08-31 WeKnora 借鉴项复核

- 复核外部 WeKnora v0.7.2 分析与当前实现：SQLite 文档/chunk/embedding 持久化、稳定 ID/hash/偏移、
  CJK FTS5、Dense+RRF、结构化来源证据、增量同步/自愈和离线评估均已接入；当前知识库为 **12 文件 / 156 块**。
- 固定检索集保持 **10/10**（正例命中率、负例空召回率、hit@1、hit@3、MRR 均 1.0）。全量门禁
  **1734 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。
- overlap、parent-child、版本 diff/回滚暂不提前引入；先等待人工重启后的真实检索 trace 和延迟/误召回样本，
  再以测量结果决定下一步，避免以 WeKnora 能力清单替代本项目证据。

## 2026-08-31 命令与相册 Store 线程边界补齐

- 红测在慢 `Memory.get_stats()` 与慢 `find_recent_image_senders()` 下均观察到事件循环 tick=0，
  证明异步命令/相册路径仍可直接阻塞 SQLite。
- `CommandRouter` 新增统一 Store worker，迁移亲密度、记忆、生日、点赞、传话、机器人名单和遥控发言的
  同步读写；`AlbumLiker.scan_and_like()` 同样复用 16 槽并发门，保留旧替身兼容。
- 定向前台/命令回归 **10 + 72 passed**；全量门禁 **1736 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察 Store p95、事件环 tick
  和命令/相册真实延迟。

## 2026-08-31 离线补读 Store 线程边界补齐

- 红测在慢 Store（每次 50ms）下复现补读群消息计数/读取和错过点名记忆查询会阻塞事件循环；修复前
  ticker `ticks=0`，证明持续流量期间可能拖住其它消息。
- `CatchUpManager` 统一复用有界 Store worker：群消息计数、消息读取、补回复记忆查询、摘要前置上下文
  均移出事件循环；保留取消收口、并发上限和慢调用日志契约。
- 定向补读回归 **2 passed**；全量门禁 **1739 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动、未改生产记忆库；待安全重启后观察补读
  p95 与事件环 tick，确认真实离线恢复流量下的延迟收益。

## 2026-08-31 定时主动事件 Store 线程边界补齐

- 红测在每次 30ms 的慢 Store 下复现 `_fire()` 的事件记录、租约推进、DecisionRun 绑定和终态写入均跑在
  事件循环线程，持续任务期间 ticker `ticks=0`。
- `CronScheduler` 统一使用有界 Store worker；所有协调步骤仍按 record → claim → executing → decision →
  send → finish 顺序执行，失败/未确认/租约释放语义不变；私有同步适配器保留 `finalize_proactive_decision`
  的显式生产接线，避免静态接线审计误报死代码。
- 定向主动事件与接线回归 **25 passed**；全量门禁 **1740 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察调度器 Store p95、事件环 tick
  和重复任务收敛情况。

## 2026-08-31 感知反馈 Store 线程边界补齐

- 红测对 `record_feedback` 注入 50ms 延迟，确认反馈评估的同步写入会占用事件循环；修复前后均以 ticker
  和线程 ID 作为运行时证据。
- `PerceptionEngine.evaluate()` 的中性与 LLM 评估两条写入路径统一复用有界 Store worker；反馈语义、置信度
  和失败降级保持不变。
- 定向感知/长期状态回归 **17 passed**；全量门禁 **1742 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待运行观测反馈写入 p95。

## 2026-08-31 生日个性化 Store 线程边界补齐

- 红测对生日状态 `kv_get/kv_set` 与人物档案读取注入 30ms 延迟，证实 `_check_birthdays()` 原有直接调用
  会在后台任务中执行同步 SQLite。
- `BirthdayGreeter` 增加异步状态读写适配器，保留同步兼容接口供重启恢复测试；生日去重、uncertain 冻结和
  发送确认语义不变。
- 定向个性化/发送契约回归 **37 passed**；全量门禁 **1742 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察生日循环延迟与状态持久化。

## 2026-08-31 自然动作记忆查询 Store 线程边界补齐

- 红测对自然动作 `intimacy` / `memory` 注入 50ms 慢读，修复前两条路径均观察到事件循环
  `ticks=0`，证明 LLM 工具执行之外仍存在同步记忆查询缺口。
- `MessageHandler._execute_natural_action()` 现将 `get_stats()` 与带 `source_group_id=""` 的
  `recall_formatted()` 统一交给有界 Store worker；私聊作用域护栏和返回文案保持不变。
- 定向自然动作/私聊契约回归 **14 passed**；全量门禁 **1744 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察自然动作 p95。

## 2026-08-31 前台偏好注入 Store 线程边界补齐

- AST 契约红测确认群聊与私聊每条消息的 `PreferenceTracker.get_preferences()` 原先直接执行，
  会在前台事件循环中读取 SQLite。
- 两条前台路径现统一经 `_run_store_io` 查询，并继续按群聊 `group_id` / 私聊空域传递来源范围，
  不改变偏好内容或隐私边界。
- 前台 Store 边界回归 **11 passed**；全量门禁 **1745 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察偏好注入 p95。

## 2026-08-31 反思整合 Store 线程边界补齐

- 红测在反思摘要/日记写入各注入 50ms 延迟，原 `_apply_reflection()` 在 `maybe_reflect()` 内同步落库，
  事件循环 `ticks=0`。
- 反思结果的摘要与日记写入统一走有界 Store worker；自我叙事、关系和经验缓冲仍在事件循环中更新，
  游标文件写入也移到线程，避免跨线程修改内存状态。
- 反思边界回归 **1 passed**；全量门禁 **1746 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察反思任务 p95 与状态收敛。

## 2026-08-31 语音缓存文件 I/O 线程边界补齐

- 红测对 GPT-SoVITS WAV 缓存写入与定时语音资产哈希读取注入 50ms 延迟，修复前均观察到事件循环
  `ticks=0`，证明语音后台任务仍有同步文件 I/O。
- GPT-SoVITS 缓存落盘和任务语音资产哈希读取统一使用有界 blocking worker；语音内容、缓存键、
  SHA-256 与发送回执语义不变。
- 定向语音/发送回归 **53 passed**；全量门禁 **1749 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察语音生成 p95 与事件循环 tick。

## 2026-08-31 图片分享下载 I/O 线程边界补齐

- 红测对图片分享调度器下载和 LLM `fetch_huaban_image` 技能注入 50ms 文件写入延迟，
  修复前均观察到事件循环 `ticks=0`，确认网络 await 后仍有同步文件 I/O 缺口。
- 下载目录创建、图片落盘及技能结果的文件状态读取统一使用有界 blocking worker；图片筛选、
  CQ 路径、发送确认和失败语义保持不变。
- 图片分享定向回归 **8 passed**；全量门禁 **1751 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察图片下载 p95 与事件循环 tick。

## 2026-08-31 ASR 文件读取线程边界补齐

- 红测对音频头读取与 WAV 样本读取注入 50ms 延迟，修复前事件循环均为 `ticks=0`，
  确认 QQ 语音转码/转写链路存在同步文件读取缺口。
- 音频头、WAV 样本读取及重采样统一使用有界 blocking worker；SILK/ffmpeg 选择、识别文本和
  失败降级语义保持不变。
- ASR 定向回归 **12 passed**；全量门禁 **1754 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察语音转写 p95。

## 2026-08-31 AI 画图落盘线程边界补齐

- 红测对生成图片落盘注入 50ms 延迟，修复前事件循环 `ticks=0`，确认异步 API 返回后仍会被同步
  文件写入卡住。
- 输出目录创建与 PNG 落盘统一使用有界 blocking worker；DashScope 轮询、CQ 路径及失败返回语义不变。
- 画图/媒体定向回归 **15 passed**；全量门禁 **1754 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察画图 p95。

## 2026-08-31 NapCat 入站媒体落盘线程边界补齐

- 红测对 `download_record()` 与 `download_file()` 的 base64/HTTP 文件写入注入 50ms 延迟，
  修复前事件循环均为 `ticks=0`，确认 QQ 入站媒体可能拖住同会话处理。
- 语音/文件的 base64、HTTP 落盘及本地复制统一使用有界 blocking worker；文件名清洗、URL 兜底、
  返回值和错误语义保持不变。
- NapCat 下载定向回归 **2 passed**；全量门禁 **1756 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察入站媒体下载 p95。

## 2026-08-31 识图与知识文档读取线程边界补齐

- 红测对 `_call_vision()` 的本地图片读取和 `_load_document_content()` 的全文读取注入 50ms 延迟，
  修复前均出现事件循环阻塞证据；两条均属于消息/LLM 技能前台路径。
- 本地图片存在性检查、图片 bytes 读取及知识文档全文读取统一使用有界 blocking worker；识图缓存、
  表情包 prompt、文档匹配/敏感目录护栏和返回格式保持不变。
- 识图/知识定向回归 **35 passed**；全量门禁 **1758 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察识图与文档技能 p95。

## 2026-08-31 自治启动清理标记线程边界补齐

- 接线审计发现自治启动清理完成后直接调用 `mark_cleanup_ran()`，其内部会同步写 SQLite；
  该调用绕过了已建立的 Store worker。
- 启动路径现通过 `autonomy.mark_cleanup_ran` Store worker 写入日期，并显式使用模块属性引用，
  让运行接线和静态契约审计同时可见。
- 自治/接线定向回归 **12 passed**；全量门禁 **1760 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察启动清理耗时与状态落盘。

## 2026-08-31 NapCat 自动重启信号线程边界补齐

- 红测对 `_restart_napcat()` 的信号文件写入注入 50ms 延迟，修复前事件循环 `ticks=0`，
  证明故障恢复路径会被同步写盘拖住。
- SnowLuma 重启信号写入统一使用有界 blocking worker；信号文件位置、时间戳格式、返回值和控制台
  重启协议保持不变。
- NapCat 定向回归 **3 passed**；全量门禁 **1761 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察故障恢复耗时。

## 2026-08-31 入站语音临时文件清理线程边界补齐

- 红测对 `_transcribe_voice()` 的临时文件删除注入 50ms 延迟，修复前事件循环 `ticks=0`，
  证明语音失败/取消收口时仍可能同步阻塞。
- 临时目录创建与 `finally` 清理统一使用有界 blocking worker；唯一临时文件、失败不伪造文本、
  取消必清理和同文件去重语义保持不变。
- 群语音定向回归 **9 passed**；全量门禁 **1762 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 未启动，待安全重启后观察语音转写与清理 p95。

## 2026-08-31 chat_log 时间线复合索引基线与迁移补齐

- 只读生产基线：数据库约 778MB、`chat_log` 约 24.6 万行；最大群（22.1 万行）最近消息查询中位数
  **73.5ms**，执行计划出现 `USE TEMP B-TREE FOR ORDER BY`。
- 临时副本验证 `group_id,timestamp,id` 索引后降至 **0.016ms**；群内指定用户查询由 **29.7ms**
  降至 **0.042ms**，且不再临时排序。
- 新库建表契约与停机迁移脚本新增 `idx_chat_log_group_time`、`idx_chat_log_group_user_time`；
  旧生产库不会在启动时隐式建索引，已在 bot 停止窗口显式运行迁移脚本。
- 生产库迁移后 `PRAGMA integrity_check` 返回 `ok`；最大群时间线查询中位数 **0.018ms**、
  群内指定用户 **0.044ms**、私聊 **0.021ms**，执行计划分别命中两个复合索引。
- 索引计划回归 **4 passed**；全量门禁 **1764 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动，待安全重启后观察真实 p95。

## 2026-08-31 GPT‑SoVITS 管道异常收口补齐

- 历史运行证据（boot `f6017972`）显示 `readuntil() called while another coroutine is already waiting`
  来自启动/就绪切换期间对同一 Windows `StreamReader` 的并发读取；当前代码已改为每条管道单一长期
  `readline()` reader，不再用 `wait_for()` 周期性取消读取。
- 复核发现 `proc.wait()` 自身异常时，监控函数原异常分支未统一取消 reader；现新增统一收口，避免
  读流任务泄漏或产生未取回的后台异常。并修正文档注释，明确 stdout/stderr 均会被保留与 drain。
- ServiceManager 定向回归 **15 passed**；全量门禁 **1765 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。本次未启动生产 bot；下一步仍需人工重启后确认日志不再
  出现 `readuntil()` 竞态，并完成语音 canary/2h soak。

## 2026-08-31 命令重载移出事件循环

- 红测向 `/知识 重载` 与 `/歌单 重载` 注入 50ms 同步重载延迟，修复前前台 ticker 均为
  **0**，证明文件扫描/索引重建会冻结 QQ 命令事件循环。
- 两条重载统一通过 `run_bounded_blocking()` 执行，保留原返回文本、重载结果和异常语义，
  并沿用慢任务耗时/排队观测。
- 命令定向回归 **9 passed**；全量门禁 **1767 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动；后续需关注重载与并发知识检索
  的对象级一致性专项。

## 2026-08-31 知识库重载/检索快照一致性

- 并发红测让 `KnowledgeBase.reload()` 在清空 `chunks` 后暂停；旧实现的并行 `search()` 会在
  重载尚未完成时返回空结果，造成瞬时知识丢失。
- `KnowledgeBase` 新增对象级可重入锁，统一保护 reload、普通检索、结构化证据检索和 embedding
  预热；检索现在只观察完整快照，重载不会与检索交叉读写。
- 知识相关回归 **60 passed**；全量门禁 **1768 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动，后续需在真实运行中测量重载等待
  与检索 p95，确认锁粒度不会放大高流量尾延迟。

## 2026-08-31 知识索引 schema 版本 fail-closed

- 红测创建 `schema_version=999` 的索引后，旧初始化逻辑会先用 upsert 覆盖为 `1`，导致未知版本
  被静默接受，存在旧程序误读新索引的持久化风险。
- `KnowledgeIndex._initialize()` 现在仅在首次建库时写入当前版本；已有版本缺失、非数字或不匹配
  时直接抛出 `RuntimeError`，保留原值并由上层诚实回退内存路径。
- 知识索引/检索相关回归 **61 passed**；全量门禁 **1769 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动，后续升级 schema 必须提供显式迁移，
  禁止再次覆盖未知版本。

- 只读固定集复核（12 文档 / 156 块，含 BGE 语义路径）仍为 **10/10**，正例命中率 **1.0**、
  负例空召回率 **1.0**、hit@1 **0.8333**、hit@3 **1.0**、MRR **0.9167**；该结果是离线检索
  基线，不替代真实重启后的 LLM 工具 canary。

## 2026-08-31 场景热重载一致性与线程边界

- 红测向 `/场景 重载` 注入 50ms YAML 扫描延迟，修复前前台 ticker 为 **0**；场景重载现经
  `run_bounded_blocking()` 执行。
- `ScenarioManager` 增加对象级可重入锁，保护 `reload/get/list_all/count`，避免 worker 重载时
  消息路径观察到清空中的场景表。
- 场景/命令定向回归 **2 passed**；全量门禁 **1771 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动，后续观察真实场景切换延迟与并发
  读取尾延迟。

## 2026-08-31 角色卡热重载线程边界

- 红测向 `/角色卡 重载` 注入 50ms 角色卡/YAML 读取延迟，修复前前台 ticker 为 **0**，
  证明角色配置热重载也会冻结消息事件循环。
- 角色卡重载现通过 `run_bounded_blocking()` 执行，保留找不到文件时的原返回语义；与场景、知识、
  曲库重载统一慢任务观测边界。
- 命令定向回归 **11 passed**；全量门禁 **1773 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产 bot 仍未启动，角色卡与 LLM 并发读取仍需在真实
  canary 中观察配置切换瞬间的一致性。

## 2026-08-31 角色卡缓存原子快照

- 并发红测在 `_cached_base` 已更新、`_cached_minimal` 尚未更新的暂停窗口中确认，旧实现会让提示词构建线程穿透半更新状态。
- `PersonalityEngine` 增加对象级可重入锁，统一保护角色卡重载后的快照交换、指定角色文件切换、缓存失效和系统提示词构建；文件读取/解析在锁外完成，worker 重载与消息路径读取不会互相阻塞且只会看到完整缓存。
- 人格/对话质量定向回归 **39 passed**；生产 bot 仍未启动，待人工重启后观察真实热重载等待与提示词构建尾延迟。

## 2026-08-31 角色切回默认身份缓存修复

- 红测复现 `/角色 糖糖` 只刷新完整角色卡、遗留上一角色精简缓存的问题；替换型场景会继续带入旧身份。
- `load_role_file("")` 现在在锁外生成完整/精简默认快照，再在锁内同时交换并清除语音角色覆盖。
- 角色切换回归新增 **1 passed**；全量门禁 **1774 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 生产运行取证（未收口）

- 当前 boot `24d6af00` 的自治、任务、调度心跳持续正常；截至 05:08 未见 `readuntil()` 竞态或未取回任务异常。
- GPT‑SoVITS 在 04:48:35 与 04:57:52 两次就绪后以 code=1 退出，第二次后达到本次运行恢复预算，9880 当前不可达；日志没有 traceback，不能把原因臆断为代码回归，需保留为子进程/GPU 稳定性专项。
- `runtime_observer.py --once` 仅得 1 个快照，结果为 **INSUFFICIENT**；memory backlog 当前约 705 条，按策略均为未达到老化准入的低频积压，需在连续观测中确认 service rate 不低于 arrival rate。

## 2026-08-31 自忆数据级审计

- 生产库 `origin=self` 共 3703 条，其中历史无对象/无证据记录 3504 条已为 `retracted`/`superseded`，不进入默认读路径。
- 当前 active 自忆 199 条全部具备 `target_qq`、`evidence_ids`、`memory_evidence`，且原始证据均为对应对象的 bot 回复；active 缺目标、缺证据、错对象计数均为 **0**。

## 2026-08-31 05:19-05:22 短周期生产观测

- 4 个快照内数据库持续 `ok`、NapCat 端口持续在线，自治/任务/调度心跳连续，观察区间无新增
  `ERROR`；观察器完整性通过（无计数回退、日志截断或未结构化错误）。
- 记忆前向积压保持 **706 条 / 183 用户**，全部为策略延后项；最早单条消息距 21 天准入仍约
  **8.6 小时**，没有证据表明提取 worker 停摆。
- GPT‑SoVITS 端口 9880 在全部快照不可达，生产依赖因此判定 FAIL；其前序 boot 已记录两次
  就绪后 `code=1` 退出但无 traceback，根因仍未证实。需人工重启后做语音 canary，再决定是否
  增加子进程级诊断或调整恢复策略；本次未执行重启。

## 2026-08-31 05:23 离线容量与检索基线复核

- 当前 `get_extraction_backlog_snapshot()` 在 20 次冷/热混合调用中 p50 **59.716ms**、p95
  **63.319ms**；最大群最近 30 条消息查询 p50 **1.612ms**、p95 **1.969ms**，现有复合索引
  生效，暂不需要为该路径增加缓存。
- 知识库文本检索 p50 **0.929ms**、p95 **1.037ms**（首次惰性路径出现一次 445ms 冷启动）；
  结构化证据检索 p50 **1.904ms**、p95 **2.158ms**。这支持“重启后预热、线上使用证据入口”的
  方向，后续 canary 仍需验证 LLM 工具端到端延迟。
- 体检通过 Python/依赖/数据库完整性/GPU/NapCat 等检查；数据库约 **801MB**，9880 当前未运行，
  不将体检通过误解为语音链路已恢复。

## 2026-08-31 角色卡预览 I/O 线程边界

- 静态审查发现 `/角色卡 查看` 仍在前台协程直接执行 `Path.exists()` 与 `read_text()`；注入 50ms
  延迟后事件循环 ticker 为 0，确认存在可复现的阻塞缺口。
- 预览文件存在性检查与读取现统一经 `run_bounded_blocking()`，返回文本和无文件语义不变；定向回归
  **12 passed**，全量门禁 **1775 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 ASR WAV 快速路径 I/O 线程边界

- 静态审查发现 `_convert_to_wav()` 对已是 WAV 的输入仍在事件循环直接执行存在性检查和
  `_read_wav()`；注入 50ms 读取延迟后 ticker 为 0，确认语音输入存在可复现阻塞。
- 输入检查与 16kHz 快速判定现统一经 `run_bounded_blocking()`，SILK/ffmpeg 转码和识别语义不变；
  ASR 定向回归 **13 passed**，全量门禁 **1776 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。生产实例未重启，待人工重启后观察真实语音输入 p95。

## 2026-08-31 CosyVoice 临时音频清理线程边界

- 静态审查发现 CosyVoice 的 `finally` 收口直接调用 `Path.unlink()`；失败/降级时注入 50ms 删除
  延迟可复现事件循环 ticker 停顿。
- 临时 WAV 删除现统一经 `run_bounded_blocking()`，异常收口和 WAV/MP3 降级语义不变；语音 I/O
  定向回归 **4 passed**，全量门禁 **1777 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。生产实例未重启，待语音服务 canary 验证实际 p95。

## 2026-08-31 ASR 转写临时文件清理线程边界

- 复查 ASR 全链路后发现 `VoiceRecognizer.transcribe()` 的 `finally` 仍直接删除转换产生的 WAV；
  延迟注入回归证明该清理会阻塞事件循环。
- 删除操作现经 `run_bounded_blocking()` 收口，取消/异常和文本返回语义不变；ASR 定向回归
  **14 passed**，全量门禁 **1778 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。生产实例未重启，待人工重启后观察真实语音输入链路。

## 2026-08-31 记忆信任存量可观测性

- 数据审计显示 active 记忆 **13,379** 条，其中 **12,066** 条为 `legacy_unverified/unverified`；
  它们按 `trusted_only` 协议不会进入 LLM，但此前健康检查没有展示该存量，容易把“安全降级”误认为
  “记忆质量已完成”。可信 active 记忆 **1,313** 条的证据主体、群作用域和消息类型核验均无异常。
- `_check_memory_truth()` 现在显式报告未验证 active 记忆数量及“已排除自动召回”，不自动提升历史可信度，
  避免无证据记忆再次污染上下文；新增健康检查回归，定向 **17 passed**，全量门禁 **1779 passed，
  3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 运行观察器记忆存量可见性

- 观察器原先只输出 `trusted_memories` 与未知枚举值，无法区分“可信记忆少”与“历史线索被安全排除”；
  生产库实测存在 **12,066** 条 active 未验证记忆，这属于待治理债务而非未知枚举错误。
- 记忆域证据新增 `untrusted_active_memories = active - trusted`，不改变健康判定和业务读路径；观察器
  定向回归 **116 passed**，全量门禁 **1780 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。

## 2026-08-31 GPT‑SoVITS 重启后 canary 与稳定性观察

- 手动重启后的 boot `c3854dcd` 中，GPT‑SoVITS 子进程在 06:06:25 曾以 code=1 退出并自动恢复；
  06:06:57 恢复就绪后，连续 3 次本地 `/tts` 请求均为 **HTTP 200**，耗时 **1.24–1.54s**，
  音频体积 **242–342KB**。
- 06:12:58–06:15:58 的只读观察中 GPT‑SoVITS **4/4 快照在线、0 次重启**；本区间无真实 QQ
  入站，因此全链路结果仍为 `INSUFFICIENT`，不能替代真实对话验收。

## 2026-08-31 定时重启哨兵隔离主动事件

- 生产库取证发现每日 `__RESTART__` 哨兵从不发送 QQ 消息，却先写入主动事件；重启中断后形成
  `PROCESS_RESTARTED_DURING_EVENT/uncertain`，当前已有 **10 条**伪事件并触发启动警告。
- `CronScheduler._fire()` 现于事件租约流程前处理系统哨兵，避免创建事件、调用 LLM 或触碰发送链路；
  普通定时任务的“事件先落盘再发送”语义不变。
- 新增重启哨兵回归测试；调度器定向 **30 passed**，全量门禁 **1781 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。历史伪事件未做破坏性删除，保留供审计。

## 2026-08-31 GPT‑SoVITS 子进程诊断增强

- 06:19:58 的第二次 ready-child 仍以 code=1 退出，stdout/stderr 只到推理进度，未出现 Python
  traceback；父进程和 3001 端口保持在线，Windows Application/System 日志也没有对应崩溃事件，
  因此退出根因仍标记为**未证实**，不把它臆断成代码异常。
- GPT‑SoVITS 启动命令增加 `python -u`，使模型阶段、请求上下文和包装器异常即时进入父进程管道；
  这是诊断增强，不改变 GPU/CPU、权重、参考音频或恢复次数策略。下次人工重启后才能生效。
- 新增无缓冲启动契约测试；全量门禁 **1782 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。当前实例的 9880 已因本次 ready-child 再次退出而离线，
  主程序仍在线，未由本轮自动重启。

## 2026-08-31 知识索引降级证据身份隔离

- 红测模拟持久化索引不可用，并在两个嵌套目录放置同名 `guide.md`；旧回退逻辑按文件 stem
  生成 `document_id`，两个来源得到同一证据身份。
- 结构化检索的回退 ID 现按 `relative_path`（无相对路径时才退回 stem）计算，保留同名文档的
  来源隔离与稳定 chunk ID；不改变正常索引路径和旧文本搜索契约。
- 新增同名文档降级回归；知识证据定向 **7 passed**，全量门禁 **1783 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 06:16-06:46 生产观察收口

- 观察器生成 `runtime-20260831-061631-43760-0f67.json`：7 个快照、23 条群入站；记忆提取
  **2/2 完成**，无失败/死任务，队列到达 **23**、服务 **21**，但观察窗口太短且低频任务多，
  不能据此宣称长期稳定。
- GPT‑SoVITS 在 6 个快照均不可用；主程序 3001 端口仍在线。原始日志只见两个 ServiceMgr
  错误和子进程输出中断，没有 traceback/CUDA 错误，根因继续标记为**未证实**，待下次手动重启
  后用 `python -u` 诊断增强重新取证。
- `糖糖.Handler` 心跳在该 30 分钟窗口只有 1 次，观察器因此标记样本不足；原始日志显示反思
  心跳确实存在，属于周期为 30 分钟、要求 2 样本的观测覆盖不足，不作为 Handler 故障结论。

## 2026-08-31 知识库基线增加延迟证据

- `tools/knowledge_retrieval_eval.py` 现在为每条固定问句记录 `latency_ms`，并汇总样本数、
  p50、p95、最大值；命令行摘要同步显示 p50/p95，便于比较分块、FTS 和 embedding 改造前后
  的真实收益。
- 当前固定集仍为 **10/10**，正例/负例命中率均 **1.0**，结构化检索 hit@1、hit@3、MRR
  均 **1.0**；最近一次冷启动测量 p50 **1.844ms**、p95 **346.438ms**（首轮加载占主要部分）。
  该基线只代表 12 个文档/156 个块，不能外推到千人持续流量。
- 评估工具回归与全量门禁 **1783 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。

## 2026-08-31 自适应分块前的语义对照

- 当前知识库 156 个块中有 **41 个超过 512 字符**、11 个超过 1000 字符；BGE 目前只取每块
  前 450 字编码，因此长块后半段的语义证据尚未覆盖，这是分块实验的明确动机。
- `--semantic` 对照仍为 **10/10**、正负命中率均 **1.0**，但 hit@1 **0.8333**、MRR
  **0.9167**（p50 **7.614ms**、p95 **309.918ms**）；词法/FTS 路径的排序指标更高，后续
  改造必须同时比较召回率、排序质量和尾延迟。
- 本阶段只完成测量，没有替换线上切块策略；下一步先在隔离实验中比较短子块 + overlap 与
  当前 156 块基线，再决定是否升级 schema。

## 2026-08-31 短子块与 overlap 隔离实验

- 在不改线上代码的临时内存索引中，将 156 个现有块切为 450 字子块（223 块），以及 450
  字子块 + 80 字 overlap（229 块），使用同一 BGE 与固定 10 条问句对照。
- 短子块使语义路径 hit@1 **0.8333→1.0**、MRR **0.9167→1.0**；overlap 没有额外提升，
  且 p50/p95 略增。词法路径在“记忆幻觉防护”出现文档排序下降，说明直接替换主块会损伤
  精确召回。
- 结论：不直接把主索引改成短块；下一步设计“原始父块保留词法召回 + 短子块仅供语义向量
  召回 + 命中后回填父块”的 parent-child 方案，并为子向量规划独立持久化元数据。

## 2026-08-31 parent-child 语义子段第一阶段

- `KnowledgeBase` 保留原始父块、FTS 和关键词路径不变；长父块额外生成最多 450 字的内存
  语义子段，按子段最高相似度回投父块，`search`、`search_evidence` 和预热路径统一使用。
- 新增长块尾部语义回归，证明只出现在父块后半段的事实不会再因前 450 字截断而漏召回；
  子段生成失败或维度不符时自动跳过，父块向量仍可用。
- BGE 固定集复测：10/10、hit@1/hit@3/MRR 均 **1.0**；预热耗时单独记录约 **2825ms**，
  查询 p50 **7.912ms**、p95 **392.559ms**。评估脚本现在分离 warm-up 与稳态查询延迟。
- 当前子段仍是进程内缓存，尚未写入 SQLite；下一阶段再设计 schema 迁移和增量持久化，避免
  未经容量/重启验证就扩大生产索引。

## 2026-08-31 语义子段 SQLite 持久化

- 新增 `semantic_segments` 与 `semantic_embeddings` 派生表，沿用父 `chunks` 外键级联；父块
  变更/删除时不会遗留失效子段。主 `schema_version` 保持兼容，另以
  `semantic_schema_version` fail-closed 管理派生表。
- 子段元数据由父块 ID、偏移和内容 hash 生成稳定 `segment_id`；模型指纹和向量维度校验
  与主 embedding 一致，索引不可用或接口缺失时仍回退内存编码。
- 生产知识索引已验证可建立 **114 个语义子段 / 114 个向量**；BGE 固定集仍 10/10、
  hit@1/hit@3/MRR 均 **1.0**。首次生成预热约 **2820ms**，重启复用后预热降至 **20.694ms**，
  稳态 p50 **7.547ms**、p95 **327.496ms**。
- 新增索引 round-trip、陈旧子段清理、跨重启复用和长块尾部召回回归；全量门禁 **1788 passed，
  3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 语义子段增量同步验收

- 运行期探针与回归测试验证：知识文档内容变更会生成全新的父块/子段 ID，旧语义向量由
  外键级联清理，新向量重新持久化；旧 ID 查询为空，孤儿子段和孤儿 embedding 均为 **0**。
- 当前生产索引保持 **12 文档 / 156 父块 / 114 子段 / 114 向量**，未发现残留或交叉引用。
- 全量门禁更新为 **1790 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 两小时生产观察终稿与下一步

- 观察器 `runtime-20260831-061631-43760-0f67.json` 已完成 **2 小时、25 个快照**：QQ/NapCat
  端口持续在线，群入站 **41** 条，未发现回调丢失；记忆提取生命周期 **2/2 完成、0 失败、
  0 死任务**。前台对话、发送、语音和知识工具均无完整样本，相关域保持 `INSUFFICIENT`，不把
  “无样本”误报为功能通过或故障。
- 记忆待处理量 **706→726**，全部是策略延后项；最早消息为 2026-08-10，仍未达到单条 21 天
  准入阈值。当前证据支持“策略等待”而非 worker 停摆；准入点后还需观察实际服务率。
- GPT‑SoVITS 在 **24/24** 个快照不可达，生产依赖与健康检查因此 FAIL；主程序 3001 端口仍在线。
  本区间未取得新 traceback，根因继续标记为未证实，待下一次人工重启后用无缓冲子进程诊断重新取证。
- WeKnora 调研结论纳入后续路线：不整体接入，继续沿当前 `knowledge.py` 演进；优先补齐基线
  评估、持久化证据索引、自适应分块/父子召回、FTS5/CJK 词法与统一检索 trace，再评估轻量
  知识图谱。当前已完成持久化子段与增量清理，下一项是线上检索 trace 与容量压测。
- 离线热路径容量探针（生产知识索引只读）：词法检索 200 次 p50 **1.940ms**、p95
  **2.561ms**、p99 **2.818ms**；BGE 语义检索 100 次 p50 **8.309ms**、p95
  **9.367ms**、p99 **9.885ms**。BGE 加载约 **3282ms**，属于一次性冷启动成本；当前没有
  证据支持继续调整 RRF 权重，后续应在真实 `search_knowledge` 流量中补齐命中率与来源分布。

## 2026-08-31 私聊自动索引绑定与线程边界

- 静态审查发现私聊回复收尾的 `_index_private_message()` 同步执行 BGE 编码、查询最新
  `chat_log` ID 和 SQLite 写入；并发回复可能把向量挂到后来一条消息，慢调用还会占用事件循环。
- 现在复用 16 槽取消安全 Store worker，并传入同一回合 `log_chat()` 返回的 `chat_id`；只有
  旧调用方未提供 ID 时才回退查询最新消息，避免新旧回复交叉绑定。
- 慢夹具回归证明线程执行期间事件循环仍可调度（40ms 编码 + 40ms 查询 + 40ms 写入），且显式
  ID 不触发 latest 查询。定向 **2 passed**，全量门禁 **1792 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。GitNexus 对该超大 Handler 方法解析为 UNKNOWN，
  已以单调用点、线程归属测试和全量回归替代影响证明。

## 2026-08-31 自忆 pending 持久化线程边界

- 慢 I/O 探针证明旧 `_extract_self_memories()` 在在线回复路径直接 `kv_set`，一次 80ms 写入会
  占用事件环约 **93ms**；drain 内 inflight/ack/rollback 也存在同类同步写入。
- 群聊和私聊入口现使用 `_extract_self_memories_async()`，复用取消安全 Store worker；pending
  写入期间若缓冲发生变化会标记 dirty 并安排重试，不能把旧快照误当成最新状态。drain 的所有
  自忆状态写入统一异步化，保留失败回滚与重启恢复协议。
- 新增慢写回归后全量门禁 **1793 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、
  `COMPILEALL=0`。生产实例尚未重启，需人工重启后观察 `self_memory` 慢调用、事件环 tick
  与 pending 在崩溃/重启场景的恢复证据。

## 2026-08-31 自忆 pending 重启恢复回归补强

- 新增“异步 pending 写入后由新 Handler 恢复”回归，验证进程重启边界不会丢失待处理自忆；
  定向自忆测试 **14 passed**。
- 全量门禁最终 **1794 passed，3 warnings**；期间一次 NapCat 恢复用例超时，定向及整文件
  重跑均通过，最终全量复跑通过，暂不将一次性时序波动误判为代码故障。
- `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例仍需人工重启后验证真实 pending 快照、
  self-memory 慢调用和事件环 tick；知识库下一项仍为线上检索 trace 与容量压测。

## 2026-08-31 知识检索 trace 纳入运行观察

- 知识库已有 `Knowledge Retrieval` 摘要日志（hits/sources/paths/latency），但观察器原先只看
  工具 success/empty，无法比较真实召回路径和尾延迟。现已增加无正文 trace 解析、跨增量批次
  合并、p50/p95 与路径分布证据，保持检索排序和 LLM 决策不变。
- 观察器专项 **117 passed**，全量门禁最终 **1795 passed，3 warnings**；
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。
- 下一次人工重启并执行受控 `search_knowledge` canary 后，使用该 trace 统计线上命中/空召回、
  来源分布和延迟；在取得真实样本前不调整 FTS/RRF 权重。

## 2026-08-31 知识库离线基线复测

- 当前 12 文档/156 父块的固定集在词法、语义模式均 **10/10**；正负召回、hit@1、hit@3、MRR
  均 **1.0**。
- 词法 p50/p95/max **1.724/347.210/347.210ms**；语义预热 **24.194ms**，p50/p95/max
  **8.360/306.428/306.428ms**。p95 主要由独立评估首条冷路径贡献，不能当作预热线上稳态，
  下一阶段容量压测必须拆分冷启动/稳态并扩大样本。

## 2026-08-31 知识库只读检索并发锁优化

- 取证显示 `KnowledgeBase` 的全局 RLock 将只读检索串行化：隔离副本 16 线程、3200 次词法
  检索总耗时 **5683ms**、p95 **144.526ms**。改为读写锁后总耗时 **4025ms**、p95 **34.860ms**；
  同时执行 10 次 reload 无异常，读写互斥仍成立。
- 检索使用读锁；reload、首次 embedding 预热和带语义/重排的可能写路径使用写锁，保持状态一致，
  不改变召回排序、证据格式或 LLM 决策。新增读者重叠回归，全量门禁 **1796 passed，3 warnings**。
- 下一步人工重启后，在真实 `search_knowledge` 流量中观察 p95、reload 和索引同步；没有线上数据
  前不继续扩大并发策略或调整 RRF 权重。

## 2026-08-31 GPT-SoVITS 隔离诊断补充

- 隔离端口 `19880` 以生产同款 V4/CUDA 配置启动 `start_api_patched.py`，使用 UTF-8 正式 `/tts` 请求得到
  **200 / 207404 bytes**，完整跑通参考音频、BERT、T2S 和并行合成；进程只在诊断收口时被主动终止。
- 早先诊断中的 400 已定位为 PowerShell 默认 ANSI 编码把中文路径/文本破坏，不能作为模型失败证据。
- 生产 `9880` 的 ready 后 `code=1` 仍没有 traceback，不能据此修改引擎或扩大自动重启；下一步是人工重启后做
  真实文字/语音 canary，并保留 PID、退出码、最后诊断尾部及 Windows 事件，继续区分外部终止与原生运行时退出。
- 隔离端口 `19881` 并发 8 请求全部返回 200，5 秒内进程保持存活；官方 Changelog 也将 V4 并行推理列为
  正式能力并持续修复相关问题。当前没有证据要求关闭 `parallel_infer`，后续应继续收集生产退出时的进程树、
  原生事件和 launcher 尾部，而不是凭猜测回滚并行模式。
- 服务监护日志现在额外记录 GPT‑SoVITS 子进程 PID 与 ready 后生命周期，同时保留原有 `code=...` 格式，
  便于与 Windows 事件和语音请求时间线对齐；定向 **16 passed**，全量门禁 **1796 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。该改动只增强取证，不改变恢复预算或停止语义。
- 生命周期时间戳已绑定到具体 `Process`，避免旧 drain 与新一代 supervisor 重启竞态串读；定向 **17 passed**，
  全量门禁最终 **1797 passed，3 warnings**，静态检查与编译通过。
- 运行观察器已消费这组退出元数据：同一进程的 supervisor/drain 双日志按 `boot+pid+code+lifetime` 去重，
  本地依赖证据展示退出次数、退出码分布、最长生命周期和最近一次退出；不落原始输出或用户文本。
  观察器专项 **118 passed**，全量门禁 **1798 passed，3 warnings**，静态检查与编译通过。

## 2026-08-31 自忆诊断工具作用域纠偏

- 只读诊断曾因省略 `target_qq`，把 bot 全局自忆池打印成“每条消息都会注入”，造成跨用户串线假象。
- 复核生产链路确认 `_get_self_memory_context → MemorySystem.recall → Store.query_memories` 已绑定对象、可信证据和
  群作用域；修正脚本后主人样本为 10 条、无关用户为 0 条，并显示当前作用域。
- 该项是工具误报修复，不涉及生产记忆清理；以后审查自忆必须同时核对诊断筛选协议与实际 Handler 注入路径。

## 2026-08-31 自忆数据库隔离复核

- 只读 SQLite 聚合确认：`origin=self` 共 3703 条，但无目标 active 为 **0**；其中 3503 条无目标历史记录均已
  `retracted`，有目标 active 为 **199**，active 且无 `evidence_ids` 为 **0**。
- 启动迁移保留撤回行用于审计，不删除历史，避免把“历史总量”误当成“当前可注入记忆”。后续以
  `target_qq + evidence_ids` 为新写入门槛并监控违规计数。
- 修正后的诊断脚本前后 `memories.recall_count` 总和均为 34264（Δ=0），只读性得到运行时探针确认。

## 2026-08-31 GPT‑SoVITS 系统事件交叉核验

- 围绕 06:06–06:20 退出窗口检查 Windows Application/System 日志，未发现 Application Error、Python/VCRUNTIME、
  CUDA/显示驱动、WHEA 或 Kernel-Power 事件；仅见无关的 Defender `MpTelemetry/ValidateUpdate`。
- 该结果排除“已有系统崩溃事件可直接定因”，下一次人工重启后继续依靠新 PID/生命周期日志、进程树和 launcher 尾部取证，
  不凭空回滚 V4 或并行推理。

## 2026-08-31 记忆提取积压时效观测

- 09:55 生产快照有 723 条/185 人前向积压，最早 08-10；当前准入策略计算到期 0、全部 deferred，自治周期持续
  `open=0 pending=0`。策略本身按设计等待低频消息满 21 天，不判为实现 bug。
- 但这给低频记忆带来明确的 21 天时效上限。下一阶段以真实 LLM 成本、提取收益和 backlog 老化曲线为证据，评估
  廉价批处理或缩短阈值；在数据不足前不直接改门槛。

## 2026-08-31 开源记忆系统借鉴补充

- Mem0 的抽取/更新分离、异步摘要和 ADD/UPDATE/DELETE/NOOP 操作，为当前证据化记忆冲突处理提供参考：
  <https://arxiv.org/html/2504.19413v1>。
- Letta 的消息缓冲、核心记忆块、召回记忆、归档记忆与 sleep-time 后台整理，验证了“在线对话与记忆维护解耦”的方向：
  <https://www.letta.com/blog/agent-memory/>。
- 当前不引入外部 Agent 或图数据库；先基于真实 backlog 老化、LLM 成本、冲突率和召回质量，设计低频后台整理与分层预算的最小变更。

## 2026-08-31 驱动力与自忆一致性待办

- `.tangtang_self.json` 显示承诺/信息/表达/好奇心等多个驱动力长期约 **0.998**，关系场却没有任何 `unfinished`；日志连续多日
  每小时报告承诺压力接近 1.0，内部消化只能短暂降到约 0.94。
- 静态调用检查发现 `release_by_action("reply_commitment")` 没有生产调用方，自忆 `promise` 与 `action_completed` 也未建立完成关联，
  这是潜在的“驱动力饱和→错误状态注入/自治噪声”共因。下一阶段先设计带证据的 promise↔completion 结算协议，再修动力学；禁止手工清空现有状态。

## 2026-08-31 体检知识库容量口径修复

- 体检脚本原先用全目录 `*.md` 数量作为知识库规模，实际会把敏感目录计入；当前同一目录为 16 个 md，生产可索引集合为 12 个。
- `tools/体检.py::check_resources` 现复用 `agent.knowledge.discover_document_files()`，输出“可索引文档”，避免容量与安全边界不一致。
- 新增回归覆盖敏感目录过滤；全量门禁 **1799 passed，3 warnings**，静态检查与编译通过。

## 2026-08-31 自忆证据硬门禁与承诺结算

- `remember_self` 现在要求 evidence ID 在 `chat_log` 中真实存在，且必须属于同一目标、同一群/私聊作用域的糖糖已发送回复；
  不再接受“非空编号但无原文”的自忆。
- 自忆提取会向 LLM 展示同作用域的 active promise 候选；只有明确返回候选 `promise_id` 的 `action_completed` 才能结算，
  结算写入 `fulfilled` 和 `superseded_by=completion_id`，历史可审计，active 查询自动排除已履行承诺。
- 跨用户/跨群/未知承诺 ID 和无完成证据均 fail-closed；现有生产 promise 不自动清理，待真实 canary 后再评估历史结算。
- 定向自忆测试 13 passed；全量门禁 **1802 passed，3 warnings**，静态检查与编译通过。下一步仍需人工重启后观察真实提取与结算日志。

## 2026-08-31 自忆完成事实增加动作回执锚点

- 静态取证发现：此前 `action_completed` 只校验糖糖回复聊天记录，普通文字中的“已经查完/已经发了”仍可能被 LLM 当作完成事实。
- 现将 `confirmed_action_facts` 作为第二硬锚点：仅发送链明确确认、同对象同群/私聊且 `self_memory_eligible=1` 的动作，才允许写入
  `action_completed`；普通文字回复仍可记录 `said/promise`，但不能伪装成外部动作已完成。
- 语音动作 envelope 已显式标记为可用于自忆，并把 confirmed action ID 随 pending 一起持久化；旧 pending 缺少该字段时安全降级为不可结算。
- 定向记忆/动作投影回归 **53 passed**；生产实例尚未重启，需人工重启后验证真实语音回执、pending 恢复和 action_completed 过滤。

## 2026-08-31 旧完成事实安全隔离

- 继续审查发现：生产库已有 **21** 条 active `action_completed`，但当前仅有 1 条 confirmed action fact 且
  `self_memory_eligible=0`，旧完成事实没有可关联的动作回执 ID。
- 新增一次性迁移 `20260831_self_action_receipt_v1`：保留原始记忆和证据用于审计，仅将这类旧行降为
  `legacy_unverified`，使 `trusted_only` 自忆召回 fail-closed；不删除、不把未证实内容改写成“错误事实”。
- `MemorySystem.remember_self()` 也增加同一硬门禁，避免任何旁路重新写入无回执的 `action_completed`。
- 新增迁移/旁路拒绝回归；全量门禁 **1805 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 自忆提取提示协议补强

- 批次上下文现在显式标注每条回复是否带 `confirmed_action`，并要求 LLM 仅在标记为“是”的来源考虑
  `action_completed`；系统硬门禁仍保留，提示层只负责减少无效分类和 token 浪费。
- 定向自忆测试 **24 passed**；全量门禁保持 **1805 passed，3 warnings**；GitNexus 刷新至 34,768 nodes /
  72,625 edges / 300 flows。

## 2026-08-31 知识库语义检索读锁并发优化

- 取证确认：embedding 已预热时，`search/search_evidence` 仍因引擎 ready 无条件持有写锁，语义检索被串行化。
- 将懒加载/持久化向量准备收口到短写锁，评分、RRF、格式化阶段改用读锁；reranker 仍保留写锁以维持未知模型线程安全边界，召回排序不变。
- 隔离测量（32 并发、每次模拟 50ms 评分）：旧写锁 **2013.7ms**，新读锁 **125.4ms**，约 16 倍吞吐改善；新增语义读者重叠回归。
- 知识库定向 **40 passed**；全量门禁 **1806 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus 最新 34,781 nodes /
  72,639 edges / 300 flows。生产实例尚未重启，需线上 trace 验证真实 p95。

## 2026-08-31 知识库读锁懒加载边界补强

- 复核发现无指纹 embedding 引擎会在每次检索懒加载；若保留旧的内部 ensure 调用，可能在读锁内重新编码并写缓存。
- 已将三条内部搜索路径的向量准备移除，统一由 `search/search_evidence` 的短写锁阶段完成；读锁阶段只做评分和格式化，避免读写竞态。
- 知识库定向 **58 passed**；全量门禁 **1806 passed，3 warnings**；GitNexus 最新 34,782 nodes / 72,635 edges / 300 flows。

## 2026-08-31 知识索引版本时间语义修复

- WeKnora 对照审计发现 `KnowledgeIndex.sync()` 每次同步都会覆盖 `documents.updated_at`，即使文档 hash 未变化，导致版本时间失真；
  这会削弱来源时间核验和后续 diff/回滚基础。
- 现改为：新增文档或内容 hash 变化才更新时间；未变化文档以及仅修复 chunk/FTS 的同步保留原时间。生产数据库未直接修改，
  下次重启/索引同步后自然生效。
- 新增“重复同步保留时间”和“内容变更更新时间”回归；知识索引定向 **20 passed**，全量门禁 **1808 passed，3 warnings**，
  `PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus 刷新至 34,787 nodes / 72,650 edges / 300 flows。

## 2026-08-31 GPT-SoVITS 正常返回终止信号纳入诊断

- 生产日志显示 GPT-SoVITS 多次在就绪后以 code=1 退出，stderr 只有警告/进度，当前仍无法区分正常 API 返回、外部终止或原生崩溃；
  Windows 同时段事件仅为无关的 Defender 更新失败。
- `start_api_patched.py` 已定义“API returned unexpectedly without an exception”终止信号，但 ServiceManager 原先不会把它提升到优先诊断队列，
  进度刷屏时可能丢失。现补入诊断分类器，不改变重启策略。
- 新增诊断信号回归；定向服务测试 **22 passed**，全量门禁 **1809 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；
  GitNexus 刷新至 34,796 nodes / 72,662 edges / 300 flows。真实根因仍待下一次人工重启取得新 stderr/生命周期证据。

## 2026-08-31 记忆积压准入复核

- 12:14 只读 SQLite 快照：前向积压 **733 条 / 190 人**，最早未处理消息为 **2026-08-10 13:54:45**，距 21 天单条准入阈值尚差约 1.1 小时；
  按当前策略 190 人均为 deferred，没有已到期用户。
- 该结果与观察器的 `open=0/pending=0` 一致，未发现游标或时间解析错误；暂不降低 21 天门槛，待跨阈值后观察一次真实准入、LLM 成本和队列收敛速度，再决定是否引入低成本低频整理。

## 2026-08-31 秒级提醒契约收口

- 取证发现 `set_reminder` 代码接受 `30秒`，但 `tasks.remind_at` 只有分钟级精度，旧逻辑会把 30 秒静默变成 1 分钟；工具 schema 也只声明分钟/绝对时间。
- 现对秒级输入 fail-closed，明确返回“暂不支持秒级提醒”，要求改用分钟或 `HH:MM`，不改变底层任务表和已有分钟提醒行为。
- 新增无静默舍入回归；任务动作定向 **29 passed**，全量门禁 **1810 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；
  GitNexus 刷新至 34,801 nodes / 72,668 edges / 300 flows。

## 2026-08-31 WeKnora 对照：评估冷/稳态延迟分层

- 用户提供的 WeKnora 分析与当前实现复核一致：持久化索引、稳定证据元数据、CJK FTS5、Dense+RRF、父块/语义子段和检索 trace 已存在；不整体引入 WeKnora。
- `tools/knowledge_retrieval_eval.py` 现同时输出总延迟、首条冷路径和后续稳态延迟，避免 jieba/SQLite/懒加载的一次尖峰污染线上 p95 判断；保持原有指标兼容。
- 当前固定集已扩展为 **19/19**（12 文档/156 父块）；本次实测总延迟 p50/p95 **1.827/349.396ms**。下一步应扩充多来源、长文和版本变更样本，再决定自适应分块/overlap 与版本历史，不凭 WeKnora 能力清单直接改检索器。

## 2026-08-31 知识库泛词文件名误召回收口

- 新增近似负例“完全不存在的随机天气系统”后复现：关键词“系统”仅命中文件名即可获得 2 分，绕过正文证据门槛并返回无关文档。
- 将“系统”纳入知识检索停用词；保留具体文件名召回（如 `minecraft`）和正文/标题命中规则，不改变 RRF 或 LLM 决策。
- 知识库回归 **47 passed**；固定评估集扩展为 **11/11**，该负例现在稳定空召回。后续继续补多来源、长文和版本变更样本。

## 2026-08-31 知识库长概念召回与实时问句边界

- 评估扩展发现“主要矛盾怎么用”被单正文词门槛误杀；对长度≥4的正文概念给予 2 分，短正文词仍保持 1 分，避免重新放开“启动器”类噪声。
- 该放宽同时暴露“今天天气怎么样”命中知识文档中的示例句；将“今天天气”纳入实时问句停用词，静态知识库不抢答实时天气工具。
- 固定集现为 **19/19**，正例命中率、负例空召回率、hit@1、hit@3、MRR 均 **1.0**；后续继续补更多工具型实时问句和近义误召回。

## 2026-08-31 send_stickers 阻塞事件循环收口

- 阻塞探针确认：`send_stickers` 的语义匹配会同步调用贴图 `embed_engine.encode()`；120ms 模拟工作下工具回合期间事件循环只 tick 1 次。
- 现改用已有 `run_bounded_blocking`（独立于 SQLite 配额，取消时等待线程收口），不改变贴图排序、动作意图顺序或 LLM 决策。
- 贴图/角色/回复管线定向 **55 passed**；下一步在人工重启后的真实贴图工具 trace 中观察匹配耗时和线程池排队。

## 2026-08-31 记忆/聊天语义查询向量编码线程化

- 前台工具审查发现 `search_chat_history`（私聊）和 `search_memories` 在调用 SQLite 前仍直接执行 BGE `encode()`；慢编码会阻塞同一事件循环。
- 新增 80ms 慢编码探针后，修复前心跳仅 tick **2–5** 次；现统一使用 `run_bounded_blocking`，与贴图语义查询共享有界 CPU 工作门，取消时等待线程收口。
- 历史/记忆相关回归 **45 passed**；下一步人工重启后观察真实 BGE 编码耗时、排队和语义查询尾延迟。

## 2026-08-31 窗口过期摘要线程化

- 取证确认：群消息处理调用 `get_window_bonus()` 时，窗口过期会同步对最多 20 条消息执行 BGE 编码；20ms/条探针下事件循环心跳为 **0**，存在消息处理停顿风险。
- 新增异步窗口门槛接口：先在事件循环摘除过期窗口快照，再用有界阻塞门计算摘要，最后回到事件循环提交关系状态；群消息入口已切换，摘要内容与窗口阈值不变。
- 新增非阻塞回归；相关定向测试 **90 passed**，全量门禁 **1818 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例尚未重启，需在线上观察窗口过期时的摘要耗时与排队。

## 2026-08-31 清理过期窗口摘要后台化

- 继续追踪发现：`force_engage/on_reply_sent → _cleanup()` 也会在消息事件循环中同步处理多个过期窗口；多窗口慢编码探针可将清理时间线性放大。
- `_cleanup()` 现在先摘除过期窗口；运行在事件循环时将快照排入后台有界摘要队列，线程完成后再提交关系状态；无事件循环的离线/兼容调用仍保持同步收口。
- 新增多过期窗口非阻塞回归；窗口/记忆/贴图/历史定向 **91 passed**，全量门禁 **1819 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。GitNexus 刷新至 **34,848 nodes / 72,765 edges / 300 flows**。生产实例尚未重启，需线上观察队列排空与摘要延迟。

## 2026-08-31 过期窗口摘要优雅退出收口

- 后台摘要队列新增显式 `flush_pending_summaries()`，优雅关机时先等待已排队摘要完成，再保存关系状态，避免异步化后因退出丢失尾部摘要。
- 新增退出 flush 回归；全量门禁 **1820 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例尚未重启，仍需线上验证队列 drain 与关机顺序日志。

## 2026-08-31 插话语义去重向量编码线程化

- 前台入口审查发现主动插话的语义去重仍直接调用 `embed_engine.encode()`；这是每条候选消息进入 LLM 前的同步模型工作。
- 现统一通过 `run_bounded_blocking` 执行，保留相似度阈值、按用户隔离和十分钟缓存语义不变；新增 AST 契约测试防止回退。
- 前台入口定向 **12 passed**；全量门禁 **1821 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。GitNexus 刷新至 **34,851 nodes / 72,768 edges / 300 flows**，生产实例尚未重启。

## 2026-08-31 durable 窗口增量读取线程化

- 复核窗口异步入口发现：`_sync_durable_events()` 内的 SQLite 增量读取仍同步执行；80ms 慢读探针下事件循环心跳降为 **0**。
- 新增 `_sync_durable_events_async()`：仅把 Store 读取交给 `run_bounded_store_io`，事件应用、游标推进和状态语义仍在主循环完成；群窗口异步门槛已切换。
- 新增慢读非阻塞回归；全量门禁 **1822 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。GitNexus 刷新至 **34,866 nodes / 72,797 edges / 300 flows**，生产实例尚未重启。

## 2026-08-31 durable 读穿回合级去重

- 同一 NapCat scope worker 会连续处理多条消息；异步 durable 同步完成后，后续窗口状态读取复用本回合快照，不再重复查询 SQLite；下一条消息会重新异步刷新。
- 新增调用次数回归；全量门禁 **1823 passed，3 warnings**，`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例尚未重启，需线上观察游标推进和读延迟。

## 2026-08-31 记忆提取 embedding 线程化

- 记忆审查发现事实簇提取、画像合成、簇合并和摘要重建虽声明为 async，内部仍直接执行 BGE 编码；持续流量下会冻结主事件循环。
- 新增记忆层统一 `_encode_embedding()`，所有上述异步路径通过有界阻塞门执行；证据校验、簇匹配、写入顺序和失败语义不变。
- 记忆相关定向 **68 passed**；全量门禁 **1824 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。GitNexus 刷新至 **34,878 nodes / 72,822 edges / 300 flows**，生产实例尚未重启。

## 2026-08-31 自我状态周期保存非阻塞化

- 事件循环探针确认：1000 个关系对象触发周期保存时，旧的同步 `_save()` 会在消息热路径内复制并序列化完整状态，心跳几乎不推进。
- 现将周期/自动保存改为合并后的 `save_async` 后台任务：关系快照每 50 人主动让出事件循环，JSON 序列化与原子替换经有界阻塞门执行；无事件循环的离线调用仍保留同步语义。
- 优雅停机先等待 `flush_pending_save()`，再执行同步保存兜底，避免异步化引入尾部状态丢失；新增事件循环与停机时序回归。
- 自我状态/停机定向 **6 passed**；全量门禁 **1826 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,892 nodes / 72,849 edges / 300 flows**。生产实例尚未重启，需人工重启后观察实际保存耗时与退出日志。

## 2026-08-31 敏感知识参考读取非阻塞化

- 慢读探针确认：已授权私聊加载“色色参考”时，8 个文件的同步枚举/stat/读取约 **250ms**，会把消息事件循环完全卡住。
- 新增 `_seductive_knowledge_async()`，通过有界阻塞门执行原有加载器，并以异步锁合并并发读取；群/私聊场景改为 await，公共知识库过滤和缓存内容不变。
- 新增慢读心跳回归；知识边界定向 **6 passed**，全量门禁 **1827 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,904 nodes / 72,864 edges / 300 flows**。生产实例尚未重启，需线上观察授权场景的读取尾延迟。

## 2026-08-31 群聊关系辅助检查 Store 线程化

- 慢 Store 探针确认：群消息进入 LLM 前的每日首次互动检查直接访问 SQLite，单次模拟耗时约 **266ms**，会阻塞消息事件循环；纪念日检查走同一同步路径。
- 现将每日加成和纪念日检查统一经 `_run_store_io` 调度，保留原同步函数供离线/兼容调用；亲密度更新与纪念日判定语义不变。
- 新增前台 AST 契约回归；前台定向 **13 passed**，全量门禁 **1828 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,907 nodes / 72,867 edges / 300 flows**。生产实例尚未重启，需线上观察 Store 排队与群消息 p95。

## 2026-08-31 发送 outbox Store I/O 非阻塞化

- 慢 Store 探针确认：`process_send_outbox` 的待发送列表读取可让事件循环停顿约 **87ms**；重试、确认修复和失败落账路径还有同类同步 SQLite 调用。
- 现将 outbox 的读取、claim、settle、confirmed 修复和异步发送入队统一交给 `run_bounded_store_io`；保留同步兼容入口与“已确认不可重放”语义。
- 新增慢读与媒体校验心跳回归；outbox 定向 **24 passed**，全量门禁 **1830 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,923 nodes / 72,900 edges / 300 flows**。生产实例尚未重启，需线上观察 outbox 排队、重试 p95 与确认修复日志。

## 2026-08-31 入站 inbox Store 并发门控

- 入站事件登记/claim/执行标记/完成/失败回写原先虽使用线程，但未纳入共享 Store 并发门；持续流量可能挤满默认线程池，放大消息排队。
- 现统一经 `run_bounded_store_io`，保留单 scope FIFO、持久去重和崩溃恢复语义；新增慢登记心跳回归。
- 入站/outbox 定向 **41 passed**，全量门禁 **1831 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,932 nodes / 72,914 edges / 300 flows**。生产实例尚未重启，需线上观察 inbox 排队和回写耗时。

## 2026-08-31 记忆上下文阻塞任务有界化

- 高频 LLM 前置路径中的自我记忆召回、BGE 语义排序和紧凑上下文格式化原先使用裸 `asyncio.to_thread`，会绕过统一 CPU 工作门。
- 现统一通过 `run_bounded_blocking`，保留用户/群作用域过滤、证据门槛、排序和格式化内容；新增自忆召回门控契约测试。
- 记忆相关定向 **55 passed**，全量门禁 **1832 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,937 nodes / 72,919 edges / 300 flows**。生产实例尚未重启，需线上观察 BGE 排队与 LLM 首 token 延迟。

## 2026-08-31 工具侧代发私信记忆读取有界化

- `send_private_message` 工具路径读取目标用户记忆原先使用裸 `asyncio.to_thread`，绕过统一 CPU 工作门。
- 现改用 `run_bounded_blocking`，保留授权、作用域、记忆格式和发送确认语义；代发私信不再与前台 BGE 任务无限争抢默认线程池。
- 代发/记忆定向 **48 passed**，全量门禁 **1832 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,939 nodes / 72,921 edges / 300 flows**。生产实例尚未重启，需线上观察工具回合尾延迟。

## 2026-08-31 群聊上下文记忆召回门控

- 群消息主链路中的记忆召回原先使用裸 `asyncio.to_thread`，用户最近发言读取也未显式经过 Store 门控。
- 现将混合记忆召回纳入有界 CPU 门，用户历史读取纳入 `_run_store_io`；保留群作用域、优先用户、召回排序和上下文拼接语义。
- 新增前台契约回归；前台/记忆定向 **42 passed**，全量门禁 **1833 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,943 nodes / 72,926 edges / 300 flows**。生产实例尚未重启，需线上观察 BGE 排队与群消息首 token 延迟。

## 2026-08-31 私聊上下文记忆召回门控

- 私聊主链路中的 `memory.recall` 遗留裸 `asyncio.to_thread`，与群聊路径存在不一致的并发治理。
- 现改用 `run_bounded_blocking`，保留私聊主体绑定、可信证据过滤、作用域和排序语义；新增私聊前台契约回归。
- 前台/记忆定向 **43 passed**，全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,946 nodes / 72,929 edges / 300 flows**。生产实例尚未重启，需线上观察私聊 BGE 排队与首 token 延迟。

## 2026-08-31 自治提取 embedding 回填有界化

- 自治提取 worker 的批量 embedding 回填原先使用裸 `asyncio.to_thread`，后台高负载时会绕过 CPU 工作门。
- 现改用 `run_bounded_blocking`，保留批量大小、失败退避、队列状态和持久化语义；提取/健康定向 **44 passed**。
- 全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,948 nodes / 72,932 edges / 300 flows**。生产实例尚未重启，需线上观察提取回填排队和前台 BGE 延迟。

## 2026-08-31 角色贴图语义预热有界化

- 启动批量预热和运行时角色切换的 `warm_semantic_cache` 原先使用裸 `asyncio.to_thread`，可能与前台 BGE 任务争抢默认线程池。
- 现统一使用 `run_bounded_blocking`，保留角色顺序、增量缓存和切换语义；贴图定向 **10 passed**。
- 全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,950 nodes / 72,934 edges / 300 flows**。生产实例尚未重启，需线上观察预热排队和角色切换延迟。

## 2026-08-31 启动记忆回填与 BGE 加载有界化

- 启动时的缺失 embedding 回填与 BGE 加载原先使用裸 `asyncio.to_thread`，可能绕过统一 CPU 背压。
- 现改用 `run_bounded_blocking`，保留启动顺序、ready 门、批量大小和失败降级语义；提取/贴图定向 **18 passed**。
- 全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,952 nodes / 72,936 edges / 300 flows**。生产实例尚未重启，需线上观察启动耗时与前台首 token 延迟。

## 2026-08-31 识图后端调用有界化

- 识图路由的本地/远程描述函数原先使用裸 `asyncio.to_thread`，图片流量升高时可能绕过 CPU/线程池背压。
- 现统一使用 `run_bounded_blocking`，保留 GIF 分支、缓存、后端兜底和失败语义；识图/可观测定向 **18 passed**。
- 全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`；GitNexus **34,954 nodes / 72,938 edges / 300 flows**。生产实例尚未重启，需线上观察识图排队与图片消息延迟。

## 2026-08-31 后台生日/反思旁路收口

- 生产日志取证发现 inbox Store 慢调用 **276.7–655.5ms**，且 `queue_wait_ms=0`，指向 SQLite 锁/事务本身，而非线程池排队。
- 生日查询和反思游标写入原有同步 I/O 旁路；现分别接入 `run_bounded_store_io` 与 `run_bounded_blocking`，不改变业务语义。
- 定向 **2 passed**；全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。下一步定位 SQLite 大事务/锁持有者。

## 2026-08-31 每日播报统计旁路收口

- 每日播报统计会同时读取全局统计、反馈并可能写入消费时间，原先整体裸 `asyncio.to_thread`。
- 现改用 `run_bounded_store_io`，纳入 SQLite 单写者/并发背压；播报内容和失败降级不变。
- 定向 **35 passed**；全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。

## 2026-08-31 SQLite 入站阶段耗时观测

- 为 `register_inbound_event`、`claim_inbound_event`、`insert_chat` 增加慢调用阶段日志，区分 SQL 写入与 commit/fsync；仅超过 250ms 告警，不改变事务边界。
- 定向存储/入站 **52 passed**；全量门禁 **1834 passed，3 warnings**；生产实例未重启。待下一轮真实流量收集阶段分布后再决定数据库参数或事务优化。

## 2026-08-31 每日播报状态文件异步化

- 播报发送前后的 `sending/confirmed/uncertain` 状态写入原先在事件循环内同步执行。
- 新增异步有界文件写入，日期翻页和播报状态变更不再直接阻塞事件循环；同步 `stop()` 兼容入口保留。
- 每日播报定向 **35 passed**；全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。

## 2026-08-31 QQ SILK 语音解码有界化

- SILK v3 解码原先使用裸 `asyncio.to_thread`，语音输入洪峰时可能绕过统一 CPU 背压。
- 现改用 `run_bounded_blocking("asr.silk_to_wav", ...)`，保留临时文件、失败清理和识别流程语义。
- ASR 定向 **14 passed**；全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。

## 2026-08-31 ASR 模型首次加载有界化

- 首次语音输入触发的 `_load_recognizer()` 原先在异步初始化函数内同步执行，可能阻塞事件循环。
- 现接入 `run_bounded_blocking`，保留模型准备、失败降级和 ready 状态语义。
- ASR 定向 **14 passed**；全量门禁 **1834 passed，3 warnings**；生产实例未重启。

## 2026-08-31 DiffSinger 歌声链路有界化

- DiffSinger 服务准备和歌声合成原先使用裸 `asyncio.to_thread`，可能在点歌高峰绕过 CPU 背压。
- 现统一接入 `run_bounded_blocking`，保留 MiniEngine 启动、REST 合成和失败语义。
- 全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。

## 2026-08-31 Reranker 启动加载有界化

- Reranker 模型加载原先以裸 `asyncio.to_thread` 启动，可能与 BGE/ASR/识图初始化争抢默认线程池。
- 现统一接入 `run_bounded_blocking`，保留后台启动、失败降级和精排可用性语义。
- 前台/记忆定向 **43 passed**；全量门禁 **1834 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。

## 2026-08-31 ASR 模型下载/解压有界化

- 缺失 ASR 模型时的下载与解压原先使用裸 `asyncio.to_thread`，可能长时间占用默认线程池。
- 现接入 `run_bounded_blocking`，保留下载、解压、失败清理和模型就绪语义。
- ASR 定向 **14 passed**；全量门禁 **1834 passed，3 warnings**；生产实例未重启。

## 2026-08-31 ASR 初始化回归覆盖

- 新增延迟模型加载测试，验证 `_load_recognizer()` 在线程池运行且事件循环 ticker 不被阻塞。
- ASR 定向 **15 passed**；全量门禁 **1836 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 回复 @解析异步化

- 普通群聊、私聊、好友问候、延迟发送和自然动作路径的 `_enrich_reply` 改为异步 `enrich_async`。
- 贴图匹配进入有界阻塞门，群聊 @昵称解析进入 Store 有界门；同步 `enrich` 保留给旧调度器兼容接口。
- 回复/前台定向 **40 passed**；全量门禁 **1837 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。生产实例未重启。
- GitNexus 重建：35,010 nodes / 73,019 edges / 1,390 clusters / 300 flows。

## 2026-08-31 每日播报状态写入回归覆盖

- 新增回归测试，使用人为延迟验证 `_save_state_async` 不阻塞事件循环且确实在线程池执行。
- 测试基线更新为 **1835 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 生产 SQLite/记忆真值复核

- 运行日志确认 SQLite 尾延迟仍有 **280–826ms**，但隔离副本提交仅毫秒级；暂不凭此切换同步级别。
- active legacy_unverified 记忆约 **1.2 万条**，无证据链，继续 trusted-only 排除；后续设计可回溯重验证/归档流程。
- 下一步：重启后收集提交阶段分布，并验证跨事件循环单写者覆盖，再决定数据库参数优化。

## 2026-08-31 记忆真值只读审计器

- 新增 `tools/memory_truth_audit.py`，只读核验 legacy 记忆的 evidence ID 与原始聊天行，输出可回溯候选 JSON。
- 实测 12,087 条 legacy 中仅 24 条有证据、3 条可核验；禁止批量提升信任等级，后续按候选逐条受控复核。
- 全量门禁 **1838 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 六分钟生产观测

- 3 个快照/360 秒：数据库、NapCat、GPT‑SoVITS 在线，无 ERROR、发送失败、死任务或指标回退。
- 记忆提取完成 1 个完整任务（created→admitted→LLM succeeded→completed）；积压 744 条全部 deferred，不能视为 worker 故障。
- 对话、发送、语音、贴图、识图和知识工具本区间无样本，保持 `INSUFFICIENT`，等待真实流量验证。
- 增量日志发现 19:01 一次入站持久化耗时 **1387ms**，其中写门排队 **688ms**；后续优先评估入站写合并/降频，避免用提高并发掩盖 SQLite 单写者瓶颈。
- 关联观察：`register/claim/persist/mark_executing` 在同一窗口均出现 594–723ms；候选优化为合并入站幂等登记与领取事务，先完成影响分析和基准再实施。

## 2026-08-31 入站领取/执行合并事务

- `received → executing` 与 attempts+1 合并为单事务，旧替身自动回退原两步路径。
- 隔离副本 FULL synchronous 基准：median **3.11ms → 1.36ms**（约降低 56%，仅代表提交阶段）。
- 定向入站 **23 passed**；全量门禁 **1839 passed，3 warnings**；GitNexus **35,038 nodes / 73,052 edges / 1,393 clusters / 300 flows**。
- 已检测到新 boot `3c79191a`，代码已加载；截至 19:29 尚无普通入站样本，线上收益待真实消息验证。
- 19:30 后至少 2 条群消息以 `attempts=1,status=processed` 完成；新路径未触发 ≥250ms 慢告警，确认已在线执行，但精确成功耗时仍未记录。

## 2026-08-31 入站合并事务延迟分桶

- 合并领取路径新增 5/10/25/50/100/>100ms 延迟桶和样本计数，仍走内存指标缓冲，不增加高频日志。
- 新增 worker 回归验证；全量门禁 **1840 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。
- 下一次运行观察器可据桶计数估算真实 p50/p95；当前实例无需再次重启，已运行的新 boot 会采集后续样本。

## 2026-08-31 记忆真值审计器只读连接加固

- 审计器连接改为 SQLite `mode=ro`，从连接层阻断误写生产数据库的可能。
- 新增只读 URI 回归测试；全量门禁 **1841 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 自我状态异步保存测试去竞态

- 取证确认失败来自固定 30ms 断言早于后台保存完成，并非保存任务丢失。
- 测试改为等待 `flush_pending_save()` 后再验证文件，同时保留事件循环心跳断言。
- 全量门禁 **1842 passed，3 warnings**；`PYFLAKES_UNDEFINED=0`、`COMPILEALL=0`。

## 2026-08-31 首个生产延迟样本

- 新 boot `09adcc03` 处理的入站消息已完成，指标 flush 后出现 `samples=1`、`≤25ms=1`。
- 证明新合并事务和延迟埋点已在线生效；样本量仅 1，暂不据此宣称 p50/p95 达标，继续累积真实流量。
