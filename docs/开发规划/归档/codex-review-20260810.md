# Codex 全面审查报告（2026-08-10）

> 审查方式：`codex exec` 高强度模式（model_reasoning_effort=high），只读，538K tokens
> 覆盖：agent/ 核心模块 + handler.py 关键路径 + tests/ 覆盖缺口 + 安全/并发/持久化
> 上下文：AGENTS.md（2026-08-10 增强版——项目背景 + 7 条历史踩坑模式）

## 遗漏清单（最值得优先处理 5 件事）

1. 给所有记忆、聊天和删除工具增加执行阶段授权，封死第三方私聊泄露
2. 消除 `_last_private_user` 和所有 `_pending_*` 全局回合状态，改成隔离的 TurnContext
3. 修复 `last_chat/total_chats` 写入链路（事实提取、关系、每日奖励的基础计数全断了）
4. 重新设计反思输入/成功确认/持久化（防幻觉、防清空失败数据、防重复摘要）
5. 轮换明文密钥 + 修复 `.env → 配置 → UI 保存` 链路 + 补权限/并发/持久化测试

## CRITICAL

1. **[handler.py:3120-3783]** 所有群聊/私聊都获得跨用户记忆、聊天记录、群活跃度、`delete_friend` 工具——执行器无身份/群成员/数据作用域校验；`delete_friend` 只保护主人和机器人，未实现提示词声称的"一次警告后才能删"。
2. **[handler.py:2492,3466,3806]** Tool Calling 用全局 `_last_private_user` 推断查询对象；群聊 `search_chat_history` 无 qq_id 时可能读取"最近一个私聊用户"的记录；并发私聊可改写该字段。
3. **[handler.py:2279-2307]** 私聊中写出其他 QQ 号 → 自动注入该人档案及最近私聊内容，无权限判断。⚠️ **与 CLAUDE.md「交叉上下文」设计冲突**——是特性还是漏洞需用户裁决。

## HIGH

1. **[store.py:373 / memory.py:614]** `log_chat` 从未更新 `people.total_chats`/`last_chat`，全项目无 `total_chats=True` 调用——事实簇阈值、画像合成、里程碑、每日奖励长期不触发；`last_chat` 停留建档时间 → 每天重复 +5。
2. **[handler.py:54-82,1916-1991]** `_pending_voice/_pending_sing/贴图/CG/删除集合/选中记忆` 全是 Handler 实例级共享字段，私聊群聊并发时可能串到别人的回合。
3. **[config.yaml / 控制台 UI]** 明文密钥 + UI 保存把 `${ENV}` 占位符物化成明文；SnowLuma/语音/seductive 配置未接入 UI 环境变量。
4. **[self_state.py:330,469-596]** 保存触发不完整：群氛围不保存、驱动力不保证保存、关系场 10 次才落盘、关闭/定时重启无最终 flush——最近 9 次关系更新可能丢。
5. **[self_state.py:363 / reflection.py:314-348]** 经验缓冲只有昵称/时间/被@，无消息内容——反思提示词要求总结"学到了什么"但 LLM 没有事实来源 → 泛化或虚构摘要；私聊 `group_id=""` 格式化成"群"。
6. **[reflection.py:119-122]** 两次反思都失败仍清空经验缓冲 + 更新 `_last_reflection` → 数据永久丢失且 ~20 小时不重试。
7. **[tests/]** 82 测试未覆盖：Tool Calling 权限、并发回合隔离、log_chat 统计、记忆 recall 排序、ContextBuilder 预算、SelfState 持久化、Reflection 失败恢复。

## MEDIUM（精选）

- 静默模式改全局 `active_interjection=False`——一个群静默关所有群插话 [handler.py:1390-1405]
- `_voice_blocked` 子串误触发——"不要发语音这个设置是什么意思"会关语音 [handler.py:1273,2108]
- scenario 私聊找最近群不验证成员关系 [scenario.py:172-203]
- memory.py 异步方法直接同步开 SQLite，绕过"Store 唯一访问层"契约 [memory.py:1369-1375]
- context_builder `_assemble_with_budget()` 未被调用——分层预算没生效 [context_builder.py:157-223]
- personality + 模板重复拼接 group_vibe 等（浪费 token）[handler.py:1694-1756]
- `_safe_task()` 定义了但无调用；裸 create_task 不登记不取消；llm httpx client 不关闭 [handler.py:390-539]
- store.py 外键只在 delete_cluster 单连接开启——孤儿 embedding 记录 [store.py:154-160]
- `_last_reflection` 不持久化 + daily_digests 无唯一约束——重启重复反思 [reflection.py:89-90]
- knowledge.py 超长子段只留第一个 ~1500 字，其余永久丢弃 [knowledge.py:137-154]
- voice `split_sentences(max_chars=80)` 忽略参数；语音缓存无清理策略 [voice.py:188-224,229-291]
- reply_pipeline NSFW 用 `"骚" in text` 子串——"反对性骚扰"误触发 [reply_pipeline.py:133]
- 控制台 UI 与 config.yaml 不同步（catch_up/greeting/profile 等无设置入口）[控制台:1040-1053]
- personality 热重载不重建 `_cached_minimal` [personality.py:154-186]

## LOW

- reply_pipeline 清洗后不足 3 字恢复原文，可能带回控制标记 [reply_pipeline.py:121-123]
- scenario 文档说 replace 上限 5000 字，实际常量 500 字 [scenario.py:10,28]

## 明确没问题的方面

- 无缩进死代码（return 后无同级可执行语句）
- Store SQL 全部占位符，无注入路径；YAML safe_load，无 eval/pickle 风险
- SelfState 持久化字段 _save/_load 成对、原子写入
- personality 缓存契约（稳定前缀在前）正确，测试钉住了
- Voice 临时转码清理基本正确；knowledge 已用 jieba 分词

## 验证情况

- `total_chats=True` 全项目无调用 ✅ 实锤
- delete_friend 提示词保护 vs 执行器实现不符 ✅ 实锤
- 其余 CRITICAL/HIGH 位置具体，可信度高，修复前逐条复查
