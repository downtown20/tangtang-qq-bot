/**
 * 界面偏好（2026-10-25 第 45 轮）。
 *
 * ## 为什么需要「讲解模式」
 *
 * 第 17 轮为了「小白也能看懂」，在回放里加了大量解释：这站干什么、判断依据、
 * 每个参数一句话、怎么看这张图、三态图例……对**第一次看的人**这是必要的，
 * 对**天天看的人**这是噪音。
 *
 * 主人原话：「当前注释很密集，需要一个开关来控制注释的显示。」
 *
 * ## 实现选择：CSS 属性，而不是给每个组件传 prop
 *
 * 开关只做一件事：把 `html[data-explain="off"]` 挂上，全局 CSS 隐藏
 * `.explain-only`。这样**不用改任何组件签名**——传 prop 的写法每加一个
 * 解释区块就要改一次链路，漏一处就是「关了还漏一条」（教训 #47：
 * 同一语义 N 个实现点，漏一个就静默错）。
 *
 * ## 纪律：它只能藏解释，不能藏问题
 *
 * `.explain-only` 只准加在**解释性文字**上（role / logic / 参数解释 / howto /
 * 图例）。数据、结论（`did`）、异常原因（`note`）永远显示——否则这个开关
 * 会变成「眼不见为净」的藏 bug 工具。闸门在
 * `desktop/src/components/TurnReplayFlow.tsx` 的类名注释 + 本文件的说明里。
 *
 * ## 2026-10-26 修正：从「回放页专用」扩到**全站**
 *
 * 主人原话：「右上角的『讲解』按钮需要控制**全局**的注释显示，
 * 目前只控制『她怎么想』的注释。」
 *
 * 两件事一起修：
 *
 * 1. **选择器太窄**：CSS 原来只写 `.explain-only`（单类），于是
 *    `PromptLedger.tsx` 里 `className="pl-note pl-explain-only"` 的
 *    **前缀写法永远匹配不到**——按钮点了它不动，用户只会觉得
 *    「这开关只管一部分」。现在改成 `[class*='explain-only']`，
 *    凡是类名里含这个词的都受控。
 * 2. **覆盖面太窄**：资产页的分组说明/协议作用、调参页的参数作用、
 *    设置页的字段说明、特殊模式的机制说明、概览页的入口说明、
 *    两处图例——全部补上 `explain-only`。
 *
 * ## 分类标准（加类之前先问「藏了会不会做错操作」）
 *
 * | 藏（explain-only） | 不藏 |
 * |---|---|
 * | 这东西**为什么/怎么工作** | **怎么操作**（启动方式、双击改名、右键菜单） |
 * | 参数/字段**是什么意思** | **改完怎么生效**（apply_hint） |
 * | 图例（颜色对应什么） | **数据和状态**（数字、篇数、已运行多久、核验状态） |
 * | 分组/通道的机制说明 | **错误与预警**（note / warn / err） |
 * | 空状态引导算什么：**不藏**（不然关讲解后一片空白，不知道能干什么） | |
 */

/** localStorage 键（镜像 `tangtang.theme` / `tangtang.pinnedSessions` 的命名） */
export const EXPLAIN_MODE_KEY = 'tangtang.explainMode'

/** 默认**开**：新用户先看懂，老手自己关 */
export function readExplainMode(): boolean {
  try {
    const raw = localStorage.getItem(EXPLAIN_MODE_KEY)
    if (raw === 'off' || raw === '0' || raw === 'false') return false
    if (raw === 'on' || raw === '1' || raw === 'true') return true
  } catch {
    /* 隐私模式 / 无 localStorage：用默认值，不抛错 */
  }
  return true
}

export function writeExplainMode(on: boolean): void {
  try {
    localStorage.setItem(EXPLAIN_MODE_KEY, on ? 'on' : 'off')
  } catch {
    /* 忽略 */
  }
}
