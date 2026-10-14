# 任务简报 F1：亲密模式 CG 触发器接线

- 派发：2026-09-03 · 主负责人
- 来源：主人报告「亲密模式特殊 CG 一直未触发」+ 主负责人取证（全日志 0 次 send_cg_sticker 调用）
- 执行方：Codex · 执行后写 `docs/协作/reports/执行报告_F1.md`
- **允许改代码；不动 config、不重启 bot、生产库只读**

## 背景（主负责人已取证的事实，可信但请复核）

- 发送链路完整：库加载（handler.py:676，色色场景专用）→ 工具注册（:5074，**无门控**）→ 调用处理（:6902-6907）→ 发送（:4566 私聊 / :3663 群聊 → `_send_cg_actions`，确定性选图+回执+重试安全，:2390）
- 触发器断线：色色模式状态机（`_sed_active` TTL 字典，:11300-11309 落盘；标记处理 :11311-11335）与 CG **零连接**；全日志 0 次调用——LLM 无「现在是亲密模式」信号，保守到底
- `_call_llm_with_skills` 已有 `current_user: str = ""` 参数（:4899）——工具构建处可取当前用户身份；`_sed_active` 存的是**过期时间戳**（`time.time() + SED_TTL_SECONDS`），判断激活需查 TTL 是否未过期

## 任务一：工具门控（核心）

`send_cg_sticker` 注册条件（:5074）从「库可用」收紧为「库可用 **且当前用户色色模式激活（TTL 有效）**」：

- 新增 helper（建议 `_seductive_active_for(self, user_id: str) -> bool`）：查 `self._sed_active[user_id]` 且 `time.time() < 过期时间戳`；顺带惰性清理过期项
- `current_user` 为空（无身份上下文）时 fail-closed 不注册
- 群聊 per-user 隔离：工具构建按当前说话人判断（A 激活不影响 B 的回合）——实现时验证群聊工具构建路径的 current_user 确实是当前说话人
- 工具描述微调：说明仅亲密模式可用（既然已门控，出现即信号）

## 任务二：激活态一行状态事实（轻量）

色色模式激活的回合，系统提示注入一行能力状态（参考 `_mood_snapshot_context` 的「只给状态，不规定行为」模式，找色色知识注入处 :3118/:4191 附近最自然的位置）：大意「❤️ 亲密模式已激活——CG 贴图能力可用，氛围到位时可以发」；未激活不注入任何行。

## 任务三：回归测试

新增（参考 tests/test_sticker_roles.py / test_sticker_send_results.py 模式）：
1. 激活（TTL 有效）→ 工具注册；未激活 → 不注册
2. TTL 过期 → 不注册（helper 判定）
3. current_user 空 → 不注册（fail-closed）
4. 群聊 per-user：A 激活、B 说话 → B 的回合无 CG 工具
5. 门控前后发送链路不变（`_send_cg_actions` 调用契约）

## 危险区

- handler.py 近万行——方法边界处插入，禁 heredoc 盲改；改前 `.bak-20260903`
- 不动 `_process_seductive_markers` / `_set_seductive_active` / `_send_cg_actions` 的现有逻辑
- 色色模式的权限静态（scenario_targets）/激活动态（TTL）哲学不得破坏——门控只加一层投影，不改状态机
- compileall 必过；pytest/pyflakes 沙箱跑不了留给主负责人（报告如实写）

## 验收标准

1. 四场景回归写好（主负责人跑）
2. helper TTL 判定正确、惰性清理无副作用
3. 报告按协议格式，含群聊 current_user 验证证据与异议栏
