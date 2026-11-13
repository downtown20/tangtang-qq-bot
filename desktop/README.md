# 小糖糖桌面端（React + Electron）

DSH（DeepSeek Harness）风格的桌面控制台，替代旧的 PySide6 控制台。旧的 Qt 控制台已移进 `运维/`（功能保留，根目录不再有它），两者可以并存。

```
QQ消息 → SnowLuma → bot(agent/handler.py) → LLM → 回复
                        ↑
          tools/desktop_bridge.py（本地 HTTP API）
                        ↑
        desktop/（Electron 壳 + React 界面）
```

## 为什么是这个架构

渲染进程**不直接读写文件**。所有配置/数据库/进程操作都经过 `tools/desktop_bridge.py`，它复用 `agent/secret_refs.py` 等既有模块：

- 密钥归一/迁移/体检只有一份实现（教训：2026-09-23 第三次 401 事故就是两处判据漂移）
- config.yaml 保存要「备份 + 合并未接入字段 + normalize」，这套语义已在旧控制台踩过坑
- bot 进程启停、日志流、SQLite 统计本来就在 Python 侧

安全边界：桥只监听 `127.0.0.1`，每次启动生成随机 token，Electron 主进程从 stdout 读取后经 preload 注入渲染进程。

## 运行

### 方式一：绿色版（双击即用，推荐日常使用）

已经打包好的在 `release/小糖糖桌面端/`：

- **`小糖糖桌面端.exe`** ← 双击这个（在包内原位运行，别把它单独拷出去）

> **2026-11-14 修正（实测推翻旧结论）**：以前这里写"exe 裸启动会静默秒退、必须带
> `--no-sandbox`"，并让用户双击 `.lnk`。两条都不对：
>
> - 用 `explorer` 拉起（= 真实双击）实测**裸 exe 存活并拉起了 Python 桥**：
>   `ROOT=<包根> bridgeExists=true` → `python tools\desktop_bridge.py --port 0`。
>   那个 `STATUS_BREAKPOINT` 秒退只在**从别的进程树里启动**时出现（DSH/脚本/某些
>   启动器），用户桌面双击不受影响。
> - `.lnk` 里存的是**打包机的绝对路径**（实测内容指向打包机的盘符绝对路径），用户解压到
>   任何别的目录，双击就是死链——没反应、没报错。发布包里已不再提供 `.lnk`。
>
> 兜底：双击之后没反应，就用项目根的 `启动桌面端.bat`（带 `--no-sandbox`、把工作
> 目录钉在包根）。

重新打包（改了代码之后）：

```bash
python tools/打包桌面端.py      # 在项目根执行；会先 vite build，再组装绿色版
```

### 方式二：开发模式

```bash
cd desktop
pnpm install          # 首次；Electron 二进制约 110MB
pnpm dev              # Vite + Electron，自动拉起 Python 桥
```

也可以双击项目根的 `启动桌面端.bat`。

> ⚠ Windows 上如果环境里存在 `ELECTRON_RUN_AS_NODE=1`，Electron 会以纯 Node 模式启动、主进程直接崩。`vite.config.ts` 已显式清掉该变量。

## 目录

```
desktop/
  electron/
    main.ts            # 窗口（无边框 + 自绘标题栏）、spawn Python 桥、IPC
    preload.ts         # 只暴露窗口控制 + 桥地址
  src/
    lib/api.ts         # 与桥的唯一通信层（Bearer token）
    lib/configStore.ts # 配置 path 读写（不可变）
    lib/settingsGroups.ts    # 设置分类（Settings 与 ⌘K 共用一份，防漂移）
    lib/settingsSchema.json  # 从旧控制台提取的 16 组 / 70 个字段
    lib/uiPrefs.ts     # 界面偏好（群排序/置顶，只落 localStorage）
    components/        # TitleBar / Sidebar / Composer / SettingsFields
                       # ModelSwitcher（两级菜单）/ CommandPalette（Ctrl+K）
                       # ContextMenu（右键菜单）
    pages/             # Dashboard / Gallery / Logs / Settings（模态）
    pages/settings/ModelSettings.tsx  # 多模型档位管理
    styles/tokens.css  # DSH 真实设计 token（见下）
```

## 交互

| 操作 | 位置 |
|---|---|
| `Ctrl+K` / `⌘K` | 命令面板：搜页面 / 设置分类 / 动作，↑↓ 选择、Enter 执行 |
| 右键群聊条目 | 复制群号/群名、置顶、恢复默认顺序 |
| 拖拽群聊条目 | 调整顺序（只存 localStorage，不改 config.yaml） |
| 右键图库卡片 | 查看大图、复制文件名/标签、加标签、在文件夹中显示、移入回收站 |
| 图库卡片左上勾选 | 批量选择（Shift 连选），底部浮条批量打标签 / 移入回收站 |
| 点击图库卡片 | 大图预览（← → 切图，Esc 关闭） |
| 输入框旁模型 chip | 两级菜单：模型列表 + 推理档位（点一下即写 config，不用开设置） |

回收站落在 `stickers/_trash/<时间戳>/` 与 `stickers_cg/_trash/<时间戳>/`，**不是真删**，可以自己搬回去。

## 设计依据

`docs/开发规划/DSH_UI_调研_20261023.md`：从 DSH 的 `app.asar` **只读**提取的真实规格。

关键值：
- 底色 `#151517` / 卡片 `#2c2c2e` / 侧栏 `#1b1b1c` / 主文字 `#f9fafb` / 强调 `#7aaaff`
- 圆角六档 4/8/12/16/20/28；标题栏 40px；侧栏 280（264–420）
- 设置是**居中模态**（800 宽、28 圆角、188 导航），不是整页
- 主按钮是**反色填充**（`brand-primary`），不是蓝色

## 后端桥 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 免鉴权健康检查 |
| GET | `/api/config` | 配置（密钥掩码） |
| POST | `/api/config` | 保存（备份 + normalize + 掩码还原） |
| GET | `/api/models` | 模型档位清单 |
| POST | `/api/models/fetch` | 填 base_url + key → 拉取 `/v1/models` |
| POST | `/api/models/activate` | 切换当前档位（可同时改推理档位），只改 llm 下几个键 |
| GET | `/api/stats` | 人数/记忆/对话/表情包 |
| GET | `/api/status` | bot 进程与当前模型 |
| GET | `/api/groups` | 群列表（名称/人数/最近消息/场景） |
| GET | `/api/gallery` | 图库列表（`source`=sticker\|cg，另有 q/tag/state/sort/page） |
| GET | `/api/gallery/thumb` \| `/api/gallery/file` | 图片字节（缩略图按需生成并缓存） |
| POST | `/api/gallery/tags` | 批量加/减情绪标签（备份 + 原子写 metadata.json） |
| POST | `/api/gallery/trash` | 批量移入 `_trash/<时间戳>/`（不是真删） |
| POST | `/api/gallery/reveal` | 在系统文件管理器里定位该图 |
| POST | `/api/bot/start` \| `/api/bot/stop` | 启停 bot |
| GET | `/api/logs` | SSE 日志流 |
| GET | `/api/console/status` | 会客厅是否可用（**恒 200**，前端看 `available`） |
| GET | `/api/console/sessions` | 会客厅会话列表 |
| POST | `/api/console/sessions` | 新建会话 |
| DELETE | `/api/console/sessions/<id>` | 删除会话 |
| GET | `/api/console/messages` | 某会话的消息（`session_id` + `limit`） |
| GET | `/api/console/snapshot` | 糖糖此刻的内部状态（情绪/驱动力/自我/关系场/记忆） |
| POST | `/api/console/say` | 以主人身份对糖糖说一句（走她的真实本体） |
| POST | `/api/console/turn` | 自主回合：不给文本，让她按当前状态主动说一句 |

> 图库缩略图落在系统临时目录 `%TEMP%\tangtang-thumbs\`（原图最大 7.9MB、956 张共 233MB，
> 直接塞 `<img>` 会把渲染进程拖死；320px JPEG 首次约 11ms/张，之后命中缓存）。

### 会客厅（2026-10-24）

桌面端原来的底部「模型试话」是**裸调 `/chat/completions`**——没有人格、没有记忆、
没有驱动力、消息只活在 React state 里。会客厅取代了它：

| | 旧的模型试话 | 会客厅 |
|---|---|---|
| 后端 | `httpx` 直连模型网关 | 糖糖进程内的 `MessageHandler`（QT 私聊同一条路径） |
| 人格 | 无 | `role_card.md` + 场景引擎 |
| 记忆 | 无 | 真实检索（22456 条）并**写回** |
| 驱动力 | 无 | 6 维真实消耗/释放 |
| 关系场 | 无 | 285 人，closeness 真的会动 |
| 存档 | 无 | SQLite `状态/会客厅.db` |
| 角色 | 用户能输入 | **只读旁观**（输入发生在 QQ 或通过 CLI） |

关键设计：**会客厅的实体在 bot 进程内**（`agent/console_api.py`）。独立进程会分裂
`状态/.tangtang_self.json`、驱动力与短期记忆窗口——那会变成「两个糖糖」。代价是
糖糖没启动时桌面端只能显示引导态，这是诚实的代价。

CLI（Lead / 脚本向糖糖汇报或提问）：

```bash
python tools/会客厅.py say "会客厅接好了，跟我说说你这边的实际情况"  # 真身对话
python tools/会客厅.py turn                                        # 让她主动说一句
python tools/会客厅.py snapshot                                    # 打印她的内部状态
python tools/会客厅.py sessions / messages / new / delete
```

调试：打包版默认关闭 CDP；需要看渲染进程真实请求时
`$env:TANGTANG_DEBUG_PORT='9222'` 再启动，然后连 `ws://127.0.0.1:9222`。

## 已实现 / 待办

- [x] DSH 同款布局：**侧栏通高（深色）+ 主区（浅色）**，标题栏 40px、侧栏 260px
- [x] **浅色 / 深色主题切换**（标题栏右上角月亮/太阳按钮，选择记在 localStorage）
- [x] DSH 式组件：卡片、列表行（彩色图标块 + 标题/描述 + 右侧控件）、反色主按钮
- [x] 设置改**居中模态**（800 宽 / 28 圆角 / 188 分类导航）
- [x] 侧栏**真实群列表**（group_info 群名 + chat_log 最近时间，DSH 式相对时间）
- [x] **模型试话**（底部 composer，直接问当前模型，不经过 QQ —— 换 Key/端点后立刻验证）
- [x] 后端桥（配置/模型/群/统计/状态/日志/启停/试话/图库）
- [x] 多模型档位：添加、填 Key 拉取、参数编辑、切换（**agent 侧真正生效**）
- [x] schema 驱动表单（16 组 70 字段，从旧控制台自动提取）
- [x] **日志页实时流**（SSE + 关键词过滤 + 暂停/清空）
- [x] **图库页**：卡片网格（贴图 956 + 丛雨 96 + 米雪儿 235 = 1287，CG 222）、
      **贴图按三套分组**（默认 / 丛雨 / 米雪儿，来自 `stickers/`、`stickers_murasame/`、
      `stickers_michele/`）、搜索、标签 chips、排序、分页、多选批量打标签/回收站
      （跨库多选会按库分组调用写接口）、大图预览（←→ 切图）、右键菜单
- [x] **模型切换器**在输入框旁原地弹两级菜单（模型 / 推理档位），点一下即写 config
- [x] **设置字段按 DSH 通用设置范式**重排：裸行 + 0.5px 分割线（padding 16 0、gap 24）、
      标题 14/20 + 说明 12/18、右侧控件列、未保存字段打蓝点
- [x] **⌘K 命令面板**、**右键菜单**、**群列表拖拽排序 / 置顶**
- [x] **会客厅**（2026-10-24）：接糖糖**真身**的三栏会话窗——左栏会话存档、中栏只读对话流
      （tangtang/user/lead/system 四类气泡 + 每轮 meta：模型·耗时·tokens）、右栏状态面板
      （情绪三条 + 6 维驱动力带「急躁」阈值标记 + 主导驱动力 + 自我叙事 + 价值倾向 +
      关系场 Top5 + 最近记忆 + 模型）。顶部「让糖糖说点什么」走自主回合
- [x] bot 运行状态**不再只看 bridge 自己 spawn 的进程**——用户从控制台/命令行启动的糖糖
      也能被正确识别为「运行中」（会客厅 health / 3001 端口多路判定）
- [ ] 图库标签的「多标签 AND/OR 组合筛选」（现在只支持单选一个标签）
- [ ] Electron 打包（已用离线脚本产出绿色版；electron-builder 因 npm 超时弃用）

## 相关

- 后端桥：`../tools/desktop_bridge.py`
- agent 多模型解析：`../agent/handler.py` 的 `_get_llm_config`（测试 `../tests/test_llm_model_profiles.py`）
- DSH 调研：`../docs/开发规划/DSH_UI_调研_20261023.md`
