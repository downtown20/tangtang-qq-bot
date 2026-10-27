// 与 Python 后端桥的唯一通信层。
// 渲染进程不读文件、不碰 Node：一切都经过 http://127.0.0.1:<随机端口>/api/*

export type BridgeInfo = { port: number; token: string }

export type StatCounts = {
  people: number | null
  memories: number | null
  chats: number | null
  stickers: number | null
}

export type BotStatus = {
  bot: { running: boolean; pid: number | null }
  llm: { provider: string | null; model: string | null; models_count: number }
  log_seq: number
}

export type ModelProfile = {
  id: string
  label: string
  provider: string
  base_url: string
  api_key: string
  model: string
  max_tokens?: number | string | null
  temperature?: number | string | null
  thinking?: string | null
  thinking_effort?: string | null
  /** 用途标记（2026-10-27）：`chat` / `vision` / `tag`，可多选。
   *  缺这个键的老档位一律按 `['chat']` 算（见 agent/model_refs.py）。 */
  kinds?: string[]
}

/**
 * Ollama 模型（`/api/models/ollama` 返回的形状）。
 *
 * 对齐 DSH Harness 的 `llm-pi-ai.providers.<id>.models[]`：每个模型带
 * `contextWindow` 和 `input: [text, image]`。`input` 含 `image` 才可能用来
 * 识图 / 打标——这比让用户自己判断「这个模型能不能看图」可靠。
 */
export type OllamaModel = {
  id: string
  name: string
  size?: number | null
  family?: string
  parameter_size?: string
  input: string[]
  context_window: number
  vision: boolean
}

export type OllamaModelsResponse = {
  ok: boolean
  host: string
  models: OllamaModel[]
  count?: number
  error?: string
}

/**
 * 「选模型」面板的一项（`/api/models/picker`）。
 *
 * `kind` 决定「选它意味着什么」：`ref` = 用 AI 模型页的档位（云端），
 * `ollama` = 用本机 Ollama 的某个模型。选中即同时确定「打标方式 + 模型」——
 * 这两件事拆开就会出现「方式选了 api、模型还停在 ollama 名字」的矛盾组合。
 */
export type ModelPickerItem = {
  key: string
  label: string
  sub: string
  /** 上下文窗口徽标：`8K` / `256K` / `1M`（拿不到就空） */
  badge: string
  vision: boolean
  kind: 'ref' | 'ollama'
  value: string
  provider: string
}

export type ModelPickerGroup = {
  key: string
  label: string
  items: ModelPickerItem[]
}

export type ModelsPickerResponse = {
  ok: boolean
  groups: ModelPickerGroup[]
  /** `ref:<档位id>` / `ollama:<模型名>` / `api:<模型名>`（老配置） */
  current: string
  ollama_host?: string | null
  ollama_error?: string | null
}

export type SecretEntry = { name: string; label: string; configured: boolean; masked: string }
export type SecretsResponse = { provider_env: string; entries: SecretEntry[]; env_path: string }
export type SnowlumaStatus = {
  installed: boolean
  dir: string
  running: boolean
  webui_url: string
  release_url: string
}
export type CheckResultItem = {
  key: string
  title: string
  status: 'ok' | 'warn' | 'fail' | 'skip' | string
  detail: string
  next_step: string
}

export type ModelsResponse = {
  profiles: ModelProfile[]
  active: string
  legacy: boolean
}

export type GroupInfo = {
  id: string
  name: string
  member_count: number | null
  last_at: string | null
  scenario: string
}

export type GalleryItem = {
  name: string
  source: string
  ext: string
  size: number
  mtime: string
  emotions: string[]
  scene_tags: string[]
  act: string
  desc: string
}

export type GalleryTagCount = { tag: string; count: number }

/** 一个贴图/CG 库（贴图组里有 默认 / 丛雨 / 米雪儿 三套） */
export type GallerySourceInfo = { key: string; label: string; count: number }

/** 图库打标：这一轮的进度（`running` 为真时界面 2 秒轮询一次）。 */
export type GalleryTagProgress = {
  running: boolean
  total: number
  done: number
  ok: number
  fail: number
  /** 正在处理哪一张（空 = 没在跑） */
  current: string
  /** **最近一次**单张失败的原因。不随进度清零——跑完还要能看见为什么有几张失败 */
  error: string
  started_at: number
  finished_at: number
}

/** 图库打标的扫描结果：「已打标」= metadata 里有 `persons`，与脚本 `--resume` 同口径。 */
export type GalleryTagScan = {
  /** 实际扫描的目录；合集（多个库合计）时是 null */
  dir: string | null
  /** 合集时逐个库的目录（`dir` 为 null 时看这里） */
  dirs?: string[]
  dir_exists: boolean
  files: number
  tagged: number
  pending: number
  metadata_entries: number
}

export type GalleryTagStatus = {
  ok: boolean
  /** 这次 status 对应的图库来源（就是图库页当前选中的库 key） */
  source: string
  /** 实际会打标的目录；合集时为 null（合集不可写） */
  dir: string | null
  /** 打标配置（**不含 key**，只有 `api_key_configured` 这个布尔）——只放「参数/模型」 */
  config: {
    provider: string
    dir: string
    ollama_host: string
    ollama_model: string
    api_base: string
    api_model: string
    max_side: number
    timeout_s: number
    rename: boolean
    api_key_configured: boolean
  }
  /** 空串 = 可以开跑；非空 = **为什么不能**（直接显示，不让人猜） */
  provider_ready: string
  /** 空串 = 这个库能打标；非空 = 为什么不能（目录不存在 / 合集不可写…） */
  dir_ready: string
  scan: GalleryTagScan
  progress: GalleryTagProgress
}

/** 「打哪些图 / 怎么处理」——由图库页的控件决定，不再只读设置里的配置。 */
export type GalleryTagRunRequest = {
  /** 图库页当前选中的库（sticker / murasame / michele / cg）；不传=旧行为 */
  source?: string
  /** pending=只打未打标的 / all=整个图库 / selected=只打 names 里的 */
  scope?: 'pending' | 'all' | 'selected'
  /** scope='selected' 时用；纯文件名，桥会用 _safe_name 校验 */
  names?: string[]
  /** 打完是否改名（不传=配置里的 rename） */
  rename?: boolean
  /** 已有标签要不要重打（不传=scope 为 all 时 true） */
  overwrite?: boolean
  limit?: number
}

export type GalleryResponse = {
  items: GalleryItem[]
  total: number
  page: number
  page_size: number
  pages: number
  total_all: number
  source: string
  keys: string[]
  sources: GallerySourceInfo[]
  emotions: GalleryTagCount[]
  scene_tags: GalleryTagCount[]
}

export type GalleryQuery = {
  source: string
  q?: string
  tag?: string
  tag_field?: 'emotions' | 'scene_tags'
  state?: '' | 'tagged' | 'untagged'
  sort?: string
  page?: number
  page_size?: number
}

/* ═══════════════════════════════════════════════════════════════
   会客厅（Console）—— 旁观糖糖的本体会话
   契约由后端 tools/desktop_bridge.py 提供，字段名严格对齐，不要改名。
   ═══════════════════════════════════════════════════════════════ */

/** 本体可用性探测。糖糖未启动时 available=false + reason，界面走引导态而不是报错弹窗。 */
export type ConsoleStatus = {
  available: boolean
  port: number
  bot_online: boolean
  reason?: string
}

export type ConsoleSession = {
  id: string
  title: string
  created_at: number
  updated_at: number
  message_count: number
  last_preview: string
}

/** 旁观的四种说话人：糖糖本体 / 主人 / Lead / 系统 */
export type ConsoleRole = 'tangtang' | 'user' | 'lead' | 'system'

/** meta 是后端透传的自由字段，常见键见 ConsoleTurnMeta */
export type ConsoleMessage = {
  id: number
  session_id: string
  role: ConsoleRole
  content: string
  ts: number
  meta: Record<string, unknown> | null
}

export type ConsoleMood = {
  energy: number
  mood: number
  patience: number
  context: string
}

/** 六种驱动力。urgent=超过阈值（界面标「急躁」），active=当前参与竞争 */
export type ConsoleDrive = {
  key: string
  label: string
  value: number
  urgent: boolean
  active: boolean
  intensity: string
}

/** 一次真实回合的元信息（/say 与 /turn 共用，也是 messages[].meta 的形状） */
export type ConsoleTurnMeta = {
  ms: number
  model: string
  provider: string
  tokens: number
  tools: string[]
}

/**
 * 状态快照。
 * ⚠️ 实测（agent/console_api.py::snapshot）：每个分区**独立 try**，
 * 子系统异常时该分区就是 null —— 前端必须按「可能没有」渲染，不能假设一定有。
 */
export type ConsoleSnapshot = {
  bot: { name: string; qq: string; uptime_s: number }
  mood: ConsoleMood | null
  drives: ConsoleDrive[]
  dominant: { key: string; label: string; text: string } | null
  self: {
    narrative: string
    values: string
    /** ⚠️ 档位字符串（'low' | 'normal' | 'high' …），不是数值，别做条形图 */
    energy: string
    /** 心情基调，例如 'tired' —— 原样显示 */
    mood_tint: string
    /** 近期体验：一条条「谁说了什么」的互动记录，不是摘要 */
    experiences: Array<{ summary: string; nickname: string; time: string }>
  } | null
  relationships: {
    total: number
    close: number
    familiar: number
    top: Array<{ qq: string; nickname: string; closeness: number; tier: string }>
  } | null
  memory: {
    total: number
    /** time 是字符串（'2026-10-14 13:58'），不是时间戳 */
    recent: Array<{ text: string; time: string; importance: number }>
  } | null
  model: { provider: string; model: string } | null
  /** 上一回合；字段名是 ms / tokens / tools（不是 last_*） */
  turn: (ConsoleTurnMeta & { autonomous?: boolean }) | null
}

export type ConsoleTurnResult = {
  reply: string
  meta: ConsoleTurnMeta
  session_id: string
  /** 后端顺手带回的最新快照，可以直接拿去刷新右栏 */
  state?: ConsoleSnapshot | null
}

/* ═══════════════════════════════════════════════════════════════
   会客厅 v2 —— QQ 聊天镜像中心 + 可输入 + 回合痕迹（2026-10-24）

   与 v1 的区别：v1 只有「一个桌面会话」，v2 把 QQ 的私聊/群聊全部镜像
   进来，每条糖糖回复还能展开看她「怎么想的」。
   字段名与后端 agent/console_api.py + tools/desktop_bridge.py 严格对齐。
   ═══════════════════════════════════════════════════════════════ */

/**
 * 会话来源：
 *  - desktop    桌面本地会话（.会客厅.db）
 *  - private    QQ 私聊镜像（chat_log）
 *  - group      QQ 群聊镜像（chat_log）
 *  - monologue  独白频道——她没发出去的内在活动（日记 / 内心消化），只读
 */
export type ConsoleSourceKind = 'desktop' | 'private' | 'group' | 'monologue'

export type ConsoleSource = {
  /** 会话唯一键，传给 feed/send 等接口 */
  key: string
  kind: ConsoleSourceKind
  /** 本地=会话标题；私聊=昵称；群=群名 */
  title: string
  /** 最后一条消息预览（已截断） */
  subtitle: string
  updated_at: number
  /** 最后一条消息 id，用于未读判断；本地会话为 0 */
  last_id: number
  message_count: number
}

/**
 * 一条糖糖回复对应的「回合痕迹摘要」。只有 tangtang 消息可能非 null。
 * 详情要另外拉 /api/console/trace（点开折叠卡片时才请求）。
 */
export type ConsoleTraceSummary = {
  run_id: string
  reasoning_chars: number
  tool_count: number
  duration_ms: number
  /** 'completed' | 'failed'（failed 时卡片用红色左边框） */
  status: string
  event_key: string
}

/**
 * 一条消息里可以显示的媒体附件（图片 / 语音）。
 *
 * 后端会把「本地 file:// 路径」「QQ 临时 url」「chat_log.segments 里的 CQ 码」
 * 三种来源统一解析成这个形状——前端只需要 `consoleMediaUrl(bridge, path)`。
 * ⚠️ QQ 图/语的 url 带 rkey，会过期；过期后后端返回 404，UI 要能优雅降级
 * 成「媒体已过期」而不是裂图。
 */
export type ConsoleMediaItem = {
  type: 'image' | 'audio'
  /** 原文件名，alt 文本 / 下载名 */
  name: string
  /** 相对路径（形如 `/api/console/media?scope=…&id=…&n=0`），需拼 host+token */
  path: string
  /** 语音时长（秒）；未知为 0 */
  duration: number
  /** 来自本机文件（贴图 / TTS 成品）——UI 可标「本机」 */
  local: boolean
  /**
   * 字节在本地缓存里**确实拿得到**。
   *
   * QQ 图片/语音的临时链接几小时就失效，历史媒体基本全都取不回来——
   * 后端在 feed 时就查好了缓存，前端 `cached === false` 直接渲染「已过期」
   * 占位，**不要**再发请求（否则打开一个 100 条的群会话会当场打出几十个
   * 注定 410 的请求）。
   */
  cached: boolean
}

export type ConsoleFeedMessage = {
  /** 该会话内单调递增 */
  id: number
  role: 'user' | 'tangtang'
  /** 发送者显示名；糖糖固定 "糖糖" */
  speaker: string
  content: string
  /** unix 秒 */
  ts: number
  /**
   * chat_log 行的稳定键。
   *
   * 糖糖自己发的回复是 `delivery:<触发消息id>:<气泡序号>`，对方的消息是
   * `v2:<scope>:<ts>:<qq>:<msg_id>`。回合回放要用它去换 trace——因为
   * `turn_traces` 记的是**触发消息**的键，两者不是同一个值。
   */
  event_key: string
  trace: ConsoleTraceSummary | null
  /** 该消息附带的图片/语音；没有就是 undefined 或空数组 */
  media?: ConsoleMediaItem[]
  /** 独白条目类型：'journal' 日记 / 'digest' 内心消化；普通会话为空 */
  entry_kind?: 'journal' | 'digest' | ''
  /** 独白条目的情绪标签（'mixed' / 'thoughtful' …），原样显示 */
  mood?: string
  /** 独白条目的卡片标题（「10-23 的日记」「内心消化 · 承诺压力」） */
  heading?: string
}

/**
 * 会话搜索的一条命中。
 *
 * 搜的是**消息内容**（chat_log.message LIKE），不是会话标题——
 * 所以同一个人可能有几十条命中，前端按会话聚合展示。
 */
export type ConsoleSearchHit = {
  /** 会话 key，可直接传给 onSelectScope */
  scope: string
  kind: ConsoleSourceKind
  /** 会话显示名（昵称 / 群名 / 本地标题） */
  title: string
  /** chat_log 行 id —— 点进去要滚到这条 */
  id: number
  role: 'user' | 'tangtang'
  speaker: string
  /** 命中片段（前后各截一段，已把 CQ 码洗掉） */
  snippet: string
  /** 关键词在 snippet 中的起始下标（前端做高亮） */
  match_start: number
  /** 关键词长度 */
  match_len: number
  ts: number
}

export type ConsoleSearchResult = {
  hits: ConsoleSearchHit[]
  /** 命中总数（可能远大于 hits.length） */
  total: number
  /** 是否因为 limit 被截断 */
  truncated: boolean
}

export type ConsoleTraceStep = {
  name: string
  args: string
  result: string
  ms: number
}

export type ConsoleTrace = {
  id: number
  run_id: string
  event_key: string
  scope_id: string
  model: string
  /** 思维链全文，可能为空字符串 */
  reasoning: string
  tool_steps: ConsoleTraceStep[]
  prompt_chars: number
  duration_ms: number
  recorded_at: string
}

/**
 * 背景注入里的一块材料（后端 `fit_backgrounds(report=…)` 落库）。
 * 三态都要显示——只显示「保留了什么」等于把裁掉/丢掉的那部分藏起来。
 */
export type ConsoleTurnFlowBlock = {
  label: string
  /** 淘汰优先级（数字越小越该留下） */
  priority: number
  tokens: number
  tokens_before?: number
  /** 'kept' | 'truncated' | 'dropped' */
  status: string
  preview?: string
}

/**
 * 「她怎么想的」九段流程里的一段（2026-10-25 第 17 轮 ④ 扩展 + 二次整改）。
 *
 * 收到 → 路由与窗口 → 系统提示 → **背景注入（含每块三态/预算）** →
 * **记忆召回（含逐条记忆）** → 工具调用 → 生成（含 token 账本）→
 * 发送 → 对方反应。
 *
 * 二次整改（主人：「前端要直白、毫无保留地让用户看到糖糖的运作过程」）
 * 给每段加了六个**教学字段**，让不写代码的人也能看懂：
 *
 * | 字段 | 回答的问题 |
 * |---|---|
 * | `group` | 这站属于哪个大阶段（进入/理解/决策/输出） |
 * | `role` | 这一站在干什么 |
 * | `logic` | 判断依据是什么（为什么这样走） |
 * | `did` | 这一轮实际发生了什么（结论，仅供参考） |
 * | `status` | 四态：ok 正常 / warn 打折 / error 异常 / **skip 未触发（不是故障）** |
 * | `carries` | 交给下一站的东西（环节之间怎么配合） |
 *
 * `params[].doc` 是每个参数的**一句话解释**——光有「预算上限 1100t」，
 * 运营者不知道自己该不该在意。字段名与 `agent/console_api.py::_build_turn_flow` 严格对齐。
 */
export type ConsoleTurnFlowPhase = {
  /** 1..9 */
  phase: number
  /** inbound / routing / context / inject / recall / tools / generate / send / feedback */
  key: string
  label: string
  /** 阶段分组：进入 / 理解 / 决策 / 输出（连续，不跳） */
  group: string
  /** 这一站负责什么（一句人话） */
  role: string
  /** 判断依据（为什么这样走、按什么规则裁/选） */
  logic: string
  /** 这一轮实际发生了什么（结论，可能为空字符串） */
  did: string
  /** 四态。`skip` 不是故障——这一轮本来就不该有（没调工具/决定沉默/对方没回） */
  status: 'ok' | 'warn' | 'error' | 'skip'
  /** = status 是 ok 或 skip（保留旧的布尔字段，向后兼容） */
  ok: boolean
  ms: number
  items: { k: string; v: string }[]
  /** 全部参数明细（界面折叠展示；`doc` 是给小白的一句话解释） */
  params: { k: string; v: string; status?: string; doc?: string }[]
  /** 只属于这一站、交给下一站的东西 */
  carries: string[]
  /** 仅背景注入段：每块的标签/优先级/token/三态/内容预览 */
  blocks?: ConsoleTurnFlowBlock[]
  /** 仅记忆召回段：逐条记忆内容 */
  entries?: string[]
  /** 仅工具调用段：每个工具的性质/空手/她的理由（第 47 轮） */
  effects?: ConsoleToolEffects
  note: string
}

/**
 * 整条流程的体检结论（后端 `_summarize_turn_flow` 算好）。
 *
 * **为什么不让前端自己数**：「什么算问题」是判据，不是样式。前端再数一遍
 * 就有两个实现点，迟早漂移（第 47 条教训：同一语义 N 个实现点，漏一个就静默错）。
 */
export type ConsoleFlowSummary = {
  total: number
  ok: number
  warn: number
  error: number
  skip: number
  /** 一句话结论，直接显示在顶部横幅 */
  verdict: string
  /** 该先看哪一站（''=全绿） */
  focus_key: string
  focus_label: string
  /** 有问题的站（异常+打折），点一行可跳转 */
  broken: {
    phase: number
    key: string
    label: string
    status: string
    note: string
  }[]
}

/**
 * 账本里的一段 token（工具定义 / 系统提示词 / 历史对话 / 本轮消息）。
 *
 * ⚠ **分母纪律**：`in_context` 为 `true` 的三段才共用 `context.budget`
 * 这一个分母；`tools`（工具定义）是请求里的另一块，比整个上下文预算还大，
 * 所以它的 `share` 恒为 `null`——画在一起就是又一次「假超支」。
 */
export type ConsoleLedgerSection = {
  /** tools / system / history / user */
  key: string
  label: string
  tokens: number
  /** 是否计入「上下文预算」。工具定义不计入 */
  in_context: boolean
  /** 同分母内占比（只在 context 三段之间比）。工具那段恒为 null */
  share: number | null
  /** 这一段到底是什么（给不写代码的人看的一句话） */
  doc: string
}

/** 一个背景块的最终命运（保留 / 被裁 / 被淘汰）。 */
export type ConsoleLedgerBlock = {
  label: string
  priority: number | null
  tokens: number
  /** 裁之前多少 token（没裁过就与 tokens 相同） */
  before: number
  /** kept / truncated / dropped */
  status: string
  /** 被裁掉的百分比（只有 truncated 才有；前端不要自己算，判据在后端） */
  cut_pct: number | null
  preview: string
}

/**
 * 一条预警。
 *
 * `evidence` 是**触发它的原始字段**——不是给人看的漂亮话，是「你去核对这里」。
 * 主人要「有效的预警，取代日志」：没有 evidence 的预警等于让你信我，
 * 而不是让你自己能查。
 */
export type ConsoleLedgerWarning = {
  level: 'warn' | 'error'
  key: string
  text: string
  evidence: string
}

/**
 * 她这一轮「收到的 prompt」的账本（后端 `_build_prompt_ledger` 算好）。
 *
 * 数据一直躺在 `turn_traces.prompt_digest` 里，从来没有出口——这是第 47 轮
 * 补上的那个「有事实、没出口」。判据（warnings / verdict / headline）全在后端，
 * 前端只负责画和上色。
 */
export type ConsolePromptLedger = {
  sections: ConsoleLedgerSection[]
  context: {
    /** 上下文总预算（后端 `ContextBuilder.BUDGET_TOTAL`，6000） */
    budget: number
    /** 三段之和（不含工具定义） */
    used: number
    left: number | null
    used_pct: number | null
    over: boolean
    doc: string
  }
  /**
   * 实际输入总量（`TokenReport.total`）。**与 `context.budget` 不同分母**，
   * 界面上必须并列显示、禁止相除。为 0 表示这一轮没采到。
   */
  measured_total: number
  background: {
    /** 这一轮到底有没有背景材料参与（false 时不要显示「0%」） */
    injected: boolean
    /** 预算和逐块命运有没有采到（false = 采集缺口，不是没用预算） */
    measured: boolean
    limit: number
    used: number
    left: number | null
    used_pct: number | null
    protected_overflow: boolean
    evidence_reserve_used: boolean
    gap_notice: boolean
    kept: number
    truncated: number
    dropped: number
    blocks: ConsoleLedgerBlock[]
    doc: string
  }
  system_sections: string[]
  user_bg_labels: string[]
  memory_marks: { corrected: number; uncertain: number }
  history_count: number
  total_chars: number
  /** true = 四段分账没采到（只有 prompt 结构摘要）。此时不要说「材料齐」 */
  partial: boolean
  warnings: ConsoleLedgerWarning[]
  verdict: 'ok' | 'warn' | 'error' | 'skip'
  /** 一句话结论，直接显示 */
  headline: string
}

/** 一次工具调用的可读版：它是什么性质、拿到料没有、她的理由是什么。 */
export type ConsoleToolEffect = {
  name: string
  /** retrieval 检索 / perception 感知 / decision 决定 / action 动作 / memory_write 改记忆 / unknown */
  kind: string
  kind_cn: string
  ms: number
  chars: number
  /** 只有「拿材料」的类才谈得上空手——决定/动作类返回短是回执，不是失败 */
  empty: boolean
  /** hit 查到了 / empty 没查到 / done 已执行 / decision 她的决定 */
  verdict: string
  verdict_cn: string
  brief: string
  args: string
  result: string
  /** 决策类工具的入参里那句人话（`skip_response.reason` 之类）——「她凭什么这么决定」 */
  rationale: string
}

/**
 * 这一轮的工具效果汇总（后端 `_build_tool_effects` 算好）。
 *
 * 「空手率」是「她不知道」的**直接证据**，不是猜测：检索类工具返回
 * 「(记忆库中没有关于'陶艺'的内容)」时，她这轮就是没材料可用。
 */
export type ConsoleToolEffects = {
  items: ConsoleToolEffect[]
  /** 按类计数（retrieval/perception/decision/action/memory_write/unknown/total/empty） */
  counts: Record<string, number>
  /** 检索+感知的总次数（空手率的分母） */
  lookups: number
  empty_rate: number
  summary: string
  /** 她的决策理由（决策类工具入参里提取的原文） */
  rationale: string
  warnings: ConsoleLedgerWarning[]
  verdict: 'ok' | 'warn' | 'error' | 'skip'
  note: string
}

/**
 * 一条回复的完整回合回放（`/api/console/replay`）。
 *
 * 与 `ConsoleTrace` 的区别：这里除了思维链还带了「决定了什么」和「对方怎么
 * 反应」——两块数据分别来自 decision_runs 和 feedback。四段都可能是 null
 * （某一轮没留痕 / 对方还没反应 / 某张表读不出来），界面按段隐藏。
 */
export type ConsoleReplay = {
  message: {
    id: number
    content: string
    timestamp: string
    /** '糖糖' / '你（桌面代发）' / 对方 QQ 号 */
    speaker: string
    scope: string
  } | null
  trace: {
    run_id: string
    model: string
    reasoning: string
    reasoning_chars: number
    tools: ConsoleTraceStep[]
    duration_ms: number
    recorded_at: string
    /** true = 旧采集口径，这段思维链可能混入了情绪分类器的输出 */
    suspect: boolean
  } | null
  decision: {
    decision: string
    status: string
    tool_calls: string[]
    started_at: string
    finished_at: string
    /** 出错时的错误码（空字符串 = 没出错）。原来前端拿不到，只能看到「failed」两个字母 */
    error_code?: string
    /** 决策记录自己的模型/会话/关联号——核对「是不是同一条轮次」用 */
    model?: string
    correlation_id?: string
    recorded_at?: string
    run_id?: string
  } | null
  feedback: {
    sentiment: string
    confidence: number
    user_reaction: string
    reply_ms: number
    timestamp: string
    direction_verified: boolean
  } | null
  /**
   * 九段流程：收到消息 → 路由与窗口 → 系统提示 → 背景注入 → 记忆召回 →
   * 工具调用 → 生成 → 发送 → 对方反应。
   *
   * ⚠ 可选：后端 2026-10-25 之后才加这个字段（更早的 /replay 响应没有它），
   * 界面用 `data.flow ?? []` 兜底，不要写成必填。
   */
  flow?: ConsoleTurnFlowPhase[]
  /**
   * 整条链的体检结论（顶部横幅）。同样可选——老后端没有。
   * 缺省时前端退化成「只画流程、不显示结论」，不报错。
   */
  flow_summary?: ConsoleFlowSummary
  /**
   * 她这一轮收到的 prompt 账本（第 47 轮）。**可选**——老后端没有这个字段，
   * 界面整块不渲染，不要显示成「0 token」。
   */
  prompt_ledger?: ConsolePromptLedger
  /**
   * 她这一轮的工具效果（分类 + 空手 + 决策理由）。同样可选。
   */
  tool_effects?: ConsoleToolEffects
}

/* ── 群管理（场景 + 黑名单 合并页）────────────────────────────
   数据来源分两半：读走 bot（要 chat_log 的消息量），写走 bridge
   （只碰 config.yaml）。所以保存后必须重启糖糖才生效。
   ──────────────────────────────────────────────────────────── */

/** 一个可用场景。`name` 是配置里存的键，`display` 给人看。 */
export type GroupScenario = {
  name: string
  display: string
  type: string
  description: string
}

/** 场景绑定的一个目标（群里某个人，或全局私聊对象）。 */
export type GroupTarget = {
  qq: string
  /** 场景的 name 或 display，读回来是什么就原样放回 */
  scenario: string
  /** 昵称；查不到就是空串，界面回退显示 QQ 号 */
  name: string
}

export type ManagedGroup = {
  id: string
  name: string
  member_count: number
  max_members: number
  /** 群里历史消息总数（排序依据：她在哪个群真的活跃） */
  messages: number
  last_active: string
  /** 群默认场景 name（'' = 默认闲聊） */
  scenario: string
  /** 群里被单独指定场景的人 */
  targets: GroupTarget[]
  blacklisted: boolean
  /** config.yaml 里有没有这个群的配置节点 */
  in_config: boolean
}

export type GroupManageData = {
  groups: ManagedGroup[]
  scenarios: GroupScenario[]
  global_targets: GroupTarget[]
  blacklist: { groups: string[]; private_users: string[] }
}

export type GroupManageSavePayload = {
  /** 群号 → { 群默认场景, 群内定向 }；没出现的群保持原样 */
  groups: Record<string, { scenario: string; targets: Record<string, string> }>
  blacklist: { groups: string[]; private_users: string[] }
  /** 全局（私聊）场景目标，QQ 号 → 场景 display */
  global_targets: Record<string, string>
}

export type GroupManageSaveResult = {
  ok: boolean
  /** 永远是 true：这两块配置只在 bot 启动时读一次 */
  needs_restart: boolean
  backup: string
  normalized: string[]
}

/* ── 记忆图谱 ────────────────────────────────────────────────
   后端把节点/边都组装好了，id 带类型前缀（person:/cluster:/fact:/
   memory:/cat:），前端只管画。
   ──────────────────────────────────────────────────────────── */

export type GraphNodeKind = 'person' | 'category' | 'cluster' | 'fact' | 'memory'

export type GraphNode = {
  id: string
  kind: GraphNodeKind
  label: string
  /** 建议半径（px）——后端按「她记得多少」开方算好的，别自己再套一层非线性 */
  size: number
  qq?: string
  meta: Record<string, string | number>
}

export type GraphEdge = {
  source: string
  target: string
  /** 'has' | 'contains' | 'remembers' */
  kind: string
  weight: number
}

export type MemoryGraphStats = {
  people: number
  people_known: number
  clusters: number
  facts: number
  memories: number
  memories_active: number
  evidence: number
  episodes: number
}

export type MemoryGraphMemory = {
  id: number
  key: string
  value: string
  importance: number
  origin: string
  event_time: string
}

export type MemoryGraphEpisode = {
  id: number
  title: string
  summary: string
  time_start: string
  time_end: string
}

export type MemoryGraphSide = {
  person: Record<string, string | number>
  memories: MemoryGraphMemory[]
  episodes: MemoryGraphEpisode[]
}

export type MemoryGraph = {
  mode: 'overview' | 'person'
  /** person 模式下是中心那个人的 QQ 号 */
  center: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** 只有 overview 有 */
  stats: MemoryGraphStats | null
  /** 只有 person 有 */
  side: MemoryGraphSide | null
}

/* ── 记忆剖面（七维索引 + 六层架构）───────────────────────────
   `memories` 表上七个正交维度：key / cognitive / retention /
   trust / status / origin / importance。后端已聚合好分布，前端只做展示
   与「按维度筛选」。
   ──────────────────────────────────────────────────────────── */

export type DimItem = { name: string; count: number }

export type MemoryProfileCounts = {
  memories: number
  active: number
  retracted: number
  superseded: number
  episodic: number
  semantic: number
  /** 关于这个人（target_qq） */
  about: number
  /** 这个人说的（qq_id 且不指向他人） */
  spoken: number
  clusters: number
  facts: number
  episodes: number
  /** 本次带回的记忆条数（上限 1000，可能小于 `memories`） */
  returned: number
  /** 既是 ta 产生、又指向 ta 自己的记忆（自我陈述） */
  self_talk: number
  evidence: number
  /** event_time 与写入时间都缺失、无法归到任何月份的条数 */
  undated: number
  /** 能进入可信召回的活跃记忆数（= 上下文里真会出现的条数，2026-10-24） */
  trusted_injectable: number
  /** 活跃但被信任门挡住（legacy/未验证）的条数——她「记得」却用不上 */
  trusted_blocked: number
}

export type MemoryProfileDimensions = {
  key: DimItem[]
  cognitive: DimItem[]
  retention: DimItem[]
  trust: DimItem[]
  status: DimItem[]
  origin: DimItem[]
  /** 1..10，固定十项，count 可能为 0 */
  importance: DimItem[]
}

export type MemoryTimelinePoint = {
  month: string
  /** 该月记忆总数（event_time 缺失时按写入时间 ingested_at 归档） */
  count: number
  superseded: number
  /** 其中真正有「发生日期」的条数——用来区分「真知道」和「按写入日归档」 */
  by_event: number
}

export type MemoryEvidence = {
  chat_id: number
  message: string
  timestamp: string
  is_bot_reply: boolean
}

export type MemoryProfileFact = {
  id: number
  fact: string
  confidence: number
  importance: number
}

export type MemoryProfileCluster = {
  id: number
  category: string
  category_label: string
  title: string
  summary: string
  fact_count: number
  permanence: string
  facts: MemoryProfileFact[]
}

export type MemoryProfileMemory = {
  id: number
  key: string
  value: string
  importance: number
  origin: string
  event_time: string
  /** 认知类型：episodic（情景）/ semantic（语义） */
  cognitive: string
  /** 保留策略：permanent / durable / normal / transient */
  retention: string
  /** 可信度：corrected / verified / unverified / legacy_unverified */
  trust: string
  /** 生命周期：active / superseded / retracted / fulfilled */
  status: string
  /** 这条记忆能指回的原话 */
  evidence: MemoryEvidence[]
}

export type MemoryProfileEpisode = {
  id: number
  title: string
  summary: string
  time_start: string
  time_end: string
}

export type MemoryCapabilities = {
  chat_log: number
  inbound_events: number
  evidence: number
  memories: number
  memories_active: number
  memories_episodic: number
  memories_semantic: number
  embeddings: number
  embedding_dims: number
  embedding_cover: number
  embedding_failures: number
  clusters: number
  facts: number
  people: number
  people_profiled: number
  /** 有画像且能注入的人数（verified/manual/corrected 且非 dirty） */
  people_profiled_injectable: number
  /** 有画像但被信任门挡住的（legacy/dirty）——存在却不进上下文 */
  people_profiled_blocked: number
  episodes: number
  journal: number
  digests: number
  extraction_jobs: number
  extraction_cursors: number
  /**
   * 信任门两侧（2026-10-24 立项研究第三期）。此前信任门安静地工作，
   * 「挡掉了多少」从未出现在任何界面上——全库 74% 的记忆其实是死的。
   */
  trusted_injectable: number
  trusted_blocked: number
  /** 可信召回覆盖率 = trusted_injectable / memories_active */
  trusted_cover: number
  /** 有证据字段的活跃记忆数（补证据这件事的靶子） */
  memories_with_evidence: number
  /** 证据覆盖率 = memories_with_evidence / memories_active */
  evidence_cover: number
  /** 变更留痕条数（memory_revisions，2026-10-24 起） */
  revisions: number
  /** 留痕能力生效起点（最早一条流水的时刻）；空串 = 还没有任何记录 */
  revision_since: string
}

export type MemoryProfile = {
  person: Record<string, string | number>
  /**
   * 称呼（2026-10-24 P0/P1）。**刻意不塞进 `person`**：
   * `person` 代表 people 表的列（全是 string|number），而这里是派生视图——
   * 一个 boolean 一个数组，硬塞会让索引签名与运行时自相矛盾，页面就只能靠
   * `as` 断言骗过编译器。放顶层让声明与数据真正一致。
   */
  addresses: MemoryProfileAddresses
  counts: MemoryProfileCounts
  dimensions: MemoryProfileDimensions
  timeline: MemoryTimelinePoint[]
  layers: {
    clusters: MemoryProfileCluster[]
    memories: MemoryProfileMemory[]
    episodes: MemoryProfileEpisode[]
    evidence: Array<{
      memory_id: number
      chat_id: number
      message: string
      timestamp: string
      is_bot_reply: number
    }>
  }
  capabilities: MemoryCapabilities
}

/* ── 称呼分层（`memory_profile.addresses`，2026-10-24 P0/P1）──
   为什么必须拆开：
     · `nickname` = QQ 昵称，**跨群稳定身份**；可能为空——昵称只在档案创建
       那一刻写入，历史上有人的创建路径没带昵称，之后永不更新（P0 修复：
       之后只补空值、不覆盖已有值）。
     · `aliases` = 对方**自称**过的名字 / 系统自动记的外号。自称可能来自
       角色扮演（「米雪儿」是《卡拉彼丘》角色），**绝不写进 nickname**。
   混在一起展示，等于让「她自称米雪儿」看起来像「她叫米雪儿」。
   ──────────────────────────────────────────────────────────── */

export type MemoryProfileAlias = {
  alias: string
  /** 'self_declared'（对方自称）| 'auto'（系统自动记）| 'llm'（模型记）；旧数据可能为空串 */
  source: string
  /** 这个称呼依据哪条记忆（0 = 无指针的旧数据） */
  memory_id: number
  /** 'YYYY-MM-DD HH:MM' */
  created_at: string
}

export type MemoryProfileAddresses = {
  /** QQ 昵称，可能是空串 */
  nickname: string
  /** true = 档案里没有昵称（此前 27 人是这种状态） */
  nickname_missing: boolean
  aliases: MemoryProfileAlias[]
}

/* ── 记忆演化史 ──────────────────────────────────────────────
   `superseded_by` 是 memories.id 指针，但它是 N→1 的（画像重合成时
   多条旧事实被同一条新画像取代），所以链条是合并树：一条主线 head→leaf
   外加被并入的兄弟节点。
   ──────────────────────────────────────────────────────────── */

export type HistoryVersion = {
  id: number
  key: string
  value: string
  /** active / superseded / retracted / fulfilled */
  status: string
  /** 事件时间；缺失时退回写入时间 */
  time: string
  valid_to: string
  origin: string
  importance: number
}

/** 被并入的兄弟节点：`into` 指向它并入的那一版 */
export type HistoryMerged = HistoryVersion & { into: number }

export type MemoryChain = {
  head_id: number
  leaf_id: number
  /** 末端的 key（profile_synthesis / fact_synthesis / …），用来判断这是哪种演化 */
  kind: string
  /** [最早时间, 最晚时间] */
  span: [string, string]
  versions: HistoryVersion[]
  merged: HistoryMerged[]
}

export type MemoryHistoryGroup = {
  key: string
  count: number
  /** 该主题下按时间正序的前 30 条 */
  items: HistoryVersion[]
}

export type MemoryHistory = {
  /** 这个人显示用的名字（可能为空串） */
  person: string
  owner: string
  summary: {
    total: number
    active: number
    superseded: number
    /** 参与版本链的条数 */
    chained: number
    chains: number
    /** 被推翻但不知道被谁取代的条数（数据缺口，如实展示） */
    orphan_superseded: number
  }
  chains: MemoryChain[]
  by_key: MemoryHistoryGroup[]
  orphans: HistoryVersion[]
}

/* ── 记忆变更流水（memory_revisions，append-only，2026-10-24 起）──
   与「版本链」互补、**不可合并**：
     · 版本链 = 从 superseded_by / valid_to 推出来的「整行被替代」，
       能覆盖历史，但只认得那一种事件；
     · 变更流水 = 每一次写操作的事实记录，能表达内容就地改写 / 物理删除 /
       调 importance / 调 trust，但**只有 2026-10-24 之后**的数据。
   UI 上必须分开展示——混进一条时间线就分不清哪条是证据、哪条是推论。
   ──────────────────────────────────────────────────────────── */

export type MemoryRevisionActorCount = { actor: string; count: number }
export type MemoryRevisionOpCount = { op: string; count: number }

export type MemoryRevisionRow = {
  id: number
  /** memories / cluster_facts / fact_clusters / people */
  entity: string
  entity_id: string
  qq: string
  /** update / status / supersede / retract / fulfill / merge / delete / migrate */
  op: string
  /** 被改的列名（value / importance / status …）；空串 = 整行快照（主要是 delete） */
  field: string
  /** 删除类操作的 before 是整行 JSON 快照 */
  before: string
  after: string
  reason: string
  actor: string
  /** 'YYYY-MM-DD HH:MM:SS' */
  at: string
}

export type MemoryRevisionSummary = {
  total: number
  /** 本次带回的条数（limit 钳 20..500） */
  returned: number
  by_actor: MemoryRevisionActorCount[]
  by_op: MemoryRevisionOpCount[]
  first_at: string
  last_at: string
}

export type MemoryRevisions = {
  person: string
  owner: string
  summary: MemoryRevisionSummary
  rows: MemoryRevisionRow[]
  /** 一句话口径说明，直接展示在区块头部 */
  note: string
}


/**
 * 发送结果。mode 告诉你这次是「只在本地跟糖糖说」(local)
 * 还是「真的发到 QQ 了」(qq)。
 */
export type ConsoleSendResult = {
  ok: boolean
  mode: 'local' | 'qq'
  reply?: string
  message_id?: number
}

declare global {
  interface Window {
    desktop?: {
      platform: string
      minimize: () => Promise<void>
      toggleMaximize: () => Promise<boolean>
      close: () => Promise<void>
      isMaximized: () => Promise<boolean>
      onWindowState: (cb: (s: { maximized: boolean }) => void) => () => void
      getBridgeInfo: () => Promise<{
        info: BridgeInfo | null
        error: string | null
        root: string
      }>
      onBridgeReady: (cb: (info: BridgeInfo) => void) => () => void
      onBridgeExit: (cb: (s: { code: number | null; error: string | null }) => void) => () => void
    }
  }
}

let cached: BridgeInfo | null = null

export async function ensureBridge(timeoutMs = 20000): Promise<BridgeInfo> {
  if (cached) return cached
  const desktop = window.desktop
  if (!desktop) throw new Error('不在桌面端环境中（缺少 preload 桥）')

  const initial = await desktop.getBridgeInfo()
  if (initial.info) {
    cached = initial.info
    return cached
  }

  return new Promise<BridgeInfo>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(initial.error || '后端桥启动超时')),
      timeoutMs,
    )
    const off = desktop.onBridgeReady((info) => {
      clearTimeout(timer)
      off()
      cached = info
      resolve(info)
    })
  })
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const bridge = await ensureBridge()
  const res = await fetch(`http://127.0.0.1:${bridge.port}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${bridge.token}`,
      ...(init?.headers ?? {}),
    },
  })
  const text = await res.text()
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      msg = JSON.parse(text).error ?? msg
    } catch {
      /* keep default */
    }
    throw new Error(msg)
  }
  return text ? (JSON.parse(text) as T) : (undefined as T)
}

/* ── 可编辑资产（2026-10-26 第 49 轮）─────────────────────────
   主人要「参数完全暴露可以改，自由度拉满」：人格卡 / 协议条文 / 场景规则 /
   知识库（恋爱军师、亲密范本）都能在桌面上改。
   白名单、备份、校验全在后端 `tools/asset_edit.py`——前端**不做任何路径判断**，
   拿到的 `error` 直接显示（判据只有一处）。
   ──────────────────────────────────────────────────────────── */

/** 特殊模式包（2026-10-26 第五轮）：一个场景 + 一批范本。
 *
 * `require_marker`：要不要她判断「该进入」才生效。false = 挂上就生效。
 * `ttl_seconds`：0 = 永久（默认）；>0 = 激活后多少秒过期。
 */
export type ScenarioPack = {
  id: string
  scenario: string
  dir: string
  label: string
  budget: number
  require_marker: boolean
  ttl_seconds: number
  /** 群聊是否也注入（默认 false = 只私聊）。开了不看激活状态。 */
  allow_groups?: boolean
  /** 下面几项是后端算出来的现况（保存时不要回传） */
  files?: number
  chars?: number
  slots?: Record<string, number>
  warnings?: string[]
  usage_pct?: number
  dir_exists?: boolean
  scenario_known?: boolean
  error?: string
}

export type ScenarioOption = { name: string; display: string; type: string }

export type AssetFile = {
  path: string
  name: string
  size: number
  mtime: string
  editable: boolean
  /** 身份卡的人类可读名字（从文件首行 `# 标题` 读，不硬编码） */
  label?: string
  /** 是否默认卡（`role_card.md`）——只有它缺失才会回落 config.yaml */
  is_default?: boolean
}

/** 目录分类（第 49 轮 B5）。**名字就是语义**：
 *  `slot` 数字前缀 → 进常驻注入；`archive` 下划线前缀 → 归档不进；
 *  `category` 普通分类（只在恋爱军师这类无语义前缀的地方出现）。 */
export type AssetDir = {
  path: string
  name: string
  kind: 'slot' | 'archive' | 'category'
  depth: number
  empty: boolean
  /** 她会不会读到这个目录里的内容——界面必须如实显示，不然就是撒谎 */
  in_injection: boolean
}

/** 一张场景人格卡（`scenarios/*.yaml` 的 `display`/`description`/`type`）。
 *
 * 为什么要卡片而不是只给文件名：她在群里挂的是「🧠 心理陪伴」，
 * 文件却叫 `psychology.yaml`——两边对不上，用户就不知道自己在改哪张卡。 */
export type ScenarioCard = {
  path: string
  name: string
  display: string
  description: string
  /** `overlay` 叠加在人格上 / `replace` 替换掉一部分人格 */
  type: string
  /** 解析失败时的说明（卡片照样显示——那正是要改的那张） */
  warning: string
  size: number
  mtime: string
  editable: boolean
}

export type AssetSpec = {
  key: string
  label: string
  /** 人格 / 规则 / 必要背景知识 */
  group: string
  /** 这一类资产是什么、为什么可写 */
  desc: string
  root: string
  exists: boolean
  files: AssetFile[]
  /** 这份内容怎么进她上下文：
   *  - `forced` 每轮都在（身份卡、协议条文）
   *  - `scenario` **对应模式激活后才注入**（不是常驻！私聊 + 已挂该模式 + 进入该模式）
   *  - `retrieval` 她主动查了才有
   *  - `none` 不进上下文 */
  injection: 'forced' | 'scenario' | 'retrieval' | 'none'
  /** 触发它的场景 name（只有 scenario 型有值） */
  triggered_by: string[]
  /** 场景类的卡片（其余类为空数组） */
  cards: ScenarioCard[]
  /** 能不能加篇目（单文件资产、以及 prompts/ 这种固定文件名的不行） */
  can_create_file: boolean
  /** 能不能在树里删篇目（只开放给知识类；其他类用「恢复默认」或去群管理） */
  can_delete: boolean
  /** 命名规则说明；空串 = 这一类不支持建目录 */
  dir_rule: string
  can_mkdir: boolean
  dirs: AssetDir[]
  /** 这一类接受的后缀（新建篇目时补默认后缀用） */
  exts: string[]
}

export type AssetGroup = {
  group: string
  /** 分组整体说明（「必要背景知识」要说明三种进入方式） */
  group_desc: string
  items: AssetSpec[]
}

/** 一条协议文本的覆盖状态：`active` = `prompts/<name>.md` 有内容 → 用它，否则回落内置默认。 */
export type PromptOverride = {
  name: string
  const: string
  path: string
  exists: boolean
  active: boolean
  chars: number
  /** 分组 key（`understand` / `reply` / …）—— 组的唯一真相是 `prompts/_groups.yaml` */
  group: string
  /** 分组标题（中文，直接显示），来自组表，不来自这条文件 */
  group_title: string
  /** 这一组的整体说明（「什么样的场合会用到这组协议」） */
  group_desc: string
  /** false = 文件头写的 `group` 不在组表里 → 界面标红，会落到「未分组」 */
  group_known: boolean
  /** 一句话作用：改它会改变什么行为 */
  desc: string
  /** 来源：空串 = 原生协议；「知识蒸馏」= 从知识语料蒸馏进协议的那两条 */
  origin: string
  /** 文件头还残留着已失效的 `group_title`（2026-10-26 起组标题只在组表里改） */
  stale_group_title: boolean
}

/** 协议分组（顺序 = 界面顺序，`files` = 这一组的条目数）。 */
export type PromptGroup = {
  key: string
  title: string
  desc: string
  files: number
}

export type PromptGroups = {
  ok: boolean
  path: string
  exists: boolean
  groups: PromptGroup[]
  /** `group:` 写成组表里没有的 key 的条目数（界面会显示「未分组」） */
  unknown_files: number
}

export type AssetList = {
  groups: AssetGroup[]
  backup_root: string
  prompt_overrides: PromptOverride[]
}

/** 知识库两条通道的现状（`tools/knowledge_status.py` 是唯一口径）。 */
export type KnowledgeStatus = {
  retrieval: {
    /** 磁盘上**可被检索**的文档数（敏感目录已排除） */
    disk: number
    /** 索引里真有记录的文档数 */
    indexed: number
    /** 磁盘有、索引没有——这些查不到 */
    missing: string[]
    /** 索引有、磁盘没了——重载时会清掉 */
    orphan: string[]
    index_mtime: string
    index_bytes: number
    index_readable: boolean
  }
  /** 场景触发通道：对应模式激活时才注入，平时一个字都不进上下文 */
  scenario_channel: { files: number; slots: string[]; trigger: string }
  /** 按设计不进检索索引的目录（界面**只显示数量**，目录名是使用者的内容设定） */
  protected_dirs: string[]
  needs_sync: boolean
  sync_hint: string
}

export type AssetBackup = { dir: string; path: string; size: number }

export type AssetContent = {
  path: string
  group: string
  label: string
  desc: string
  content: string
  size: number
  mtime: string
  backups: AssetBackup[]
  /** 改完怎么生效（「保存后重启」之类）——**必须显示**，不能让人以为存完就变了 */
  apply_hint: string
}

export type AssetWriteResult = {
  ok: boolean
  path: string
  backup: string
  /** 非阻塞提醒（例如「这份 md 现在是空的」） */
  warnings: string[]
  apply_hint: string
  size: number
  /** 场景卡改名时跟着改过去的挂载点（'2524950083' 这类）。空 = 没受影响 */
  renamed_mounts: string[]
}

// ── 高级调参（第 49 轮 B4）────────────────────────────────────
// 结构完全由后端 `agent/tuning.py` 的 `ui_payload()` 决定——
// 前端不另存一份 schema.json，否则两处必然漂移。

export type TuningItem = {
  key: string
  /** 代码里的常量名，例如 `PROFILE_MAX_LEN` */
  const: string
  label: string
  /** 作用说明（人话） */
  desc: string
  kind: 'int' | 'float'
  value: number
  default: number
  lo: number
  hi: number
  /** 源码位置 `agent/protocols.py:445` */
  source: string
  changed: boolean
}

export type TuningGroup = {
  key: string
  title: string
  items: TuningItem[]
}

export type TuningPayload = {
  groups: TuningGroup[]
  total: number
  changed: number
  config_key: string
  /** 「改完要重启」的说明——必须显示 */
  note: string
}

export type TuningSaveResult = {
  ok: boolean
  errors?: string[]
  saved?: Record<string, number>
  backup?: string | null
  tuning?: TuningPayload
  note?: string
}

export const api = {
  health: () => request<{ ok: boolean; root: string; python: string }>('/api/health'),
  stats: () => request<StatCounts>('/api/stats'),
  status: () => request<BotStatus>('/api/status'),
  models: () => request<ModelsResponse>('/api/models'),
  groups: () => request<{ groups: GroupInfo[]; count: number }>('/api/groups'),
  // ── 可编辑资产（人格 / 规则 / 场景 / 知识）────────────────────
  assets: () => request<AssetList>('/api/assets'),
  assetRead: (path: string) =>
    request<AssetContent>('/api/assets/read?path=' + encodeURIComponent(path)),
  assetWrite: (path: string, content: string) =>
    request<AssetWriteResult>('/api/assets/write', {
      method: 'POST',
      body: JSON.stringify({ path, content }),
    }),
  assetRestore: (path: string, backup: string) =>
    request<AssetWriteResult>('/api/assets/restore', {
      method: 'POST',
      body: JSON.stringify({ path, backup }),
    }),
  knowledgeStatus: () => request<KnowledgeStatus>('/api/knowledge/status'),
  assetReset: (path: string) =>
    request<{ ok: boolean; path: string; deleted: boolean; apply_hint: string }>(
      '/api/assets/reset',
      { method: 'POST', body: JSON.stringify({ path }) },
    ),
  // ── 结构管理（第 49 轮 B5）────────────────────────────────
  assetRenameDir: (path: string, name: string) =>
    request<{ ok: boolean; path: string; new_path: string; note?: string }>(
      '/api/assets/rename-dir',
      { method: 'POST', body: JSON.stringify({ path, name }) },
    ),

  scenarioPacks: () =>
    request<{ packs: ScenarioPack[]; available_scenarios: ScenarioOption[] }>(
      '/api/scenario/packs',
    ),

  scenarioPacksSave: (packs: ScenarioPack[]) =>
    request<{ ok: boolean; packs: ScenarioPack[] }>('/api/scenario/packs/save', {
      method: 'POST',
      body: JSON.stringify({ packs }),
    }),

  assetRename: (path: string, name: string) =>
    request<{ ok: boolean; path: string; new_path: string; backup: string; note?: string }>(
      '/api/assets/rename',
      { method: 'POST', body: JSON.stringify({ path, name }) },
    ),

  // ── 协议分组（2026-10-26：组标题从文件头收敛到 prompts/_groups.yaml）──
  promptGroups: () => request<PromptGroups>('/api/prompts/groups'),

  promptGroupRename: (key: string, title: string) =>
    request<{
      ok: boolean
      key: string
      title: string
      backup: string
      created: boolean
      apply_hint: string
    }>('/api/prompts/group-rename', {
      method: 'POST',
      body: JSON.stringify({ key, title }),
    }),
  assetCreate: (path: string, content = '') =>
    request<{
      ok: boolean
      path: string
      created: boolean
      warnings: string[]
      apply_hint: string
      size: number
    }>('/api/assets/create', { method: 'POST', body: JSON.stringify({ path, content }) }),
  assetDelete: (path: string) =>
    request<{
      ok: boolean
      path: string
      deleted: boolean
      backup: string
      apply_hint: string
    }>('/api/assets/delete', { method: 'POST', body: JSON.stringify({ path }) }),
  assetMkdir: (path: string) =>
    request<{
      ok: boolean
      path: string
      name: string
      kind: AssetDir['kind']
      in_injection: boolean
      warnings: string[]
      apply_hint: string
    }>('/api/assets/mkdir', { method: 'POST', body: JSON.stringify({ path }) }),
  assetRmdir: (path: string) =>
    request<{ ok: boolean; path: string; deleted: boolean; apply_hint: string }>(
      '/api/assets/rmdir',
      { method: 'POST', body: JSON.stringify({ path }) },
    ),
  // ── 高级调参（代码常量开放）────────────────────────────────
  tuning: () => request<TuningPayload>('/api/tuning'),
  tuningSave: (values: Record<string, number>) =>
    request<TuningSaveResult>('/api/tuning/save', {
      method: 'POST',
      body: JSON.stringify({ values }),
    }),
  config: () =>
    request<{
      raw: Record<string, unknown>
      resolved: Record<string, unknown>
      /** false = 没有 config.yaml 且模板也不在（前端要给「去跑安装器」的提示） */
      configured: boolean
      /** 这次桥启动时有没有从 config.example.yaml 自举 */
      bootstrapped: boolean
      example_available: boolean
    }>('/api/config'),
  saveConfig: (config: Record<string, unknown>) =>
    request<{ ok: boolean; normalized: string[]; backup: string }>('/api/config', {
      method: 'POST',
      body: JSON.stringify({ config }),
    }),
  fetchModels: (base_url: string, api_key: string) =>
    request<{ models: string[]; count: number; endpoint: string }>('/api/models/fetch', {
      method: 'POST',
      body: JSON.stringify({ base_url, api_key }),
    }),
  /**
   * 列出某个 Ollama 端点上的模型（含「能不能看图」）。
   *
   * 以前 `ollama_model` 靠手填字符串，填错要等真跑起来才暴露。现在从
   * `/api/tags` + `/api/show` 自动拉——对标 DSH Harness 的 provider.models 清单。
   */
  ollamaModels: (host: string) =>
    request<OllamaModelsResponse>('/api/models/ollama', {
      method: 'POST',
      body: JSON.stringify({ host }),
    }),
  /** 「选模型」面板的统一清单：云端档位 + 本机 Ollama，按 provider 分组。 */
  modelsPicker: () => request<ModelsPickerResponse>('/api/models/picker'),
  /**
   * 面板里直接选模型（不跳设置页）：同时定下「打标方式 + 模型」。
   * `kind="ref"` → 档位（provider=api）；`kind="ollama"` → 本机模型（provider=ollama）。
   */
  galleryTagModel: (payload: { kind: 'ref' | 'ollama'; value: string }) =>
    request<{
      ok: boolean
      backup: string
      provider: string
      model: string
      provider_ready: string
    }>('/api/gallery/tag/model', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // ── 连接密钥 / SnowLuma / 连接自检（2026-10-26 桌面端主入口化）──────
  secrets: () => request<SecretsResponse>('/api/secrets'),
  saveSecret: (name: string, value: string) =>
    request<{ ok: boolean; name: string; configured: boolean; masked: string; backup: string }>(
      '/api/secrets/save',
      { method: 'POST', body: JSON.stringify({ name, value }) },
    ),
  snowlumaStatus: () => request<SnowlumaStatus>('/api/snowluma/status'),
  startSnowluma: () =>
    request<{
      ok: boolean
      pid?: number
      already?: boolean
      dir?: string
      webui_url: string
      note?: string
    }>('/api/snowluma/start', { method: 'POST', body: '{}' }),
  // 运行中要的是「重启」：只调 start 会走 already 分支回来，界面看着像没反应。
  restartSnowluma: () =>
    request<{
      ok: boolean
      pid?: number
      already?: boolean
      restarted?: boolean
      dir?: string
      webui_url: string
      note?: string
    }>('/api/snowluma/restart', { method: 'POST', body: '{}' }),
  connectCheck: () => request<{ results: CheckResultItem[] }>('/api/connect-check'),
  botStart: () =>
    request<{ ok: boolean; pid?: number; already?: boolean }>('/api/bot/start', {
      method: 'POST',
      body: '{}',
    }),
  botStop: () =>
    request<{ ok: boolean; already?: boolean }>('/api/bot/stop', {
      method: 'POST',
      body: '{}',
    }),
  chat: (message: string) =>
    request<{ reply: string; model: string; provider: string }>('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
  activateModel: (payload: { id: string; thinking_effort?: string; thinking?: string }) =>
    request<{
      ok: boolean
      active: string
      model: string
      thinking_effort: string
      backup: string
    }>('/api/models/activate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ── 图库 ──────────────────────────────────────────
  gallery: (query: GalleryQuery) => {
    const qs = new URLSearchParams()
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
    })
    return request<GalleryResponse>(`/api/gallery?${qs.toString()}`)
  },
  // ── 图库打标（2026-10-26 第 63 轮；2026-10-27 改为「按图库页当前库打标」）──
  //
  // 打标是**长任务**（本地模型约 1.5 秒一张），所以 start 立刻返回，
  // 进度靠 status 轮询。别把 run 写成同步等待——那会让整个界面卡住几十秒。
  //
  // `source` 用的是图库页那套库 key（sticker / murasame / michele / cg）：
  // 「打哪些图」由图库页当前选中的库决定，不是另造一个「当前目录」。
  galleryTagStatus: (source = '') => {
    const qs = new URLSearchParams()
    if (source) qs.set('source', source)
    const q = qs.toString()
    return request<GalleryTagStatus>(`/api/gallery/tag/status${q ? `?${q}` : ''}`)
  },

  /**
   * 试打一张：只跑模型、**不写 metadata.json**。
   *
   * 为什么要它：配好 API Key / 本地模型之后，先让用户看到「这条路通不通、
   * 模型到底吐什么」。否则第一次就是几十张批量失败，而失败原因（key 填错、
   * 模型名不对、显存不够）往往要翻日志才知道。`raw` 是模型原话——
   * 标签提取错了，得先看它是怎么描述的。
   */
  galleryTagProbe: (payload: { source: string; name: string }) =>
    request<{
      ok: boolean
      error?: string
      name?: string
      provider?: string
      model?: string
      raw?: string
      tags?: {
        persons?: string
        act?: string
        emotions?: string
        scene_tags?: string
        desc?: string
      }
      rename_to?: string
      note?: string
    }>('/api/gallery/tag/probe', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  galleryTagRun: (payload: GalleryTagRunRequest = {}) =>
    request<{
      ok: boolean
      started?: boolean
      total?: number
      message?: string
      error?: string
      provider?: string
      dir?: string
      scope?: string
      overwrite?: boolean
    }>('/api/gallery/tag/run', { method: 'POST', body: JSON.stringify(payload) }),

  galleryTagCancel: () =>
    request<{ ok: boolean; running: boolean; message?: string }>('/api/gallery/tag/cancel', {
      method: 'POST',
      body: '{}',
    }),

  galleryTags: (payload: { source: string; names: string[]; add?: string[]; remove?: string[] }) =>
    request<{ ok: boolean; changed: number; backup: string | null }>('/api/gallery/tags', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  galleryTrash: (payload: { source: string; names: string[] }) =>
    request<{
      ok: boolean
      moved: string[]
      missing: string[]
      trash_dir: string
      backup: string | null
    }>('/api/gallery/trash', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  galleryReveal: (payload: { source: string; name: string }) =>
    request<{ ok: boolean; path: string }>('/api/gallery/reveal', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ── 会客厅（糖糖本体会话）─────────────────────────
  // 说明：本体未启动时这些接口会返回 4xx/5xx，request 抛错。
  // 页面必须先看 consoleStatus()，available=false 时走引导态。
  consoleStatus: () => request<ConsoleStatus>('/api/console/status'),
  consoleSessions: () =>
    request<{ sessions: ConsoleSession[] }>('/api/console/sessions'),
  consoleCreateSession: (title?: string) =>
    request<{ id: string; title: string; created_at: number }>('/api/console/sessions', {
      method: 'POST',
      body: JSON.stringify(title ? { title } : {}),
    }),
  consoleDeleteSession: (id: string) =>
    request<{ ok: boolean }>(`/api/console/sessions/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  consoleMessages: (sessionId: string, limit = 200) =>
    request<{ messages: ConsoleMessage[] }>(
      `/api/console/messages?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`,
    ),
  consoleSnapshot: () => request<ConsoleSnapshot>('/api/console/snapshot'),
  /** 以主人身份对糖糖说一句（会客厅界面不输入文本，只有后端/调试会用到） */
  consoleSay: (payload: { session_id: string; text: string }) =>
    request<ConsoleTurnResult>('/api/console/say', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /** 自主回合：不给文本，让她按此刻的驱动力/情绪主动说一句 */
  consoleTurn: (sessionId: string) =>
    request<ConsoleTurnResult>('/api/console/turn', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    }),

  // ── 会客厅 v2 ────────────────────────────────────────
  /** 会话源列表：本地 / 私聊 / 群聊，已由后端分组排序 */
  consoleSources: () => request<{ sources: ConsoleSource[] }>('/api/console/sources'),
  /**
   * 某会话的消息流。after_id > 0 时只返回更大的 id（增量轮询）；
   * 首次进入传 0 拉最近 limit 条。
   */
  consoleFeed: (scope: string, afterId = 0, limit = 100) =>
    request<{ messages: ConsoleFeedMessage[]; has_more: boolean }>(
      `/api/console/feed?scope=${encodeURIComponent(scope)}&after_id=${afterId}&limit=${limit}`,
    ),
  /** 单条回合痕迹详情：run_id 与 event_key 二选一 */
  consoleTrace: (q: { run_id?: string; event_key?: string }) =>
    request<{ trace: ConsoleTrace | null }>(
      `/api/console/trace?${
        q.run_id
          ? `run_id=${encodeURIComponent(q.run_id)}`
          : `event_key=${encodeURIComponent(q.event_key || '')}`
      }`,
    ),
  /**
   * 一条回复的完整回合回放：思维链 → 工具链 → 决定 → 对方反应。
   * 入参用被点那条消息的 `event_key`（bot 回复是 `delivery:` 键，
   * 后端会解析回触发它的那条消息再找痕迹）。
   */
  consoleReplay: (eventKey: string) =>
    request<{ replay: ConsoleReplay | null }>(
      `/api/console/replay?event_key=${encodeURIComponent(eventKey)}`,
    ),
  /** 群管理页数据：群列表 + 场景清单 + 现有绑定（场景 / 黑名单） */
  consoleGroups: () => request<GroupManageData>('/api/console/groups'),
  /**
   * 保存群管理。**不会热生效**——必须在保存后重启糖糖，
   * 因为 `_group_blacklist` 和 `scenario_targets` 只在 bot 启动时读一次。
   */
  groupManageSave: (payload: GroupManageSavePayload) =>
    request<GroupManageSaveResult>('/api/group-manage/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /**
   * 记忆图谱。`center` 空 = 全局（人—类别）；传 QQ 号 = 那个人的认知切片。
   * 节点/边由后端组装，前端只画。
   */
  memoryGraph: (center = '', limit = 40) =>
    request<MemoryGraph>(
      `/api/console/memory-graph?center=${encodeURIComponent(center)}&limit=${limit}`,
    ),
  /**
   * 记忆剖面：七维索引 + 六层架构 + 全局存储能力。
   * 比 memoryGraph 重（带证据原话和全局统计），切人时才拉。
   */
  memoryProfile: (qq: string) =>
    request<MemoryProfile>(
      `/api/console/memory-profile?qq=${encodeURIComponent(qq)}`,
    ),
  /**
   * 记忆演化史：同一主题的版本链（A 被 B 取代 → B 被 C 取代）+ 断链清单。
   */
  memoryHistory: (qq: string) =>
    request<MemoryHistory>(
      `/api/console/memory-history?qq=${encodeURIComponent(qq)}`,
    ),
  /**
   * 记忆变更流水（append-only 写操作留痕）。`qq` 空串 = 全库视角。
   * 后端实际是 GET + query（不是 POST body）：limit 默认 200、服务端钳 20..500。
   */
  memoryRevisions: (qq = '', limit = 200) =>
    request<MemoryRevisions>(
      `/api/console/memory-revisions?qq=${encodeURIComponent(qq)}&limit=${limit}`,
    ),
  /** 统一发送入口：本地会话走真身对话，QQ 镜像会话会真的发到 QQ */
  consoleSend: (payload: { scope: string; text: string }) =>
    request<ConsoleSendResult>('/api/console/send', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  /**
   * 会话内容搜索——搜的是消息正文，不是会话标题。
   * `q` 少于 1 个字符时后端返回空结果（不发请求也行，但这里不拦）。
   */
  consoleSearch: (q: string, limit = 40) =>
    request<ConsoleSearchResult>(
      `/api/console/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
}

/** 给需要自己拼 URL（图片、SSE）的组件用。 */
export function getBridge(): Promise<BridgeInfo> {
  return ensureBridge()
}

/** 图库图片地址（缩略图/原图）。token 走 query —— <img> 没法带 Authorization 头。 */
export function galleryImageUrl(
  bridge: BridgeInfo,
  source: string,
  name: string,
  kind: 'thumb' | 'file' = 'thumb',
  w = 360,
): string {
  const qs = new URLSearchParams({
    source,
    name,
    w: String(w),
    token: bridge.token,
  })
  return `http://127.0.0.1:${bridge.port}/api/gallery/${kind}?${qs.toString()}`
}

/**
 * 会客厅媒体地址（图片 / 语音）。
 *
 * `path` 是后端 `ConsoleMediaItem.path` 原样带回来的相对路径——解析
 * 「本地文件 / QQ url / segments CQ 码」全在后端做，前端只负责拼 host+token。
 * 同 galleryImageUrl：token 走 query，因为 <img>/<audio> 带不了请求头。
 */
export function consoleMediaUrl(bridge: BridgeInfo, path: string): string {
  const sep = path.includes('?') ? '&' : '?'
  return `http://127.0.0.1:${bridge.port}${path}${sep}token=${encodeURIComponent(bridge.token)}`
}

/** 日志 SSE 订阅，返回取消函数。 */
export async function subscribeLogs(
  onLine: (line: string) => void,
  onError?: (e: Error) => void,
): Promise<() => void> {
  const bridge = await ensureBridge()
  const url = `http://127.0.0.1:${bridge.port}/api/logs?token=${encodeURIComponent(bridge.token)}`
  const es = new EventSource(url)
  es.onmessage = (ev) => onLine(ev.data)
  es.onerror = () => onError?.(new Error('日志流断开'))
  return () => es.close()
}
