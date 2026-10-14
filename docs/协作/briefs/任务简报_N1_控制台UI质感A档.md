# 任务简报 N1：控制台 UI 质感升级（A 档四批）

> 执行人：Codex（副负责人）｜ 主负责人：Claude Code（本会话）
> 发起：2026-09-05 ｜ 方案已获主人批准（主人原话：执行 A 档，sol 极高强度）

## 0. 工作区纪律（必须遵守）

- **只改开发目录 `<项目根>\` 下的文件**。`D:\小糖糖-发布\` 是构建产物快照，**禁止触碰**（主负责人负责重建与推送）。
- 允许改的文件：`糖糖控制台_qt.py`、新建 `agent/ui_anim.py`、新建 `tests/test_ui_anim.py` 与 `tests/test_ui_theme_tokens.py`。**禁止改** handler.py / main.py / memory.py / store.py 等核心链路文件。
- **Read before Edit**：任何修改前先 Read 目标段落（该文件 5000+ 行，禁止凭记忆/搜索猜测改）。
- 大段插入/替换注意 Python 缩进即逻辑：方法定义只插在方法边界；批改后自查 return 后死代码。
- 每批完成后 `python -m compileall 糖糖控制台_qt.py agent/ui_anim.py` 自证语法。
- **沙箱限制**：沙箱内 python 可能不在 PATH、无 pytest/pyflakes——正式测试门禁由主负责人环境复跑。执行报告中**不得宣称 pytest 通过**，用 AST 探针/compileall/自写小脚本自证，并如实注明。
- 不引入任何第三方依赖（方案决议：纯 QSS + Qt Animation Framework 自研）。
- 不要动 `_navigate` 之外的交互逻辑；不要重构布局框架；不要重命名既有对象名/样式名（QSS 选择器依赖）。

## 1. 任务背景

控制台（糖糖控制台_qt.py，PySide6，~5000 行）布局框架合理，但零动画、无层次感、部分内联样式绕过主题。完整方案见 `docs/开发规划/控制台UI质感升级方案_20260905.md`（先通读）。执行其中 **A 档四批**。

## 2. 铁律认知（方案核心，违背即返工）

1. **Qt QSS 没有时间概念**：`transition`/`animation`/`box-shadow`/渐变全无效（静默忽略）——平滑过渡一律用 QPropertyAnimation/QVariantAnimation + QEasingCurve。
2. **性能三铁律**：`QGraphicsOpacityEffect`/`QGraphicsDropShadowEffect` 是离屏 CPU 整块重绘——动画 finished 后必须 `setGraphicsEffect(None)` 清理；同屏 ≤2-3 个；不挂列表项。动画对象必须持引用防 GC。动画期间避免逐帧重排版（几何动画固定内容高度/禁 relayout）。
3. 缓动选型：位移/滑入 OutCubic/OutQuad；淡入 InOutQuad；**禁止 Bounce/Elastic**。时长：hover 100-200ms、页面切换 200-350ms、首屏入场 300-500ms。

## 3. 分批任务

### 批 1：设计令牌收敛 + Fluent 视觉打磨（纯静态，无动画）
- 在 糖糖控制台_qt.py 配色常量区（约 L113-130）旁补充设计令牌常量：控件圆角 4px、卡片/浮层圆角 8px（现有卡片 12px 属既有风格可保留但需一致化决策：**保守策略=新令牌只约束新增/统一值，不强行改既有视觉**，圆角微调以不破坏现有观感为限，改动克制）、控件标准高 32px（现有按钮/输入 min-height 不一，只统一明显过矮处）、间距 4px 基数。令牌命名如 `RADIUS_* / H_* / SPACING_*`。
- `_build_stylesheet`（L135+）引用令牌：暗色分层——卡片/侧边栏补 1px 高光描边（`rgba(255,255,255,0.06-0.08)` 级别，叠加在现有 border 上可用 border 色微调或新增顶层 1px border-top 高光），**不引入阴影**（暗色下阴影不可见且性能差）。
- 文字层级：label 分级已有（#title/#heading/#body/#muted）——检查 muted 用色是否达 60% 对比，必要时微调；**不做大改**。
- 清理高可见处内联色值（如日志页 filter 计数 `#555`/`#f0a060`、图库缩略图背景等）改走令牌/常量——**只清理你确认可达的少量处，禁止大范围机械替换**（42 处全清超出本批范围，低优先处保留）。
- 验收：现有 4 页观感不倒退（无布局错位、无颜色崩坏），截屏级自查在报告中描述。

### 批 2：动画基础设施（新建 agent/ui_anim.py，~300-400 行）
- 薄封装工具，纯函数 + 类，供 批 3/4 使用，设计要点：
  - `fade_in(widget, ms=200, easing=InOutQuad)`：QGraphicsOpacityEffect 0→1，**finished 必须清理 effect**（`setGraphicsEffect(None)`），组件内部持动画引用。
  - `animate_prop(widget, prop: bytes, start, end, ms, easing)`：QPropertyAnimation 薄封装，持引用，finished 后 release。
  - `SlideStack(QStackedWidget)` 子类：float 代理属性 `_slide_progress`（用 `Property` 注册）实现页面滑屏 200-300ms OutCubic——新旧页同时布局、动画期间 `setEnabled(False)` 锁换页、finished 解锁并归位。**备选（若滑屏实现遇布局顽固问题可降级）**：`fade_in` 新页方案（旧页瞬退+新页淡入 120-200ms）——两案都在 ui_anim 内提供，报告说明选用。
  - `HoverEngine`：全局事件过滤器（`installEventFilter` 在 main window 或 QApplication），跟踪 enter/leave，对注册过渡规则的控件做背景色插值动画（QVariantAnimation 驱动，`valueChanged` 里直接 `widget.setStyleSheet`?——**禁止逐帧改 styleSheet**（开销大）：优先用 palette/自绘? 实际上 Qt 无原生背景插值：务实方案=用 QVariantAnimation 插值颜色，逐帧 `setStyleSheet` 只改 `background-color` 单属性会被 QSS 重解析——**性能可接受仅在低频 hover 且控件少**，若担心可对目标控件用 `setAttribute(WA_StyledBackground)` + 逐帧调 palette 的 QPalette.Button 并在 QSS 中让该控件颜色跟 palette（需 QSS 不写死背景）。由你权衡选择并说明。**默认不铺开**：只注册 导航按钮(navBtn)、主按钮(pinkBtn/greenBtn/actionBtn)、卡片(statItem/QFrame#card 如需)。
- 文件头部 docstring 写明三铁律与「QSS 无动画」认知，防后人误用。
- 验收：`python -m compileall agent/ui_anim.py`；报告附 3-5 个自证探针（类存在、fade_in 后 effect 清理逻辑可达、SlideStack 锁换页标志）。

### 批 3：页面切换动画 + 状态动效（用户可感知大头）
- `_navigate()`（约 L703）改走 SlideStack/fade 方案：切换时播放动画。注意 dashboard 等页面含滚动区——滑屏时滚动区重排版伪影处理（方案 2.4：动画期间禁重排、结束 updateGeometry）。
- 仪表盘/状态卡数值色变平滑化：现有 3+ 处 `setStyleSheet(f"...color: {GREEN/RED/ORANGE/TEXT_MUTED}")` 直接换色（约 L4195、L4202、L4462、L4472、L4480、L4486、L4491 附近的 _val_label/_sidebar_status）——如可行做成颜色插值动画（QLabel 无 color 属性——用 ui_anim 的颜色插值 helper：QVariantAnimation QColor 插值 + 逐帧 setStyleSheet 仅 color 单属性，控件数少（~4 个）可接受；若实现复杂可退化为保持现状 + 报告说明，**禁止阻塞主任务**）。
- 窗口启动淡入：`__init__` 末尾（UI 构建完成后）`windowOpacity` 0→1，250ms（windowOpacity 走合成器，安全）。
- 验收：手动路径自查（主负责人复跑）——4 页互切、启停 bot 状态卡变色、窗口启动。

### 批 4：仪表盘首屏一次性入场
- dashboard 状态卡/统计区首屏入场：构建完成后播放一次交错淡入+上移（index×30-50ms 延迟，QSequentialAnimationGroup 或 QTimer 链），300-500ms 总时长，OutCubic。
- **一次性**：定时刷新 `_refresh_stats`（5s 周期）不得重播——需要门闩标志（如 `_intro_played`），报告说明门闩实现位置。
- 验收：首启观感 + 刷新不重播（代码路径自查）。

## 4. 报告要求（写入 docs/协作/reports/执行报告_N1.md）

逐批写：改了什么（文件:行级）、设计决策与理由（尤其 SlideStack 滑屏 vs 淡入选型、HoverEngine 实现路径、批 3 色变动画是否落地）、自证结果（compileall/探针输出，如实）、**异议栏**（你对方案的任何不同意见/风险提示）、遗留事项。不要宣称 pytest 结果。

## 5. 完成定义

四批全部落地 + 报告写入 + 未违反 0 节纪律。主负责人随后复跑正式门禁（pytest 全量 + pyflakes + 重建发布快照 + 主人验收）。
