export type Provider = 'codex' | 'claude' | 'terminal'
export type SessionStatus = 'idle' | 'running' | 'needs_input' | 'completed' | 'error' | 'stopped'
export interface Project {
  id: string
  name: string
  path: string
}
export interface Worktree {
  path: string
  branch: string
  head: string
  primary: boolean
  locked?: boolean
}
export interface Session {
  id: string
  projectId: string
  worktreePath: string
  title: string
  provider: Provider
  providerSessionId?: string
  status: SessionStatus
  createdAt: number
  updatedAt: number
  unread: boolean
}
export interface SessionEvent {
  id: string
  sessionId: string
  seq: number
  at: number
  type: 'prompt' | 'output' | 'status' | 'error' | 'result' | 'terminal'
  text: string
}
export interface GitFile {
  path: string
  status: string
}
export interface GitCommit {
  hash: string
  subject: string
  author: string
  date: string
}
export interface GitSnapshot {
  branch: string
  files: GitFile[]
  commits: GitCommit[]
}
export interface FileEntry {
  name: string
  path: string
  directory: boolean
}
export interface Snapshot {
  projects: Project[]
  sessions: Session[]
  providers: { id: Provider; available: boolean }[]
}
export interface SessionDetail {
  session: Session
  events: SessionEvent[]
}
export interface CreateSession {
  projectId: string
  worktreePath: string
  provider: Provider
  title?: string
}
export type BuddyUpdate = { type: 'snapshot'; snapshot: Snapshot } | { type: 'event'; event: SessionEvent }
export interface BuddyAPI {
  snapshot(): Promise<Snapshot>
  addProject(): Promise<Project | null>
  removeProject(projectId: string): Promise<void>
  worktrees(projectId: string): Promise<Worktree[]>
  createWorktree(projectId: string, branch: string): Promise<Worktree>
  git(projectId: string, worktreePath: string): Promise<GitSnapshot>
  diff(projectId: string, worktreePath: string, file: string): Promise<string>
  files(projectId: string, worktreePath: string, relativePath: string): Promise<FileEntry[]>
  readFile(projectId: string, worktreePath: string, relativePath: string): Promise<string>
  createSession(input: CreateSession): Promise<Session>
  session(id: string): Promise<SessionDetail>
  send(id: string, prompt: string): Promise<void>
  stop(id: string): Promise<void>
  renameSession(id: string, title: string): Promise<void>
  markRead(id: string): Promise<void>
  terminalWrite(id: string, data: string): Promise<void>
  terminalResize(id: string, cols: number, rows: number): Promise<void>
  onUpdate(listener: (update: BuddyUpdate) => void): () => void
}
