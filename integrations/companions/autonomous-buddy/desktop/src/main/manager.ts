import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs'
import { realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import type {
  BuddyUpdate,
  CreateSession,
  Project,
  Provider,
  Session,
  SessionDetail,
  SessionEvent,
  SessionStatus,
  Snapshot,
  WorkspaceMeta,
  WorkspacePatch,
} from '../shared/types.js'
import { stageFiles, unstageFiles, commitStaged, commitFiles, commitDiff, fileDiff, fileEntries, fileText, gitCommand, gitSnapshot, listWorktrees } from './git.js'
import { available, launch, parseEvent, type Launcher, type ProcessHandle } from './providers.js'
interface DeviceReceipt {
  fingerprint: string
  result?: unknown
  error?: string
  finished: boolean
}
interface Stored {
  workspaces?: WorkspaceMeta[]
  deviceReceipts?: Record<string, DeviceReceipt>
  version: 1
  projects: Project[]
  sessions: Session[]
  events: Record<string, SessionEvent[]>
}
interface Run {
  process?: ProcessHandle
  buffer: string
  failed: boolean
  completed: boolean
  needsInput: boolean
  stopped: boolean
}
export interface ManagerOptions {
  launch?: Launcher
  available?: (provider: Provider) => boolean
}
export class Manager {
  private state: Stored
  private runs = new Map<string, Run>()
  private pending = new Set<string>()
  private removingWorkspaces = new Set<string>()
  private deviceRequests = new Map<string, Promise<unknown>>()
  private saveTimer?: ReturnType<typeof setTimeout>
  private closed = false
  private readonly stateFile: string
  private readonly launcher: Launcher
  private readonly providerAvailable: (provider: Provider) => boolean
  constructor(
    storageDirectory: string,
    private readonly update: (update: BuddyUpdate) => void,
    options: ManagerOptions = {},
  ) {
    mkdirSync(storageDirectory, { recursive: true, mode: 0o700 })
    this.stateFile = path.join(storageDirectory, 'manager.json')
    this.launcher = options.launch ?? launch
    this.providerAvailable = options.available ?? available
    try {
      this.state = JSON.parse(readFileSync(this.stateFile, 'utf8')) as Stored
      if (
        this.state.version !== 1 ||
        !Array.isArray(this.state.projects) ||
        !Array.isArray(this.state.sessions) ||
        typeof this.state.events !== 'object' ||
        !this.state.events
      )
        throw new Error('Unsupported or invalid manager state')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT')
        throw new Error(`Could not load Buddy sessions: ${(error as Error).message}`)
      this.state = { version: 1, projects: [], sessions: [], events: {} }
    }
    for (const session of this.state.sessions)
      if (session.status === 'running' || session.status === 'needs_input') {
        session.status = 'stopped'
        session.updatedAt = Date.now()
        this.append(session, 'status', 'Stopped: Buddy restarted; send a follow-up to resume this session.')
      }
    this.persist()
  }
  private persist() {
    if (this.saveTimer) {
      clearTimeout(this.saveTimer)
      this.saveTimer = undefined
    }
    writeFileSync(this.stateFile + '.tmp', JSON.stringify(this.state), { mode: 0o600 })
    renameSync(this.stateFile + '.tmp', this.stateFile)
  }
  private scheduleSave() {
    this.saveTimer ??= setTimeout(() => this.persist(), 150)
  }
  private snapshotValue(): Snapshot {
    return structuredClone({
      workspaces: this.state.workspaces ?? [],
      projects: this.state.projects,
      sessions: this.state.sessions,
      providers: (['codex', 'claude', 'terminal'] as Provider[]).map((id) => ({
        id,
        available: this.providerAvailable(id),
      })),
    })
  }
  private broadcast() {
    this.update({ type: 'snapshot', snapshot: this.snapshotValue() })
  }
  private findProject(id: string) {
    const project = this.state.projects.find((v) => v.id === id)
    if (!project) throw new Error('Unknown project')
    return project
  }
  private findSession(id: string) {
    const session = this.state.sessions.find((v) => v.id === id)
    if (!session) throw new Error('Unknown session')
    return session
  }
  private append(session: Session, type: SessionEvent['type'], text: string) {
    const events = (this.state.events[session.id] ??= [])
    const event: SessionEvent = {
      id: randomUUID(),
      sessionId: session.id,
      seq: (events.at(-1)?.seq ?? 0) + 1,
      at: Date.now(),
      type,
      text: text.slice(-65536),
    }
    events.push(event)
    // Bound both event count and total retained output per session.
    let size = events.reduce((sum, item) => sum + item.text.length, 0)
    while (events.length > 2000 || size > 2 * 1024 * 1024) size -= events.shift()!.text.length
    session.updatedAt = event.at
    this.scheduleSave()
    this.update({ type: 'event', event: structuredClone(event) })
  }
  private status(session: Session, status: SessionStatus) {
    session.status = status
    if (status === 'completed' || status === 'needs_input' || status === 'error') session.unread = true
    this.append(session, 'status', status)
    this.broadcast()
  }
  private async workspace(projectId: string, selected: string): Promise<string> {
    const project = this.findProject(projectId)
    const actual = await realpath(selected)
    const permitted = await listWorktrees(project.path)
    for (const tree of permitted) if ((await realpath(tree.path).catch(() => '')) === actual) return actual
    throw new Error('Workspace is not a worktree of this project')
  }
  async deviceRequest(id: string, fingerprint: string, execute: () => Promise<unknown>): Promise<unknown> {
    if (!/^[a-zA-Z0-9_-]{8,128}$/.test(id)) throw new Error('request_id must contain 8–128 letters, numbers, dashes or underscores')
    const receipts = this.state.deviceReceipts ??= {}
    const prior = Object.hasOwn(receipts, id) ? receipts[id] : undefined
    if (prior) {
      if (prior.fingerprint !== fingerprint) throw new Error('request_id was already used for different parameters')
      const pending = this.deviceRequests.get(id)
      if (pending) return pending
      if (!prior.finished) throw new Error('Previous request was interrupted; inspect the session before issuing a new request')
      if (prior.error) throw new Error(prior.error)
      return structuredClone(prior.result)
    }
    if (Object.keys(receipts).length >= 10000) throw new Error('Device request history is full')
    const receipt: DeviceReceipt = { fingerprint, finished: false }
    Object.defineProperty(receipts, id, { value: receipt, enumerable: true, writable: true, configurable: true })
    // Reserve durably before effects. A crash is uncertain, never an automatic replay.
    this.persist()
    const request = Promise.resolve().then(execute).then((result) => {
      receipt.result = result
      receipt.finished = true
      this.persist()
      return structuredClone(result)
    }, (error: unknown) => {
      receipt.error = error instanceof Error ? error.message : String(error)
      receipt.finished = true
      this.persist()
      throw error
    }).finally(() => this.deviceRequests.delete(id))
    this.deviceRequests.set(id, request)
    return request
  }
  async workspacePath(projectId: string, selected: string) {
    return this.workspace(projectId, selected)
  }
  async setProjectGroup(projectId: string, groupName: string | null) {
    const project = this.findProject(projectId)
    if (groupName !== null && (typeof groupName !== 'string' || !groupName.trim() || groupName.length > 80))
      throw new Error('Group name must contain 1–80 characters')
    project.group = groupName?.trim() || undefined
    this.persist()
    this.broadcast()
  }
  async updateWorkspace(projectId: string, selected: string, patch: WorkspacePatch) {
    const worktreePath = await this.workspace(projectId, selected)
    if (!patch || typeof patch !== 'object' || Array.isArray(patch) ||
        Object.keys(patch).some((key) => !['pinned', 'status', 'unread', 'displayName', 'parentWorktreePath'].includes(key)) ||
        (patch.pinned !== undefined && typeof patch.pinned !== 'boolean') ||
        (patch.unread !== undefined && typeof patch.unread !== 'boolean') ||
        (patch.status !== undefined && !['active', 'review', 'done'].includes(patch.status)))
      throw new Error('Invalid workspace update')
    if (patch.displayName !== undefined && (typeof patch.displayName !== 'string' || patch.displayName.length > 120))
      throw new Error('Workspace name must be at most 120 characters')
    if (patch.parentWorktreePath !== undefined && patch.parentWorktreePath !== null) {
      if (typeof patch.parentWorktreePath !== 'string') throw new Error('Invalid parent workspace')
      const parent = await this.workspace(projectId, patch.parentWorktreePath)
      const visited = new Set([worktreePath])
      let cursor: string | null | undefined = parent
      while (cursor) {
        if (visited.has(cursor)) throw new Error('Workspace parent would create a cycle')
        visited.add(cursor)
        cursor = (this.state.workspaces ?? []).find((item) => item.projectId === projectId && item.worktreePath === cursor)?.parentWorktreePath
      }
      patch = { ...patch, parentWorktreePath: parent }
    }
    const workspaces = this.state.workspaces ??= []
    let metadata = workspaces.find((item) => item.projectId === projectId && item.worktreePath === worktreePath)
    if (!metadata) {
      metadata = { projectId, worktreePath, pinned: false, unread: false, status: 'active' }
      workspaces.push(metadata)
    }
    Object.assign(metadata, patch)
    this.persist()
    this.broadcast()
  }
  async sleepWorkspace(projectId: string, selected: string) {
    const full = await this.workspace(projectId, selected)
    const sessions = this.state.sessions.filter((item) => item.projectId === projectId && item.worktreePath === full)
    if (sessions.some((item) => this.pending.has(item.id))) throw new Error('A session is starting; try Sleep again')
    for (const session of sessions) await this.stop(session.id)
  }
  async removeSession(id: string) {
    this.findSession(id)
    if (this.runs.has(id) || this.pending.has(id)) throw new Error('Stop the session before deleting it')
    this.state.sessions = this.state.sessions.filter((item) => item.id !== id)
    delete this.state.events[id]
    this.persist()
    this.broadcast()
  }
  async removeWorktree(projectId: string, selected: string) {
    const project = this.findProject(projectId)
    const full = await this.workspace(projectId, selected)
    if (this.removingWorkspaces.has(full)) throw new Error('Workspace removal is already in progress')
    this.removingWorkspaces.add(full)
    try {
      const trees = await listWorktrees(project.path)
      const tree = trees.find((item) => item.path === full)
      if (!tree || tree.primary || tree.locked) throw new Error('Cannot remove the primary or locked worktree')
      if (full === project.path) throw new Error('Cannot remove the registered project root')
      if (this.state.sessions.some((item) => item.worktreePath === full && (this.runs.has(item.id) || this.pending.has(item.id))))
        throw new Error('Stop the workspace sessions before deleting it')
      if ((await gitCommand(full, ['status', '--porcelain', '--untracked-files=all'])).trim())
        throw new Error('Worktree has changes; commit or preserve them before removal')
      await gitCommand(project.path, ['worktree', 'remove', '--', full])
      // Keep session history, even though its worktree is now unavailable.
      this.state.workspaces = (this.state.workspaces ?? []).filter((item) => item.worktreePath !== full)
      for (const metadata of this.state.workspaces)
        if (metadata.parentWorktreePath === full) metadata.parentWorktreePath = null
      this.persist()
      this.broadcast()
    } finally { this.removingWorkspaces.delete(full) }
  }
  async snapshot() {
    return this.snapshotValue()
  }
  async addProject(directory: string): Promise<Project> {
    const full = await realpath(directory)
    if (!(await stat(full)).isDirectory()) throw new Error('Project must be a directory')
    const existing = this.state.projects.find((v) => v.path === full)
    if (existing) return structuredClone(existing)
    const project = { id: randomUUID(), name: path.basename(full), path: full }
    this.state.projects.push(project)
    this.persist()
    this.broadcast()
    return structuredClone(project)
  }
  async removeProject(id: string) {
    this.findProject(id)
    if (
      this.state.sessions.some((v) => v.projectId === id && (this.runs.has(v.id) || this.pending.has(v.id)))
    )
      throw new Error('Stop this project’s sessions before removing it')
    for (const session of this.state.sessions.filter((v) => v.projectId === id))
      delete this.state.events[session.id]
    this.state.sessions = this.state.sessions.filter((v) => v.projectId !== id)
    this.state.projects = this.state.projects.filter((v) => v.id !== id)
    this.state.workspaces = (this.state.workspaces ?? []).filter((item) => item.projectId !== id)
    this.persist()
    this.broadcast()
  }
  async worktrees(projectId: string) {
    return listWorktrees(this.findProject(projectId).path)
  }
  async createWorktree(projectId: string, branch: string) {
    const project = this.findProject(projectId)
    if (typeof branch !== 'string' || !branch || branch.startsWith('-') || branch.length > 200)
      throw new Error('Invalid branch name')
    await gitCommand(project.path, ['check-ref-format', '--branch', branch])
    const destination = path.join(
      path.dirname(project.path),
      `${path.basename(project.path)}-${branch.replace(/[^a-zA-Z0-9_-]/g, '-')}-${randomUUID().slice(0, 6)}`,
    )
    await gitCommand(project.path, ['worktree', 'add', '-b', branch, destination])
    const tree = (await this.worktrees(projectId)).find((v) => v.path === destination)
    if (!tree) throw new Error('Created worktree was not found')
    return tree
  }
  async git(projectId: string, workspace: string) {
    return gitSnapshot(await this.workspace(projectId, workspace))
  }
  async stageFiles(projectId: string, selected: string, files: string[]) {
    return stageFiles(await this.workspace(projectId, selected), files)
  }
  async unstageFiles(projectId: string, selected: string, files: string[]) {
    return unstageFiles(await this.workspace(projectId, selected), files)
  }
  async commitStaged(projectId: string, selected: string, message: string) {
    return commitStaged(await this.workspace(projectId, selected), message)
  }
  async commitFiles(projectId: string, selected: string, hash: string) {
    return commitFiles(await this.workspace(projectId, selected), hash)
  }
  async commitDiff(projectId: string, selected: string, hash: string, file: string) {
    return commitDiff(await this.workspace(projectId, selected), hash, file)
  }
  async diff(projectId: string, workspace: string, file: string) {
    return fileDiff(await this.workspace(projectId, workspace), file)
  }
  async files(projectId: string, workspace: string, relative: string) {
    return fileEntries(await this.workspace(projectId, workspace), relative)
  }
  async readFile(projectId: string, workspace: string, relative: string) {
    return fileText(await this.workspace(projectId, workspace), relative)
  }
  async createSession(input: CreateSession): Promise<Session> {
    if (!['codex', 'claude', 'terminal'].includes(input.provider)) throw new Error('Unsupported provider')
    if (!this.providerAvailable(input.provider))
      throw new Error(`${input.provider} is not installed or is not on PATH`)
    const workspace = await this.workspace(input.projectId, input.worktreePath)
    this.findProject(input.projectId)
    if (this.removingWorkspaces.has(workspace)) throw new Error('Workspace is being removed')
    const now = Date.now()
    const session: Session = {
      id: randomUUID(),
      projectId: input.projectId,
      worktreePath: workspace,
      title:
        input.title?.trim().slice(0, 120) || (input.provider === 'terminal' ? 'Terminal' : 'New session'),
      provider: input.provider,
      status: 'idle',
      createdAt: now,
      updatedAt: now,
      unread: false,
    }
    this.state.sessions.push(session)
    this.state.events[session.id] = []
    this.persist()
    this.broadcast()
    if (session.provider === 'terminal') await this.start(session, '')
    return structuredClone(session)
  }
  async session(id: string): Promise<SessionDetail> {
    return structuredClone({ session: this.findSession(id), events: this.state.events[id] ?? [] })
  }
  async renameSession(id: string, title: string) {
    if (typeof title !== 'string' || !title.trim()) throw new Error('Title is required')
    const session = this.findSession(id)
    session.title = title.trim().slice(0, 120)
    session.updatedAt = Date.now()
    this.persist()
    this.broadcast()
  }
  async markRead(id: string) {
    this.findSession(id).unread = false
    this.scheduleSave()
    this.broadcast()
  }
  async send(id: string, prompt: string) {
    const session = this.findSession(id)
    if (session.provider === 'terminal') throw new Error('Use the terminal to interact with this session')
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 100000)
      throw new Error('Prompt must contain 1–100000 characters')
    if (this.runs.has(id) || this.pending.has(id)) throw new Error('Session is already running')
    if (this.removingWorkspaces.has(session.worktreePath)) throw new Error('Workspace is being removed')
    this.pending.add(id)
    try {
      await this.workspace(session.projectId, session.worktreePath)
      // Never silently start a new conversation after a turn failed to yield an ID.
      if (!session.providerSessionId && (this.state.events[id] ?? []).some((v) => v.type === 'prompt'))
        throw new Error('Provider did not return a session ID. Create a new session to avoid losing context.')
      this.append(session, 'prompt', prompt)
      if (session.title === 'New session') session.title = prompt.trim().slice(0, 70)
      await this.start(session, prompt)
    } finally {
      this.pending.delete(id)
    }
  }
  private async start(session: Session, prompt: string) {
    if (this.closed) throw new Error('Buddy is shutting down')
    const run: Run = { buffer: '', failed: false, completed: false, needsInput: false, stopped: false }
    this.runs.set(session.id, run)
    session.unread = false
    this.status(session, 'running')
    this.persist()
    const consume = (line: string) => {
      const event = parseEvent(line, session.provider)
      if (event.sessionId) {
        if (session.providerSessionId && session.providerSessionId !== event.sessionId) {
          run.failed = true
          this.append(
            session,
            'error',
            'Provider returned a different session ID; refusing to change conversation.',
          )
          run.process?.kill()
          return
        }
        session.providerSessionId = event.sessionId
        this.persist()
        this.broadcast()
      }
      if (event.text) this.append(session, event.kind ?? 'output', event.text)
      run.failed ||= !!event.failed
      run.completed ||= !!event.completed
      run.needsInput ||= !!event.needsInput
    }
    const exit = (code: number) => {
      if (this.runs.get(session.id) !== run) return
      if (run.buffer) {
        consume(run.buffer)
        run.buffer = ''
      }
      this.runs.delete(session.id)
      const status: SessionStatus = run.stopped
        ? 'stopped'
        : session.provider === 'terminal'
          ? code === 0
            ? 'stopped'
            : 'error'
          : run.needsInput
            ? 'needs_input'
            : code !== 0 || run.failed || !run.completed
              ? 'error'
              : 'completed'
      if (status === 'error' && !run.failed)
        this.append(
          session,
          'error',
          `Process exited (${code})${!run.completed && session.provider !== 'terminal' ? ' without a completion event' : ''}.`,
        )
      this.status(session, status)
      this.persist()
    }
    try {
      run.process = await this.launcher({
        provider: session.provider,
        cwd: session.worktreePath,
        sessionId: session.providerSessionId,
        prompt,
        diagnostic: (text) => {
          if (this.runs.get(session.id) === run) this.append(session, 'output', text)
        },
        data: (text) => {
          if (this.runs.get(session.id) !== run) return
          if (session.provider === 'terminal') {
            this.append(session, 'terminal', text)
            return
          }
          run.buffer += text
          let index: number
          while ((index = run.buffer.indexOf('\n')) >= 0) {
            const line = run.buffer.slice(0, index)
            run.buffer = run.buffer.slice(index + 1)
            if (line.trim()) consume(line)
          }
          if (run.buffer.length > 1024 * 1024) {
            this.append(session, 'error', 'Provider event exceeded 1 MiB.')
            run.failed = true
            run.buffer = ''
            run.process?.kill()
          }
        },
        error: (error) => {
          if (this.runs.get(session.id) === run) {
            run.failed = true
            this.append(session, 'error', error.message)
            exit(1)
          }
        },
        exit,
      })
      if (run.stopped || this.closed) run.process.kill()
    } catch (error) {
      run.failed = true
      this.append(session, 'error', (error as Error).message)
      exit(1)
    }
  }
  async stop(id: string) {
    const session = this.findSession(id)
    const run = this.runs.get(id)
    if (run) {
      run.stopped = true
      run.process?.kill()
    } else if (this.pending.has(id)) throw new Error('Session is starting; try Stop again')
    else if (session.status !== 'stopped') {
      this.status(session, 'stopped')
      this.persist()
    }
  }
  async terminalWrite(id: string, data: string) {
    const session = this.findSession(id)
    if (session.provider !== 'terminal' || typeof data !== 'string' || data.length > 65536)
      throw new Error('Invalid terminal input')
    const process = this.runs.get(id)?.process
    if (!process) throw new Error('Terminal has exited. Create a new terminal session.')
    process.write(data)
  }
  async terminalResize(id: string, cols: number, rows: number) {
    if (
      this.findSession(id).provider !== 'terminal' ||
      !Number.isInteger(cols) ||
      !Number.isInteger(rows) ||
      cols < 2 ||
      rows < 2 ||
      cols > 1000 ||
      rows > 1000
    )
      throw new Error('Invalid terminal dimensions')
    this.runs.get(id)?.process?.resize?.(cols, rows)
  }
  dispose() {
    this.closed = true
    for (const [id, run] of this.runs) {
      run.stopped = true
      run.process?.kill()
      this.findSession(id).status = 'stopped'
    }
    this.runs.clear()
    this.persist()
  }
}
