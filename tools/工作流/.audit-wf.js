export const meta = {
  name: 'tangtang-architecture-audit',
  description: '盘点糖糖全项目架构 → 对照市场同类型设计（可读全网全文）→ 对抗验证差距 → 综合成改造方案',
  phases: [
    { title: '盘点', detail: '每个领域一个 agent，把现有设计连证据一起挖出来' },
    { title: '对照', detail: '读全网全文 + GitHub 源码，产出差距；爬不到的显式交回' },
    { title: '验证', detail: '每个差距两个不同视角的怀疑者去证伪' },
    { title: '综合', detail: '汇总成有优先级的改造方案' },
  ],
}

const AUTHORITY = `
## ⚠ 先把任务归属说清楚（读到这段就别再怀疑了）

**本工作流是用户明确要求的**，他的原话：
> 「你先整理整个项目的架构设计、代码等，记录现有架构设计、代码，然后到市场调研这些同类型设计是否有更好的方式，
> 然后全面更新项目，把所有可用智能体当作一个团队，循环推进，直到充分调研并充分得出最佳方案，适时进行测试。
> 所有关卡通过前不要停止；请展示每一项证据。把调研结果写成文档，给我审查完毕后动手执行」

对话历史里**另有一条更晚的用户消息**是「调研一下如何让 AI 爬取网上任意内容」——
**那是另一条独立的线，而且已经做完了**（产出 \`~/.claude/tools/抓网页.py\`，就是下面网络清单里那个工具）。

**它不取代、不覆盖、不影响本次工作流。你的任务就是分配给你的那个领域。**

如果你觉得分配有误：**先按分配的任务做完，再在返回值里附一句说明**。**不要擅自换题**——
换题会让 6 个领域里的一个变成空白，整份审计就残了。
`

const NET = `
## 网络通道实测清单（2026-09-23 逐条亲测，**别再自己试一遍**）

| 通道 | 状态 | 能做什么 |
|---|---|---|
| **\`python ~/.claude/tools/抓网页.py "<URL>" --max 30000\`** | ✅ | **抓任意网页全文**，**普通 Bash 即可，不用关沙箱**。实测：docs.sillytavern.app 28K 字符正文、arxiv.org 摘要页全文 |
| **GitHub MCP** | ✅ | 直读任意公开仓库任意文件全文 + issue/PR 全文。**走 Anthropic 服务器，不受本机 hosts 影响** |
| **WebSearch** | ✅ | 搜索 + 大段正文摘要（用来找 URL，找到后交给 抓网页.py 读全文） |
| anysearch search | ✅ | 搜索 + 描述 |
| WebFetch | ❌ **全局封死** | 连 example.com 都不行。**试 1 次和试 10 次一样，别浪费轮次** |
| curl 直连 github 域名 | ❌ | hosts 有 149 条屏蔽记录把它指向 127.0.0.1，443 上有 sinkhole。**永远 exit 28，别试** |
| zenodo.org 等 | ⚠️ | 不在 hosts 里但 DNS 解析不了，标进 unfetchable |

### 排查口诀（抓不到时先跑这一行）
\`\`\`
python -c "import socket;print(socket.gethostbyname('域名'))"
\`\`\`
解析成 127.0.0.1 → hosts 屏蔽，**换源**（GitHub 域名一律改走 GitHub MCP）｜ 解析正常但连不上 → DNS 层，标 unfetchable ｜ 解析正常且能连 → **就是能抓，别放弃**

### 因此调研必须这样打
1. **优先读全文，不满足于摘要。** 官方文档站、arxiv、厂商 docs 一律用 \`抓网页.py\` 读全文。**"我读到了摘要"不算"我调研过了"。**
2. **GitHub 走 MCP。** 想知道"业界怎么做"，先问"**有没有开源实现**"——读源码全文的置信度远高于读二手描述。
   候选：AstrBot / LangBot / Koishi / SillyTavern(含扩展) / Letta(MemGPT) / mem0 / Zep / Cognee / Rasa / Botpress / nonebot 生态。
   **issue 和 PR 里的踩坑记录最硬**——比 README 的宣传语硬得多。
3. **证据等级诚实标注**：\`官方文档(直读)\` / \`源码(MCP直读)\` / \`官方文档(经搜索摘要)\` / \`第三方分析\` / \`个人博客\` / \`未找到证据\`。
   ⚠ **本次最容易犯的错：把"搜索摘要里的说法"写成"官方文档说"。不许。**
4. **爬不到的写进 schema 的 unfetchable 字段**：想查什么、为什么查不到、对结论有什么影响。
   **宁可交"未找到证据"，绝不交编的。** 但也别因为一个源爬不到就放弃整条结论——**换源**。
`

const RULES = `
${AUTHORITY}
## 铁律（违反即本次产出作废）
- **只读，绝不修改任何文件**（不改代码、不写文档、不动配置）
- 每条结论必须带证据：\`文件路径:行号\` 或 URL。**找不到证据就写"未找到"，绝不推测**
- 项目根目录：d:\\qq-小糖糖（不是 git 仓库，行尾混用）
- 这是中文项目，输出中文
- 用 Read/Grep/Glob 读代码；agent/handler.py(679K) 和 agent/store.py(664K) 极大，**不要整读**，用 Grep 定位再局部 Read
- 网络工具是延迟加载的，直接调用会报错——**先用 ToolSearch**（如 \`select:WebSearch\`、\`select:mcp__github__get_file_contents\`）载入 schema 再调
- \`docs/\` 下有 197 篇项目自己的文档（审查报告/开发规划/运行验收），**先查这里**——很多问题项目自己已经记录过
${NET}
`

const DOMAINS = [
  {
    key: 'messaging',
    name: '消息进出与发送链路',
    files: 'onebot/ws_client.py (108K), agent/handler.py 的 handle_group_message/handle_private_message/_checked_send 等方法, agent/reply_pipeline.py, agent/send_actions.py, agent/message_batcher.py, agent/inbound_event.py, agent/action_contract.py, agent/action_plan.py, agent/platform_receipts.py',
    focus: '入站事件路由、去重与幂等、批处理、发送结果三态(confirmed/uncertain/failed)、outbox 重试、分条[分段]与引用[引用]协议(2026-09-22/23 新加)、部分送达 PARTIAL_DELIVERY 与"轮/条"对应关系',
  },
  {
    key: 'memory',
    name: '记忆、真值与检索',
    files: 'agent/memory.py (121K), agent/store.py (664K, 用 Grep 定位), agent/knowledge.py, agent/knowledge_index.py, agent/reranker.py, agent/reflection.py',
    focus: '记忆提取与置信度、真值生命周期(status/active/retracted/superseded)、画像合成、知识库 hybrid+RRF+rerank、SQLite schema(chat_log/memories/people 等表)、短期缓冲 deque 的结构与渲染',
  },
  {
    key: 'self',
    name: '自我状态与内在动力',
    files: 'agent/self_state.py, agent/drives.py, agent/mood.py, agent/relationship.py, agent/perception.py, agent/scenario.py, agent/personalization.py',
    focus: '**这是"让糖糖像人"的核心**——关系场 closeness/seek_willingness、6 种驱动力、情绪三维、自我叙事、体验缓冲、场景引擎、持久化边界。重点问：这些状态真的驱动行为了吗，还是只被写不被读？有没有"状态丰富了但根本动态没变"的问题？',
  },
  {
    key: 'persona',
    name: '人格与上下文组装',
    files: 'agent/personality.py, agent/protocols.py, agent/context_builder.py, role_card.md, config.yaml 的 personality 段, agent/interaction_contract.py',
    focus: '系统提示词分层与缓存契约、role_card 与 protocols 的边界、背景块标记 bg() 与 token 预算(fit_backgrounds)、轻量协议族([SING]/[贴图]/[不说话]/[进入色色]/[分段]/[引用])、上下文来源标记、说话风格锚',
  },
  {
    key: 'autonomy',
    name: '自治、时序与自检',
    files: 'agent/handler_autonomy.py, agent/interjection.py, agent/conversation_tracker.py, agent/tasks.py, agent/scheduler.py, agent/catch_up.py, agent/opinion.py, agent/daily_report.py, agent/self_check.py, agent/health_check.py',
    focus: '主动私聊与群插话的准入/冷却/配额、对话窗口检测与退让、定时任务与延迟动作、离线补读、失败重试与冻结(uncertain 不重放)、自检层能拦什么拦不了什么',
  },
  {
    key: 'capability',
    name: '能力层与运维',
    files: 'agent/voice.py, agent/songs.py, agent/sticker.py, agent/asr.py, agent/diff_singer.py, agent/image_share.py, agent/web_search.py, agent/skills*(目录), main.py, 糖糖控制台_qt.py (340K, 用 Grep), agent/secret_refs.py, agent/service_manager.py, agent/metrics.py',
    focus: '技能注册机制、语音/唱歌/贴图/识图的装配与降级链、密钥解析与回填(secret_refs)、控制台的读写契约、健康检查与体检、发布脱敏、错误诊断链（日志说的是不是真话）',
  },
]

const MAP_SCHEMA = {
  type: 'object',
  properties: {
    domain: { type: 'string' },
    overview: { type: 'string', description: '这个领域在项目里承担什么职责，300 字以内' },
    modules: { type: 'array', items: { type: 'object', properties: {
      path: { type: 'string' }, role: { type: 'string' }, evidence: { type: 'string' },
    }, required: ['path', 'role'] } },
    mechanisms: { type: 'array', description: '核心机制：它是怎么工作的', items: { type: 'object', properties: {
      name: { type: 'string' }, how: { type: 'string', description: '具体怎么实现的，含关键分支条件' }, evidence: { type: 'string' },
    }, required: ['name', 'how', 'evidence'] } },
    invariants: { type: 'array', description: '项目自称的不变式/契约，以及它在代码或测试里怎么被强制', items: { type: 'object', properties: {
      claim: { type: 'string' }, enforced_by: { type: 'string', description: '哪个测试/闸门管它，找不到就写"无机器强制"' }, evidence: { type: 'string' },
    }, required: ['claim', 'enforced_by'] } },
    weak_spots: { type: 'array', description: '真实存在的设计缺陷——要具体（写了没接上/两个判定源/状态只写不读/失败被静默吞/注释与代码不符），不要泛泛而谈"缺乏测试"', items: { type: 'object', properties: {
      issue: { type: 'string' }, evidence: { type: 'string' }, why_it_matters: { type: 'string' },
    }, required: ['issue', 'evidence', 'why_it_matters'] } },
  },
  required: ['domain', 'overview', 'modules', 'mechanisms', 'weak_spots'],
}

const GAP_SCHEMA = {
  type: 'object',
  properties: {
    domain: { type: 'string' },
    industry_patterns: { type: 'array', items: { type: 'object', properties: {
      pattern: { type: 'string' }, who: { type: 'string' }, source: { type: 'string' },
      evidence_strength: { type: 'string', enum: ['官方文档(直读)', '源码(MCP直读)', '官方文档(经搜索摘要)', '第三方分析', '个人博客', '未找到证据'] },
    }, required: ['pattern', 'who', 'source', 'evidence_strength'] } },
    gaps: { type: 'array', description: '我方设计 vs 市场做法的真实差距，按对"让糖糖像人"的影响排序，最多 6 条', items: { type: 'object', properties: {
      title: { type: 'string' },
      our_design: { type: 'string', description: '我们现在怎么做（带 file:line）' },
      industry_design: { type: 'string' },
      source: { type: 'string' },
      why_better: { type: 'string', description: '市场方案为什么更好，或者为什么不适用' },
      humanness_impact: { type: 'string', enum: ['高', '中', '低', '负'] },
      effort: { type: 'string', enum: ['小', '中', '大', '重构'] },
      confidence: { type: 'string', enum: ['高', '中', '低'] },
    }, required: ['title', 'our_design', 'industry_design', 'why_better', 'humanness_impact', 'effort', 'confidence'] } },
    unfetchable: { type: 'array', description: '想查但查不到的：写清想查什么、为什么查不到、对结论有什么影响', items: { type: 'object', properties: {
      wanted: { type: 'string' }, why_blocked: { type: 'string' }, impact: { type: 'string' },
    }, required: ['wanted', 'why_blocked', 'impact'] } },
  },
  required: ['domain', 'industry_patterns', 'gaps', 'unfetchable'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    gap_title: { type: 'string' }, lens: { type: 'string' },
    refuted: { type: 'boolean', description: '这个差距站不住脚吗' },
    reason: { type: 'string' }, evidence: { type: 'string' },
    confidence: { type: 'string', enum: ['高', '中', '低'] },
  },
  required: ['gap_title', 'lens', 'refuted', 'reason', 'confidence'],
}

phase('盘点')
log(`6 个领域并行盘点：${DOMAINS.map(d => d.name).join('、')}`)

// 2026-09-23 修（上一次跑死在这里）：pipeline 只把每级返回值传给**下一级**，
// stage 3 拿不到 stage 1 的 map。上次 stage 3 里写了裸的 `map` → ReferenceError
// → 每个领域被丢成 null → 综合阶段拿到四个空数组。用外层对象收住即可。
const maps = {}
const results = await pipeline(
  DOMAINS,
  (d) => agent(
    `${RULES}\n\n# 任务：盘点「${d.name}」领域的现有架构与设计\n\n## 要读的\n${d.files}\n\n## 重点\n${d.focus}\n\n` +
    `把"它现在是怎么做的"挖清楚。不是复述注释——要看真实分支条件、真实调用链、真实的边界处理。\n` +
    `weak_spots 要找的是**真实存在的设计缺陷**，每条必须能指着代码说。\n` +
    `**先查 docs/ 目录**（197 篇项目自己的文档，含审查报告与踩坑记录）——项目自己记录过的问题优先采信，避免重复发现。`,
    { label: `盘点:${d.key}`, phase: '盘点', schema: MAP_SCHEMA },
  ).then((m) => { maps[d.key] = m; return m }),
  (map, d) => agent(
    `${RULES}\n\n# 任务：调研「${d.name}」领域——市场同类设计有没有更好的做法\n\n` +
    `## 我方现状（上一个 agent 盘出来的，直接采信，不用重新挖）\n${JSON.stringify(map).slice(0, 12000)}\n\n` +
    `## 我方领域说明\n${d.focus}\n\n` +
    `## 你要做的\n` +
    `1. **先调研，再判断**——去查同类系统（AI 伴侣 / 角色扮演 / agent 记忆 / 长时对话系统 / IM bot 框架）在这件事上怎么做。` +
    `**用 抓网页.py 读全文，不要只读搜索摘要。** 具体到项目名、产品名、标准名、API 字段名，带出处。**没有证据就写"未找到证据"。**\n` +
    `2. 对我方每个 weak_spot，回答：市场怎么解决的？有没有成熟方案？**或者——市场根本没解决，我们是领先的？**` +
    `后者同样是结论，别硬凑差距。\n` +
    `3. humanness_impact 的判据：这个改动会让糖糖**更像一个有连续存在的人**，还是只是让代码更干净？` +
    `只让代码干净的标"低"；会伤害拟人性的标"负"并说明。\n` +
    `4. 爬不到的写进 unfetchable，**别因为一个源爬不到就放弃整条结论**——换源。`,
    { label: `对照:${d.key}`, phase: '对照', schema: GAP_SCHEMA },
  ),
  (gapResult, d) => {
    const top = (gapResult.gaps || []).slice(0, 3)
    if (!top.length) return { domain: d.key, name: d.name, map: maps[d.key] || {}, gaps: [], industry: gapResult.industry_patterns, unfetchable: gapResult.unfetchable, verified: [] }
    return parallel(top.map((g) => () =>
      parallel([
        () => agent(
          `${RULES}\n\n# 任务：**证伪**下面这条"差距"（视角 A：事实核查）\n\n` +
          `差距：${g.title}\n我方做法：${g.our_design}\n市场做法：${g.industry_design}\n出处：${g.source || '未给'}\n声称更好的理由：${g.why_better}\n\n` +
          `## 你的活\n` +
          `1. **去核实我方做法**——它真的像描述的那样吗？去读代码。` +
          `常见错误：描述的是注释里的**意图**而代码实际行为不同；或者那个缺陷**早就被修了**。\n` +
          `2. **去核实市场做法**——那个出处真的说了这件事吗？用 GitHub MCP 或 抓网页.py 把原文调出来看，别信二手描述。` +
          `常见错误：把某个项目的**通用理念**套到具体问题上，而它其实没解决这个问题。\n` +
          `3. 拿不准时**判 refuted=true**（宁可漏掉一条真差距，不可把假的写进方案）。\n` +
          `4. evidence 写你实际查了什么、看到了什么。`,
          { label: `验证A:${d.key}:${g.title.slice(0, 16)}`, phase: '验证', schema: VERDICT_SCHEMA },
        ),
        () => agent(
          `${RULES}\n\n# 任务：**证伪**下面这条"差距"（视角 B：对拟人性的影响）\n\n` +
          `差距：${g.title}\n声称的影响：${g.humanness_impact}\n理由：${g.why_better}\n\n` +
          `## 你的活——只问这一件事\n` +
          `**按市场方案改，对"糖糖像一个有连续存在的人"是改善、无影响，还是伤害？**\n\n` +
          `这个项目的北极星（CLAUDE.md 零章）：「LLM 提供瞬间；架构提供生命」——她的存在来自围绕 LLM 构建的**持久状态结构**，` +
          `她的三观要从几千次真实互动里长出来，不是设定出来的。\n\n` +
          `判 refuted=true 的典型情形（认真找）：\n` +
          `- 改动是**为了让代码更工程化**（更好的抽象/更少重复/更标准的模式），但对她的行为没有任何可观测影响\n` +
          `- 市场方案来自**不同性质的产品**（客服/工具型 agent），它优化的目标（吞吐、准确、合规）不是"像一个人"\n` +
          `- 改动会**减少她的自主权**（系统替 LLM 做更多决策），与本项目"LLM 是唯一决策者"的原则相抵\n` +
          `- 会让她**更可预测、更像模板**（统一所有回复的长度/结构/风格）\n` +
          `- 收益是**指标上的**（延迟、token、成本），而不是她这个人更像人\n\n` +
          `如果你认为它**确实**提升拟人性，说清**通过什么机制**——具体到她哪个状态、哪段经历、哪种连续性。` +
          `不要用"更自然""更真实"糊弄。`,
          { label: `验证B:${d.key}:${g.title.slice(0, 16)}`, phase: '验证', schema: VERDICT_SCHEMA },
        ),
      ]).then((vs) => ({ gap: g, lensA: vs[0], lensB: vs[1] }))
    )).then((verified) => ({ domain: d.key, name: d.name, map: maps[d.key] || {}, gaps: gapResult.gaps, industry: gapResult.industry_patterns, unfetchable: gapResult.unfetchable, verified: verified.filter(Boolean) }))
  },
)

const clean = results.filter(Boolean)
// 2026-09-23 加：上次就是所有 pipeline 项静默失败、把空数组传给综合阶段，
// 而综合 agent 只顾着写方案、没报告输入是空的。宁可在这一步就喊出来。
log(`pipeline 返回 ${results.length} 项，其中非空 ${clean.length} 项`)
if (!clean.length) {
  log('⚠⚠ 所有 pipeline 项都失败了——综合阶段会拿到空输入。先修脚本再跑。')
  throw new Error('pipeline produced zero usable domains; refusing to synthesize from empty input')
}
const survivors = clean.map((r) => ({
  domain: r.name,
  gaps: (r.verified || [])
    .filter((v) => !(v.lensA && v.lensA.refuted && v.lensB && v.lensB.refuted))
    .map((v) => ({ ...v.gap, verdict_a: v.lensA, verdict_b: v.lensB })),
  killed: (v0 => v0)((r.verified || [])
    .filter((v) => v.lensA && v.lensA.refuted && v.lensB && v.lensB.refuted)
    .map((v) => ({ title: v.gap.title, reason_a: v.lensA.reason, reason_b: v.lensB.reason }))),
}))

const total = clean.reduce((n, r) => n + (r.gaps || []).length, 0)
const passed = survivors.reduce((n, s) => n + s.gaps.length, 0)
log(`差距候选 ${total} 条 → 双视角验证后存活 ${passed} 条`)

phase('综合')
const synthesis = await agent(
  `${RULES}\n\n# 任务：把 6 个领域的调研综合成一份改造方案\n\n` +
  `## 各领域现状\n${JSON.stringify(clean.map((r) => ({ domain: r.name, map: r.map }))).slice(0, 40000)}\n\n` +
  `## 通过验证的差距\n${JSON.stringify(survivors).slice(0, 60000)}\n\n` +
  `## 被证伪的差距\n${JSON.stringify(clean.map((r) => ({ domain: r.name, killed: (survivors.find((s) => s.domain === r.name) || {}).killed }))).slice(0, 15000)}\n\n` +
  `## 各领域爬不到的东西\n${JSON.stringify(clean.map((r) => ({ domain: r.name, unfetchable: r.unfetchable }))).slice(0, 12000)}\n\n` +
  `## 你要产出的\n` +
  `1. **executive_summary**：项目现在处于什么状态，最要紧的三件事。不要客套。\n` +
  `2. **root_causes**：把 6 个领域的差距合并去重——**不同领域里可能是同一个根因**（比如"两个判定源"或"写了没接上"可能反复出现）。找出共同根因，那是主要矛盾。\n` +
  `3. **findings**：按"对让糖糖像人"的影响排序。每条：发现 / 证据(file:line 或 URL) / 建议 / 优先级(P0-P3) / 代价 / 置信度。\n` +
  `4. **milestones**：有顺序的改造里程碑。每个要有：包含什么、**明确不做什么**、怎么验证（跑什么命令/看什么现象）、风险与回滚。第一个里程碑要窄到今天就能做完并验证。\n` +
  `5. **do_not_touch**：调研中发现的"看起来该改但其实是对的"的设计——列出并说明为什么别动。` +
  `**这份清单和改造清单一样重要**：项目已积累 35 条踩坑教训，很多"不优雅"是有原因的。\n` +
  `6. **unfetchable_summary**：汇总所有爬不到的，说明**它让哪些结论只能标中/低置信**。\n\n` +
  `## 一条硬要求\n` +
  `北极星是"让糖糖像一个有连续存在的人"。**任何"让代码更工程化但对她的行为无可观测影响"的建议，不要放进 findings 主体**——放进 engineering_only_appendix。` +
  `我要的是让她更像人，不是让代码更漂亮。`,
  { label: '综合方案', phase: '综合', schema: {
    type: 'object',
    properties: {
      executive_summary: { type: 'string' },
      root_causes: { type: 'array', items: { type: 'object', properties: {
        cause: { type: 'string' }, appears_in: { type: 'array', items: { type: 'string' } }, evidence: { type: 'string' },
      }, required: ['cause', 'appears_in', 'evidence'] } },
      findings: { type: 'array', items: { type: 'object', properties: {
        title: { type: 'string' }, domain: { type: 'string' }, evidence: { type: 'string' },
        recommendation: { type: 'string' }, priority: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] },
        effort: { type: 'string', enum: ['小', '中', '大', '重构'] }, humanness_impact: { type: 'string' },
        confidence: { type: 'string', enum: ['高', '中', '低'] },
      }, required: ['title', 'domain', 'evidence', 'recommendation', 'priority', 'effort', 'confidence'] } },
      milestones: { type: 'array', items: { type: 'object', properties: {
        name: { type: 'string' }, includes: { type: 'array', items: { type: 'string' } },
        excludes: { type: 'array', items: { type: 'string' } },
        verification: { type: 'string' }, risk: { type: 'string' }, rollback: { type: 'string' },
      }, required: ['name', 'includes', 'excludes', 'verification'] } },
      do_not_touch: { type: 'array', items: { type: 'object', properties: {
        design: { type: 'string' }, why_leave_it: { type: 'string' },
      }, required: ['design', 'why_leave_it'] } },
      unfetchable_summary: { type: 'array', items: { type: 'string' } },
      engineering_only_appendix: { type: 'array', items: { type: 'string' } },
    },
    required: ['executive_summary', 'root_causes', 'findings', 'milestones', 'do_not_touch', 'unfetchable_summary'],
  } },
)

return {
  synthesis,
  per_domain: clean.map((r) => ({
    domain: r.name,
    overview: (r.map && r.map.overview) || '',
    modules: (r.map && r.map.modules) || [],
    mechanisms: (r.map && r.map.mechanisms) || [],
    invariants: (r.map && r.map.invariants) || [],
    weak_spots: (r.map && r.map.weak_spots) || [],
    industry: r.industry,
    unfetchable: r.unfetchable,
    gaps_total: (r.gaps || []).length,
    gaps_survived: (survivors.find((s) => s.domain === r.name) || {}).gaps || [],
    gaps_killed: (survivors.find((s) => s.domain === r.name) || {}).killed || [],
  })),
}
