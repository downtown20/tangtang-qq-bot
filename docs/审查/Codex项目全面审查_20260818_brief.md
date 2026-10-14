# 小糖糖项目全面审查 Brief（2026-08-18）

> 执行：`codex exec --skip-git-repo-check -s workspace-write < 本文件 > docs/审查/Codex项目全面审查_20260818_result.txt 2>&1`
> 审查纪律见 skill `evidence-review`（已安装到 ~/.codex/skills/），先读它再开工。

## 任务

对 QQ 机器人项目「小糖糖」（<项目根>，Windows，Python）做全面审查：**发现并修复 bug + 优化效率**。项目不是 git 仓库，没有 git diff 可用；GitNexus 索引今天已刷新（analyze --skip-git 已跑）。

## 项目背景（压缩版）

- 本质：有持久状态结构的 AI 猫娘，不是客服机器人。LLM 是唯一决策者（Native Tool Calling），系统只提供能力。
- 链路：`QQ消息 → SnowLuma(NapCat) → WebSocket → handler.py → LLM(deepseek-v4-flash) → reply_pipeline → 发送`
- 必读：项目根 CLAUDE.md（29 条踩坑教训 + 角色速查表 + 改哪里改哪个文件）——它是思维方式文档，审查判断依据。
- 关键文件：handler.py（消息路由中枢，~7300 行，偏大）、handler_commands.py / handler_autonomy.py（已拆出）、memory.py（记忆系统）、store.py（SQLite 唯一访问层）、personality.py / protocols.py（提示词/协议）、self_state.py / reflection.py / drives.py（自我状态/反思/驱动力）、context_builder.py、reply_pipeline.py、interjection.py、perception.py、knowledge.py、voice.py / sticker.py、糖糖控制台_qt.py。
- 测试：`python -m pytest tests/ -q`（当前 479 全绿基线）。pyflakes：`python -m pyflakes agent/*.py napcat/*.py main.py 糖糖控制台_qt.py | grep -i undefined`（必须清零）。

## 跳过区（近两天刚被三轮 Codex 审查并 Approve，只做回归检查，不重审）

- 空回复链路修复（THINKING_OFF / _call_llm_light 重试 / _stream_deepseek 降级分支 / finish_reason 日志）——见 docs/审查/Codex空回复修复审查_20260818_*.txt
- 色色模式状态机（_sed_active 6h TTL + [进入色色]/[退出色色] 标记协议）
- 纠正三闸门（correct_memory 主语解析）+ _resolve_mentioned_people 昵称交叉上下文
- 对话质量瘦身批次（背景块预算/状态信号化/风格锚/minimal 遥控）
- 意见征集/主动私聊 seek_willingness/记忆治理工程——均已审过

## 审查范围与优先级

**P0 核心路径（bug 影响用户可见行为）：**
1. handler.py 未拆分的其余部分（命令路由、工具执行器 _execute_tool、背景块组装、插话路径、自治循环入口）——重点找：缩进死代码（return 后代码）、未定义变量、状态判定用内容字符串匹配（反模式 #19）、内存态未持久化（反模式 #15：冷却/观察/配额类必须落盘）
2. reply_pipeline.py 清洗/分句/贴图——找：清洗后空白回复、特殊字符注入、长消息截断
3. store.py SQL——找：无事务的多步写、游标推进与写入的非原子性
4. napcat/ 与 SnowLuma 交互——找：重连、心跳、消息丢失

**P1 后台自治系统（bug 影响长期状态质量）：**
5. handler_autonomy.py 定时任务（提取调度/兜底提取/反思/主动私聊）——找：重复调度、异常吞噬、无节流
6. memory.py 提取/合成/纠正管线——找：游标推进一致性、confidence 分层漏洞
7. self_state.py / drives.py / reflection.py 持久化成对性（_save/_load 检查）

**P2 效率优化（每条必须附测量证据）：**
- 已知抓手 A：**「事实簇提取失败（LLM/格式）」约 74 例非空但 JSON 解析失败**（tangtang.log 里 grep）——思考关闭后应复测；可加格式容错（剥 markdown 围栏/重试）
- 已知抓手 B：**「降级非流式」329 次**——流式故障频率偏高，查 httpx 超时/重试配置与 503 潮的关系
- 已知抓手 C：`get_login_info` 每 30s 轮询——是否有更省的方式
- 后台 LLM 调用总量核算：提取触发频率×批大小×思考关闭前后 token 费用对比
- handler.py 拆分候选（模块行数/职责分析）——注意：拆分历史上有过导入丢失事故，必须走影响分析
- 日志噪音：插话跳过/生成中每 20 字——信息价值评估

## 已知背景事实（供取证参考，避免重复调查）

- 主模型 deepseek-v4-flash（推理模型）；提取类任务已挂 thinking:disabled；seductive 栈用 v4-pro
- memory.db 表：people / memories / chat_log / fact_clusters / aliases / kv_store / reply_metrics 等
- 事实簇提取失败游标不推进（fail-closed 设计，别当 bug 改掉——失败会重试同批次直到成功）
- 持久化铁律：重启后必须记得的运行时状态必须落盘；未回复前绝不二次主动打扰
- 插话配额：被 @ 和 /命令 不消耗主动插话配额
- 交叉上下文（私聊提其他 QQ）仅主人或亲密度≥50 开放
- 日志查法：tangtang.log 70 万+行，必须 python 脚本过滤，注意绝对行号

## 输出要求

1. bug 发现：Critical/Important/Minor 三级，每条附证据（日志行/DB 查询/会红的测试/探针输出）
2. 效率优化：单列一节，每条附测量（前后对比数字）
3. 修复：先修 Critical/Important，每修一处跑全量测试+pyflakes；改动清单汇总
4. 总体结论 + 遗留观察项
5. 诚实原则：没验证的标「待验证」，不许假装跑过测试
