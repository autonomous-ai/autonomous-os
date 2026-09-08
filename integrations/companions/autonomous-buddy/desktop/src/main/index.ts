import { app, BrowserWindow, dialog, ipcMain, Notification, Menu, shell, clipboard, nativeTheme } from 'electron'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { execFileSync } from 'node:child_process'
import { homedir } from 'node:os'
import { SettingsStore } from './settings'
import { Manager } from './manager'
import { AgentDeviceBridge } from './agent-device-bridge'
import { ProviderUsageService } from './provider-usage'
import { NativeHelper } from './native-helper'
import type { BuddyUpdate, AppearanceSettings } from '../shared/types'

app.setName('Autonomous Buddy')
if (process.env.BUDDY_DATA_DIR) app.setPath('userData', process.env.BUDDY_DATA_DIR)
const rendererPath = join(__dirname, '../renderer/index.html')
let window: BrowserWindow | null = null
let manager: Manager
let settings: SettingsStore
let native: NativeHelper
let agentBridge: AgentDeviceBridge
let deviceConnected = false
const providerUsage = new ProviderUsageService()
let quitting = false
let shutdownComplete = false
const notified = new Map<string, string>()

function publish(update: BuddyUpdate) {
  void agentBridge?.update(update).catch(() => {})

  if (window && !window.isDestroyed()) window.webContents.send('buddy:update', update)
  if (update.type !== 'snapshot') return
  for (const session of update.snapshot.sessions) {
    const prior = notified.get(session.id)
    notified.set(session.id, session.status)
    if (prior === undefined || prior === session.status || window?.isFocused()) continue
    if (['completed', 'needs_input', 'error'].includes(session.status) && Notification.isSupported()) {
      const notice = new Notification({ title: session.title, body: `Agent ${session.status.replace('_', ' ')}` })
      notice.on('click', () => {
        showManager()
        const target = window
        if (!target) return
        const navigate = () => {
          if (!target.isDestroyed()) target.webContents.send('buddy:focusSession', session.id)
        }
        if (target.webContents.isLoadingMainFrame()) target.webContents.once('did-finish-load', navigate)
        else navigate()
      })
      notice.show()
    }
  }
}

function applyAppearance(patch: Partial<AppearanceSettings>) {
  const value = settings.update(patch)
  nativeTheme.themeSource = value.appearance.theme
  if (window && !window.isDestroyed()) {
    window.webContents.setZoomFactor(value.appearance.zoom)
    window.setBackgroundColor(nativeTheme.shouldUseDarkColors ? '#202124' : '#f5f6f8')
    window.webContents.send('buddy:settingsChanged', value)
  }
  return value
}
function zoomBy(delta: number) {
  const current = settings.read().appearance.zoom
  applyAppearance({ zoom: Math.min(1.5, Math.max(0.75, Math.round((current + delta) * 100) / 100)) })
}
function createWindow() {
  window = new BrowserWindow({
    width: 1460,
    height: 920,
    minWidth: 960,
    minHeight: 620,
    title: 'Autonomous Buddy',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#202124' : '#f5f6f8',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 17 },
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  window.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.meta && !input.alt && !input.shift && input.key.toLowerCase() === 'w') {
      event.preventDefault()
      window?.webContents.send('buddy:closeActiveTab')
    }
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) =>
    callback(false),
  )
  window.webContents.setZoomFactor(settings.read().appearance.zoom)
  void window.loadFile(rendererPath)
  window.on('closed', () => {
    window = null
  })
}

function showManager() {
  if (quitting) return
  if (!window) createWindow()
  if (window?.isMinimized()) window.restore()
  window?.show()
  window?.focus()
}

if (!app.requestSingleInstanceLock()) app.quit()
else {
  app.on('second-instance', showManager)
  void app
    .whenReady()
    .then(() => {
      if (app.isPackaged && process.platform === 'darwin') {
        // Finder does not inherit the terminal's nvm/Homebrew/CLI PATH.
        let loginPath = ''
        try {
          const output = execFileSync(
            process.env.SHELL || '/bin/zsh',
            ['-ilc', 'printf "\\n__BUDDY_PATH__%s\\n" "$PATH"'],
            { encoding: 'utf8', timeout: 5000, maxBuffer: 65536, stdio: ['ignore', 'pipe', 'ignore'] },
          )
          loginPath =
            output
              .split('\n')
              .find((line) => line.startsWith('__BUDDY_PATH__'))
              ?.slice(14) ?? ''
        } catch {
          /* Use the inherited PATH and standard install locations when shell setup fails. */
        }
        process.env.PATH = [
          process.env.PATH,
          loginPath,
          join(homedir(), '.local/bin'),
          '/opt/homebrew/bin',
          '/usr/local/bin',
        ]
          .filter(Boolean)
          .join(':')
      }
      settings = new SettingsStore(app.getPath('userData'))
      nativeTheme.themeSource = settings.read().appearance.theme
      manager = new Manager(app.getPath('userData'), publish)
      const nativeExecutable = app.isPackaged
        ? join(process.resourcesPath, 'native/AutonomousBuddy')
        : join(__dirname, '../../../macos/.build/release/AutonomousBuddy')
      agentBridge = new AgentDeviceBridge(manager, (event) => native.agentEvent(event))
      native = new NativeHelper(nativeExecutable, (state) => {
        if (window && !window.isDestroyed()) window.webContents.send('buddy:nativeState', state)
        const connected = state.available && state.connection === 'connected'
        if (connected && !deviceConnected) void agentBridge.reconnect().catch(() => {})
        deviceConnected = connected
      }, undefined, (action) => {
        if (action === 'quit') app.quit()
        else showManager()
      }, (command) => agentBridge.dispatch(command))
      native.start()
      // Only this window's main frame can invoke the finite preload surface.
      const handle = (name: string, callback: (...args: unknown[]) => unknown) => {
        ipcMain.handle(`buddy:${name}`, (event, ...args: unknown[]) => {
          if (
            !window ||
            event.sender !== window.webContents ||
            event.senderFrame !== window.webContents.mainFrame ||
            event.senderFrame.url !== pathToFileURL(rendererPath).href
          ) {
            throw new Error('Untrusted IPC sender')
          }
          return callback(...args)
        })
      }
      handle('addProject', async () => {
        if (!window) return null
        const choice = await dialog.showOpenDialog(window, {
          title: 'Open a project',
          properties: ['openDirectory'],
        })
        return choice.canceled || !choice.filePaths[0] ? null : manager.addProject(choice.filePaths[0])
      })
      handle('settings', () => settings.read())
      handle('updateAppearance', (patch) => applyAppearance(patch as Partial<AppearanceSettings>))
      handle('providerUsage', async (force) => {
        if (force !== undefined && typeof force !== 'boolean') throw new Error('Invalid refresh flag')
        if (process.env.BUDDY_NATIVE_TEST_MODE === '1') return ['claude', 'codex'].map((provider) => ({
          provider, state: 'unavailable', message: 'Account usage is disabled in tests', windows: [], updatedAt: Date.now(),
        }))
        return providerUsage.read(force as boolean | undefined)
      })
      handle('copyWorkspacePath', async (id, selected) => {
        clipboard.writeText(await manager.workspacePath(id as string, selected as string))
      })
      handle('openWorkspace', async (id, selected, target) => {
        const full = await manager.workspacePath(id as string, selected as string)
        if (target === 'finder') {
          const error = await shell.openPath(full)
          if (error) throw new Error(error)
        } else if (target === 'terminal' || target === 'vscode') {
          execFileSync('/usr/bin/open', ['-a', target === 'terminal' ? 'Terminal' : 'Visual Studio Code', full], { timeout: 5000 })
        } else throw new Error('Unsupported workspace application')
      })
      handle('nativeStatus', native.status.bind(native))
      handle('nativeAction', native.action.bind(native) as (...args: unknown[]) => unknown)
      handle('computerCommand', native.command.bind(native) as (...args: unknown[]) => unknown)
      for (const method of [
        'snapshot',
        'removeProject',
        'updateWorkspace',
        'setProjectGroup',
        'sleepWorkspace',
        'removeWorktree',
        'removeSession',
        'worktrees',
        'createWorktree',
        'git',
        'diff',
        'stageFiles', 'unstageFiles', 'commitStaged', 'commitFiles', 'commitDiff',
        'files',
        'readFile',
        'createSession',
        'session',
        'send',
        'stop',
        'closeSession',
        'restartInteractive',
        'renameSession',
        'markRead',
        'terminalWrite',
        'terminalResize',
      ] as const)
        handle(method, manager[method].bind(manager) as (...args: unknown[]) => unknown)
      Menu.setApplicationMenu(
        Menu.buildFromTemplate([
          {
            label: 'Autonomous Buddy',
            submenu: [
              { role: 'about' },
              { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: () => {
                showManager()
                const target = window
                if (!target) return
                const open = () => { if (!target.isDestroyed()) target.webContents.send('buddy:openSettings') }
                if (target.webContents.isLoadingMainFrame()) target.webContents.once('did-finish-load', open)
                else open()
              } },
              { type: 'separator' },
              { role: 'services' },
              { type: 'separator' },
              { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
              { type: 'separator' }, { role: 'quit' },
            ],
          },
          { role: 'editMenu' },
          { label: 'View', submenu: [
            { role: 'reload' }, { role: 'toggleDevTools' }, { type: 'separator' },
            { label: 'Zoom In', accelerator: 'CmdOrCtrl+=', click: () => zoomBy(0.05) },
            { label: 'Zoom Out', accelerator: 'CmdOrCtrl+-', click: () => zoomBy(-0.05) },
            { label: 'Reset Zoom', accelerator: 'CmdOrCtrl+0', click: () => applyAppearance({ zoom: 1 }) },
            { type: 'separator' }, { role: 'togglefullscreen' },
          ] },
          { role: 'windowMenu' },
        ]),
      )
      createWindow()
      app.on('activate', showManager)
    })
    .catch((error: unknown) => {
      console.error(error)
      dialog.showErrorBox('Autonomous Buddy could not start',
        `${error instanceof Error ? error.message : 'Unexpected startup error'}.\n\nIf settings could not load, back up and rename settings.json in ${app.getPath('userData')}, then reopen Buddy. Your existing files are preserved.`)
      app.quit()
    })
  app.on('window-all-closed', () => {
    // On macOS the menu bar keeps device control and agent sessions available.
    if (process.platform !== 'darwin') app.quit()
  })
  process.on('SIGTERM', () => app.quit())
  process.on('SIGINT', () => app.quit())
  app.on('before-quit', (event) => {
    if (shutdownComplete) return
    event.preventDefault()
    if (quitting) return
    quitting = true
    providerUsage.dispose()
    manager?.dispose()
    void (native?.stop() ?? Promise.resolve()).finally(() => {
      shutdownComplete = true
      app.quit()
    })
  })
}
