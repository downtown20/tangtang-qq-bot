# 台式机依赖与 GPT-SoVITS 修复计划

详细版请阅读：

[换机依赖与GPT-SoVITS启动修复计划_20260905.md](../docs/开发规划/换机依赖与GPT-SoVITS启动修复计划_20260905.md)

## 执行顺序

1. 建立台式机 Python、依赖、日志基线，不覆盖原始证据。
2. 补齐根目录与 `gpt-sovits/` 依赖清单，统一安装器和体检程序。
3. 在 GPT‑SoVITS 启动包装器中实现 `jieba-fast → jieba` 回退及清晰错误。
4. 将 GPT‑SoVITS 正式 `/tts` 健康检查 timeout 调整到可覆盖 CPU 首轮推理的预算。
5. 确认 `main.py` 不负责安装依赖；语音失败时主聊天仍可降级运行。
6. 添加回归测试、运行全量测试和端到端语音验证。

## 完成条件

- `cl.exe` 存在且可输出 MSVC 版本。
- `import jieba_fast, matplotlib` 成功，`pip check` 通过。
- GPT‑SoVITS `/tts` 返回 200；无 CUDA 时 CPU 路径可通过健康检查。
- 主程序文字链路不因可选语音依赖缺失而退出。
- `python -m pytest tests/ -q` 全绿。
- pyflakes undefined 扫描为 0。
