import { contextBridge, ipcRenderer } from 'electron'

export type BridgeInfo = { port: number; token: string }

// 渲染进程不碰 Node/fs：窗口控制 + 后端桥地址，其余全部走本地 HTTP。
contextBridge.exposeInMainWorld('desktop', {
  platform: process.platform,
  minimize: () => ipcRenderer.invoke('win:minimize'),
  toggleMaximize: () => ipcRenderer.invoke('win:toggle-maximize'),
  close: () => ipcRenderer.invoke('win:close'),
  isMaximized: () => ipcRenderer.invoke('win:is-maximized'),
  onWindowState: (cb: (s: { maximized: boolean }) => void) => {
    const handler = (_: unknown, s: { maximized: boolean }) => cb(s)
    ipcRenderer.on('win:state', handler)
    return () => ipcRenderer.removeListener('win:state', handler)
  },
  getBridgeInfo: () =>
    ipcRenderer.invoke('bridge:info') as Promise<{
      info: BridgeInfo | null
      error: string | null
      root: string
    }>,
  onBridgeReady: (cb: (info: BridgeInfo) => void) => {
    const handler = (_: unknown, info: BridgeInfo) => cb(info)
    ipcRenderer.on('bridge:ready', handler)
    return () => ipcRenderer.removeListener('bridge:ready', handler)
  },
  onBridgeExit: (cb: (s: { code: number | null; error: string | null }) => void) => {
    const handler = (_: unknown, s: { code: number | null; error: string | null }) => cb(s)
    ipcRenderer.on('bridge:exit', handler)
    return () => ipcRenderer.removeListener('bridge:exit', handler)
  },
})
