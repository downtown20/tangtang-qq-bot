# 任务简报 L1：糖糖控制台 UI 优化 + 语音/曲库设置重构

- 日期：2026-09-05 · 主负责人派发 · 执行：Codex（副负责人）
- 模型档位：`gpt-5.6-terra` + `model_reasoning_effort="xhigh"`（一般任务档）
- 背景：GitHub 发布准备——控制台是发布版的门面，需合理美化；语音/曲库设置按新需求收敛（GPT-SoVITS 单引擎、去 CosyVoice、去 DiffSinger、歌唱工作室一键全流程加依赖检测与下载）

## 一、目标（4 项，全部在 糖糖控制台_qt.py + config.yaml 内）

1. **控制台 UI 整体美化**：合理设计配色/间距/控件样式（QSS 为主）。GitHub 参考（只借鉴设计语言，**引入第三方库必须报主负责人裁决**——有 exe 打包/体积/兼容约束）：
   - PySide6-Fluent-Widgets（qfluentwidgets）· PyDracula 主题 · 各类 PySide6 QSS 方案
2. **语音设置收敛 GPT-SoVITS**：引擎选择去掉 cosyvoice；删除 CosyVoice 音色管理面板（含全部回调）；**新增/展示 GPT-SoVITS 相关设置**——至少包含「各个情绪的参考音频」的可见管理（见 §三 数据结构）
3. **曲库删除 DiffSinger**：「🎤 DiffSinger」tab（ds_tab）及其全部回调/QThread 删除；「🎙 歌唱工作室」保留
4. **歌唱工作室「一键全流程」健壮化**：点击先检测流水线依赖文件 → 全在=直接执行；缺失=明确提示缺哪些 + 出现「下载缺失文件」按钮 → 点击下载到项目对应位置（下载 URL 集中在常量表，见 §五）

## 二、现状取证（已核实的锚点，改前先读上下文）

| 锚点 | 事实 |
|---|---|
| 糖糖控制台_qt.py:1405-1418 | 语音设置 section：`voice.provider` choice=[gpt-sovits, cosyvoice]、cosy_speaker、volc_appid、emotional_tts。1405 行标题含「声音克隆」字样需改 |
| :1997-~2150 | `_build_cosy_voice_panel()`——CosyVoice 音色管理整块（注册/设当前音色/参考音频选择）。**连带删除全部 `_cosy_*` 回调**（grep 定位：_cosy_refresh_speakers/_cosy_set_speaker/_cosy_browse_ref_audio 等，删干净防死引用） |
| :2282 `_build_singing_panel` | 唱歌音频管理面板容器。内部 ds_tab（🎤 DiffSinger 分段合成，2294-2396）+ ws_tab（🎙 歌唱工作室）；2397-2398 addTab |
| :2505-2567 | DiffSinger 生成回调（_sing_generate_one/all、_ds_status 等）——查清哪些只服务 ds_tab，一并删 |
| :3015-3024 | 「一键全流程」依赖常量：rvc_dir=Retrieval-based-…WebUI、infer_script=…/tools/infer_cli.py、index_path=…/assets/weights/hutao.index、sep_script=tools/separate_vocals.py、venv_python=venv_demucs/Scripts/python.exe。3021 起已有 partial 缺失检查（只查 4 路径）→ **扩展为完整清单** |
| :3082 FullPipelineThread | 一键全流程主线程（Step1 demucs 分离 → Step2 RVC hutao → 后续去噪/裁剪步骤读完整方法） |
| :2705 BatchSepThread · :2799 BatchRvcThread · :2884 DenoiseThread · :2961 TrimThread | 歌唱工作室各子流程线程（BatchRvc 用 `--model_name hutao`） |
| 糖糖控制台_qt.py:501 | `_add_section` 的 env 映射表（如 (("voice","volc_token"),"VOLC_TOKEN")）——删 config 字段时同步查此表与 .env |
| agent/voice.py:270-279 | MODEL_PROFILES：v4（底模 GPT_SoVITS/pretrained_models）+ michele（models/michele/MiXueErV3-*） |
| agent/voice.py:361 | GPT_SPEAKER_DIR = 项目根/gpt-sovits/speakers（参考音频目录） |
| agent/voice.py:~446 `_SPEAKER_PROMPT` | 情绪表：normal/gentle/happy/excited/coquettish/tsundere/sad/surprised/angry/disgust/murasame/xueli… → (lang, prompt 文本)。**test_voice_quality_gate 有闸门钉住此表——不得改 voice.py** |
| config.yaml `voice:` 段 | 当前字段：provider/cosy_speaker/volc_appid/volc_token/emotional_tts…（读 config.yaml 自查全量） |

## 三、GPT-SoVITS 设置面板设计要点（第 2 项）

参考音频 = `gpt-sovits/speakers/` 下每个情绪的 ref.wav（与 voice.py `_SPEAKER_PROMPT` 情绪键一一对应），
prompt 文本在 voice.py 内。控制台展示层（只读展示 + 状态提示即可，不必做成写 voice.py 的编辑器）：
- 列出情绪键 → 参考音频文件（是否存在/路径）→ 可「打开所在文件夹」/「选择替换」（替换=复制新文件进 speakers/ 同名，**不写 voice.py**）
- 显示当前声线/模型档案（murasame 丛雨参考音、michele 模型、v4 底模——读 agent/voice.py MODEL_PROFILES 与 GPT_SPEAKER_DIR 展示状态）
- provider 写死/仅显示 GPT-SoVITS；cosyvoice 相关 choice 项与字段（cosy_speaker 等）从 UI 移除；volc/edge 残留字段甄别：确不可选则一并隐藏（config 字段是否清理：查引用后决定，见危险区 3）

## 四、验收标准

1. `python -m pyflakes 糖糖控制台_qt.py | grep -i undefined` **清零**（CC 复跑）+ compileall 干净
2. 启动无异常：设置页语音区无 cosyvoice 字样与控件；曲库无「DiffSinger」tab
3. 歌唱工作室一键全流程：模拟缺失（临时移走 hutao.index）→ 点击应提示缺失清单而非报错 → 出现下载按钮（下载动作在沙箱外由主人/CC 验证落位；Codex 至少验证 UI 分支逻辑静态正确）
4. config.yaml 与 UI 字段一致（改动字段全库 grep 无孤儿引用）
5. pytest 全量绿（CC 复跑；**test_voice_quality_gate 等不得因本次改动变红**——不改 voice.py）
6. UI 目验：主人本地启动控制台查看美化效果（沙箱无法跑 GUI，Codex 交付代码 + 自查，主人验收视觉）

## 五、下载功能（第 4 项）实现约定

- 新建集中常量表（如 `_PIPELINE_DOWNLOADS = {路径键: 下载URL}`）放 糖糖控制台_qt.py 顶部或 tools 复用模块；
  下载线程带进度显示，落盘到检测清单对应路径
- **URL 来源**：先查项目内既有下载器先例（tools/安装糖糖.py 首次下载 asr 模型、download_covers.py——有现成 httpx 下载模式可复用）；hutao 私训模型无公开 URL → URL 表留**占位符 + 代码注释「待主人提供」**，检测逻辑先行完成
- 下载失败/取消要有提示；下载中按钮禁用防重复点击

## 六、危险区（违反即返工）

1. **只动 糖糖控制台_qt.py 与 config.yaml**（及相关预设同步）。**禁入**：agent/ 业务代码（voice.py/cosy_voice.py/handler.py 等一律不碰）、tools/separate_vocals.py、RVC infer_cli、tests/ 现有测试
2. 方法边界纪律：def 只插/删在方法边界处（教训 #28）；删除回调方法时 grep 全量引用防死引用（教训 #26）
3. 删 config 字段前查引用（agent 读取处、.env 映射表 501、test_secret_config 等测试）——UI 隐藏 ≠ config 可删，两者分开判断
4. 沙箱无 python/pytest/pyflakes：用 AST 探针 + compileall 自证；**不得宣称测试通过**（常设条款）；正式门禁 CC 复跑
5. 用户可见行为变化须列明（协作协议 H 批追加条款）：本次变化=设置项收敛+UI 美化+一键流程提示/下载——符合主人指令，执行报告仍须逐条说明
6. 大段删除用 Edit 工具逐字匹配（教训 #25）；改前对 糖糖控制台_qt.py 做 `.bak-20260905-L1` 备份
7. 边做边写报告草稿（协作协议追加条款：大任务报告随做随写 docs/协作/reports/执行报告_L1.md）

## 七、执行方式

- 简报 → Codex 执行 → 执行报告_L1 → 主负责人独立验证（pyflakes/compileall/pytest 复跑 + 启动冒烟）→ 裁决
- 主人本地目验 UI 效果后裁决通过
