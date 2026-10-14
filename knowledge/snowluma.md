## SnowLuma 是什么
SnowLuma 是一个基于 NTQQ 的 QQ 机器人框架，是 NapCat 的新一代替代品。它像一个桥，把你的 QQ 号和 Python 程序连起来。

## SnowLuma 工作原理
- SnowLuma 登录你的 QQ 号，作为 QQ 客户端运行在电脑上
- 通过 WebSocket 把群消息实时推送给你的 Python 程序（OneBot 11 协议）
- Python 程序处理消息后，通过 HTTP API 让 SnowLuma 代发回复

简单说：SnowLuma 当手（收发 QQ 消息），Python 程序当脑（决定回复什么）。

糖糖就是跑在 SnowLuma 上的一个 Python 程序。你可以问她："糖糖你是怎么工作的？"她就会告诉你这些。
