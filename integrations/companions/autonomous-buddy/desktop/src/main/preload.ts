import { contextBridge, ipcRenderer } from 'electron'
import type { BuddyAPI, BuddyUpdate, NativeState } from '../shared/types'

const api: BuddyAPI = {
  providerUsage: (refresh) => ipcRenderer.invoke('buddy:providerUsage', refresh),
  onCloseActiveTab: (listener) => {
    const handler = () => listener()
    ipcRenderer.on('buddy:closeActiveTab', handler)
    return () => ipcRenderer.removeListener('buddy:closeActiveTab', handler)
  },
  setProjectGroup: (id, group) => ipcRenderer.invoke('buddy:setProjectGroup', id, group),
  nativeStatus: () => ipcRenderer.invoke('buddy:nativeStatus'),
  nativeAction: (action, params) => ipcRenderer.invoke('buddy:nativeAction', action, params),
  computerCommand: (action, params) => ipcRenderer.invoke('buddy:computerCommand', action, params),
  onNativeState: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, state: NativeState) => listener(state)
    ipcRenderer.on('buddy:nativeState', handler)
    return () => ipcRenderer.removeListener('buddy:nativeState', handler)
  },
  snapshot: () => ipcRenderer.invoke('buddy:snapshot'),
  addProject: () => ipcRenderer.invoke('buddy:addProject'),
  removeProject: (id) => ipcRenderer.invoke('buddy:removeProject', id),
  updateWorkspace: (id, path, patch) => ipcRenderer.invoke('buddy:updateWorkspace', id, path, patch),
  sleepWorkspace: (id, path) => ipcRenderer.invoke('buddy:sleepWorkspace', id, path),
  removeWorktree: (id, path) => ipcRenderer.invoke('buddy:removeWorktree', id, path),
  openWorkspace: (id, path, target) => ipcRenderer.invoke('buddy:openWorkspace', id, path, target),
  copyWorkspacePath: (id, path) => ipcRenderer.invoke('buddy:copyWorkspacePath', id, path),
  removeSession: (id) => ipcRenderer.invoke('buddy:removeSession', id),
  worktrees: (id) => ipcRenderer.invoke('buddy:worktrees', id),
  createWorktree: (id, branch) => ipcRenderer.invoke('buddy:createWorktree', id, branch),
  git: (id, path) => ipcRenderer.invoke('buddy:git', id, path),
  diff: (id, path, file) => ipcRenderer.invoke('buddy:diff', id, path, file),
  files: (id, path, relative) => ipcRenderer.invoke('buddy:files', id, path, relative),
  readFile: (id, path, relative) => ipcRenderer.invoke('buddy:readFile', id, path, relative),
  createSession: (input) => ipcRenderer.invoke('buddy:createSession', input),
  session: (id) => ipcRenderer.invoke('buddy:session', id),
  send: (id, prompt) => ipcRenderer.invoke('buddy:send', id, prompt),
  stop: (id) => ipcRenderer.invoke('buddy:stop', id),
  renameSession: (id, title) => ipcRenderer.invoke('buddy:renameSession', id, title),
  markRead: (id) => ipcRenderer.invoke('buddy:markRead', id),
  terminalWrite: (id, data) => ipcRenderer.invoke('buddy:terminalWrite', id, data),
  terminalResize: (id, cols, rows) => ipcRenderer.invoke('buddy:terminalResize', id, cols, rows),
  onUpdate: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, update: BuddyUpdate) => listener(update)
    ipcRenderer.on('buddy:update', handler)
    return () => ipcRenderer.removeListener('buddy:update', handler)
  },
}
contextBridge.exposeInMainWorld('buddy', api)
