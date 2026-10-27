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

## 一-2、自己去找问题，不等主人报（2026-10-25 立为常设工作流）

**主人不用报 bug——我自己去会客厅跟她说话，从她的话里发现缺陷，倒查链路，
最小改动修好，再回去用同一个问法问她一遍。**

规程全文：[docs/工作方式/自主发现修复闭环_毛选视角_20261025.md](docs/工作方式/自主发现修复闭环_毛选视角_20261025.md)
每轮诊断记录落 `docs/诊断/`（已跑 **17 轮**，第 1~17 轮诊断按序号存；第 17 轮起转做桌面端）。
**当前状态看交接单 §0**（她每轮实际收到什么）、**§0.2**（主人两项决策已执行 +
第 8 轮挖出的裁剪尺子根因）、
**§0.3**（长任务 goal 怎么续）。

四条不能丢的：

1. **她的话是证据，不是产品。** 读代码只能看到「应该怎样」，跟她对话才能看到
   「实际怎样」。先取证再归因，不预设结论。
2. **归因只有一个二分问题：她是「不知道」，还是「知道但用错」？**
   不知道 → 修注入源；知道但用错 → 修协议文本。判错的代价是修错地方，而且
   修两次都不好（那时要回到这个二分重判）。
3. **每轮只打一类缺陷**，一次只改一处注入源——否则复验改善了也不知道是哪一处
   起效（《论持久战》集中优势兵力）。
4. **禁止用「让她更小心」来修**（教训 #2）：prompt 里加「请注意现在是几点」
   「不要记错」是让她表演；真正的修法是**给她信息**，她自己会用。

---

## 二、踩过的坑——教训速查

> **每条只留「怎么认出来 + 怎么防」，一句话。**
> 完整故事（当时怎么踩的、取证数字、修复过程）在
> [反模式全集](docs/教训/反模式全集_20261025.md)——编号一一对应（#1~#56）。
>
> 为什么分家：这张表曾长到 38KB，把 `CLAUDE.md` 顶到 65KB、被系统自动截断。
> **读不完的规则等于没写。**

| # | 错误模式（怎么认出来） | 一句话教训（怎么防） |
|---|---------|-----------|
| 1 | 忙修 bug 不找根因（改 6 个文件，根因 1 行） | 先问共同根源，列现象找交集 |
| 2 | 用规则清单代替身份认知（15 条 DON'T） | 行为问题→强化身份认知，不加规则 |
| 3 | 没读文件就改（old_string 对不上） | 改文件之前必须 Read |
| 4 | 改了 config 没改控制台（用错模型一周） | config 改动涉及 UI 字段必须两边同步 |
| 5 | 一口气改太多没验证（8 文件 300 行） | 大改动分批，每批跑 pytest |
| 6 | 框架先行（600 行新框架无人用） | 框架是抽出来的，不是建出来的 |
| 7 | 调参数而不是换方法（n-gram→jieba） | 调参时先问：方法本身有问题吗 |
| 8 | 两套工具系统并存互不知情 | 选一个：原生 Tool Calling |
| 9 | 关键词门控替 LLM 做决策（7 处 if in） | 给工具让 LLM 自己决定，比正则准 |
| 10 | auto_learn 关键词碰瓷（60+ 正则） | 少而准 > 多而杂，用 LLM 语义提取 |
| 11 | testing_mode 留在生产配置 | 破坏性 boolean 开关必须启动时显式告警 |
| 12 | 记忆检索死代码（infra 从未被调用） | 写了 infra ≠ 接进了业务 |
| 13 | 魔术字符串注入锚点（replace 静默失效） | 用显式注入槽位，不靠字符串巧合 |
| 14 | 新模块写了没接主流程 | 三问：谁写？谁读？端到端跑通了吗 |
| 15 | 持久化做一半（重启清零/冷却丢失） | 状态列清单、_save/_load 成对；重启要记得的必须落盘 |
| 16 | return 之后 15 行缩进死代码 | 缩进即逻辑，return 后的代码要审查 |
| 17 | 内容块无来源分界（歌单当引用作答） | 框架注入一律包 `<背景·标签>`，用户原文永不包裹 |
| 18 | 修次要矛盾没取证（真凶在别处） | 先查 DB/日志/注入源，再动手 |
| 19 | 字符串匹配做状态分流（误触发） | 用枚举/状态标志，不用内容字符串判定 |
| 20 | 平台层缺陷不实测就猜测 | 升级后做 30 秒验证，用日志判定，不看 release notes |
| 21 | 例外没文档化（被好心改坏） | 有原则的例外写进 docstring，写明为什么 |
| 22 | 取样开口无根基（脑补材料外的细节） | 「像朋友开口」必配「材料外细节不编」契约 |
| 23 | 模型加载并发撞车（权重没数据） | 所有模型加载持同一把锁串行 |
| 24 | 系统正则解析自然语言意图 | 意图只归 LLM；系统层只认动作确认类命令 |
| 25 | heredoc 正则盲改文件（误删整类） | 大段替换用 Edit；删代码补该路径回归测试 |
| 26 | 跨文件移代码丢模块级导入 | 批改后跑 pyflakes undefined 扫描并清零 |
| 27 | 工具执行器内同步调 LLM（死锁+静默） | 工具内禁调 LLM，改后台 fire-and-forget；空回复不许静默 |
| 28 | 方法定义插进 __init__ 中段（后续全吞） | 方法只插方法边界；批改后扫 undefined |
| 29 | 纠正工具主语解析失败（改错人） | 破坏性工具三闸：精确 subject / 零匹配拒绝 / 写明前置 |
| 30 | 检查器写了但从没生效（正则永假） | 安全检查必须阳性对照：种样本必须报 |
| 31 | 按类型开天窗（1392 文件从未被扫） | 白名单穷举该扫的，剩下走兜底而不是跳过 |
| 32 | 派生数据随源数据一起发布 | 发布物只放源头；索引/缓存/构建产物不进包 |
| 33 | 命令表长了权限没跟着长 | 加命令时同时回答「谁能用」，成对出现 |
| 34 | 去重把同一对象的两个版本当重复 | 去重前确认两件事物真是同一个东西 |
| 35 | 文档说的和代码做的分家 | 对外文档当代码测：链接可达、数字可复现 |
| 36 | 把「工具报 0」当成「事实是 0」 | 工具报 0 必须 grep/AST 复核再看结论 |
| 37 | 验证尺子自己会骗人（假绿） | 闸门要阳性对照；行为改动过变异对照；子进程编码两端钉死 |
| 38 | 同一个量两个换算点，只改了一个 | 系数收敛到一个函数；裁剪闸门要双向（不超+贴近） |
| 39 | 假取回路径（工具作用域不对） | 登记「A 能取回 B」必须核对 A 的检索主体 |
| 40 | 只管不超预算，不管同预算装了多少 | 日志同时记条数；同语义两处渲染要一起审 |
| 41 | 监控一个恒为常量的量 | 先问「这个量会不会变」；只读无写的字段等于常量 |
| 42 | 接在死方法上的线 + 量不出东西的尺子 | 可达性 ≠ 存在性；验收工具必须与生产同口径 |
| 43 | 「只给事实」= 给一堆没有句柄的噪音 | 事实要配句柄、历史（次数/时间）、状态改写者 |
| 44 | 链路接好了，链路上没原料 | 看产出分布，不看接线证据（接≠在用） |
| 45 | 测量工具会污染被测系统 | 探针先问「它会写什么」；判据必须带随机基线 |
| 46 | 计划里的修法被下一个测量推翻 | 改行为前先看第二口径；计划文档只是假设 |
| 47 | 「用户能看见内容」掩埋口径错误 | 先问「有测试吗」；grep 该语义所有实现点逐一对照 |
| 48 | 参数全摆出来 ≠ 人看得懂 | 可视化验收＝使用者能否据此做决定；文案禁 Markdown |
| 49 | 用 PowerShell 改 UTF-8 中文（二次编码） | 只用 Python/编辑器；这类事故先找版本控制再谈修复 |
| 50 | 状态页只把接口字段摆一遍 | 密度用开关解决不用删减；每个数字都要有锚 |
| 51 | 同一屏自相矛盾（items 说「已注入」、did 说「没进」） | 同一事实只许一个派生点；两处派生必须互校 |
| 52 | 把内容名写进信息架构（分组用「恋爱军师」、按钮写死「色色模式」） | 架构只认机制（注入方式/叠加替换），内容名只留在路径与显示名里 |
| 53 | 脚本批量改完就当改好了（CSS 那一段因前一步报错整段没执行，汇报里却写着「已修好」） | 批量脚本的**每一步都要打 applied/SKIP**，收工前 grep 一次落盘证据；报「已修」只能依据读回来的文件，不能依据脚本「跑过了」 |
| 54 | 组级属性存成条目级副本（组标题写在每条协议的文件头，同组 6 条 6 份；改哪份生效靠遍历顺序，撞名则前端合并） | 组级的东西只留**一份定义**（组表文件），条目只写引用；「这个字段的拥有者是组还是这一条」问一次就能判 |
| 55 | 用 `Path.relative_to` 当路径校验（`图库/../secret.txt` 能通过——它只做前缀字符串比较，**不解析 `..`**） | 越界校验一律先 `resolve()` 再比；安全边界必须有「穿越样本必须被拒」的阳性对照 |
| 56 | 数字输入用 `<input type="number">` 配受控 value（0.08 输不进、负数也输不进，而且一声不响） | number 的 DOM 存不下 `0.`/`-`（设进去读回空串），受控渲染再把它写成 0 → 数字输入一律 text+inputMode+本地 draft、失焦兜底提交；闸门 `test_desktop_number_input_contract.py` |

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
| model_refs.py | 模型解析两件事：① **provider 组表唯一换算点** `provider_conn()`——接口地址/Key 只在 `llm.providers` 里存一份，档位只写 `provider: <组id>` 引用（教训 #54）；② **「选一个模型」唯一换算点** `resolve_conn()`——`model_ref` 只有三种写法（`''`=当前聊天档 / `<档位id>` / `ollama:<模型名>`），识图与打标都只调它，**别处不许自己翻 providers，也不许再加「方式」这种第二维度**（2026-10-28 主人拍板） |
| store.py | 数据库统一入口——SQLite 唯一访问层 |
| personality.py | 人格引擎——系统提示词（稳定前缀缓存契约）+ 角色卡加载 |
| protocols.py | 行为协议单一事实源——纠正信号/理解与思考协议/背景块标记 bg()/用户消息组装 |
| voice.py / sticker.py | 语音合成(**唯一 provider：GPT-SoVITS**) / 贴图系统 |
| reply_pipeline.py | 回复后处理——清洗（含 <思考> 剥离）→贴图→@解析→分句发送 |
| drives.py | 驱动力引擎——6 种驱动力积累/释放/竞争 |
| knowledge.py | 知识库——hybrid+RRF+rerank 混合检索（2026-08-15，细节见项目规划） |
| intimate_reference.py | 亲密模式范本（色色参考）——**独立模块**（T2.14）：槽位加载/格式/归档检索；投递走 `assemble_user_message` 的 `pinned`，**不进背景池**（旧路径实测 33/33 被整块淘汰） |
| self_state.py | 持久自我状态——关系场/自我叙事/价值观/体验缓冲 |
| reflection.py | 反思整合——个人反思+社交感知，定时后台 |
| conversation_tracker.py | 对话追踪——窗口检测/退让/闲聊轮次限制 |
| context_builder.py | 状态投影器——Token 预算 + 上下文组装 |
| mood.py / scenario.py | 情绪三维模型 / 场景引擎(replace/overlay) |
| interjection.py | 插话引擎——客观信号评分 |
| perception.py / relationship.py | 反馈感知(情绪三分类评估) / 关系档案合成 |
| role_card.md / config.yaml / .env | 人格定义(第一来源) / 配置 / API 密钥(最高优先) |
| 状态/ · 日志/ | 运行时状态与日志——**唯一入口** `agent/paths.py` 的 `state_path()` / `log_path()`（2026-10-15 T55-F 根目录规范化）。**禁止任何模块自己拼根相对路径** |

**语音与唱歌（2026-10-15 定版，写文档只能依据这一节）**：

- **说话**：只有 **GPT-SoVITS** 一个 provider（控制台里是只读标签）。CosyVoice3 及其目录已于 2026-10-15 整体删除——文档里不再有它的配置/启动/端口(9267)/音色注册任何说法。
- **唱歌**：**播放录音成品**——`sing` 工具 → `agent/songs.py` → `songs/audio/歌名.wav`（糖糖声线成品）+ `songs/covers/separated/歌名_FINAL.*`（原声）。**不做实时合成，不需要任何模型、不需要 RVC / demucs / UVR5。**
- **翻唱制作侧已整体移除**：歌唱工作室、曲库设置页、RVC 训练/推理/索引全部不在。保留的是**播放侧**（点歌、唱歌、`/歌单`）。
- **加歌方法**：把 `歌名.wav` 放进 `songs/audio/`（糖糖声线）；原声放 `songs/covers/separated/歌名_FINAL.wav|mp3`。然后 `/歌单 重载`。
- ⚠ **歌词与「分段唱歌」已整体删除**（2026-10-15，主人确认 22 个歌词 txt 是他删的、不需要了）：不要再往 `songs/` 放 `歌名.txt`（**不会被读取**），也不要以为 `[主歌1]`/`[副歌]` 段落标记、按段落点唱还在——`agent/songs.py` 的模块 docstring 写清了删了什么、为什么不能加回来。
- **糖糖现用音色参考音**：丛雨（murasame），素材在 `音频/丛雨/`（400 个 `mur001_*.wav`）——**不是 RVC 产物，保留**。
- **DiffSinger**：已删除，端口 9266 不再使用（教训 #35 里提到它只是当时手册审计的一条记录）。

**命令与工具系统**：

| 方式 | 机制 | 用途 |
|------|------|------|
| `/command` | 正则匹配，系统执行 | 精确操作：/人格 重载、/状态、/黑名单 |
| Native Tool Calling | OpenAI function calling | **主要方式**——LLM 自主调用 calculate/get_weather/web_search/sing/send_voice/search_facts/... |
| `[贴图:xxx]` | LLM 输出标签 | 表情包——LLM 自主选择时机和情绪 |

`[CMD:...]` 已退役。保留的轻量协议：`[SING]`（播放这首歌的成品音频）、`[贴图:关键词]`、`[不说话]`（自治循环否决）、`[进入色色]`/`[退出色色]`（亲密模式动态开关——权限静态、激活动态，退出由 LLM 判断）、`[分段]`（消息分条，2026-09-22，**只开私聊**）、`[引用]`（引用回复，2026-09-23，**只开群聊**）。技能与工具清单**不在这里手抄**——代码是唯一事实源：`agent/skills.py` 的 `@register_skill`（注册技能）+ `agent/handler.py` 的内联工具定义与动态注册。

（2026-10-15：这里原先手抄着一份清单，被查出**双向错**——虚列 `draw` / `draw_divination_lot` / `draw_tarot` 三个生产不存在的工具，同时漏掉真实存在的 `guess_number` / `fortune` / `generate_image`，并且整段重复贴了两遍。手抄清单必然腐烂，要看真实清单请直接读代码。）

**什么时候改哪个文件**：

| 要做什么 | 改哪里 |
|----------|--------|
| 改糖糖的性格/说话方式 | role_card.md → /人格 重载（⚠️ **只写身份，不加规则**——规则/协议进 protocols.py，测试有闸门） |
| 加行为规则/情境协议 | protocols.py（背景块标记/协议文本/信号表的唯一来源） |
| 加新技能/工具 | 对应模块注册 `@register_skill` → 自动变为 LLM 可调用工具 |
| 加记忆/数据查询工具 | handler.py `_build_memory_tools` + `_execute_tool` |
| 改配置参数 | config.yaml（⚠️ 必须同步 **desktop 设置页**的预设值——Qt 控制台 2026-10-25 已由主人解除红线、授权删除；桌面端改 `desktop/`，见 [立项](docs/开发规划/桌面端改进立项_20261025.md)） |
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
- **仪器纪律（2026-10-16 新增，教训 #37）**：闸门/扫描器/判据类改动必须能红（阳性对照）；行为改动必须过 `python tools/变异对照.py`（GREEN = 假变异 = 不合格）；测试拉子进程时编码两端钉死 UTF-8

**本仓环境备忘**（小糖糖专属，通用的那部分已提到全局 `~/.claude/`）：

- 测试：`python -m pytest tests/ -q`（**只跑 tests/**——根目录有其他环境相关的收集错误）
- 静态检查：`python tools/静态检查.py`，必须清零
  ⚠ **不要手写那条 pyflakes agent/\*.py 命令**：pwsh / cmd 不展开 glob，pyflakes 会报
  `Invalid argument` 把整个目录跳过 —— 2026-10-14 实测 **82 个文件只真扫了 2 个，
  而输出照样是「undefined = 0」**（「跑起来没报错」≠「真的扫了」，教训 #30 同族）。
  脚本由 Python 自己 glob，并**打印实际扫描的文件数**；pyflakes 一抱怨参数就立刻红灯。
- 日志在 `日志/tangtang.log`（2026-10-15 T55-F 起）巨大（70 万+ 行）——**用 python 脚本过滤，不要直接 grep 全文**
- 数据在 `memory.db`（SQLite）。排查记忆/关系/画像问题**直接查库**，别猜
- 运行时状态在 `状态/`、日志在 `日志/`——落盘位置唯一入口是 `agent/paths.py` 的
  `state_path()` / `log_path()`；**新加状态文件不许自己拼根相对路径**（闸门
  `tests/test_runtime_paths_contract.py` 会红）。**根目录不放状态文件**：`.stignore`
  里那几行根级旧名字是给 Syncthing 的「永不复活」守卫，删了就会被对端旧副本
  按原 mtime 填回来（2026-10-15 01:31:14 实测，syncthing.log 有据）
- **本仓是 git 仓库**（2026-10-15 起初始化，远端 GitHub `downtown20/tangtang-qq-bot`）；旧发布仓 `D:\小糖糖-发布` 已弃用。GitNexus 刷新见第八节

详细反模式记录：[[bug-fixing-anti-patterns]]

---

## 六、已确认有效的积累（不要推翻）

> 只写**结论 + 最关键的一条约束 + 闸门名**。推导过程与历史包袱见
> [已确认积累_详情](docs/教训/已确认积累_详情_20261025.md)。

- **单脑架构**：LLM 做决策、系统做执行。不回双脑。
- **群聊窗口回复契约**（2026-08-25）：窗口内同一用户的每条消息**逐条**进 LLM，系统不得再按
  waiting/长度/标点/懒回/连续次数二次门控；沉默归她自己（`skip_response` 写
  `turn_actions.respond=false`）；插话开关只控制窗口外评分入口。
- **原生 Tool Calling 为主、轻量标记为辅**：`[SING]`/`[贴图:]`/`[不说话]`/`[进入色色]`/
  `[分段]`(仅私聊)/`[引用]`(仅群聊)。**工具与技能清单以代码为唯一事实源**
  （`agent/skills.py` 的 `@register_skill` + handler 内联注册），**禁止手抄清单**（必腐烂，#35）。
- **发送协议必须在清洗前冻结**：所有发送标记一律在 `ReplyPipeline.clean()` **之前**解析成
  结构化事实——① clean 会剥掉标记，之后就再也读不到意图；② **语音路径直读 `reply`，
  标记活到那里会被念出来**。解析窄（只认半角）、兜底宽（clean 连全角一起剥）。
- **引用判断归 LLM**（2026-09-23）：唯一决策源是 `[引用]` 标记，系统只负责用哪个
  `msg_id`、挂哪一段。旧 `should_quote` 已退役（曾让 53.2% 群聊回复带引用前缀）。
  闸门 `test_system_quote_heuristic_does_not_come_back`。
- **role_card 优先**，config.yaml 兜底。**知识库递归扫描** `knowledge/` 下全部 `.md`。
- **记忆置信度**：提取 < 0.5 丢弃；合成记忆 importance=4（不盖真实记忆）；自我否定陈述
  （「我是废物」是情绪症状不是事实）不入库。
- **不做自然遗忘**（2026-10-25 主人拍板，**价值决策**）：记忆只增不减；信噪比问题只靠
  **检索精度**解决，**禁止**抑制记录/类型限流/按比删减等变相遗忘；「高价值」的唯一判据是
  **有没有真的被用上**（被想起/被兑现/被提起）。
- **真值生命周期**（2026-08-16）：`status`(active/retracted/superseded)，撤销不改
  importance；`notes_dirty` 期间禁注入；置信度分层（缺失拒绝、显式 0.7 不进画像）；
  合成＝事务化世代替换（24h 冷却，纠正绕过）；写回工具 `correct_memory`/`forget_memory`
  （本人纠自己／仅主人纠第三人）。运维见 `docs/审查/记忆系统治理运维_20260816.md`。
- **画像注入**：`people.notes` **私聊注入、群聊刻意不注入**（画像＝「我认识你」，一对一才成立）。
  信任门只放行 verified/manual/corrected；**legacy 停用自动注入**（2026-08-16 事故防线），
  要进注入只能经显式重合成产出 verified。闸门：`test_private_generation_scope_excludes_...`
  ＋ AST 断言「有且只有一个调用点注入画像」。
- **三层个人化**：怎么聊（scenario）→ 她是谁（`people.notes`）→ 聊什么（memories）。
- **心理陪伴场景**：不规定她怎么说话——只给一层理解（ta 在经历困难）+ 两条边界
  （不追问创伤细节、不演身份戏剧）。
- **插话配额分离**：被 @ 和 `/命令` 不消耗主动插话配额。
- **交叉上下文限权**：私聊提到其他 QQ 才加载那个人档案，**仅主人或亲密度≥50**（防泄露）。
- **`_allowed_groups` 初始化就排除黑名单**——所有定时系统（生日/点赞/图片分享）自动安全。
- **表情包原始路径** `file:///D:/...`，不 URL 编码中文；纯图模式贴图附加、不替换文字。
- **流式输出替代伪造延迟**：真实生成速度就是自然延迟，不加假 sleep。
- **记忆提取**：auto_learn 关键词正则已退役 → LLM 语义提取（群 10 条/私聊 6 条触发）。
- **感知**：`perception.py` 情绪三分类；五信号统计在 `handler._record_feedback`。
- **自忆端到端**：`_extract_self_memories` 写 `bot_qq` → `_get_self_memory_context` 用 BGE
  语义匹配注入（未就绪降级最近 5 条）——她记得自己说过的话。
- **上下文来源标记**（2026-08-15）：框架注入一律 `<背景·标签>`，**用户原文永不包裹、永远最后**。
  例外：主动插话是生成简报、不贴标签（已文档化）。
- **能力数据按需注入**：歌单只随 `sing` 工具出现，不常驻上下文（能力清单 ≠ 对话背景）。
- **接线审计测试** `tests/test_integration_contract.py`：零调用/管线契约/判定单一来源，
  新死代码或第二判定点直接红。
- **发送链路契约**：LLM 文本发给用户前必须过 `_enrich_reply`（清洗+贴图+@解析）。直发点清单在
  `test_llm_send_paths_must_enrich` 的 `LLM_SEND_FUNCS` 表，**新增直发路径必须追加**。
  例外：主人原话直发(<50字)/命令结果/固定文本/歌词/媒体 CQ。
- **意图解析单一决策源**（2026-08-16）：自然语言意图（提醒/延迟发言）只走 LLM 工具
  （`set_reminder`/`group_say_later`）；系统 precise 层只允许动作确认类命令。
  闸门 `TestPreciseCommandsBoundary` 禁止时间量词/提醒词回 precise 层。
- **困难轮次协议**：纠正信号/引用取不到 → 理解协议（复述→承认→问不猜→不复读）
  + `<思考>` 隐藏段（发送前剥离）+ 模型升级。复读自检只在困难轮次开。
- **引用解析状态机**：`QUOTE_RESOLVED/MISSING/NONE` 枚举分流（不用内容字符串判定），结果写回历史。

## 六-2、个人化——三层架构

| 层 | 回答问题 | 存储 | 设置方式 |
|----|---------|------|---------|
| 怎么聊 | 语气、长度、禁忌 | 场景 (psychology/seductive) | 控制台 scenario_targets |
| 她是谁 | 名字、处境、需求 | people.notes | 数据库直接编辑 |
| 聊什么 | 具体说过的事 | memories | 记忆系统自动积累 |

**心理陪伴场景 (psychology.yaml)**：通用——任何人挂上就生效。糖糖还是糖糖，不固定她的说话方式；只注入一层理解 + 两条边界（不追问创伤细节、不演身份戏剧）。主动关心由私聊插话 + 情绪闭环实现。控制台群管理 → scenario_targets → 选「🧠 陪伴」。

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

## ⚠ 重建 GitNexus 索引必须带两个参数（2026-09-22 踩出来的）

```bash
GITNEXUS_MAX_FILE_SIZE=1024 node .gitnexus/run.cjs analyze --skip-git
```

- **`--skip-git`** —— 2026-10-15 之前本仓不是 git 仓库，不带它分析器**直接拒绝**；现在已是 git 仓库，但 `--skip-git` 仍是本项目既定刷新用法（与索引历史一致，别随手去掉）
- **`GITNEXUS_MAX_FILE_SIZE`** —— 默认上限 **512KB**，而 `agent/handler.py`(676K) 和
  `agent/store.py`(664K) 都超了，**会被静默跳过**。

第二条是最阴的：跳过之后 `meta.json` 照样报 `38057 nodes / 78445 edges / 300 flows`，
**数字看着很健康**。表现是 `impact("_call_deepseek")` → `not found`、`query` 返回
`processes: []` —— **消息路由中枢 `handler.py` 和数据库层 `store.py` 根本不在图里**，
而 CLAUDE.md 这段自动生成的说明一个字都没提。加上参数后 38,057 → **43,713** 节点。

**怎么验索引是不是残的**：`impact` 一个 `agent/handler.py` 里的符号（比如 `_call_deepseek`），
报 `not found` 就是残的，去重建。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **qq-小糖糖** (44482 symbols, 93045 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
