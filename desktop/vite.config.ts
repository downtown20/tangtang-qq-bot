import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import electron from 'vite-plugin-electron/simple'

// ⚠ 必须清掉：某些环境（含 DSH 自身）会设置 ELECTRON_RUN_AS_NODE=1。
// 它让 electron.exe 以纯 Node 模式启动——require('electron') 只返回一个路径串，
// 主进程里 electron.app 是 undefined，表现为启动即崩。
delete process.env.ELECTRON_RUN_AS_NODE

// Electron + Vite：主进程/预加载由插件用 esbuild 打包到 dist-electron/
// 渲染进程就是普通 Vite 应用（React）。
export default defineConfig({
  plugins: [
    react(),
    electron({
      main: { entry: 'electron/main.ts' },
      // preload 必须是 CJS：Electron 的预加载脚本在隔离上下文里不支持 ESM。
      preload: {
        input: 'electron/preload.ts',
        vite: {
          build: {
            rollupOptions: {
              output: { format: 'cjs', entryFileNames: 'preload.cjs' },
            },
          },
        },
      },
    }),
  ],
  server: {
    port: 5178,
    strictPort: true,
    // Windows 上编辑器/工具的原子写会创建 `.<name>.<pid>.<uuid>.tmpdir/`，
    // Vite 的 watcher 扫到它会抛 EBUSY 直接把 dev server 带崩。
    watch: {
      ignored: ['**/.*.tmpdir/**', '**/.*.tmp', '**/*.bak'],
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
