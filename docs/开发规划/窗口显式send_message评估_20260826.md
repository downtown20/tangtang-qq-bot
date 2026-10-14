# 窗口显式 `send_message` 评估（2026-08-26）

## 结论

当前阶段不引入。保留“最终文本自动发送 + `skip_response` 显式沉默”，先用窗口统计验证回复率。

## 依据

- 本项目的最终文本、语音、唱歌、贴图和聊天落库共用现有发送收尾链。再加 `send_message` 会形成两条可见输出路径，必须额外解决重复发送、发送失败回退、语音正文、引用消息和自忆证据绑定。
- OpenClaw 的 `message_tool` 不是只新增一个工具，而是配套“最终文本默认不可见、工具发送才可见、遗漏工具时一次恢复重试、失败诊断和发送策略”的完整投递状态机。官方也保留 `automatic` 作为普通群请求默认值，并提示工具调用不可靠的模型可能出现“生成了最终文本但没有发出”。
- Nekro Agent 把发消息归为 `BEHAVIOR`，调用后不触发再次思考；它的前提同样是“LLM 产出执行计划、行为副作用负责回复”，与本项目当前“最终文本就是回复”的契约不同。

## 重新评估条件

只有同时满足以下条件才进入实现：

1. 窗口统计证明 `skip_response` 仍不能把刷屏率降到可接受范围；
2. 选定的主模型在场景回放中稳定调用发送工具；
3. 先设计单一投递状态机：`pending / sent / skipped / failed`，禁止工具发送与最终文本双发；
4. 文本、语音、唱歌、贴图、引用、聊天落库和自忆来源全部接入同一个发送结果；
5. 对“模型写了回复但漏调工具”提供至多一次恢复重试和可观测诊断。

## 参考

- OpenClaw ambient room events: https://github.com/openclaw/openclaw/blob/main/docs/channels/ambient-room-events.md
- OpenClaw channel delivery modes: https://github.com/openclaw/openclaw/blob/main/docs/gateway/config-channels.md
- 本地 Nekro 学习报告：`docs/开发规划/归档/Nekro_Agent_2.3.0_学习报告.md`
