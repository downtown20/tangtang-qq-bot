# vulture whitelist —— 只声明结构上必然活跃的名字（不经静态引用到达）。
#
# 依据：docs/开发规划/长期优化/记录/T2.1-死代码普查-发现.md 第三节「漏报模式清单」。
# 消费方：tools/长期优化快照.py 的 collect_vulture（带本文件跑，供棘轮对比）。
#
# 准入规则（必须同时满足）：
#   1) 属于普查记录的结构类别之一；
#   2) 定义行形态经核对（技能=装饰器注册 / Qt=框架回调 / 属性=self.X= / import=别名或跨模块导入）；
#   3) 加完后重跑阳性对照：种一个真死函数仍要被抓到。
#
# 实测陷阱：把 315 条全写白名单 -> vulture 永久报 0，等于把闸门关掉（教训 #30 变体）。
#   本文件只收结构上必活的 42 个名字。
#
# 形态：裸属性访问语句（这些名字在模块顶层未绑定，赋值形态会 AttributeError）。

import agent.calculator
import agent.color_panel
import agent.convert
import agent.drives
import agent.handler
import agent.health_check
import agent.image_gen
import agent.image_share
import agent.memory
import agent.mood
import agent.mood_tracker
import agent.personality
import agent.sensory
import agent.sticker
import agent.store
import agent.translate
import agent.ui_anim
import agent.web_search

# ── 类 1 技能注册（8 个）
agent.calculator._calculate_skill   # 装饰器注册/技能函数
agent.convert._convert_skill   # 装饰器注册/技能函数
agent.image_gen._generate_image_skill   # 装饰器注册/技能函数
agent.sensory._get_time_skill   # 装饰器注册/技能函数
agent.sensory._get_weather_skill   # 装饰器注册/技能函数
agent.image_share._share_image_skill   # 装饰器注册/技能函数
agent.translate._translate_skill   # 装饰器注册/技能函数
agent.web_search._web_search_skill   # 装饰器注册/技能函数

# ── 类 2 Qt 回调（3 个）
agent.color_panel.mouseMoveEvent   # 框架回调
agent.color_panel.mousePressEvent   # 框架回调
agent.color_panel.paintEvent   # 框架回调

# ── 类 3 实例属性（21 个）
agent.color_panel._board_img   # self.X 赋值
agent.handler._daily_like_task   # self.X 赋值
agent.mood_tracker._db   # self.X 赋值
agent.mood._energy_high   # self.X 赋值
agent.mood._energy_low   # self.X 赋值
agent.mood._energy_normal   # self.X 赋值
agent.health_check._handler   # self.X 赋值
agent.handler._last_proactive_event   # self.X 赋值
agent.handler._last_song_send_state   # self.X 赋值
agent.handler._llm_busy   # self.X 赋值
agent.handler._log_buffer   # self.X 赋值
agent.mood_tracker._on_alert   # self.X 赋值
agent.mood._reply_count_since_rest   # self.X 赋值
agent.memory.active_conversations   # self.X 赋值
agent.handler.birthday_greeter   # self.X 赋值
agent.sticker.collected_count   # self.X 赋值
agent.handler.daily_report   # self.X 赋值
agent.handler.image_gen   # self.X 赋值
agent.handler.image_share   # self.X 赋值
agent.drives.last_release   # self.X 赋值
agent.handler.vision_config   # self.X 赋值

# ── 类 6 其他导入（3 个）
agent.ui_anim.Property   # import 类
agent.color_panel.QDialog   # import 类
agent.handler._skills_mod   # import 类

# ── 类 6 导入别名（2 个）
agent.handler._asyncio2   # 模块别名/except 分支
agent.health_check._sqlite3   # 模块别名/except 分支

# ── 类 6 跨模块导入（1 个）
agent.handler.MessageHandler   # 跨模块导入

# ── 类 7 类定义（5 个）
agent.ui_anim.HoverEngine   # 类定义（子类化/框架用）
agent.handler.MessageHandler   # 跨模块导入
agent.personality.Person   # 类定义（子类化/框架用）
agent.store.ReadOnlyStore   # 类定义（子类化/框架用）
agent.ui_anim.SlideStack   # 类定义（子类化/框架用）
