# 任务简报 B2：skip_response 假红警告修复

- 派发：2026-09-02 · 主负责人
- 来源：`docs/协作/rulings/裁决_B1.md`（异议 1 的修复方案裁决）
- 执行方：Codex · 执行后写 `docs/协作/reports/执行报告_B2.md`
- **允许改代码；生产库只读；不要重启 bot**

## 背景（主负责人已裁决的方案）

三 canary 通过，但成功的 `skip_response`（LLM 故意沉默）后，handler 仍发「⚠ LLM 工具循环+收尾轮后仍无文字回复——本次回复将被丢弃」警告，观察器照单计入 `llm_failed` + `background_task_failures` → overall 假红。

**修复方案（已裁决，按此执行）**：
- **只在 handler 发射点修**：`turn_actions` 存在且 `respond=False`（LLM 已用 skip_response 落账的故意沉默）时，不发「仍无文字回复——将被丢弃」警告
- **不动观察器**（单一事实源修在日志产生处，观察器对日志忠实计数是对的）
- 真实空回复（无 skip 终态、content 空、has_tools）的警告保留——那是既有防线（教训 #27/#28 的监控），不许削弱

## 改动点

`agent/handler.py:5551-5557` 附近：当前逻辑

```python
if not content.strip():
    if has_tools:
        logger.warning("⚠ LLM 工具循环+收尾轮后仍无文字回复——本次回复将被丢弃")
    else:
        logger.warning("⚠ LLM 轻量调用返回空内容（...）")
```

最小修改：增加 `deliberate_skip` 判定（`turn_actions` 存在且 `turn_actions.get("respond") is False`），`deliberate_skip` 时不发第一条警告。注意 5532 行收尾轮已有 `turn_actions.get("respond", True)` 门控，风格保持一致。轻量调用分支不受影响。

## 回归测试

在 handler 相关测试文件中加：`turn_actions={"respond": False}` + has_tools + 空 content → 不发「仍无文字回复」警告；respond=True（或 None）+ 空 content → 警告保留。参考既有测试对 logger.warning 的捕获方式（caplog）。

## 验证要求

- 改动前 `agent/handler.py.bak-20260902` 已有（C2 批次），如冲突另存 `-b2` 后缀
- compileall 必过；pytest/pyflakes 沙箱跑不了就留给主负责人（报告如实写明）
- 报告按协议格式；异议栏如实填写
