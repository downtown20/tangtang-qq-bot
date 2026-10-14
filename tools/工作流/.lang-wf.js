export const meta = {
  name: 'tangtang-language-choice',
  description: '编程语言选型调研：agent 开发/运行/推理/工具调用的语言影响，评估本仓全 Python 是否合理',
  phases: [
    { title: '格局', detail: '行业语言分布与原因（带证据）' },
    { title: '实测', detail: '本仓 Python 痛点实测（日志/遥测/代码）' },
    { title: '对比', detail: '候选语言按本项目约束逐维对比' },
    { title: '判定', detail: '判断依据 + 结论 + 证伪记录' },
  ],
}

const RULES = `
## 铁律
- **只读，绝不修改任何文件**
- 每条结论带证据：\`文件路径:行号\` 或 URL。**找不到就写"未找到"，绝不推测**
- 项目根目录 d:\\qq-小糖糖（不是 git 仓库）；中文项目，输出中文
- 网络通道（2026-09-23 实测，**别自己再试**）：
  · \`python ~/.claude/tools/抓网页.py "<URL>" --max 30000\` ✅ **抓任意网页全文**（普通 Bash 即可，不用关沙箱）
  · **GitHub MCP** ✅ 直读任意公开仓库文件全文 + issue/PR（\`select:mcp__github__get_file_contents\`）
  · WebSearch ✅ 搜索+正文摘要（用来找 URL，然后交给 抓网页.py 读全文）
  · WebFetch ❌ **全局封死**，别试
- 延迟加载的工具先用 ToolSearch 载入 schema
`

const PROFILE = `
## 本项目的约束画像（**对比必须锚在它上面**，否则就是空谈）

**规模**：纯 Python 3.10；\`agent/\` 下 77 个模块，最大的 \`handler.py\` 679KB、\`store.py\` 664KB；
\`onebot/ws_client.py\` 108KB；控制台 \`糖糖控制台_qt.py\` 340KB 单文件（PySide6）。

**运行时形态**：全 async（asyncio）+ 线程池桥接阻塞调用（\`agent/async_io.py\` 的 \`run_bounded_blocking\` /
\`run_bounded_store_io\`，带耗时告警）；SQLite（同步驱动）；通过 OneBot 11 协议接 QQ
（反向 WebSocket 监听 3001 + HTTP 出站到 3000）。

**本地 ML 栈（这条最关键）**：BGE 嵌入、Reranker、ASR、DiffSinger、GPT-SoVITS、RVC、Demucs——
**全部是 PyTorch / ONNX 生态**，还有独立的 venv（\`cosyvoice3venv\` / \`venv_demucs\`）。

**负载量级**：单人自用 + 几个群。**不是高并发服务**——消息峰值大概每分钟个位数。
LLM 调用延迟在**秒级**，本地模型推理也在**百毫秒到秒级**。

**部署形态**：Windows 单机、双击启动、随附 SnowLuma 与 QQ；面向普通用户发布。

**已有的 Python 适配手段**（别当成"没做"）：async 全链路、阻塞调用上线程池+有界队列、
模型加载有全局锁、Store 调用有专门的 bounded wrapper、事件循环有慢调用告警。
`

const SURVEY_SCHEMA = {
  type: 'object',
  properties: {
    framework_languages: {
      type: 'array',
      description: '主流 agent / LLM 应用框架分别用什么语言，为什么（要有出处）',
      items: { type: 'object', properties: {
        name: { type: 'string' }, language: { type: 'string' },
        why_this_language: { type: 'string', description: '作者/社区给的理由，不是你猜的' },
        source: { type: 'string' },
        evidence_strength: { type: 'string', enum: ['官方文档(直读)', '源码(MCP直读)', '官方文档(经搜索摘要)', '第三方分析', '个人博客', '未找到证据'] },
      }, required: ['name', 'language', 'why_this_language', 'source', 'evidence_strength'] } },
    language_effect_claims: {
      type: 'array',
      description: '「语言影响 agent 行为」的具体主张，以及证据是支持还是证伪',
      items: { type: 'object', properties: {
        claim: { type: 'string' },
        held: { type: 'boolean', description: '证据支持这个主张吗' },
        detail: { type: 'string' }, source: { type: 'string' },
      }, required: ['claim', 'held', 'detail', 'source'] } },
    adoption_evidence: { type: 'string', description: '有没有"某项目从 Python 换到别的语言"或反向的真实案例？迁移后实际变了什么？' },
  },
  required: ['framework_languages', 'language_effect_claims', 'adoption_evidence'],
}

const MEASURE_SCHEMA = {
  type: 'object',
  properties: {
    measured_pains: {
      type: 'array',
      description: '本仓**可测量**的 Python 相关损失。每条必须有日志/遥测/代码证据；没有就写"未测到"',
      items: { type: 'object', properties: {
        pain: { type: 'string' },
        measurement: { type: 'string', description: '具体数字：多少次/多少毫秒/占比' },
        evidence: { type: 'string' },
        is_python_fault: { type: 'boolean', description: '这是 Python 语言的锅，还是这个项目写得有问题？' },
        would_other_language_fix: { type: 'string', description: '换成别的语言能消除吗？为什么' },
      }, required: ['pain', 'measurement', 'evidence', 'is_python_fault', 'would_other_language_fix'] } },
    latency_budget: { type: 'string', description: '一条消息从进来到回复出去，时间花在哪？给出各段耗时的量级' },
    irreducible: { type: 'array', description: '哪些部分**换语言也去不掉**（本地模型推理、LLM 延迟…）', items: { type: 'string' } },
  },
  required: ['measured_pains', 'latency_budget', 'irreducible'],
}

const COMPARE_SCHEMA = {
  type: 'object',
  properties: {
    candidates: {
      type: 'array',
      items: { type: 'object', properties: {
        language: { type: 'string' },
        perf_concurrency: { type: 'string' }, ecosystem: { type: 'string' },
        dev_efficiency: { type: 'string' }, deploy_ops: { type: 'string' },
        long_term: { type: 'string' },
        ml_stack_compat: { type: 'string', description: '对本仓那套 PyTorch/ONNX 本地模型的支持程度——这是硬约束' },
        verdict: { type: 'string', description: '对本项目：可行 / 部分可行 / 不可行，为什么' },
      }, required: ['language', 'perf_concurrency', 'ecosystem', 'dev_efficiency', 'deploy_ops', 'long_term', 'ml_stack_compat', 'verdict'] } },
    killer_constraints: { type: 'array', description: '一票否决式约束（满足不了就淘汰）', items: { type: 'string' } },
  },
  required: ['candidates', 'killer_constraints'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    bottom_line: { type: 'string', description: '一句话结论' },
    criteria: { type: 'array', description: '怎么选才最高效——给出可复用的判断依据，按顺序问的问题', items: { type: 'string' } },
    python_verdict: { type: 'string', description: '全 Python 是否合理，在什么条件下合理/不合理' },
    where_python_actually_hurts: { type: 'array', items: { type: 'string' } },
    what_to_do_instead: { type: 'array', description: '既然（多半）不换语言，那该做什么来消除 Python 的弱点——具体到本仓', items: { type: 'object', properties: {
      action: { type: 'string' }, why: { type: 'string' }, evidence: { type: 'string' },
    }, required: ['action', 'why'] } },
    refuted_claims: { type: 'array', description: '调研中被证伪的说法（含"Python 很慢所以该换"这类），留档免得下次重提', items: { type: 'string' } },
    unfetchable: { type: 'array', items: { type: 'string' } },
  },
  required: ['bottom_line', 'criteria', 'python_verdict', 'where_python_actually_hurts', 'what_to_do_instead', 'refuted_claims'],
}

phase('格局')
log('三路并行：行业格局 / 本仓实测 / 候选语言对比')

const [survey, measured, compare] = await parallel([
  () => agent(
    `${RULES}\n\n# 任务：agent / LLM 应用领域的**语言选择格局**\n${PROFILE}\n\n` +
    `## 要回答的\n` +
    `1. 主流 agent 框架分别用什么语言？**作者给的理由是什么**（不要你替他们猜）。\n` +
    `   至少覆盖：LangChain/LangGraph、AutoGPT、CrewAI、Letta(MemGPT)、Dify、n8n、OpenClaw、\n` +
    `   AstrBot、LangBot、Koishi、SillyTavern、Semantic Kernel、Spring AI、Rig(Rust)、Swiftide(Rust)、\n` +
    `   MCP 官方 SDK 的语言覆盖。**LangBot 和 AstrBot 是本项目最近的同类，重点看**。\n` +
    `2. **核心问题**：有没有证据表明语言影响 **agent 的行为质量**（推理、工具调用可靠性、流式体验、错误恢复）？\n` +
    `   还是说这些全部由模型决定、语言只影响工程指标？**去证伪"语言影响 agent 智能"这个直觉。**\n` +
    `3. **真实迁移案例**：有没有项目从 Python 换到别的语言（或反向）？换了之后**实际变了什么**（延迟？稳定性？开发速度？）\n` +
    `   要具体数字，不要"感觉更快了"。\n` +
    `4. MCP / 工具调用协议的语言实现差异——schema 校验、流式、并发调用，各语言实现有实质差别吗？`,
    { label: '格局:行业语言分布', phase: '格局', schema: SURVEY_SCHEMA },
  ),
  () => agent(
    `${RULES}\n\n# 任务：**本仓 Python 痛点实测**——不问抽象性能，问这个项目里 Python 到底有没有造成可测量的损失\n${PROFILE}\n\n` +
    `## 取证手段（都用上）\n` +
    `1. **日志** \`tangtang.log\`（**70 万+ 行，必须用 python 脚本过滤，不要 grep 全文**）：\n` +
    `   找这些告警并**统计条数/耗时分布**：\n` +
    `   \`较慢\` / \`elapsed_ms\` / \`queue_wait_ms\` / \`limit=\`（\`agent/async_io.py\` 的 bounded wrapper 会打）、\n` +
    `   事件循环阻塞、超时、\`降级\`、\`重试\`、HTTP 超时。**给出：哪类操作最常慢、P50/P95 是多少。**\n` +
    `2. **遥测**：\`agent/metrics.py\` 记了什么？有没有现成的耗时统计可以读？\n` +
    `3. **代码**：\`agent/async_io.py\` 的 bounded wrapper 阈值是多少？哪些调用走了线程池？\n` +
    `   有没有\`time.sleep\`、同步 IO、\`requests\`（而非 httpx）残留？有没有已知的 GIL 敏感热点？\n` +
    `4. **一条消息的延迟预算**：从收到消息到发出回复，时间花在哪几段？**各段量级**（毫秒/秒）。\n` +
    `   关键判断：**换语言能不能动这些数字**。\n` +
    `5. **本仓自己的记录**：\`docs/\` 下有 197 篇文档，查有没有性能相关的审查/验收报告已经记过这些。\n\n` +
    `⚠ **is_python_fault 这一栏要诚实**：如果慢是因为"同步调用没上线程池"或"写得不合理"，\n` +
    `那是代码问题不是语言问题——**换语言也治不好**。逐条区分。`,
    { label: '实测:本仓Python痛点', phase: '实测', schema: MEASURE_SCHEMA },
  ),
  () => agent(
    `${RULES}\n\n# 任务：候选语言**针对本项目**的逐维对比\n${PROFILE}\n\n` +
    `## 候选（至少覆盖，可增减）\n` +
    `Python（现状）/ TypeScript-Node / Go / Rust / Java-Kotlin / C#(.NET) / C++\n\n` +
    `## 六个维度，**每一维都要针对本项目的约束作答**，不要写通用百科\n` +
    `1. **性能与并发**：asyncio 的并发模型 vs goroutine vs tokio vs Node 事件循环——**在本项目的负载量级下**（每分钟个位数消息、LLM 秒级延迟）差异是否可观测？\n` +
    `2. **生态成熟度**：agent/LLM 相关库的丰富度；**但更要看本仓真正依赖的那几样**（见约束画像）\n` +
    `3. **开发效率**：本仓 77 个模块、最大的 679KB 单文件——动态类型在这里是帮助还是伤害？\n` +
    `4. **部署运维**：Windows 单机、面向普通用户双击启动——各语言的打包/分发现实（PyInstaller vs pkg vs 单二进制）\n` +
    `5. **长期维护**：类型系统、重构安全、依赖地狱、3 年后的可维护性\n` +
    `6. **⚠ 本地 ML 栈兼容（硬约束）**：BGE / Reranker / ASR / DiffSinger / GPT-SoVITS / RVC / Demucs\n` +
    `   —— 这些在 PyTorch/ONNX 生态里。**换语言意味着什么？**（子进程？ONNX Runtime 各语言绑定成熟度？\n` +
    `   RVC/GPT-SoVITS 这类社区项目有没有非 Python 实现？）**这一维可能直接决定结论。**\n\n` +
    `最后给 killer_constraints：**满足不了就该淘汰**的一票否决项。`,
    { label: '对比:候选语言', phase: '对比', schema: COMPARE_SCHEMA },
  ),
])

phase('判定')
const verdict = await agent(
  `${RULES}\n\n# 任务：给出**判断依据与结论**\n${PROFILE}\n\n` +
  `## 三路调研结果\n` +
  `### 行业格局\n${JSON.stringify(survey).slice(0, 20000)}\n\n` +
  `### 本仓 Python 痛点实测\n${JSON.stringify(measured).slice(0, 20000)}\n\n` +
  `### 候选语言对比\n${JSON.stringify(compare).slice(0, 25000)}\n\n` +
  `## 你要产出的\n` +
  `1. **bottom_line**：一句话。**不要模棱两可**——"看情况"不是结论，要说清"在什么条件下是 A，什么条件下是 B"。\n` +
  `2. **criteria**：**怎么选才最高效**——给一组**按顺序问的问题**，每个问题能把候选砍掉一批。\n` +
  `   要能复用到别的项目上，不是只对本项目有效。\n` +
  `3. **python_verdict**：全 Python 对本项目是否合理。**在什么条件下会变得不合理**（给出触发条件）。\n` +
  `4. **where_python_actually_hurts**：实测出来的、**真的**是 Python 拖后腿的地方（可能与直觉相反——\n` +
  `   实测很可能是"Python 没拖后腿，是某些代码没写好"）。\n` +
  `5. **what_to_do_instead**：既然（多半）不换语言，该做什么消除弱点——**具体到本仓的文件/机制**，不要"加强类型注解"这种空话。\n` +
  `6. **refuted_claims**：被证伪的说法，**特别是"Python 慢所以该换语言"这类直觉**。留档，免得下次重提。\n` +
  `7. **unfetchable**：爬不到的东西及影响。\n\n` +
  `⚠ **不许为了给主人一个"可以安心不换"的结论而淡化反证。** 如果证据显示 Python 在本项目某处真的造成了可观损失，\n` +
  `就直说，并给出该处的具体取舍。反之，如果"换语言"的收益确实可忽略，也要把**为什么可忽略**讲清楚（数字，不是形容词）。`,
  { label: '判定:结论', phase: '判定', schema: VERDICT_SCHEMA },
)

return { verdict, survey, measured, compare }
