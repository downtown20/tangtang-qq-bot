import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 临时探针配置：只跑渲染进程（React），**不启动 electron 主进程**。
 *
 * 为什么要它：`vite.config.ts` 里的 vite-plugin-electron 在 dev 模式会 spawn
 * electron.exe 开一个独立窗口，那个窗口 chrome-devtools MCP 抓不到。做 UI 行为
 * 取证（比如「0.08 到底能不能打进去」）时，用纯浏览器 + 在 initScript 里 stub
 * 掉 `window.desktop` 更直接。
 *
 * 只在验证时用：`npx vite --config vite.probe.config.ts`。不进构建产物。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5188,
    strictPort: true,
    /* 和 vite.config.ts 同样的坑：编辑器/工具的原子写入会创建
       `.<name>.<pid>.<uuid>.tmpdir/`，watcher 扫到它 EBUSY 直接带崩 dev server。
       （第一次跑探针就是被这个崩掉的，漏抄这一段 = 白跑一轮。） */
    watch: { ignored: ['**/.*.tmpdir/**', '**/.*.tmp', '**/*.bak'] },
  },
})
