import { afterEach, describe, expect, it } from 'vitest'
import { mkdtemp, writeFile, mkdir, symlink, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { Manager } from '../src/main/manager.js'
import { gitCommand } from '../src/main/git.js'
import { invocation, parseEvent, type LaunchOptions, type ProcessHandle } from '../src/main/providers.js'
const roots: string[] = []
const managers: Manager[] = []
afterEach(async () => {
  for (const manager of managers.splice(0)) manager.dispose()
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})
async function setup(git = true) {
  const root = await mkdtemp(path.join(tmpdir(), 'buddy-backend-'))
  roots.push(root)
  const projectPath = path.join(root, 'project')
  await mkdir(projectPath)
  if (git) {
    await gitCommand(projectPath, ['init'])
    await gitCommand(projectPath, ['config', 'user.email', 'test@example.com'])
    await gitCommand(projectPath, ['config', 'user.name', 'Test'])
    await writeFile(path.join(projectPath, 'file.txt'), 'original\n')
    await gitCommand(projectPath, ['add', 'file.txt'])
    await gitCommand(projectPath, ['commit', '-m', 'Initial'])
  }
  const calls: LaunchOptions[] = []
  const killed: boolean[] = []
  const writes: string[] = []
  const sizes: number[][] = []
  const launch = async (options: LaunchOptions): Promise<ProcessHandle> => {
    const index = calls.push(options) - 1
    return {
      write: (data) => writes.push(data),
      resize: (cols, rows) => sizes.push([cols, rows]),
      kill: () => {
        killed[index] = true
        options.exit(0)
      },
    }
  }
  const storage = path.join(root, 'state')
  const manager = new Manager(storage, () => {}, { launch, available: () => true })
  managers.push(manager)
  const project = await manager.addProject(projectPath)
  const session = await manager.createSession({
    projectId: project.id,
    worktreePath: project.path,
    provider: 'codex',
  })
  return { root, projectPath, storage, manager, project, session, calls, killed, writes, sizes, launch }
}
describe('persistent agent sessions', () => {
  it('routes fragmented output and follow-ups to the exact provider session across restart', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'first prompt')
    f.calls[0].data('{"type":"thread.star')
    f.calls[0].data(
      'ted","thread_id":"thread-one"}\n{"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}\n{"type":"turn.completed"}\n',
    )
    f.calls[0].exit(0)
    expect((await f.manager.session(f.session.id)).session.status).toBe('completed')
    f.manager.dispose()
    const restored = new Manager(f.storage, () => {}, { launch: f.launch, available: () => true })
    managers.push(restored)
    await restored.send(f.session.id, 'follow up')
    expect(f.calls[1].sessionId).toBe('thread-one')
    expect(f.calls[1].prompt).toBe('follow up')
    expect((await restored.session(f.session.id)).events.some((v) => v.text.includes('Hello'))).toBe(true)
  })
  it('keeps stderr diagnostics outside fragmented stdout JSON frames', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'inspect')
    f.calls[0].data('{"type":"thread.star')
    f.calls[0].diagnostic('Provider startup warning\n')
    f.calls[0].data('ted","thread_id":"unbroken-thread"}\n{"type":"turn.completed"}\n')
    f.calls[0].exit(0)
    const detail = await f.manager.session(f.session.id)
    expect(detail.session.providerSessionId).toBe('unbroken-thread')
    expect(detail.session.status).toBe('completed')
    expect(detail.events.some((event) => event.text.includes('startup warning'))).toBe(true)
  })
  it('keeps concurrent sessions isolated and refuses overlapping turns', async () => {
    const f = await setup()
    const other = await f.manager.createSession({
      projectId: f.project.id,
      worktreePath: f.project.path,
      provider: 'codex',
    })
    await Promise.all([f.manager.send(f.session.id, 'one'), f.manager.send(other.id, 'two')])
    await expect(f.manager.send(f.session.id, 'overlap')).rejects.toThrow('already running')
    const one = f.calls.find((v) => v.prompt === 'one')!
    const two = f.calls.find((v) => v.prompt === 'two')!
    one.data('{"type":"thread.started","thread_id":"one"}\n')
    two.data('{"type":"thread.started","thread_id":"two"}\n')
    two.data('{"type":"turn.completed"}\n')
    two.exit(0)
    expect((await f.manager.session(f.session.id)).session.status).toBe('running')
    expect((await f.manager.session(other.id)).session.providerSessionId).toBe('two')
    await expect(f.manager.removeProject(f.project.id)).rejects.toThrow('Stop')
  })
  it('reports missing completion, provider errors, and refuses lost context', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'hello')
    f.calls[0].data('{"type":"error","message":"Authentication required"}\n')
    f.calls[0].exit(1)
    const detail = await f.manager.session(f.session.id)
    expect(detail.session.status).toBe('error')
    expect(detail.events.some((v) => v.text === 'Authentication required')).toBe(true)
    await expect(f.manager.send(f.session.id, 'again')).rejects.toThrow('session ID')
  })
  it('does not silently accept a different conversation on resume', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'one')
    f.calls[0].data('{"type":"thread.started","thread_id":"one"}\n{"type":"turn.completed"}\n')
    f.calls[0].exit(0)
    await f.manager.send(f.session.id, 'two')
    f.calls[1].data('{"type":"thread.started","thread_id":"wrong"}\n')
    expect((await f.manager.session(f.session.id)).session.providerSessionId).toBe('one')
    expect(f.killed[1]).toBe(true)
  })
  it('stops process and recovers interrupted sessions as stopped', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'one')
    await f.manager.stop(f.session.id)
    expect(f.killed[0]).toBe(true)
    expect((await f.manager.session(f.session.id)).session.status).toBe('stopped')
    const raw = JSON.parse(
      await (await import('node:fs/promises')).readFile(path.join(f.storage, 'manager.json'), 'utf8'),
    )
    raw.sessions[0].status = 'running'
    await writeFile(path.join(f.storage, 'manager.json'), JSON.stringify(raw))
    const restored = new Manager(f.storage, () => {})
    managers.push(restored)
    expect((await restored.session(f.session.id)).session.status).toBe('stopped')
  })
  it('preserves Claude ID and explicit permission-denial attention', async () => {
    const f = await setup()
    const session = await f.manager.createSession({
      projectId: f.project.id,
      worktreePath: f.project.path,
      provider: 'claude',
    })
    await f.manager.send(session.id, 'hello')
    f.calls[0].data(
      '{"type":"system","subtype":"init","session_id":"claude-one"}\n{"type":"result","result":"Permission needed","permission_denials":[{}]}\n',
    )
    f.calls[0].exit(0)
    expect((await f.manager.session(session.id)).session.status).toBe('needs_input')
    await f.manager.send(session.id, 'continue')
    expect(f.calls[1].sessionId).toBe('claude-one')
  })
  it('bounds terminal history while keeping event sequence monotonic', async () => {
    const f = await setup()
    const terminal = await f.manager.createSession({
      projectId: f.project.id,
      worktreePath: f.project.path,
      provider: 'terminal',
    })
    for (let i = 0; i < 2100; i++) f.calls[0].data('x'.repeat(1100))
    const events = (await f.manager.session(terminal.id)).events
    expect(events.length).toBeLessThanOrEqual(2000)
    expect(events.reduce((sum, event) => sum + event.text.length, 0)).toBeLessThanOrEqual(2 * 1024 * 1024)
    expect(events.at(-1)!.seq).toBe(2101)
    expect(events[0].seq).toBeGreaterThan(1)
  })
  it('treats a clean process exit without a completion event as an error', async () => {
    const f = await setup()
    await f.manager.send(f.session.id, 'one')
    f.calls[0].data('{"type":"thread.started","thread_id":"one"}\n')
    f.calls[0].exit(0)
    const detail = await f.manager.session(f.session.id)
    expect(detail.session.status).toBe('error')
    expect(detail.events.some((event) => event.text.includes('without a completion event'))).toBe(true)
  })
  it('supports real terminal lifecycle contract without silence-based completion', async () => {
    const f = await setup()
    const terminal = await f.manager.createSession({
      projectId: f.project.id,
      worktreePath: f.project.path,
      provider: 'terminal',
    })
    f.calls[0].data('shell output\r\n')
    await f.manager.terminalWrite(terminal.id, 'pwd\r')
    await f.manager.terminalResize(terminal.id, 80, 24)
    expect(f.writes).toEqual(['pwd\r'])
    expect(f.sizes).toEqual([[80, 24]])
    expect((await f.manager.session(terminal.id)).session.status).toBe('running')
    f.calls[0].exit(0)
    expect((await f.manager.session(terminal.id)).session.status).toBe('stopped')
    await expect(f.manager.terminalWrite(terminal.id, 'x')).rejects.toThrow('exited')
  })
})
describe('workspace and Git boundaries', () => {
  it('reads actual status, history, staged and unstaged diff, and untracked files', async () => {
    const f = await setup()
    await writeFile(path.join(f.projectPath, 'file.txt'), 'staged\n')
    await gitCommand(f.projectPath, ['add', 'file.txt'])
    await writeFile(path.join(f.projectPath, 'file.txt'), 'unstaged\n')
    await writeFile(path.join(f.projectPath, 'new.txt'), 'new content\n')
    const git = await f.manager.git(f.project.id, f.project.path)
    expect(git.commits[0].subject).toBe('Initial')
    expect(git.files).toContainEqual({ path: 'file.txt', status: 'MM' })
    expect(git.files).toContainEqual({ path: 'new.txt', status: '??' })
    expect(await f.manager.diff(f.project.id, f.project.path, 'file.txt')).toContain('+unstaged')
    expect(await f.manager.diff(f.project.id, f.project.path, 'new.txt')).toContain('new content')
  })
  it('creates and discovers an explicit worktree, and rejects unrelated directories', async () => {
    const f = await setup()
    expect(await f.manager.worktrees(f.project.id)).toHaveLength(1)
    const tree = await f.manager.createWorktree(f.project.id, 'feature/test')
    expect(tree.branch).toBe('feature/test')
    expect(await f.manager.worktrees(f.project.id)).toHaveLength(2)
    expect(await f.manager.readFile(f.project.id, tree.path, 'file.txt')).toBe('original\n')
    await expect(f.manager.files(f.project.id, f.root, '')).rejects.toThrow('not a worktree')
    await expect(f.manager.createWorktree(f.project.id, '--bad')).rejects.toThrow('Invalid')
  })
  it('rejects traversal, external symlinks, binary and oversized previews', async () => {
    const f = await setup()
    await writeFile(path.join(f.root, 'private'), 'secret')
    await symlink(path.join(f.root, 'private'), path.join(f.projectPath, 'escape'))
    await expect(f.manager.readFile(f.project.id, f.project.path, '../private')).rejects.toThrow('escapes')
    await expect(f.manager.readFile(f.project.id, f.project.path, 'escape')).rejects.toThrow('symbolic link')
    await expect(f.manager.diff(f.project.id, f.project.path, 'escape')).rejects.toThrow('symbolic link')
    await writeFile(path.join(f.projectPath, 'binary'), Buffer.from([0, 1]))
    await expect(f.manager.readFile(f.project.id, f.project.path, 'binary')).rejects.toThrow('Binary')
    await writeFile(path.join(f.projectPath, 'large'), Buffer.alloc(1024 * 1024 + 1, 65))
    await expect(f.manager.readFile(f.project.id, f.project.path, 'large')).rejects.toThrow('1 MiB')
  })
  it('supports non-Git project registry and folder browsing', async () => {
    const f = await setup(false)
    await writeFile(path.join(f.projectPath, 'notes.md'), 'hello')
    expect((await f.manager.git(f.project.id, f.project.path)).files).toEqual([])
    expect(await f.manager.files(f.project.id, f.project.path, '')).toContainEqual({
      name: 'notes.md',
      path: 'notes.md',
      directory: false,
    })
    expect((await f.manager.addProject(f.projectPath)).id).toBe(f.project.id)
  })
})
it('uses no-approval CLI mode for new turns and exact-session resumes', () => {
  expect(invocation('codex', 'thread-one').args).toEqual([
    'exec',
    '--dangerously-bypass-approvals-and-sandbox',
    'resume',
    '--skip-git-repo-check',
    '--json',
    'thread-one',
    '-',
  ])
  expect(invocation('codex').args).toContain('--dangerously-bypass-approvals-and-sandbox')
  expect(invocation('claude').args).toContain('--dangerously-skip-permissions')
  expect(invocation('claude', 'claude-one').args).toContain('--resume')
  expect(invocation('claude', 'claude-one').args).toContain('--dangerously-skip-permissions')
  expect(
    parseEvent('{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"hello"}}}', 'claude')
      .text,
  ).toBe('hello')
})
