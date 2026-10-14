# 任务简报 B1：重启与三 canary 验收（P0-1 / P0-2）

- 派发：2026-09-02 · 主负责人
- 来源：`docs/运行验收/阶段总结_记忆与真实链路验收_20260901.md` P0-1/P0-2
- 分工：**用户重启并真实触发 → Codex 取证 → 主负责人裁决**

## 背景

上一 boot 的已知修复（新 boot 才能加载）：
1. 私聊批处理 `UnboundLocalError`（`_self_memory_action_id` 未初始化）——源码已修+回归
2. 贴图观察器只认旧 `Sticker Tool` 文案——解析器已兼容 `Sticker ActionPlan`
3. 窗口协议：「没事/好的/嗯」等收尾情境不凑回复，沉默决定权归 LLM

现有观察器进程尚未热加载修复——**必须重启**。

## 用户操作步骤

1. 停止当前糖糖（控制台退出）与观察器进程
2. 记录重启时刻（精确到分钟）
3. 正常启动糖糖（控制台），启动观察器：`python tools\runtime_observer.py --duration-hours 2`
4. 依次做三个 canary（每个只做一次，不刷样本）：

| # | 操作 | 预期 | 记录 |
|---|---|---|---|
| C1 | 私聊发一条普通文字 | 一次自然回复；日志无 UnboundLocalError/traceback | 时间 + 你的观感 |
| C2 | 私聊或群聊要一个贴图 | 贴图实际送达；观察器报告判 confirmed | 时间 + 是否送达 |
| C3 | 群聊自然闲聊至收尾（对方说「没事/好的/嗯」类） | 糖糖合理沉默（skip）或不凑回复；不刷屏 | 时间 + 她说了什么 |

5. 观察器跑满 2 小时后正常收尾（若提前结束请告知）

## Codex 取证任务（等用户操作完成后派发）

- 从 tangtang.log 过滤新 boot 区间的：canary 消息、`UnboundLocalError`/traceback、贴图 ActionPlan 判定、窗口 reply/skip/fade 分布
- 读观察器生成的 `.md/.json` 报告，确认 C2 的 confirmed 判定
- 核对 C3 的窗口样本是否出现合理 `skip_response` 或自然沉默
- 产出 `docs/协作/reports/执行报告_B1.md`

## 裁决标准（主负责人）

- C1：日志零 traceback + 用户确认一次自然回复 → 通过
- C2：贴图送达 + 观察器 confirmed → 通过
- C3：出现 skip 或合理沉默 → 通过；若凑回复，需判断是协议未加载还是 LLM 决策，再定返工方向
- 三 canary 通过 → P0-2 闭环，更新总验收矩阵「窗口自主回复」为通过（本轮）
