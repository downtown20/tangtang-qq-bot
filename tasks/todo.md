# 台式机修复执行清单

详细说明与证据：[换机依赖与GPT-SoVITS启动修复计划_20260905.md](../docs/开发规划/换机依赖与GPT-SoVITS启动修复计划_20260905.md)

- [ ] 保存台式机环境、体检和日志基线
- [ ] 安装/验证 Visual Studio 2022 Build Tools C++ 工作负载
- [ ] 用项目 Python 安装根目录和 GPT‑SoVITS 依赖
- [ ] 验证 `jieba-fast`、`jieba`、`matplotlib` 导入
- [ ] 核对启动器 fallback、setuptools 兼容补丁和日志
- [ ] 核对 `ServiceManager` 正式 `/tts` 健康检查及 CPU timeout
- [ ] 确认 `main.py` 只负责编排，不在运行时 pip 安装
- [ ] 补齐依赖契约、启动器和健康检查回归测试
- [ ] 运行全量 pytest
- [ ] 运行 pyflakes undefined 扫描
- [ ] 端到端启动主程序、验证 `/tts`、停止进程并检查端口
