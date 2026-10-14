# 换机依赖与 GPT-SoVITS 启动修复计划

> 给台式机 AI 的执行文档。笔记本上的修改可能被覆盖，请以本文件的“目标状态”和“验收标准”为准，在台式机重新取证、实现和验证。
>
> 日期：2026-09-05  
> 项目：QQ 机器人“小糖糖”  
> 故障范围：Windows 新机启动、GPT-SoVITS 语音服务、依赖安装契约

## 1. 先说结论

本次故障不是 `main.py` 的消息路由或 LLM 逻辑损坏，而是三个问题叠加：

1. **换机安装契约漏依赖**：旧版项目清单和安装器只安装了 `jieba`，没有覆盖 GPT-SoVITS 实际导入的 `jieba-fast`；同时没有声明 GPT-SoVITS 顶层导入所需的 `matplotlib`。
2. **第三方语音前端把加速包当成硬导入**：GPT-SoVITS 的中文文本模块直接 `import jieba_fast`。在 Windows/Python 3.10 上，PyPI 通常没有可用 wheel，pip 会退回源码编译，因而还需要 MSVC Build Tools。
3. **主程序健康检查超时过短**：本机无 CUDA，GPT-SoVITS 首次 CPU `/tts` 合成实测约 42.82 秒；原 15 秒 timeout 会把“能工作的慢服务”误判成“不健康”。

**最终目标**：完整桌面部署安装 MSVC + `jieba-fast`，主程序保留 `jieba` 回退；语音服务健康检查允许 CPU 首轮推理；缺少可选语音依赖时主聊天仍可启动并给出明确日志。

## 2. 已取到的证据（台式机需重新确认）

### 2.1 `jieba_fast` 缺失是可稳定复现的启动阻断

笔记本 `tangtang.log` 中多次出现以下同一链路：

```text
2026-09-05 00:01:47 ... GPT-SoVITS 进程意外退出 (code=1)
ModuleNotFoundError: No module named 'jieba_fast'
```

相同错误在 00:02:33、00:06:11、00:36:52、01:06:58 重复出现。说明不是偶发网络、端口或模型文件问题，而是稳定的 Python 导入失败。

对应源码：

- `gpt-sovits/GPT_SoVITS/text/chinese.py:19`：`import jieba_fast`
- `gpt-sovits/GPT_SoVITS/text/chinese.py:23`：`import jieba_fast.posseg as psg`
- `gpt-sovits/GPT_SoVITS/text/tone_sandhi.py:17`：再次使用 `jieba_fast`

### 2.2 `matplotlib` 是第二个启动期依赖

GPT-SoVITS 导入链会加载：

- `gpt-sovits/GPT_SoVITS/AR/modules/lr_schedulers.py:6`
- 顶层语句：`from matplotlib import pyplot as plt`

这意味着即使当前只做推理，只要该模块被导入，缺少 `matplotlib` 也会在 API 启动阶段失败。台式机 AI 应在干净环境中先安装 `jieba-fast`，再临时隔离/卸载 `matplotlib` 做一次最小探针，确认本机实际 traceback；不要只凭本文件猜测。

### 2.3 CPU 健康检查误判有运行时证据

笔记本在无 CUDA 环境下：

- GPT-SoVITS 服务成功监听 `127.0.0.1:9880`；
- 正式 `/tts` 请求返回 HTTP 200；
- 同一健康探测耗时约 **42.82 秒**。

因此 15 秒 timeout 不足以覆盖允许的 CPU 路径。修复后使用 60 秒预算，健康探测返回 `True`。

### 2.4 换机时为什么会缺

旧版 `requirements.txt.bak-20260904-K1` 只有 `jieba`，旧版 `tools/安装糖糖.py.bak-20260904-k3` 的“记忆”组也没有 `jieba-fast`。复制项目目录不会复制另一台机器的 Python `site-packages`，所以另一台机器“历史上装过”的包不会自动跟随项目迁移。

## 3. 依赖清单与合理性判断

### 3.1 必须安装（核心聊天）

这些缺失会在主程序导入或核心流程中直接失败：

| pip 包 | import/用途 | 缺失后果 |
|---|---|---|
| `websockets` | SnowLuma 反向 WebSocket | 无法接收 QQ 消息 |
| `httpx` | NapCat、LLM、语音 HTTP | 外部接口全部不可用 |
| `pyyaml` | `config.yaml` | 配置加载失败 |
| `python-dotenv` | `.env` 密钥 | 密钥加载失败 |
| `jinja2` | system prompt 模板 | `context_builder` 顶层导入失败 |
| `numpy` | 向量与数值处理 | 记忆/模型相关路径失败 |

### 3.2 记忆与 GPT-SoVITS 文本前端

| pip 包 | 性质 | 建议 |
|---|---|---|
| `jieba` | 纯 Python 中文分词；主项目记忆、知识库、插话等使用；也是语音回退 | **必须保留** |
| `jieba-fast` | `jieba` 的 C/C++ 加速实现；GPT-SoVITS 上游直接导入 | 完整桌面环境安装；运行时不得把它作为唯一生存条件 |
| `transformers==4.44.0` | BGE/情绪模型兼容版本 | 按项目锁定版本 |
| `modelscope`、CPU `torch` | 模型下载与推理 | 按“记忆”组安装 |

### 3.3 GPT-SoVITS 隐式运行依赖

| pip 包 | 证据 | 结论 |
|---|---|---|
| `matplotlib>=3.7,<4` | `lr_schedulers.py:6` 顶层导入 | GPT-SoVITS 子进程启动依赖，必须写入清单 |
| GPT-SoVITS `requirements.txt` 中的其余包 | `api_v2.py` 和 `GPT_SoVITS/` 导入链 | 使用本地 GPT-SoVITS 时按子项目清单安装；不要假定根目录清单能覆盖子项目 |

### 3.4 语音输入/辅助能力（缺失不应阻断主聊天）

`soundfile`、`edge-tts`、`sherpa-onnx`、`pilk`、`ffmpeg` 分别影响音频读写、Edge-TTS、语音识别、QQ SILK v3 解码和转码。缺失时应在体检中标成“对应能力不可用/降级”，不能让核心聊天启动失败。

### 3.5 系统级编译前置

`jieba-fast` 在 Windows/Python 3.10 上经常没有可用 wheel。此时 pip 会编译源码，需要：

- Visual Studio 2022 Build Tools；
- “使用 C++ 的桌面开发”工作负载；
- MSVC x64/x86 编译工具（含 Windows SDK）。

这不是 Python 运行时依赖，而是**安装期工具链依赖**。安装器应检查 `cl.exe`，并给出可执行的安装提示；不要让 `main.py` 在运行时偷偷安装 MSVC 或调用 pip。

## 4. 给台式机 AI 的修改任务（按顺序执行）

### Task 0：建立台式机基线（只读）

1. 阅读 `CLAUDE.md`、本计划和 `docs/用户手册/文件同步指南.md`。
2. 记录解释器：`where python`、`python --version`、`python -m pip --version`。
3. 运行 `python tools/体检.py`，保存完整输出。
4. 用 Python 探针逐个检查：`jieba`、`jieba_fast`、`matplotlib`、`torch`、`transformers`、`soundfile`、`sherpa_onnx`、`pilk`。
5. 从 `tangtang.log` 过滤 `ModuleNotFoundError`、`GPT-SoVITS`、`timed out`，不要直接把整份大日志交给模型。

**验收**：台式机必须有一份带时间戳的基线输出，后续修改前不要覆盖。

### Task 1：修正依赖声明和安装器（部署层）

检查/修改以下文件：

- `requirements.txt`
- `gpt-sovits/requirements.txt`
- `tools/安装糖糖.py`
- `tools/体检.py`
- `docs/用户手册/使用说明.md`
- `docs/用户手册/文件同步指南.md`

目标状态：

1. 根目录清单明确写出 `jieba`、`jieba-fast`、`matplotlib`；注释说明用途、Windows 的 MSVC 前置和回退策略。
2. GPT‑SoVITS 子项目清单明确写出 `jieba-fast`、`jieba`、`matplotlib`，不能只依赖根目录清单。
3. 安装器“记忆/语音”分组与根目录清单一致，并在安装前检测 Python 版本、磁盘空间和 MSVC `cl.exe`。
4. 体检程序把 `jieba_fast`、`matplotlib`、`pyflakes` 报告为可定位的依赖状态；`jieba` 缺失才属于影响主项目功能的错误。
5. 文档明确：完整桌面安装应先装 MSVC，再安装 `jieba-fast`；没有 MSVC 时启动器仍可用纯 Python `jieba`，但速度较慢。

**注意**：不要为了让测试通过而添加没有真实使用点的包。每个依赖都要能指向 import、功能或安装工具。

### Task 2：修复 GPT‑SoVITS 启动兼容层（运行层）

建议修改 `gpt-sovits/start_api_patched.py`，并保持第三方源码不改：

1. 在导入 `api_v2.py` 之前尝试导入 `jieba_fast`。
2. 若 `jieba-fast` 不可导入，导入纯 Python `jieba` 及 `jieba.posseg`，映射到 `sys.modules["jieba_fast"]` 和 `sys.modules["jieba_fast.posseg"]`。
3. 两者都缺失时抛出明确的 `RuntimeError`，说明安装 `jieba>=0.42`，禁止静默吞错。
4. 若 `jieba-fast 0.53` 与当前 setuptools 的 `pkg_resources` 接口不兼容，在包装器中做局部兼容补丁，不要为了它全局降级 setuptools。
5. 启动日志要明确显示是原生 fast 路径还是纯 Python fallback，方便台式机取证。

**最小性原则**：只做导入兼容和诊断，不在启动器中安装依赖，不修改 GPT‑SoVITS 的业务推理代码。

### Task 3：修复服务健康检查（主程序服务管理层）

建议修改 `agent/service_manager.py`：

1. 找到 `_check_gpt_sovits_healthy` 及其调用方，先做 GitNexus `impact`；若工具不可用，记录不可用原因并用静态调用点核对。
2. 健康检查必须调用正式 `/tts`，不能只请求根路径（根路径可能返回 404，但不代表服务坏了）。
3. 使用与正式语音请求一致的参考音频、prompt 参数和文本语言。
4. 将 timeout 从固定 15 秒提高到能覆盖 CPU 首轮推理的预算，建议 60 秒，并提取为常量，便于测试和调整。
5. 超时/失败日志应区分“进程退出”“端口无响应”“TTS 推理超时”，不要全部写成同一类错误。

### Task 4：确认 `main.py` 的职责边界

`main.py` 只负责启动主应用和调用服务管理器，不负责 pip/MSVC 安装。检查：

- `main.py` 启动核心聊天时不因 `jieba-fast` 缺失直接退出；
- 选择 GPT‑SoVITS 时由 `handler.start_services()`/`ServiceManager` 管理子进程；
- 语音服务不可用时记录明确 warning，主 WebSocket/LLM 仍可运行；
- 不要在 `main.py` 中加入“收到 ImportError 就自动 pip install”的逻辑。

如果台式机版本的 `main.py` 仍把语音导入放在核心模块顶层，应把语音依赖改为按 provider 延迟导入，并补回归测试；这是必要的主程序修改，不能用捕获所有异常掩盖问题。

### Task 5：补回归测试（防止换机再次发生）

至少覆盖：

1. `jieba-fast` 缺失时启动器能回退到 `jieba`；两者都缺失时错误信息清楚。
2. `jieba-fast` 已安装时不走 fallback。
3. GPT‑SoVITS 健康探测使用统一 timeout 常量，并判断 HTTP 200 且响应体非空。
4. 依赖契约测试：根目录清单、GPT‑SoVITS 子清单、安装器、体检项目互相不漂移。
5. 主程序在语音依赖缺失时仍能启动文字链路（使用 mock，不启动真实模型）。

## 5. 安装顺序（台式机实际操作）

1. 确认使用项目 Python 3.10，不要混用 Microsoft Store Python 或其他虚拟环境。
2. 安装 Visual Studio 2022 Build Tools 的 C++ 工作负载和 Windows SDK。
3. 关闭并重新打开终端，使 PATH/开发者环境生效。
4. 验证 `cl.exe`：

   ```powershell
   Get-ChildItem 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC' -Recurse -Filter cl.exe
   ```

5. 在项目 Python 中安装依赖：

   ```powershell
   python -m pip install -r requirements.txt
   python -m pip install -r gpt-sovits/requirements.txt
   python -m pip install jieba-fast==0.53
   ```

   若两个 requirements 都安装同一包，允许 pip 跳过已满足项；最终以 `pip check` 为准。

6. 验证：

   ```powershell
   python -c "import jieba, jieba_fast, matplotlib; print(jieba.__file__); print(jieba_fast.__file__); print(matplotlib.__version__)"
   python -m pip check
   ```

## 6. 验收标准（全部满足才算完成）

### 6.1 依赖与工具链

- [ ] 项目 Python 版本为 3.10.x。
- [ ] `cl.exe` 文件真实存在，且能输出 MSVC 版本。
- [ ] `import jieba_fast` 成功，打印出 `site-packages/jieba_fast` 路径。
- [ ] `import matplotlib` 成功。
- [ ] `python -m pip check` 输出 `No broken requirements found`。

### 6.2 语音服务

- [ ] 直接启动 `gpt-sovits/start_api_patched.py` 成功监听 `127.0.0.1:9880`。
- [ ] 启动日志显示原生 `jieba-fast` 路径；若故意隔离该包，显示 fallback 而不是崩溃。
- [ ] 正式 `/tts` 请求返回 HTTP 200 且音频响应体大于 1000 字节。
- [ ] 无 CUDA 时允许 CPU；健康检查不在 15 秒时误报失败。

### 6.3 主程序

- [ ] NapCat/SnowLuma 连接成功，主程序保持运行。
- [ ] GPT‑SoVITS 启动失败时有明确 warning，但不会让文字聊天核心静默崩溃。
- [ ] 诊断结束后停止测试进程，端口 3001、9880 无残留监听。

### 6.4 自动化门禁

```powershell
python -m pytest tests/ -q
python -m pyflakes agent/*.py napcat/*.py main.py 糖糖控制台_qt.py | Select-String -Pattern undefined
```

要求：pytest 全绿；pyflakes 未定义名称为 0。功能改动后必须再走一次“启动主程序 → 启动/探测 GPT‑SoVITS → 正式 `/tts` → 停止并检查端口”的端到端流程。

## 7. 不要这样修

- 不要只在 `main.py` 里捕获 `ModuleNotFoundError` 然后继续运行；这会把语音子进程错误变成无上下文的假成功。
- 不要把 `jieba-fast` 的 C 扩展源码复制进项目；它属于外部依赖，应由 pip + MSVC 安装。
- 不要为了 `jieba-fast` 全局降级 setuptools；当前 torch/其他依赖可能要求较新 setuptools，应使用启动器局部兼容补丁。
- 不要把 15 秒 timeout 继续当作所有机器的固定标准；GPU、CPU、首次模型加载的耗时不同。
- 不要为了让“全量安装”表面成功而从清单删除真实 import 依赖；应让安装器明确区分核心、语音和开发组。
- 不要修改数据库、`config.yaml` 或人格卡来解决本次 Python 依赖故障。

## 8. 预期修改文件与回滚边界

优先允许修改：

- `requirements.txt`
- `gpt-sovits/requirements.txt`
- `gpt-sovits/start_api_patched.py`
- `agent/service_manager.py`
- `tools/安装糖糖.py`
- `tools/体检.py`
- `docs/用户手册/使用说明.md`
- `docs/用户手册/文件同步指南.md`
- 对应 `tests/test_gpt_launcher.py`、`tests/test_voice_speaker.py`、`tests/test_install_deps_contract.py`

默认不要修改：`memory.db`、`.tangtang_self.json`、`config.yaml`、`role_card*.md`、模型文件和用户数据。

每次改动前先读取当前文件，保留台式机原始日志和配置备份；每完成一个任务就跑对应测试，不要一次性盲改多个模块。

## 9. 当前笔记本的参考结果

以下结果用于和台式机对照，不代表可以跳过台式机验证：

- Python：3.10.11（项目使用的解释器）
- MSVC：Build Tools 2022，`cl` 19.44.35228
- `jieba-fast`：0.53，源码编译安装成功
- `matplotlib`：3.10.9
- `pip check`：通过
- GPT‑SoVITS 正式 `/tts`：HTTP 200
- CPU 健康探测：`True`，约 42.82 秒
- 全量测试：`1894 passed, 2 warnings`
- pyflakes undefined：0

## 10. 最终判断

从 GPT‑SoVITS 上游项目角度，直接导入 `jieba_fast` 和 `matplotlib` 是其内部实现选择，缺模块就启动失败在 Python 层面“合理”。从小糖糖产品角度，语音是可降级能力，不能因为一个性能加速包或未使用的绘图模块让主机器人整体不可用，因此项目必须：

1. 把真实的传递依赖写进子项目清单；
2. 把 Windows 编译前置写进安装器和换机文档；
3. 在启动边界提供纯 Python 分词回退；
4. 用与硬件实际耗时匹配的健康检查；
5. 用依赖契约测试防止下一次换机再次漂移。

这五项完成后，台式机 AI 才算完成“主程序修改”，而不是只在当前机器临时 `pip install` 后看起来能运行。
