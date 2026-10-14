# ADR-005 Phase 1 生产 canary 验收

日期：2026-08-29  
结论：**PASS（纯文本提醒生产链路）**  
运行：PID `29584`，boot `3dd84a1f`

## 前置门禁

- `task_action_schema_missing=false`。
- open、uncertain、dead、stale sending、linked invariant、bare sending 均为 0。
- canary 前 `task_action_attempts=0`、`task_action_events=0`、linked outbox=0。
- `python tools/体检.py` 通过；全量 `1354 passed`；pyflakes `undefined=0`。

## 端到端证据

1. `00:26:06` 主人私聊创建“3分钟后提醒我：ADR-005 Phase 1 canary”。
2. `00:26:11` LLM 原生调用 `set_reminder`；任务 `#6` 落为 legacy pending，
   `current_attempt_id=NULL`，到期时间 `00:29`。
3. `00:29:50` TaskManager 冻结 generation 0 ActionPlan，并在同一事务建立 attempt、
   task current pointer 与唯一 linked outbox。
4. `00:29:51` outbox worker 唯一领取并只调用一次 `send_private_msg`；平台返回
   `message_id=15652973`。
5. 同秒 task=`done`，attempt=`confirmed`、accounting=`clean`；linked outbox 清空。
6. `confirmed_action_facts` 保存唯一 action `act-57a6ae48d930a4b2`、唯一 message ID 和
   `chat_log_id=241130`；chat 投影为非 synthetic，稳定 event key 唯一。
7. 独立观察器在 `00:34:19` 判定 sending=`PASS`（1/1 confirmed）、
   reminders=`PASS`（durable_confirmed=1）；uncertain/failed/invariant 均为 0。

日志和事件中只出现一次 outbox claim、一次平台发送、一次 confirmed；到期后的观察窗口
未出现重复发送或重试。

## 非 canary 告警

观察器整体仍显示 FAIL，原因是数据库已有一条历史累计 inbound failed；本观察区间
`inbox_new_failed=0`、网关无掉线。后台 Handler 样本仍在启动/采样宽限中，因此为
INSUFFICIENT。两者不改变本次定时文本 canary 判定，继续由正在运行的 2 小时观察收集。

## 晋级

ADR-005 Phase 1 的生产上线闸门通过。允许开始 Phase 2a 源码实现，但人工 generation
开关必须默认关闭；Phase 2a 仍需独立迁移、红测、故障注入和下一次 canary，不能把本次
结果外推为 Phase 2 已完成。
