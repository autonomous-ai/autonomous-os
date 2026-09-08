import { app, BrowserWindow, dialog, ipcMain, Notification, Menu } from 'electron'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { Manager } from './manager'
import type { BuddyUpdate } from '../shared/types'

app.setName('Autonomous Buddy')
if (process.env.BUDDY_DATA_DIR) app.setPath('userData', process.env.BUDDY_DATA_DIR)
const rendererPath = join(__dirname, '../renderer/index.html')
let window: BrowserWindow | null = null
let manager: Manager
const notified = new Map<string, string>()

function publish(update: BuddyUpdate) {
  if (window && !window.isDestroyed()) window.webContents.send('buddy:update', update)
  if (update.type !== 'snapshot') return
  for (const session of update.snapshot.sessions) {
    const prior = notified.get(session.id)
    notified.set(session.id, session.status)
    if (prior === undefined || prior === session.status || window?.isFocused()) continue
    if (['completed', 'needs_input', 'error'].includes(session.status) && Notification.isSupported()) {
      new Notification({ title: session.title, body: `Agent ${session.status.replace('_', ' ')}` }).show()
    }
  }
}

function createWindow() {
  window = new BrowserWindow({
    width: 1460,
    height: 920,
    minWidth: 960,
    minHeight: 620,
    title: 'Autonomous Buddy',
    backgroundColor: '#202124',
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 17 },
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) =>
    callback(false),
  )
  void window.loadFile(rendererPath)
  window.on('closed', () => {
    window = null
  })
}

if (!app.requestSingleInstanceLock()) app.quit()
else {
  app.on('second-instance', () => {
    window?.show()
    window?.focus()
  })
  void app
    .whenReady()
    .then(() => {
      manager = new Manager(app.getPath('userData'), publish)
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
      for (const method of [
        'snapshot',
        'removeProject',
        'worktrees',
        'createWorktree',
        'git',
        'diff',
        'files',
        'readFile',
        'createSession',
        'session',
        'send',
        'stop',
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
            submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'quit' }],
          },
          { role: 'editMenu' },
          { role: 'viewMenu' },
          { role: 'windowMenu' },
        ]),
      )
      createWindow()
      app.on('activate', () => {
        if (!window) createWindow()
      })
    })
    .catch((error: unknown) => {
      console.error(error)
      app.quit()
    })
  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', () => manager?.dispose())
}
