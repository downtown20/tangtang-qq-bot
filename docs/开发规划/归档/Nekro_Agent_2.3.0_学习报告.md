# Nekro Agent 2.3.0 技术架构深度解析与设计思想学习报告

> 阅读源码版本：v2.3.0
> 源码路径：D:\claude code相关资源\nekro-agent-2.3.0
> 写作日期：2026-08-02
> 目的：理解 Nekro 的设计思想，提取对糖糖架构演进有参考价值的设计模式

---

## 零、先定位——Nekro 是什么，不是什么

Nekro Agent 是一个 **以工作区为核心的通用 Agent 运行平台**。它不是聊天机器人框架——聊天只是它支持的场景之一。它的核心隐喻是：

> **AI 生成代码 → Docker 沙盒执行代码 → RPC 桥接真实世界**

这个模型决定了它的一切设计选择。

和糖糖的根本差异：

| 维度 | 小糖糖 | Nekro Agent |
|------|--------|-------------|
| LLM 产出 | 自然语言 + Tool Calling | Python 代码 |
| 执行环境 | 无隔离，主进程直接执行 | Docker 沙盒，exit code 控制流 |
| 安全模型 | Tool Calling 白名单 | 容器隔离 + RPC 代理 |
| 人格 | 单一猫娘，深度发展 | 多角色（Preset），工作区隔离 |
| 定位 | 一个特定的人 | 任何人的 Agent 平台 |
| 用户 | 一个人 | 多租户 |

理解这个差异是阅读本报告的前提。Nekro 的很多设计选择是为了"让任何人都能搭建自己的 Agent"——糖糖不需要这个目标。但它为了支撑这个目标而发展出来的架构模式，值得学习。

---

## 一、架构全景

### 1.1 目录结构

```
nekro-agent-2.3.0/
├── nekro_agent/              # 核心引擎 (~80 个 .py 文件)
│   ├── core/                 # 配置、日志、数据库、向量库基础设施
│   │   ├── config.py         # 104KB——所有配置定义
│   │   ├── overridable_config.py  # 54行——配置覆盖机制
│   │   ├── database.py       # Tortoise ORM 初始化
│   │   └── vector_db.py      # Qdrant 初始化
│   ├── services/             # 业务服务层
│   │   ├── agent/            # Agent 执行引擎
│   │   │   ├── run_agent.py  # 核心循环：编译 prompt → LLM → 解析代码 → 沙盒执行 → 迭代
│   │   │   ├── openai.py     # OpenAI 兼容 API 调用
│   │   │   ├── resolver.py   # 从 LLM 输出解析代码块
│   │   │   └── templates/    # Jinja2 模板（系统提示、历史、示例）
│   │   ├── plugin/           # 插件系统
│   │   │   ├── base.py       # 993行——NekroPlugin 基类（全部挂载点）
│   │   │   ├── collector.py  # 插件收集器（扫描、加载、生命周期）
│   │   │   ├── manager.py    # 插件管理器
│   │   │   ├── schema.py     # 沙盒方法类型枚举 + 数据结构
│   │   │   └── task.py       # 异步任务系统
│   │   ├── sandbox/          # 沙盒执行层
│   │   │   ├── runner.py     # Docker 容器管理 + 代码注入 + 结果收集
│   │   │   ├── ext_caller.py # 为沙盒生成 RPC 代理代码
│   │   │   └── ext_caller_code.py  # 沙盒内运行的 RPC 客户端代码
│   │   ├── memory/           # 记忆系统（14个文件，核心模块）
│   │   │   ├── consolidator.py    # LLM 驱动的记忆提取与沉淀
│   │   │   ├── retriever.py       # 语义检索 + 权重计算 + 上下文编排
│   │   │   ├── episode_aggregator.py  # 确定性事件聚合
│   │   │   ├── scheduler.py       # 记忆维护调度
│   │   │   ├── rebuild.py         # 记忆重建
│   │   │   └── qdrant_manager.py  # Qdrant 向量库操作
│   │   ├── chat/             # 通用聊天服务
│   │   ├── message/          # 消息广播服务
│   │   ├── config/           # 配置服务
│   │   ├── timer/            # 定时器服务
│   │   └── command/          # 命令系统
│   ├── adapters/             # 平台适配器层（9个平台）
│   │   ├── interface/        # 适配器抽象接口
│   │   ├── onebot_v11/       # QQ (OneBot 协议)
│   │   ├── discord/          # Discord
│   │   ├── telegram/         # Telegram
│   │   ├── minecraft/        # Minecraft
│   │   ├── bilibili_live/    # B站直播
│   │   ├── wechatpad/        # 微信
│   │   ├── sse/              # 通用 SSE + SDK
│   │   └── email/            # 邮件
│   ├── schemas/              # 数据模型（Pydantic）
│   │   └── agent_ctx.py      # AgentCtx——统一上下文对象
│   ├── models/               # 数据库模型（Tortoise ORM）
│   └── api/                  # Web API 路由
├── sandbox/                  # 沙盒 Docker 镜像
│   └── nekro_agent_sandbox/
│       └── ext_caller_code.py  # 沙盒内 RPC 客户端（独立于主进程）
├── plugins/                  # 内置插件
│   └── builtin/              # ~20 个内置插件
└── frontend/                 # WebUI
```

### 1.2 核心数据流

这是理解 Nekro 最关键的一张图：

```
外部消息（QQ/Discord/...）
    │
    ▼
[适配器层] — 统一为内部消息格式
    │
    ▼
[MessageService] — 写入 DB，触发 Agent
    │
    ▼
[run_agent()] — 核心循环入口
    │
    ├─ 1. PromptCompiler 组装系统提示词
    │     ├── PolicyKernelPrompt     (硬行为准则)
    │     ├── PersonaPrompt          (角色人设)
    │     ├── RuntimeContractPrompt  (平台/能力/格式要求)
    │     └── PluginsPrompt          (插件动态注入)
    │
    ├─ 2. LLM 生成 → Python 代码文本
    │
    ├─ 3. parse_chat_response() → 从文本提取代码块
    │
    ├─ 4. limited_run_code() → Docker 沙盒执行
    │     │
    │     ├── 代码中的函数调用 → __extension_method_proxy
    │     ├── pickle 序列化参数 → HTTP POST → 主进程 RPC 端点
    │     ├── 主进程执行真正的插件方法
    │     └── pickle 序列化返回值 → HTTP Response → 沙盒
    │
    └─ 5. 根据退出码决定下一步：
          exit 0  → 结束，沙盒输出为最终回复
          exit 8  → AGENT 方法被调用，返回值注入上下文，LLM 再跑一轮
          exit 11 → MULTIMODAL_AGENT，多模态内容注入，LLM 再跑
          exit 1  → 错误，错误信息注入上下文，LLM 修正代码
          (最多重试 N 次)
```

**这个循环的核心洞察**：Nekro 的 LLM 不直接生产"回复"——它生产"执行计划"（Python 代码）。真正的"回复"是代码在沙盒里跑完后的副作用（调用 `send_message` 等）和输出。这意味着：

- LLM 可以写复杂的多步骤逻辑（循环、条件、数据处理）
- 执行过程有完整的 Python 运行时（可以 `import`、算数、调 API）
- 安全由 Docker 容器保证，不需要白名单

对糖糖来说，这个模式太重了——糖糖不需要 AI 写代码。但其中 **exit code 作为控制流信号** 的设计思路，可以轻量化地借鉴到 Tool Calling 返回值处理中。

---

## 二、设计模式深度分析

### 2.1 沙盒方法四分类——最值得糖糖吸收的设计

#### 问题

当 LLM 调用一个工具后，系统需要决定：要不要把返回值注入上下文？要不要触发 LLM 再想一轮？

糖糖目前不区分——所有 Tool Calling 的返回值都注入上下文，由 LLM 自己决定下一步。这在大多数情况下是对的，但缺少精细控制。比如 `send_voice` 成功后，不需要 LLM 再想"我已经发完语音了，现在该说什么"——这会浪费一次 API 调用。

#### Nekro 的方案

[Nekro 的 SandboxMethodType](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\plugin\schema.py) 定义了四种类型：

```python
class SandboxMethodType(str, Enum):
    TOOL = "tool"
    AGENT = "agent"
    BEHAVIOR = "behavior"
    MULTIMODAL_AGENT = "multimodal_agent"
```

| 类型 | 返回值 | 是否注入上下文 | 是否触发 LLM 再思考 | 典型场景 |
|------|--------|:---:|:---:|------|
| TOOL | 任意可序列化 | 否（LLM 在沙盒内直接拿到返回值继续执行代码） | 否 | 计算、文件读写 |
| AGENT | str | 是 | **是** | 搜索、知识库查询 |
| BEHAVIOR | str | 是（记录到系统消息） | **否** | 发消息、设定时器 |
| MULTIMODAL_AGENT | 多模态消息段 | 是 | **是** | 图片理解 |

#### 分层信号机制

注意：这个分类只在 **沙盒内** 生效。在沙盒里，LLM 生成的代码调用这些方法，代理函数根据方法类型决定行为：

```python
# ext_caller_code.py —— 沙盒内的 RPC 代理
def __extension_method_proxy(method):
    def acutely_call_method(*args, **kwargs):
        # 通过 RPC 调主进程执行真正的插件方法
        response = requests.post(RPC_URL, data=pickle.dumps(body), ...)

        if response.headers.get("Method-Type") == "agent":
            # AGENT: 打印返回值 → exit(8) → 外层循环捕获 → 注入上下文 → LLM 再跑
            print(f"The agent method returned:\n{ret_data}\n[result end]")
            exit(8)

        if response.headers.get("Method-Type") == "multimodal_agent":
            # MULTIMODAL: 打印返回值 → exit(11) → 同上
            print(f"The multimodal agent method returned:\n{ret_data}\n[result end]")
            exit(11)

        # TOOL / BEHAVIOR: 正常 return，沙盒代码继续执行
        return ret_data
    return acutely_call_method
```

外层循环捕获退出码：

```python
# run_agent.py
if stop_type == ExecStopType.AGENT:
    # 把返回值当作用户消息注入，触发新一轮 LLM 生成
    msg = msg.extend(OpenAIChatMessage.from_text(
        "user",
        f"[Agent Method Response] {sandbox_output}\n"
        "Please continue based on this agent response."
    ))
    # → messages 扩展后，下一轮循环 LLM 继续生成
```

#### 对糖糖的启发——Tool Calling 的 AGENT vs BEHAVIOR 区分

糖糖不需要沙盒，不需要 exit code。但可以在 Tool Calling 定义层面引入类似的区分：

```python
# 当前：所有工具平权
@register_skill("send_voice", "发送一段语音")
async def send_voice(text: str): ...

@register_skill("web_search", "搜索互联网")
async def web_search(query: str): ...

# 改进：标记工具的行为模式
@register_skill("send_voice", "发送一段语音", method_type="behavior")
async def send_voice(text: str): ...
# → 工具执行后，返回值不触发 LLM 再处理

@register_skill("web_search", "搜索互联网", method_type="agent")
async def web_search(query: str): ...
# → 工具执行后，返回值注入上下文，触发 LLM 消化搜索结果
```

实现上很简单——在 `skills.py` 的 `Skill` dataclass 加一个 `method_type` 字段，在 `handler.py` 的工具执行结果处理中分支判断。这个改动的代码量不超过 30 行，但能消除不必要的 LLM 轮次。

---

### 2.2 插件系统——装饰器驱动的完整生命周期

#### 设计哲学

Nekro 的插件系统不是"注册一个函数"——是**完整的生命周期管理**。每个插件是一个独立的自治单元：

```
on_enabled() → 挂载沙盒方法 → 提示词注入 → 消息回调 → 异步任务 → on_disabled()
     ↑              ↑               ↑            ↑           ↑
   资源初始化    暴露给 AI       每次对话      事件响应    后台长任务
                 的能力         动态注入
```

#### NekroPlugin 的挂载点全貌

```python
class NekroPlugin:
    # 名称: 插件名称（默认）
    # module_name: 插件模块名
    # description: 插件描述（默认）
    # version: 插件版本
    # author: 插件作者
    # url: 插件地址
    # support_adapter: 支持的适配器
    # allow_sleep: 是否允许休眠

    # === 挂载点 ===

    @plugin.mount_sandbox_method(type, name, desc)
    # → 注册沙盒方法（AI 可调用的能力）

    @plugin.mount_prompt_inject_method(name, desc)
    # → 每次对话前注入自定义提示词

    @plugin.mount_on_user_message()
    # → 收到用户消息时的回调

    @plugin.mount_on_channel_reset()
    # → 频道重置时的回调

    @plugin.mount_router()
    # → 自定义 FastAPI 路由（对外 Web API）

    @plugin.mount_async_task(task_type)
    # → 注册异步后台任务

    @plugin.mount_command(name, desc, ...)
    # → 注册命令

    @plugin.on_enabled()
    # → 插件启用回调（可注册多个）

    @plugin.on_disabled()
    # → 插件禁用回调（可注册多个）

    @plugin.mount_cleanup_method()
    # → 插件卸载时清理

    @plugin.mount_config()
    # → 绑定配置类（自动加载/保存 YAML）
```

#### 每个插件自带数据库

```python
class PluginStore:
    async def get(chat_key, user_key, store_key) -> str | None
    async def set(chat_key, user_key, store_key, value) -> int  # 0=新建 1=更新
    async def delete(chat_key, user_key, store_key) -> int
```

内部映射到 PostgreSQL 的 `DBPluginData` 表，自动按 `(plugin_key, chat_key, user_key, store_key)` 隔离。这意味着插件开发者永远不需要写 SQL——`plugin.store.set()` 一行搞定持久化。

#### 对糖糖的启发

糖糖不需要完整插件系统。但有两个点值得提取：

**1. 插件级 K-V 存储的模式**。糖糖现在各模块各自写 `_save()/_load()` JSON 文件（TangTangSelf、reflection buffer、perception 统计），格式和路径各不同。如果提取一个统一的 `ModuleStore`：

```python
class ModuleStore:
    """每个模块独立的 K-V 持久化存储"""
    def __init__(self, module_name: str):
        self._table = f"module_{module_name}"

    async def get(key: str) -> str | None: ...
    async def set(key: str, value: str): ...
    async def delete(key: str): ...
```

底层用 SQLite（一行 `CREATE TABLE IF NOT EXISTS`），各模块通过统一的 `ModuleStore("self_state")` / `ModuleStore("reflection")` 访问。不再需要每个模块手写文件 I/O。

**2. 生命周期回调的注册模式**。糖糖的模块初始化和清理散落在 handler.py 各处。如果每个模块有明确的 `init()` 和 `cleanup()` 约定，启动/关闭流程会更清晰。

---

### 2.3 AgentCtx——统一上下文对象

#### 设计

Nekro 用 [AgentCtx](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\schemas\agent_ctx.py)（Pydantic BaseModel）作为所有插件方法、沙盒方法、回调的统一上下文。它的关键属性：

```
AgentCtx
├── 频道信息
│   ├── chat_key: str          # 频道唯一ID（如 "onebot_v11-group_12345678"）
│   ├── channel_id: str        # 平台原始ID
│   ├── channel_name: str      # 频道名称
│   ├── channel_type: str      # "group" / "private"
│   └── adapter_key: str       # "onebot_v11" / "discord" / ...
│
├── 适配器
│   └── adapter: BaseAdapter   # @property 懒加载，通过 adapter_key 查找
│
├── 沙盒
│   └── container_key: str     # Docker 容器ID
│
├── 文件系统
│   └── fs: FileSystem         # @property，按 container_key 隔离
│       ├── mixed_forward_file(url/path/bytes) → sandbox_path  # 外部→沙盒
│       └── get_file(sandbox_path) → host_path                 # 沙盒→外部
│
├── 消息
│   └── ms: message 模块       # @property
│       ├── send_text(chat_key, content, ctx, record=True/False)
│       ├── send_image(chat_key, path, ctx)
│       ├── push_system(chat_key, msg, ctx, trigger_agent=True/False)
│       └── ...
│
├── 配置
│   └── get_core_config() → CoreConfig  # async，三级合并后的最终配置
│
├── 人设
│   ├── get_preset_by_id(id) → DBPreset | None
│   ├── get_effective_preset_by_id(id) → DBPreset | DefaultPreset
│   ├── set_preset(id) → bool
│   └── current_preset() → preset
│
├── 数据库
│   ├── db_chat_channel: DBChatChannel  # 当前频道 DB 对象
│   └── db_user: DBUser                # 触发用户 DB 对象
│
└── Webhook
    └── webhook_request: WebhookRequest  # 如果是 webhook 触发
```

#### 设计要点

1. **懒加载**——`fs`、`ms`、`adapter` 都是 `@property`，第一次访问时才初始化
2. **不可变**——是 Pydantic BaseModel，创建后基本字段不变，行为通过方法暴露
3. **创建路径分离**——有 `create_by_db_chat_channel()`、`create_by_chat_key()`、`create_by_webhook()` 三个工厂方法，根据触发来源选择
4. **消息记录的 record 开关**——`send_text(content, record=False)` 可以发消息但不记入对话历史，避免"正在处理中..."这类状态消息污染 LLM 上下文

#### 对糖糖的启发

糖糖目前没有统一上下文对象。handler.py 里靠参数传递：

```python
# 当前——散落各处的参数
async def _handle_group_message(self, message, group_id, user_id, nickname, ...):
    self.memory      # 记忆系统
    self.self_state  # 自我状态
    self.config      # 配置
    self.bot_qq      # Bot QQ号

# 如果引入轻量上下文：
@dataclass
class ChatContext:
    chat_type: str          # "group" / "private"
    chat_id: str            # 群号或用户QQ
    user_id: str
    user_nickname: str
    chat_name: str          # 群名或用户名
    # 不持有服务引用——服务由 handler 注入
```

不需要像 Nekro 那么重（糖糖不需要文件系统、不需要多适配器），但一个 `ChatContext` dataclass 可以减少 handler 方法签名中的参数数量。

---

### 2.4 记忆系统——四层结构与权重工程

Nekro 的记忆系统是整个项目中设计最精细的模块，14 个文件，覆盖了提取、存储、检索、编排、维护、重建的全生命周期。

#### 2.4.1 数据模型——四层记忆

```
DBMemEpisode (情节)
├── title: str                    # "张三和李四讨论周末计划"
├── narrative_summary: str        # "张三说想去爬山；李四说周日有空..."
├── time_start / time_end         # 情节时间跨度
├── participant_entity_ids: list  # 参与者实体引用
├── paragraph_ids: list           # 包含的段落引用
├── phase_mapping: dict           # 各阶段 (OPENING/DEVELOPMENT/CLIMAX/RESOLUTION) 的段落分组
└── base_weight: float            # 情节权重

DBMemParagraph (段落) ← 最小编程单元
├── content: str                  # 原文或 LLM 摘要
├── summary: str                  # 一句话概括
├── cognitive_type: enum          # EPISODIC (发生过的事) / SEMANTIC (知道的事)
├── knowledge_type: enum          # fact / preference / experience / decision / task / skill / relation
├── episode_id: int | None        # 所属情节
├── episode_phase: enum | None    # 在情节中的阶段位置
├── base_weight: float            # 基础权重
├── decay_rate: float             # 衰减率
├── event_time: datetime          # 事件发生时间
├── origin_chat_key: str          # 来源频道
├── is_inactive: bool             # 是否已失活
└── embedding (在 Qdrant 中)      # 向量

DBMemEntity (实体)
├── canonical_name: str           # 规范名称
├── name: str                     # 原始名称
├── entity_type: enum             # PERSON / ORGANIZATION / LOCATION / ...
└── is_inactive: bool

DBMemRelation (关系)
├── subject_entity_id: int        # 主体实体
├── predicate: str                # 谓词 ("likes", "works_at", "friend_of")
├── object_entity_id: int         # 客体实体
├── paragraph_id: int | None      # 来源段落
├── base_weight / decay_rate      # 同段落
└── is_inactive: bool
```

#### 2.4.2 记忆的创建——LLM 驱动的提取

Consolidator（[consolidator.py](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\memory\consolidator.py)，31KB）从聊天消息中提取记忆：

```
未处理的聊天消息 (DBChatMessage)
    │
    ▼
LLM 提取 (consolidator prompt)
    ├── 识别实体 (人/物/概念)
    ├── 提取关系 (谁对谁做了什么)
    ├── 生成段落 (情景记忆 paragraph + 语义记忆 paragraph)
    └── 输出结构化 JSON
    │
    ▼
写入数据库 + 生成向量 → Qdrant
```

关键细节：
- 所有记忆以**第三人称**视角记录（"张三说他喜欢火锅"）——不是"用户说我喜欢火锅"。这避免了记忆中的立场混淆
- `PersonaContext` 包含 Bot 的身份信息，确保 LLM 知道自己是谁，不出现在记忆中
- 解析失败有完整日志（写入 `memory_parse_failures/` 目录）

#### 2.4.3 Episode 聚合——确定性算法，不调 LLM

[EpisodeAggregator](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\memory\episode_aggregator.py) 将时间接近的 episodic paragraph 聚合为 Episode。**全程不调 LLM**——纯粹的时间 + 来源聚类：

```python
# 算法核心：
for paragraph in paragraphs:
    if (时间差 < 阈值) and (来源频道相同):
        加入当前组
    else:
        如果当前组 >= 最小条数 → 创建 Episode
        开启新组

# 自动分配情节阶段：
# 前 25%  → OPENING
# 25-65%  → DEVELOPMENT
# 65-85%  → CLIMAX
# 后 15%  → RESOLUTION
```

Episode 的标题和摘要也由确定性算法生成（取第一个段落的摘要截断 + 参与实体名称拼接），不额外消耗 LLM 调用。这个设计选择很重要——把它和 LLM 驱动的记忆提取解耦，避免了"为了聚合记忆再跑一次 LLM"的成本。

#### 2.4.4 记忆检索——权重工程

[MemoryRetriever](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\memory\retriever.py)（32KB）是检索的核心。

**检索流程**：

```
1. 查询向量化 (embed_text)
     │
2. Qdrant 向量搜索 (取 limit×2 候选)
     │
3. 计算有效权重:
     effective_weight = base_weight × similarity × episodic_boost × recent_boost
     ├── base_weight: 衰减后的基础权重 (paragraph.compute_effective_weight)
     ├── similarity: 向量相似度
     ├── episodic_boost: 情景记忆 1.3x 加成 (更具体，更有价值)
     └── recent_boost: 近期记忆 (24h内) 1.15x 加成
     │
4. 关系图谱补充检索:
     ├── 从查询文本中提取实体名
     ├── 匹配 DBMemEntity (canonical_name / name)
     └── 查找 DBMemRelation (subject/object entity)
     │   关系匹配分数 = 0.55 + 0.2(主体命中) + 0.2(客体命中) + 0.15(谓词语义匹配)
     │
5. Episode 级检索补充:
     匹配标题/摘要中的关键词 → 自动加权关联的 paragraph
     │
6. 按 effective_weight 排序 → 截断
```

**上下文编排**——不是简单拼接，有类型配额：

```python
# CORE_PLUS_EVIDENCE 模式下的配额：
TYPE_QUOTA = {
    "paragraph": 4,   # 最多 4 条段落记忆
    "episode":   2,   # 最多 2 条情节记忆
    "relation":  2,   # 最多 2 条关系记忆
}

CATEGORY_QUOTA = {
    DECISION:    3,   # 决策类最多 3 条
    FACT:        2,   # 事实类最多 2 条
    PREFERENCE:  1,   # 偏好类最多 1 条
    EXPERIENCE:  2,   # 经验类最多 2 条
    CONVERSATION: 2,  # 对话类最多 2 条
    RELATION:    2,   # 关系类最多 2 条
    EMOTION:     0,   # 情感类不注入
}
```

编排后的输出格式：

```
[记忆上下文]
当前关注：用户询问周末活动安排

需要关注的核心记忆：
- [事实 08-01 14:30] 张三说他周末有空
- [经验 07-28 10:15] 上次爬山张三体力不支...

辅助参考记忆：
- [关系] 张三 - 喜欢 - 户外活动
- [事件 07-30] 周末聚餐讨论...
```

#### 2.4.5 记忆维护——衰减与清理

Scheduler（[scheduler.py](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\memory\scheduler.py)）定期执行：

- **衰减**：根据 `decay_rate` 和距上次访问的时间降低 `base_weight`
- **失活**：权重低于阈值 → `is_inactive = True`（不删除，只是不再参与检索）
- **强化**：被检索访问的记忆记录到 `DBMemReinforcementLog`，根据访问频率微调 `base_weight`
- **重建**：从 `DBMemReinforcementLog` 反向检查哪些记忆应该被强化/失活

#### 对糖糖的启发

糖糖的记忆系统（`memory.py` 1465 行 + SQLite 单表）和 Nekro 的记忆系统不在一个量级。但有三个点可以轻量借鉴：

**1. 确定性 Episode 聚合**。糖糖的 Phase D 反思循环可以用类似的确定性算法先分组，再让 LLM 写摘要——减少 LLM 调用次数，同时产出结构化的"情节"而非自由文本日记。

**2. 检索的多类型配额制**。糖糖的 `recall()` 目前是单一维度（importance + similarity）排序。如果按 Nekro 的思路加入类型配额——确保 facts、profiles、episodes（如果有了）各占一定比例——上下文的覆盖会更均衡。

**3. cognitive_type 区分**。"发生过的事"（EPISODIC）和"知道的事实"（SEMANTIC）在检索时应该区别对待——对话中"上次你说过..."比"XX是一个XX的人"更能建立连接感。糖糖目前没有显式做这个区分。

---

### 2.5 配置层级覆盖——54 行实现

[overridable_config.py](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\core\overridable_config.py) 是 Nekro 中最短但最优雅的模块之一：

```python
def create_overridable_config_model(model_name, source_model):
    new_fields = {}
    for name, field in source_model.model_fields.items():
        # 检查字段是否标记为 overridable
        if field.json_schema_extra.get("overridable"):
            # 生成一对字段：
            #   enable_<name>: bool = False   # 是否启用覆盖
            #   <name>: T                     # 覆盖值
            new_fields[f"enable_{name}"] = (bool, Field(default=False))
            new_fields[name] = (field.annotation, value_field)
    return create_model(model_name, **new_fields, __base__=ConfigBase)
```

**使用方式**：

1. 在 `CoreConfig` 中标记字段为 `overridable`
2. 框架自动生成 `OverridableConfig` 类，每个字段多了 `enable_xxx` 开关
3. 适配器和频道可以分别保存自己的 `OverridableConfig`
4. `get_effective_config()` 时按 `系统 → 适配器 → 频道` 三层合并

**三级合并逻辑**（在 `DBChatChannel.get_effective_config()` 中）：

```python
effective = CoreConfig.load()                          # 系统级
if adapter_overrides:
    effective = effective.merge(adapter_overrides)     # 适配器覆盖
if channel_overrides:
    effective = effective.merge(channel_overrides)     # 频道覆盖（最高优先级）
return effective
```

#### 对糖糖的启发

糖糖的 `config.yaml` 是扁平的，群级别覆盖靠零散字段实现（`groups.xxx.scenario`、`groups.xxx.admins` 等）。如果要系统化：

1. 在 `config.yaml` 中给可覆盖的字段加 `overridable: true` 标记
2. 群级别配置存在数据库里（类似现在的 `groups` 表）
3. 运行时 `get_effective_config(chat_id)` 做三层合并

这个改动不大——核心就是 54 行的 `create_overridable_config_model` 逻辑。但收益明显：任何配置项都可以被群级别的设置覆盖，不再需要为每个需要覆盖的配置项单独写代码。

---

### 2.6 Prompt 工程——Jinja2 模板 + 分层组装

Nekro 的 Prompt 不是字符串拼接，而是 Jinja2 模板引擎驱动的分层组装。

**四层结构**（[compiler.py](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\agent\templates\compiler.py)）：

```python
class PromptCompiler:
    def compile_segments(self) -> PromptSegments:
        return PromptSegments(
            stable_static=PolicyKernelPrompt().render(env),
                # "你是 Nekro Agent。你永远不能..."
                # 硬行为准则，最稳定，几乎不改

            channel_static=PersonaPrompt(chat_preset=preset).render(env),
                # "你是一个友好的助手。你的名字是..."
                # 人设，按频道可切换

            runtime_dynamic=RuntimeContractPrompt(
                platform_name, bot_id, enable_cot, ...
            ).render(env),
                # "当前平台是 QQ。你的 ID 是 xxx。你拥有以下能力..."
                # 运行时环境信息，每次对话可能不同
        )

    def render_system_message(self):
        segments = self.compile_segments()
        return SystemPrompt(
            stable_static=segments.stable_static,
            channel_static=segments.channel_static,
            runtime_dynamic=segments.runtime_dynamic,
            plugins_prompt=self.plugins_prompt,  # 插件动态注入
        ).render(env)
```

**为什么用 Jinja2 而不是 f-string？**

- 中英文国际化——模板文件分 `j2/` 和 `j2_zh/`，切换语言只需换 Environment
- 模板可以继承和包含——`SystemPrompt` 继承 `BasePrompt`，共享结构
- 逻辑和展示分离——行为规则（Python 数据）和文本表达（Jinja2 模板）解耦

#### 对糖糖的启发

糖糖的 `personality.py` 用字符串拼接构建 system prompt——在功能上完全够用。Jinja2 模板化的主要收益是国际化（糖糖不需要）和分层管理（糖糖的人设/规则/能力已经通过 ContextBuilder 分层了）。

**不推荐迁移到 Jinja2**——糖糖只有中文，只有一个人格。增加的复杂度换不来收益。

但 Nekro 的四层分段思路值得保留在脑中：
- **不可变核心层**（行为准则）——几乎不改
- **人设层**（role_card）——偶尔调整
- **运行时层**（当前状态、平台信息）——每次不同
- **动态注入层**（记忆、知识库、插件）——每次不同

糖糖的 ContextBuilder 已经有类似的层次感（Core → Persona → State → Memory → Knowledge），可以继续沿着这个方向收紧各层的职责边界。

---

### 2.7 异步任务系统——后台长任务不阻塞主对话

Nekro 的异步任务（[task.py](D:\claude code相关资源\nekro-agent-2.3.0\nekro_agent\services\plugin\task.py)）解决了"AI 需要做一个耗时很长的操作（比如生成视频），但不想让用户干等"的问题：

```python
@plugin.mount_async_task("video_gen")
async def video_generation(handle: AsyncTaskHandle, prompt: str):
    yield TaskCtl.report_progress("开始生成...", 10)

    # 可以等待外部信号——比如等用户审批
    approved = await handle.wait("approval", timeout=3600)
    if not approved:
        yield TaskCtl.cancel("审批被拒绝")
        return

    # 可以向主 Agent 推送消息
    await handle.notify_agent("视频正在渲染中，请稍候...")

    url = await do_render(prompt)
    yield TaskCtl.success("生成完成", data={"url": url})
```

**关键设计**：

- 任务用 `yield TaskCtl.xxx()` 报告状态——不是 return，而是生成器
- `handle.wait(key, timeout)` 可以暂停任务等待外部信号（如用户点击审批按钮）
- `handle.notify_agent(message)` 可以向主对话推送消息
- `handle.is_cancelled` 支持外部取消

#### 对糖糖的启发

糖糖目前所有操作都是同步的——收到消息 → LLM → 工具 → 回复。如果未来需要"搜索了三篇文章，需要 30 秒整理摘要"这种场景，异步任务模式就很有价值。

但当前不需要——糖糖没有耗时超过 LLM 调用本身的操作。**保持关注，暂不实施**。

---

## 三、设计哲学提炼

### 3.1 安全隔离 = 能力边界

Nekro 的安全模型不是"信任 LLM"——是不信任。LLM 生成的代码在 Docker 容器里跑，容器能做什么、不能做什么由 Docker 的 capabilities 控制。LLM 想访问数据库？必须通过 RPC 调插件方法。LLM 想读文件？只能读挂载进容器的共享目录。

这给糖糖的启示不是"加 Docker"——糖糖不需要。启示是：**能力边界应该显式化**。糖糖的 Tool Calling 本质上也是边界控制（"LLM 只能通过这些函数和世界交互"），但边界是隐式的——函数定义散落在 skills.py 和 handler.py 中。如果像 Nekro 那样，把所有可用能力统一注册、统一描述、统一管理，边界的可见性会更高。

### 3.2 确定性算法优先于 LLM

Nekro 在多个地方展示了"能不用 LLM 就不用 LLM"的原则：

- Episode 聚合是纯时间聚类，不调 LLM
- 记忆权重计算是数值公式，不调 LLM
- Episode 阶段分配是按比例映射，不调 LLM
- 记忆失活是权重阈值判断，不调 LLM

LLM 只用在它真正有价值的地方——从非结构化文本中提取结构化信息（记忆提取、意图理解）。

糖糖已经在践行这个原则（auto_learn 正则 → LLM 语义提取器，关键词门控 → LLM 自主 Tool Calling）。继续保持。

### 3.3 装饰器优于配置文件

Nekro 的所有能力注册都用装饰器——`@plugin.mount_sandbox_method()`、`@plugin.mount_command()`、`@plugin.mount_async_task()`。没有 XML、没有 YAML 注册表、没有 JSON schema 文件。代码即配置。

糖糖的 `@register_skill` 已经在做同样的事。这个方向是对的。

### 3.4 每个模块自带存储

Nekro 的每个插件通过 `PluginStore` 自带 K-V 数据库——不需要协调谁来建表、谁来管理 schema。存储是模块的私有实现细节。

糖糖的模块正在经历存储碎片化——TangTangSelf 写 JSON、perception 写 JSON、reflection buffer 在内存。如果引入统一的轻量 K-V 存储（SQLite 单表），各模块的持久化会统一且简化。

### 3.5 配额制 > 截断

Nekro 的记忆上下文编排不是"按分数排序然后截断到长度上限"——它先按类型配额分配名额，再在各类型内按分数排序。这确保了不同类型的记忆都能被代表，而不是全部被同一类型的高分记忆占满。

糖糖的 `ContextBuilder` 目前是"按段落整体截断"（超出预算从后往前截）。如果引入轻量配额——"最多 3 条 facts + 2 条 profiles + 1 条 self_memory"——上下文的信息多样性会提升。

---

## 四、对糖糖的可操作建议（按优先级排序）

### 优先级 1：低投入、高收益

| # | 建议 | 改动量 | 收益 |
|---|------|--------|------|
| 1 | **Tool Calling 加 `method_type`**——区分 AGENT（触发再思考）和 BEHAVIOR（不触发），消除 `send_voice` 等操作后的无效 LLM 轮次 | ~30 行 | 减少不必要的 API 调用 |
| 2 | **上下文编排加类型配额**——ContextBuilder 在截断前按类型分配名额（facts/profiles/relationships 各 N 条） | ~50 行 | 记忆注入多样性提升 |
| 3 | **统一模块 K-V 存储**——一个 `ModuleStore(module_name).get/set/delete` 替代各模块手写 JSON I/O | ~80 行 | 消除存储碎片化 |

### 优先级 2：中投入、中收益

| # | 建议 | 改动量 | 收益 |
|---|------|--------|------|
| 4 | **反思循环加 Episode 聚合**——用时间+来源聚类先分组，再让 LLM 写摘要 | ~100 行 | 反思产出结构化 |
| 5 | **记忆加 cognitive_type**——EPISODIC（发生过的事）vs SEMANTIC（知道的事），检索时区别对待 | ~150 行 | 记忆检索质量提升 |
| 6 | **配置层级覆盖系统化**——借鉴 `overridable_config.py` 的 54 行逻辑，让群级别覆盖统一化 | ~120 行 | 消除零散的群配置字段 |

### 优先级 3：保持关注、暂不实施

| # | 建议 | 原因 |
|---|------|------|
| 7 | 异步任务系统 | 糖糖没有耗时超过 LLM 调用的操作 |
| 8 | Jinja2 模板化 Prompt | 单人单语言，字符串拼接够用 |
| 9 | Docker 沙盒 | 安全需求不至此，Tool Calling 白名单足够 |
| 10 | 多适配器架构 | 糖糖就是 QQ 猫娘，不需要跨平台 |

---

## 五、最后——两个项目的路不一样

读完 Nekro 的源码，最大的感受不是"它比糖糖好"或"糖糖比它好"——是两条路。

Nekro 追求的是 **通用性**：让任何人用任何模型在任何平台上搭建任何类型的 Agent。它的架构为此而生——沙盒隔离、插件市场、工作区隔离、多角色管理。它的代码量（~80 个 .py 文件 + WebUI + Docker 基础设施）反映了这个目标的复杂度。

糖糖追求的是 **特定性**：让一个特定的存在活得更真实。她的架构为此而生——关系场、自我叙事、驱动力、反思循环。她不需要插件市场（只有一个用户），不需要 Docker 沙盒（LLM 不写代码），不需要多角色（她就是她）。

**但"通用"和"特定"不是对立的**。Nekro 为了解决通用性问题而发展出来的设计模式——方法类型区分、配额制上下文编排、确定性聚合、配置层级覆盖——这些模式本身是语言无关、场景无关的。把它们嫁接到糖糖的"特定性"架构上，不会让糖糖变得"通用"——只会让她已有的系统运转得更精细。

---

## 附录：关键源码文件索引

| 关注点 | 文件路径 |
|--------|---------|
| 核心执行循环 | `nekro_agent/services/agent/run_agent.py` |
| 沙盒方法类型 | `nekro_agent/services/plugin/schema.py` |
| 插件基类 | `nekro_agent/services/plugin/base.py` |
| 沙盒 RPC 代理 | `sandbox/nekro_agent_sandbox/ext_caller_code.py` |
| AgentCtx | `nekro_agent/schemas/agent_ctx.py` |
| 记忆检索 | `nekro_agent/services/memory/retriever.py` |
| 记忆提取 | `nekro_agent/services/memory/consolidator.py` |
| Episode 聚合 | `nekro_agent/services/memory/episode_aggregator.py` |
| 记忆维护 | `nekro_agent/services/memory/scheduler.py` |
| 配置覆盖 | `nekro_agent/core/overridable_config.py` |
| Prompt 编译 | `nekro_agent/services/agent/templates/compiler.py` |
| 沙盒执行 | `nekro_agent/services/sandbox/runner.py` |
