import { afterEach, expect, it } from 'vitest'
import { mkdtemp, mkdir, writeFile, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Manager } from '../src/main/manager'
import { AgentDeviceBridge } from '../src/main/agent-device-bridge'
import { gitCommand } from '../src/main/git'
import type { LaunchOptions } from '../src/main/providers'

const roots: string[] = []
const managers: Manager[] = []
afterEach(async () => {
  managers.splice(0).forEach((manager) => manager.dispose())
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})
async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'buddy-workspace-'))
  roots.push(root)
  const directory = join(root, 'project')
  await mkdir(directory)
  await gitCommand(directory, ['init'])
  await gitCommand(directory, ['config', 'user.email', 'test@example.com'])
  await gitCommand(directory, ['config', 'user.name', 'Test'])
  await writeFile(join(directory, 'file.txt'), 'initial')
  await gitCommand(directory, ['add', 'file.txt'])
  await gitCommand(directory, ['commit', '-m', 'Initial'])
  const calls: LaunchOptions[] = []
  const options = { available: () => true, launch: async (call: LaunchOptions) => {
    calls.push(call)
    return { write() {}, kill() { call.exit(0) } }
  } }
  const storage = join(root, 'state')
  const manager = new Manager(storage, () => {}, options)
  managers.push(manager)
  const project = await manager.addProject(directory)
  const session = await manager.createSession({ projectId: project.id, worktreePath: directory, provider: 'codex' })
  return { root, storage, options, manager, project, session, calls }
}
it('persists workspace metadata and rejects paths outside the project', async () => {
  const f = await fixture()
  await f.manager.updateWorkspace(f.project.id, f.project.path, { pinned: true, status: 'review', unread: true })
  await expect(f.manager.updateWorkspace(f.project.id, f.root, { pinned: true })).rejects.toThrow('worktree')
  const restored = new Manager(f.storage, () => {}, f.options)
  managers.push(restored)
  expect((await restored.snapshot()).workspaces[0]).toMatchObject({ pinned: true, status: 'review', unread: true })
})
it('sleeps only the selected workspace and refuses unsafe worktree/session removal', async () => {
  const f = await fixture()
  const tree = await f.manager.createWorktree(f.project.id, 'feat/sidebar')
  const secondary = await f.manager.createSession({ projectId: f.project.id, worktreePath: tree.path, provider: 'codex' })
  await f.manager.send(f.session.id, 'primary')
  await f.manager.send(secondary.id, 'secondary')
  await expect(f.manager.removeWorktree(f.project.id, tree.path)).rejects.toThrow('Stop')
  await f.manager.sleepWorkspace(f.project.id, tree.path)
  expect((await f.manager.session(secondary.id)).session.status).toBe('stopped')
  expect((await f.manager.session(f.session.id)).session.status).toBe('running')
  await expect(f.manager.removeWorktree(f.project.id, f.project.path)).rejects.toThrow('primary')
  await writeFile(join(tree.path, 'dirty.txt'), 'preserve')
  await expect(f.manager.removeWorktree(f.project.id, tree.path)).rejects.toThrow('changes')
  await rm(join(tree.path, 'dirty.txt'))
  await f.manager.removeWorktree(f.project.id, tree.path)
  expect(await f.manager.worktrees(f.project.id)).toHaveLength(1)
  expect((await f.manager.session(secondary.id)).events.some((event) => event.type === 'prompt')).toBe(true)
  await expect(f.manager.removeSession(f.session.id)).rejects.toThrow('Stop')
  await f.manager.removeSession(secondary.id)
  await expect(f.manager.session(secondary.id)).rejects.toThrow('Unknown')
})
it('deduplicates concurrent and restarted voice prompts and resumes exact provider context', async () => {
  const f = await fixture()
  let bridge = new AgentDeviceBridge(f.manager, async () => {})
  const params = { project_id: f.project.id, session_id: f.session.id, request_id: 'request-first', prompt: 'first' }
  const command = { id: 'one', action: 'agent.send', params }
  const replies = await Promise.all([bridge.dispatch(command), bridge.dispatch({ ...command, id: 'two' })])
  expect(replies.every((reply) => reply.ok)).toBe(true)
  expect(f.calls).toHaveLength(1)
  f.calls[0].data('{"type":"thread.started","thread_id":"voice-thread"}\n{"type":"turn.completed"}\n')
  f.calls[0].exit(0)
  f.manager.dispose()
  const restored = new Manager(f.storage, () => {}, f.options)
  managers.push(restored)
  bridge = new AgentDeviceBridge(restored, async () => {})
  expect((await bridge.dispatch(command)).ok).toBe(true)
  expect(f.calls).toHaveLength(1)
  expect((await bridge.dispatch({ ...command, params: { ...params, prompt: 'changed' } })).ok).toBe(false)
  expect((await bridge.dispatch({ ...command, params: { ...params, request_id: 'request-second', prompt: 'continue' } })).ok).toBe(true)
  expect(f.calls[1].sessionId).toBe('voice-thread')
  const other = await restored.addProject(f.root)
  expect((await bridge.dispatch({ ...command, params: { ...params, project_id: other.id } })).ok).toBe(false)
})
it('deduplicates creation and never replays an interrupted persisted reservation', async () => {
  const f = await fixture()
  const bridge = new AgentDeviceBridge(f.manager, async () => {})
  const command = { id: 'create', action: 'agent.create', params: { project_id: f.project.id, request_id: 'request-create', provider: 'codex' } }
  expect(await bridge.dispatch(command)).toEqual(await bridge.dispatch(command))
  expect((await f.manager.snapshot()).sessions).toHaveLength(2)
  const file = join(f.storage, 'manager.json')
  const state = JSON.parse(await readFile(file, 'utf8'))
  state.deviceReceipts['request-uncertain'] = { fingerprint: 'same', finished: false }
  await writeFile(file, JSON.stringify(state))
  const restored = new Manager(f.storage, () => {})
  managers.push(restored)
  let replayed = false
  await expect(restored.deviceRequest('request-uncertain', 'same', async () => { replayed = true })).rejects.toThrow('interrupted')
  expect(replayed).toBe(false)
})

it('retains prototype-shaped request IDs as ordinary durable receipt keys', async () => {
  const f = await fixture()
  let effects = 0
  const execute = async () => ++effects
  expect(await f.manager.deviceRequest('__proto__', 'same', execute)).toBe(1)
  expect(await f.manager.deviceRequest('__proto__', 'same', execute)).toBe(1)
  const restored = new Manager(f.storage, () => {}, f.options)
  managers.push(restored)
  expect(await restored.deviceRequest('__proto__', 'same', execute)).toBe(1)
  expect(effects).toBe(1)
})
it('stores project groups and display names while refusing cyclic workspace parents', async () => {
  const f = await fixture()
  const tree = await f.manager.createWorktree(f.project.id, 'feat/parent')
  await f.manager.setProjectGroup(f.project.id, 'Research')
  await f.manager.updateWorkspace(f.project.id, tree.path, { displayName: 'Child', parentWorktreePath: f.project.path })
  await expect(f.manager.updateWorkspace(f.project.id, f.project.path, { parentWorktreePath: tree.path })).rejects.toThrow('cycle')
  const restored = new Manager(f.storage, () => {}, f.options)
  managers.push(restored)
  expect((await restored.snapshot()).projects[0].group).toBe('Research')
  expect((await restored.snapshot()).workspaces[0]).toMatchObject({ displayName: 'Child', parentWorktreePath: f.project.path })
})
