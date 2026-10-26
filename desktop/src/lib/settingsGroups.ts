import {
  Bot,
  Cpu,
  Heart,
  Layers,
  MessageSquare,
  Gauge,
  Plug,
  Database,
  ListChecks,
  Eye,
  Mic,
  Search,
  ImagePlus,
  Newspaper,
  ThumbsUp,
  Wand2,
  History,
  RefreshCw,
  Tags,
} from 'lucide-react'

export type SettingsSection = {
  key: string
  label: string
  icon: typeof Bot
}

export type SettingsGroup = { group: string; items: SettingsSection[] }

/** 设置左侧分类。Settings 与 ⌘K 命令面板共用同一份，避免两处漂移。 */
export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    group: '基础',
    items: [
      { key: 'bot', label: '机器人', icon: Bot },
      { key: 'personality', label: '人格', icon: Heart },
      { key: 'llm', label: 'AI 模型', icon: Cpu },
      // 「亲密模式模型」已于 2026-10-24 合并进 AI 模型（主人要求）：
      // 亲密模式与困难轮次一律跟随 AI 模型的设置，不再单独配一套端点/密钥。
      { key: 'vision', label: '识图', icon: Eye },
      { key: 'voice', label: '语音', icon: Mic },
    ],
  },
  {
    group: '对话',
    items: [
      { key: 'behavior', label: '行为', icon: MessageSquare },
      { key: 'mood', label: '情绪', icon: Gauge },
      { key: 'memory', label: '记忆', icon: Database },
      // 2026-10-25 第 47 轮：离线补读整组 7 个键原先只在 config.yaml 里，
      // 设置页没有入口——而它正是「她为什么不知道我刚说的话」的第一答案来源。
      { key: 'catch_up', label: '离线补读', icon: History },
      { key: 'web_search', label: '搜索', icon: Search },
    ],
  },
  {
    group: '连接与任务',
    items: [
      { key: 'napcat', label: 'SnowLuma', icon: Plug },
      // 2026-10-27：独立的「连接密钥」页取消了（主人：不必把密钥凑到一个设置页）。
      // 密钥改成跟着它所属的那块配置走 —— AI 模型页填 provider 的 Key、
      // SnowLuma 页填授权 Token、识图页填识图 Key；值统一写 .env。
      { key: 'tasks', label: '任务可靠性', icon: ListChecks },
      { key: 'image_share', label: '图片分享', icon: ImagePlus },
      { key: 'daily_report', label: '每日播报', icon: Newspaper },
      { key: 'daily_like', label: '每日点赞', icon: ThumbsUp },
      { key: 'image_gen', label: 'AI 画图', icon: Wand2 },
      // 图库打标（2026-10-26 第 63 轮）：原来靠双击 打标色图.bat，参数和模型名都在 bat 里。
      // 现在 provider / 模型 / 端点 / 改名开关全在这一节，图库页只留一个按钮。
      { key: 'image_tagging', label: '图库打标', icon: Tags },
      { key: 'memory_sync', label: '记忆同步', icon: RefreshCw },
    ],
  },
]

export const SETTINGS_FLAT: SettingsSection[] = SETTINGS_GROUPS.flatMap((g) => g.items)
