# 裁决 K2：安装糖糖.bat 新机闪退修复（主负责人直接热修）

- 日期：2026-09-04 · 主负责人
- 结论：**已修复并验证**（字节级热修，无代码批次，不需 Codex 批次流程）

## 根因（取证）

安装糖糖.bat 为 **LF-only 换行**（23 行全裸 LF、0 CRLF）。cmd.exe 解析 LF-only 批处理在 `goto :label` 处会静默失败直接终止。桌面机未暴露：Python 3.10 在默认路径被第一行 `if exist` 命中，`goto :found_installer` 从未执行；新笔记本无默认路径 Python → 走 `where` fallback → goto 触发 → 闪退。

同病文件：tools/separate_vocals.bat（LF-only，无 goto，一并归一化防患）。

## 修复内容

- **安装糖糖.bat** 重写：去掉全部 goto（顺序 if 结构）；路径全引号化（用户名含空格安全）；新增 `%~dp0python310\` 项目本地解释器兜底（便携版可用）；`where python` 结果过滤 WindowsApps 商店占位符（防弹商店）；未找到 Python 时显示清晰中文错误 + pause（不再闪退）
- **tools/separate_vocals.bat**：仅 CRLF 归一化，内容零改动
- 备份：两个 `.bak-20260904-k2`（SHA 见备份文件）
- 启动控制台.bat：CRLF 正常未动（结构脆弱但功能正常，最小改动原则不扩大范围）

## 验证

- 字节复核：安装糖糖.bat CRLF=26 裸LF=0；separate_vocals.bat CRLF=18 裸LF=0
- `cmd //c 安装糖糖.bat`（stdin EOF）端到端：找到解释器 → 菜单完整显示（中文正常）→ exit 0
- K1 全量门禁不受影响（本批零 py 文件改动）

## 用户须知

修复经 Syncthing 自动同步到新笔记本后重新双击即可；若新机仍未装 Python，会看到明确错误提示而不是闪退。
