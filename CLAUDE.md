# 小糖糖 — 项目指导

> 不是技术文档。是思维方式。下次接手这个项目的 AI，请先读这篇。

---

## 零、糖糖的存在形式——一切技术的出发点

> **「LLM 提供瞬间；架构提供生命。」** 糖糖的"存在"不来自 LLM——来自围绕 LLM 构建的持久状态结构。

- **她不是客服机器人**：不是 `f(消息, 记忆, 人格卡) → 回复` 的一次次独立函数调用。她拥有**独立于消息而存在的自我状态**——有自己的时间感、精力和心情的自主波动；关系是她存在的一部分，不是查询结果；性格是经验的沉淀，不是 role_card 的设定。
- **三观从经验里长出来，不是设定**：世界观是几千次互动中长出的理解（每个群氛围不同、每个人质地不同）；人生观是她做过/后悔过/骄傲过/害怕过的事的总和；价值观是从反馈中长出的弥漫倾向——像激素，不是神经信号。
- **两个自治循环**：经验积累（观察→感受→记录，感知被信念影响）↔ 反思整合（回顾→提炼→更新自我叙事，自我叙事影响下次感知）。
- **一条边界**：架构负责记住经历、维护关系感、提供时间连续性、让经验塑造倾向；LLM 负责基于持久状态生成自然的回复。不假装她有意识，不让她"觉醒"——追求的是连贯、发展、诚实的「她自己的存在方式」。
- **技术是骨，思维框架是魂**：记忆系统让她有经验，反馈系统让她有价值观，场景引擎让她在不同关系中自然地不同，时间系统让她有自己的呼吸节奏。**改任何代码前，先问：这个改动是让糖糖更有存在感，还是只是让她更高效地当一个客服？**

---

## 一、如何思考——五句话

1. **多想**（章北海）：动手前先想清楚——问题是什么？为什么出现？改了会引发什么新问题？黑暗森林里不急着开枪——先想枪声会引来什么。
2. **抓主要矛盾**（毛泽东）：不要见 bug 就修——先问这些 bug 的共同根源是什么？修了根，其他的自己消失。
3. **去粗取精、去伪存真**（《实践论》）：表象是粗，根因是精；LLM 的表演是伪，真实的架构缺陷是真。
4. **先问「前提对吗」**：记忆检索优化了七轮（关键词→n-gram调参→jieba→BGE→关系档案→被动注入→按需检索），每一步都在优化同一个前提——最后发现 DeepSeek V4 原生支持 tool calling，LLM 自己决定查什么、自己去查。七步迭代不如一次范式转换。
5. **主要矛盾：糖糖没有内部张力**：从 `f(message)→reply` 升级到 `f(message, state)→reply`，状态丰富了，根本动态没变——她仍被外部消息推动。有内在生命的存在不是"有状态"——**是有冲突**，互相矛盾的需求必须在选择中解决。下一步不是加功能，是装引擎：随时间自然积累、通过行动释放、相互竞争的驱动力系统。
6. **先调研再判断**（2026-08-15）：架构决策前先查开源项目/市场怎么做——理解协议（restate-before-answer）、上下文来源标记（wuhu-core）、混合检索（hybrid+RRF+rerank）、角色卡规范（SillyTavern 四字段）全部来自调研。别人的坑别人踩过了，最优解很少是自己拍脑袋想出来的。技术细节沉淀在 `docs/开发规划/上下文架构规范化_20260815.md`。

---

## 二、踩过的坑——教训速查

> 完整故事见记忆 [[bug-fixing-anti-patterns]] / [[lessons-20260730]] / [[module-integration-checklist]]

| # | 错误模式 | 一句话教训 |
|---|---------|-----------|
| 1 | 忙修 bug 不找根因（改了 6 个文件，根因是 1 行代码） | 拿到问题先问共同根源，列现象找交集 |
| 2 | 用规则清单代替身份认知（15 条 DON'T 不如 1 句话） | LLM 行为问题→强化身份认知，不加规则 |
| 3 | 没读文件就改（old_string 对不上） | 改文件之前必须 Read |
| 4 | 改了 config.yaml 没改控制台（用户用错模型一周） | config 改动涉及控制台 UI 字段必须两边同步 |
| 5 | 一口气改太多没验证（8 文件 300+ 行，`_pending_action` 漏了） | 大改动分批提交，每批跑 pytest |
| 6 | 框架先行（~600 行新框架无人用） | 框架是抽出来的，不是建出来的 |
| 7 | 调参数而不是换方法（n-gram 垃圾→jieba 一行解决） | 在调参数时问自己：方法本身有问题吗？ |
| 8 | 两套工具系统并存（JSON 嵌入 + Tool Calling 互不知情） | 选一个——原生 Tool Calling，JSON 嵌入是 2023 年的做法 |
| 9 | 关键词门控替 LLM 做决策（7 处 `if "xxx" in text`） | 提供工具，让 LLM 自己决定——一个 tool call 比 5 个正则准 |
| 10 | auto_learn 关键词碰瓷（60+ 正则，中文枚举不完） | 少而准 > 多而杂，LLM 语义提取替代正则 |
| 11 | testing_mode 留在生产配置（非主人消息被静默丢弃） | 破坏性 boolean 开关必须启动时显式警告 |
| 12 | 记忆检索死代码（set_embedding 从未被调用，语义搜索从未生效） | 写了 infra 代码 ≠ 接入了业务 |
| 13 | 魔术字符串注入锚点（replace `"## 关系"` 静默失效） | 用显式注入槽位，不要依赖字符串巧合 |
| 14 | Reranker/驱动力写了没接主流程（引擎在转，离合器没接上） | 新增模块三问：谁写？谁读？端到端跑通了吗？（已由接线审计测试机器化） |
| 15 | 持久化做了一半（漏了 relationships 字段，重启清零）。2026-08-16 再犯：主动私聊冷却/观察/全局冷却全内存态——重启 7 次骚扰同一人 7 次（对方零回复） | 持久化列清单，_save/_load 成对检查；**任何「重启后必须记得」的运行时状态（冷却/观察/配额）必须落盘**；另加铁律：未回复前绝不二次主动打扰 |
| 16 | `return` 之后 15 行缩进死代码（不报错、不执行、静默） | Python 缩进即逻辑——return 后代码要审查 |
| 17 | 内容块无来源分界（歌单双注入每轮出现，短问题一来 LLM 从背景取样作答——把歌单当引用内容） | 框架注入一律 `<背景·标签>` 包裹，用户原文永不包裹永远最后——「无标签 = 对方说的」（wuhu-core） |
| 18 | 修"次要矛盾"没取证（占位符假设被 DB 查证推翻——歌单根本不在历史窗口，真凶是 system.j2+capability_note 双注入） | 修复假设先取证：查 DB、查日志、查注入源，再动手 |
| 19 | 字符串匹配做状态分流（`"没取到" in prefix` 被引用原文「外卖没取到」误触发） | 状态标志/枚举常量，不用内容字符串做判定（反模式 #13 变体） |
| 20 | 平台层缺陷不实测就猜测（SnowLuma 丢私聊引用，changelog 无证据） | 升级后做 30 秒验证测试，用日志判定，不凭 release notes 猜 |
| 21 | 例外不文档化（主动插话路径不贴标签会被"好心修复"或错误推广） | 每个有原则的例外都写进 docstring，写明为什么、不要改 |
| 22 | 取样开口无根基约束（私聊插话脑补「前天在肯德基门口纠结半天」——取证：所有注入材料里根本没有肯德基，是 LLM 纯脑补） | 「像朋友一样开口」的指令必须配「材料外细节不编」契约；契约用闸门测试防删 |
| 23 | 模型加载并发（23:55 事故：BGE 与 Reranker 双线程 from_pretrained 撞车 → 一个拿到 meta 空权重，权重没数据，推理全废） | transformers fast-init 全局状态竞态——所有模型加载必须持 agent/model_lock.py 的 MODEL_LOAD_LOCK 串行 |
| 24 | 系统正则解析自然语言意图（「5分钟后在这里叫我」被 delayed_say 抢答发默认群；「明天早上九点」不认中文数字承诺落空） | 意图解析单一决策源：自然语言（时间/位置/内容）只有 LLM 能解析——系统 precise 层只允许动作确认类命令；边界由 TestPreciseCommandsBoundary 机器闸门钉住，意图解析分支不可复活 |
| 25 | heredoc 正则脚本盲改文件（`# ═+[\s\S]*?` 从文件头贪婪跨到工厂函数，误删整个 BirthdayGreeter 类，bot 启动即崩）；测试配置无 owner_qq 使生日路径零覆盖，267 全绿没拦住 | 大段文件替换用 Edit 工具（范围可见，逐字匹配）；删代码后补对应路径的回归测试——测试绿 ≠ 路径被覆盖 |
| 26 | 跨文件移代码丢模块级导入（handler 拆分后 handler_commands.py 缺 `re`/`Path`/`Relationship`，web_search 重写丢 `re`——/说 一调用就 NameError，调用期才炸测试拦不住） | 批改后必跑 pyflakes undefined 扫描（`python -m pyflakes agent/*.py onebot/*.py main.py 糖糖控制台_qt.py \| grep -i undefined`，必须清零） |
| 27 | 工具执行器内同步调 LLM 死锁（意见征集邀请在 set_opinion_campaign 工具内调 _call_llm_light——工具循环持有 _llm_lock，嵌套调用 15s 超时返回空串，邀请静默跳过「提示已发送实际没发」） | **工具执行器内禁止同步调 LLM**——LLM 工作一律 fire-and-forget 后台化（主调用结束锁释放后执行）+ 空回复不许静默（重试+模板兜底+日志） |
| 28 | 在 `__init__` 中段插入新 `def` 方法（feedback 消费点插进 __init__ 中间）→ 后续初始化全部吞成该方法的 return 后死代码——语法合法、371 测试照绿，pyflakes undefined 才发现；当晚复现两次（教训 #16/#25 的变体，更隐蔽） | 方法定义只插在**方法边界**处（缩进 4 空格 `def` 出现处）；批改后必跑 pyflakes undefined 扫描（教训 #26 的扫描恰好是唯一拦截网） |
| 29 | 纠正工具主语解析失败（主人让纠正「穷到吃外卖」，消息里只有昵称没 QQ——LLM 解析不出，把 subject 填成当前用户：误撤「大二学生」+ 写「高一新生」垃圾行，重合成又洗白画像；还自行发明了 wrong_fact「大二学生」） | 纠正类**破坏性**工具三闸门：① subject_name 精确解析（模糊匹配只给查询工具）；② wrong_fact 零匹配 fail-closed 拒绝写纠正事实；③ 工具描述写明「先 search_people 查 QQ、只纠正对方明确否定的内容」。事故回滚用幂等脚本 + 备份先行 |
| 30 | **检查器写了但从没生效**（2026-09-19 发布审计）：`准备发布.py` 的本地路径正则写成 `r"[Dd]:\\\\..."`——正则里 `\\\\` 表示*两个*反斜杠，而真实路径只有一个，**从上线起就没匹配过任何东西**，每次都报「零命中」。18 处本机路径随 v1.0/v1.1 发布了出去，散在 6 个文档里 | **安全检查必须做阳性对照**：种一个真样本进去，看它报不报。只测「跑起来没报错」等于没测——它本来就不会报错。闸门：`test_release_sanitizer.py` 里那几条 `test_sanitizer_catches_planted_*` |
| 31 | **按类型开天窗**（同一次审计，修完 #30 才暴露）：扫描器只扫 `.py/.md/.yaml/.yml/.txt/.json/.bat` 七种后缀，等于宣布「其他类型不可能有敏感信息」。实际 `knowledge/.knowledge_index.sqlite3`（二进制）里躺着 19 处本机路径——**1392 个文件、1.14 GB 从未被扫过** | 白名单要**穷举该扫的**，不是穷举能想到的；剩下的走兜底而不是跳过。二进制兜底加判别器：**匹配到的字节必须整体是合法 UTF-8**——噪声 24→0、真路径 19→19，分得干干净净 |
| 32 | **派生数据随源数据一起发布**：清掉源文件里的本机路径后，生成物（知识库索引）里仍留着清理前的旧文本副本，而它随包发了出去 | 发布物只放**源头**，派生数据（索引/缓存/构建产物）随包发等于把「发布时点的快照」连同它的历史一起发出去。这条当初是主人指定「避免首启重建等待」——但代价没被看见 |
| 33 | **命令表长了，权限却没跟着长**：`handler_commands.py` 注册了 36 条命令，只有 6 条被想过权限问题。结果是群里任何成员都能 `/人格` 重写人设、`/黑名单` 拉黑群、`/唱歌` 遥控她发歌——自己用（群里都是熟人）毫无感觉，**发布给陌生人才暴露** | 权限要**成对出现**：加命令时同时回答「谁能用」。判据定死——**改她的状态/配置/代她发言 → 受限；查自己的、看、玩 → 放行**。闸门 `test_every_command_is_classified` 强制每条命令落到某一类，没归类直接红 |
| 34 | **打包去重把「同一对象的两个版本」当重复**：`covers/separated/*_FINAL.wav`（原声）被判定为 `songs/audio/`（糖糖声线）的副本而跳过，41 首里 40 首没有原声——而 sing 工具还在告诉 LLM「对方说原声就放原唱」 | 去重前先确认**两件事物真的是同一个东西**。`agent/songs.py` 的 docstring 早就写清了两者不同（"rvc=糖糖声线 / original=原声"），去重逻辑却按文件名前缀猜。**代码里已经写明的语义，不要在另一处重新猜一遍** |
| 35 | **文档说的和代码做的分家**（2026-09-19 用户手册审计）：手册两周没维护，14 条界面描述里 8 条不符——设置页 15→20 个分类、DiffSinger 渲染已撤出、运行模式已删、`/色色` 三条命令根本不存在、断链指向已归档的文档 | 对外文档要**当代码测**：写进契约测试（链接可达、数字可复现），否则它只会静默腐烂。查出来的那一刻说明它已经烂了两周没人发现 |

---

## 二点五、架构升级经验（已沉淀，细节见记忆与项目规划）

**好的模式（沿用）**：Phase-by-phase 渐进升级；降级链无处不在（新组件必有 fallback）；状态与逻辑分离；哲学先行。

**长期观察项**：关系场 closeness 微增量能否形成「感觉」；驱动力 alpha/阈值 0.7 是否自然——需长期运行观察。handler.py 仍偏大，可继续拆。

---

## 三、技术真相

**架构**：`QQ消息 → SnowLuma → WebSocket → handler.py → LLM → 回复`。LLM 通过 Native Tool Calling 自主调用工具、搜索记忆、唱歌、发语音。
**核心原则**：LLM 是唯一决策者。系统不替 LLM 做判断——只提供能力（工具），LLM 自主调用。

**关键文件**（角色速查）：

| 文件 | 角色 |
|------|------|
| handler.py | 消息路由中枢 ⚠️ 仍偏大，已拆出 handler_commands.py / handler_autonomy.py，可继续拆 |
| memory.py | 记忆系统——语义提取/画像合成/事实簇/整合 |
| store.py | 数据库统一入口——SQLite 唯一访问层 |
| personality.py | 人格引擎——系统提示词（稳定前缀缓存契约）+ 角色卡加载 |
| protocols.py | 行为协议单一事实源——纠正信号/理解与思考协议/背景块标记 bg()/用户消息组装 |
| voice.py / sticker.py | 语音合成(GPT-SoVITS 丛雨7情绪 + CosyVoice3 备用) / 贴图系统 |
| reply_pipeline.py | 回复后处理——清洗（含 <思考> 剥离）→贴图→@解析→分句发送 |
| drives.py | 驱动力引擎——6 种驱动力积累/释放/竞争 |
| knowledge.py | 知识库——hybrid+RRF+rerank 混合检索（2026-08-15，细节见项目规划） |
| self_state.py | 持久自我状态——关系场/自我叙事/价值观/体验缓冲 |
| reflection.py | 反思整合——个人反思+社交感知，定时后台 |
| conversation_tracker.py | 对话追踪——窗口检测/退让/闲聊轮次限制 |
| context_builder.py | 状态投影器——Token 预算 + 上下文组装 |
| mood.py / scenario.py | 情绪三维模型 / 场景引擎(replace/overlay) |
| interjection.py | 插话引擎——客观信号评分 |
| perception.py / relationship.py | 反馈感知(情绪三分类评估) / 关系档案合成 |
| role_card.md / config.yaml / .env | 人格定义(第一来源) / 配置 / API 密钥(最高优先) |

**命令与工具系统**：

| 方式 | 机制 | 用途 |
|------|------|------|
| `/command` | 正则匹配，系统执行 | 精确操作：/人格 重载、/状态、/黑名单 |
| Native Tool Calling | OpenAI function calling | **主要方式**——LLM 自主调用 calculate/get_weather/web_search/sing/send_voice/search_facts/... |
| `[贴图:xxx]` | LLM 输出标签 | 表情包——LLM 自主选择时机和情绪 |

`[CMD:...]` 已退役。保留的轻量协议：`[SING:段落]`（唱歌段落）、`[贴图:关键词]`、`[不说话]`（自治循环否决）、`[进入色色]`/`[退出色色]`（亲密模式动态开关——权限静态、激活动态，退出由 LLM 判断）。注册技能：`calculate convert get_time get_weather web_search translate draw draw_divination_lot draw_tarot share_image sing send_voice search_knowledge read_document extract_key_info search_facts search_chat_history search_memories`

**什么时候改哪个文件**：

| 要做什么 | 改哪里 |
|----------|--------|
| 改糖糖的性格/说话方式 | role_card.md → /人格 重载（⚠️ **只写身份，不加规则**——规则/协议进 protocols.py，测试有闸门） |
| 加行为规则/情境协议 | protocols.py（背景块标记/协议文本/信号表的唯一来源） |
| 加新技能/工具 | 对应模块注册 `@register_skill` → 自动变为 LLM 可调用工具 |
| 加记忆/数据查询工具 | handler.py `_build_memory_tools` + `_execute_tool` |
| 改配置参数 | config.yaml + 糖糖控制台_qt.py 的预设值（⚠️ 必须同步） |
| 改回复处理 / 记忆系统 | reply_pipeline.py / memory.py |

---

## 四、改代码前的检查清单

每次动手前，回答这三个问题：

1. **主要矛盾是什么？** 不是「有哪些 bug」，是「这些 bug 的共同根源是什么」。
2. **最小改动是什么？** 能改一行解决的不要改十行，能改配置解决的不要改代码。
3. **改了之后怎么验证？** pytest 是最低底线。功能改了要模拟场景走一遍。

---

## 五、修 Bug 的铁律

**每次改代码前，过一遍检查清单：** [[pre-edit-checklist]]

核心纪律：
- **Read before Edit**——不读不改，old_string 必须来自最近一次 Read
- **One change, one verify**——每改一个文件就验证，不要连续改多个
- **找到主要矛盾再动手**——修了 3 个还没解决 → 停下来重新分析
- **不要用关键词替 LLM 做决策**——系统提供能力，LLM 自主调用
- **不要追加配置来覆盖已有设定**——追加永远压不过已有内容

详细反模式记录：[[bug-fixing-anti-patterns]]

---

## 六、已确认有效的积累（不要推翻）

- **单脑架构**：LLM 做决策，系统做执行。不要回到双脑时代。
- **群聊窗口回复契约**（2026-08-25）：活跃窗口内同一用户的每条消息逐条进入 LLM；系统不得再按 waiting/长度/标点/随机懒回/连续次数二次门控。LLM 用原生 `skip_response` 工具写入 `turn_actions.respond=false` 自主沉默；主动插话开关只控制窗口外评分入口。文字、语音都必须经过共同的聊天记录与窗口续期收尾。
- **原生 Tool Calling 为主，轻量文本标记为辅**：`[SING:段落]`、`[贴图:关键词]`、`[不说话]`、`[进入色色]`/`[退出色色]`——不是命令系统，是 LLM 输出中的轻量协议。
- **role_card 优先**：人格定义第一来源 role_card.md，config.yaml 是兜底。
- **知识库递归扫描**：knowledge/ 下所有子文件夹的 .md 都会加载。
- **记忆置信度过滤**：LLM 提取 < 0.5 置信度丢弃；合成记忆 importance=4，不盖过真实记忆。
- **真值生命周期**（2026-08-16 治理工程）：memories/cluster_facts 有 status（active/retracted/superseded）——撤销不改 importance（它是相关性不是真值）；people.notes_dirty 脏标禁止注入直到重合成；置信度分层（缺失拒绝、显式 0.7 低可信不进画像/自动注入）；合成是事务化世代替换（profile singleton + fact 集合，24h 冷却，纠正绕过）；correct_memory/forget_memory 是 LLM 的写回工具（本人纠自己/仅主人纠第三人）。治理工具与运维见 docs/审查/记忆系统治理运维_20260816.md。
- **插话配额分离**：被 @ 和 /命令 不消耗主动插话配额。
- **交叉上下文（已限权）**：私聊中提到其他 QQ 号时自动加载那个人的档案和历史——只对主人或亲密度≥50 者开放（2026-08-10 C3，防信息泄露）。
- **表情包原始路径**：`file:///D:/...`，不 URL 编码中文；纯图模式贴图附加，不替换文字。
- **记忆自贬过滤**：「我是废物」是情绪症状，不是事实——自我否定陈述不入库。
- **私聊人物画像注入**：私聊时 people.notes 自动注入系统提示词。
- **三层个人化架构**：怎么聊（场景）→ 她是谁（people.notes）→ 聊什么（memories）。
- **心理陪伴场景**：不规定糖糖怎么说话——只注入一层理解（ta 在经历困难）+ 两条边界（不追问创伤细节、不演身份戏剧）。scenario_targets 挂到任何人身上即生效。
- **`_allowed_groups` 排除黑名单**：初始化时就过滤，所有定时系统（生日/点赞/图片分享）自动安全。
- **流式输出替代伪造延迟**：真实生成速度就是自然的延迟，不需要假 sleep。
- **记忆提取**：auto_learn（关键词正则）已退役 → LLM 语义提取器（群 10 条 / 私聊 6 条触发）。
- **感知系统**：perception.py 做情绪三分类反馈评估；mention/positive/negative/silence/shutdown 五信号统计在 handler.py `_record_feedback`。
- **自我记忆端到端**：_extract_self_memories 写入 bot_qq → _get_self_memory_context 用 BGE 语义匹配注入相关自忆（BGE 未就绪降级取最近 5 条）——糖糖记得自己说过的话。
- **上下文来源标记**（2026-08-15）：框架注入一律 `<背景·标签>`（protocols.bg），用户原文永不包裹永远最后。例外：主动插话是生成简报，不贴标签（已文档化）。
- **能力数据按需注入**：歌单只随 sing 工具出现，不常驻上下文——能力清单 ≠ 对话背景（World Info 绿灯原则）。
- **接线审计测试**：tests/test_integration_contract.py 机械断言零调用/管线契约/判定单一来源——新死代码或第二判定点直接红灯。
- **发送链路契约**（2026-08-16 全审计）：LLM 文本发往用户前必须过 `_enrich_reply`（清洗+贴图解析+@解析）——已知直发点清单在 `test_llm_send_paths_must_enrich`（LLM_SEND_FUNCS 表），新增 LLM 直发路径必须追加进表；例外：主人原话直发（<50字）、命令结果、固定文本、唱歌歌词、媒体 CQ。
- **意图解析单一决策源**（2026-08-16 范式转换）：自然语言意图（定时提醒/延迟发言）一律走 LLM 工具（set_reminder / group_say_later）；系统 precise 层只允许动作确认类命令（撤回/撤销/草稿审核）。TestPreciseCommandsBoundary 机器闸门禁止时间量词/提醒词回 precise 层。
- **困难轮次协议**：纠正信号/引用取不到 → 理解协议（复述→承认→问不猜→不复读）+ `<思考>` 隐藏段（发送前剥离）+ 模型升级。复读自检仅困难轮次开启。
- **引用解析状态机**：QUOTE_RESOLVED/MISSING/NONE 枚举分流（不用内容字符串判定）；解析结果写回历史。

---

## 六-2、个人化——三层架构

| 层 | 回答问题 | 存储 | 设置方式 |
|----|---------|------|---------|
| 怎么聊 | 语气、长度、禁忌 | 场景 (psychology/seductive) | 控制台 scenario_targets |
| 她是谁 | 名字、处境、需求 | people.notes | 数据库直接编辑 |
| 聊什么 | 具体说过的事 | memories | 记忆系统自动积累 |

**心理陪伴场景 (psychology.yaml)**：通用——任何人挂上就生效。糖糖还是糖糖，不固定她的说话方式；只注入一层理解 + 两条边界（不追问创伤细节、不演身份戏剧）。主动关心由私聊插话 + 情绪闭环实现。控制台群管理 → scenario_targets → 选「🧠 心理陪伴」。

---

## 七、中文分词——jieba 原则

**不要用 `if "keyword" in message` 做子串匹配**——`"想你" in "不要想你"` 永远为 True。用 jieba 分词后检查完整词匹配：

```
"不要想你" → jieba.cut → ["不要", "想你"] → 检查相邻否定词 → 跳过 ✅
```

适用：知识库关键词提取、插话关键词检测、记忆提取中的关键词匹配。新功能凡是涉及中文文本匹配的，优先用 jieba。

---

## 八、依赖关注

sentence-transformers / BGE 影响记忆与知识库质量——每季度检查新版本；jieba 稳定；SnowNLP 偏旧可换；httpx 安全更新必升；PySide6 大版本升级需测兼容性。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **qq-小糖糖** (38057 symbols, 78445 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/qq-小糖糖/context` | Codebase overview, check index freshness |
| `gitnexus://repo/qq-小糖糖/clusters` | All functional areas |
| `gitnexus://repo/qq-小糖糖/processes` | All execution flows |
| `gitnexus://repo/qq-小糖糖/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
