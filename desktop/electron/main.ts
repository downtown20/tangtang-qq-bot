import { app, BrowserWindow, ipcMain, shell } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import { spawn, type ChildProcess } from 'node:child_process'

const DEV_URL = process.env.VITE_DEV_SERVER_URL
const isDev = !!DEV_URL

// 启动打点：生产分支崩在无输出处时，靠这个文件定位（开发模式不写）。
const DEBUG_LOG = path.join(process.env.TEMP || '.', 'tangtang-electron-debug.log')
function dbg(msg: string) {
  try {
    fs.appendFileSync(DEBUG_LOG, `${new Date().toISOString()} ${msg}\n`)
  } catch {
    /* ignore */
  }
}
if (!isDev) {
  try {
    fs.writeFileSync(DEBUG_LOG, `${new Date().toISOString()} boot execPath=${process.execPath}\n`)
  } catch {
    /* ignore */
  }
  dbg(`__dirname=${__dirname} electron=${process.versions.electron}`)
  process.on('uncaughtException', (e) => dbg(`uncaughtException: ${e?.stack || e}`))
  process.on('unhandledRejection', (e) => dbg(`unhandledRejection: ${String(e)}`))
  process.on('exit', (code) => dbg(`exit code=${code}`))
}

/**
 * 找项目根（tools/desktop_bridge.py 所在目录）。
 * 绿色版 exe 放在 release/小糖糖桌面端/，程序根在上级；开发模式在 desktop/dist-electron。
 * 不写死相对层数——逐级向上找桥脚本，打包布局变了也不会错位。
 */
function findProjectRoot(start: string): string {
  let dir = start
  for (let i = 0; i < 8; i++) {
    if (fs.existsSync(path.join(dir, 'tools', 'desktop_bridge.py'))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return start
}

// 打包版拿 exe 所在目录：必须用 process.execPath（Node 层，随时可用）。
// 不能在模块顶层调 app.getPath('exe')——那时 Electron 还没 ready，会直接崩溃。
const ROOT = findProjectRoot(
  process.env.PORTABLE_EXECUTABLE_DIR
    || (isDev ? path.resolve(__dirname, '..', '..') : path.dirname(process.execPath)),
)
const BRIDGE_SCRIPT = path.join(ROOT, 'tools', 'desktop_bridge.py')
const PYTHON = process.env.TANGTANG_PYTHON || 'python'
dbg(`ROOT=${ROOT} bridgeExists=${fs.existsSync(BRIDGE_SCRIPT)}`)

let win: BrowserWindow | null = null
let bridge: ChildProcess | null = null
let bridgeInfo: { port: number; token: string } | null = null
let bridgeError: string | null = null

/** 启动 Python 后端桥，解析首行 BRIDGE_READY。 */
function startBridge() {
  bridge = spawn(PYTHON, [BRIDGE_SCRIPT, '--port', '0'], {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    windowsHide: true,
  })

  let buf = ''
  bridge.stdout?.setEncoding('utf-8')
  bridge.stdout?.on('data', (chunk: string) => {
    buf += chunk
    const idx = buf.indexOf('\n')
    if (idx === -1) return
    const line = buf.slice(0, idx).trim()
    buf = buf.slice(idx + 1)
    if (line.startsWith('BRIDGE_READY ')) {
      try {
        bridgeInfo = JSON.parse(line.slice('BRIDGE_READY '.length))
        win?.webContents.send('bridge:ready', bridgeInfo)
      } catch (e) {
        bridgeError = `BRIDGE_READY 解析失败: ${String(e)}`
      }
    }
  })

  bridge.stderr?.setEncoding('utf-8')
  bridge.stderr?.on('data', (chunk: string) => {
    const text = chunk.trim()
    if (text) console.error('[bridge]', text)
    bridgeError = text.slice(0, 500)
  })

  bridge.on('exit', (code) => {
    bridgeInfo = null
    bridgeError = `后端桥退出（code=${code}）`
    win?.webContents.send('bridge:exit', { code, error: bridgeError })
  })
}

function stopBridge() {
  if (!bridge) return
  try {
    bridge.kill()
  } catch {
    /* ignore */
  }
  bridge = null
  bridgeInfo = null
}

function createWindow() {
  win = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1080,
    minHeight: 680,
    show: false,
    frame: false,
    backgroundColor: '#141518',
    title: '小糖糖',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  win.once('ready-to-show', () => win?.show())

  if (isDev) {
    win.loadURL(DEV_URL!)
  } else {
    const page = path.join(__dirname, '../dist/index.html')
    dbg(`loadFile page=${page} exists=${fs.existsSync(page)}`)
    win.loadFile(page).catch((e) => dbg(`loadFile failed: ${e}`))
  }

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  const sendState = () => win?.webContents.send('win:state', { maximized: win.isMaximized() })
  win.on('maximize', sendState)
  win.on('unmaximize', sendState)
}

// CDP 调试端口：dev 模式默认开；**打包版只在显式设置
// TANGTANG_DEBUG_PORT 时开**（2026-10-24 加——打包版没有源码热更，UI 上的
// 按钮到底有没有把请求发出去只能靠 CDP 看，光靠截图分不清「没点中」还是
// 「点了但失败」）。默认仍然关闭：本地调试端口等于把渲染进程交给任意本地网页。
const DEBUG_PORT =
  process.env.TANGTANG_DEBUG_PORT || (!app.isPackaged ? '9222' : '')
if (DEBUG_PORT) {
  app.commandLine.appendSwitch('remote-debugging-port', DEBUG_PORT)
  dbg(`remote-debugging-port=${DEBUG_PORT}`)
}

app.whenReady().then(() => {
  dbg('app ready')
  startBridge()
  createWindow()
  dbg('createWindow called')
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})
app.on('window-all-closed', () => {
  stopBridge()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', stopBridge)

// ── 自绘标题栏 ──
ipcMain.handle('win:minimize', () => win?.minimize())
ipcMain.handle('win:toggle-maximize', () => {
  if (!win) return false
  win.isMaximized() ? win.unmaximize() : win.maximize()
  return win.isMaximized()
})
ipcMain.handle('win:close', () => win?.close())
ipcMain.handle('win:is-maximized', () => win?.isMaximized() ?? false)

// ── 后端桥信息 ──
ipcMain.handle('bridge:info', () => ({
  info: bridgeInfo,
  error: bridgeError,
  root: ROOT,
}))
