import { afterEach, expect, it } from 'vitest'
import { mkdtemp, mkdir, rm, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { tmpdir } from 'node:os'
import { Manager } from '../src/main/manager'
import { interactiveInvocation, type LaunchOptions } from '../src/main/providers'
const roots: string[] = []
const managers: Manager[] = []
afterEach(async () => {
  for (const manager of managers.splice(0)) manager.dispose()
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })))
})
async function fixture(exitOnKill = true) {
  const root = await mkdtemp(path.join(tmpdir(), 'buddy-interactive-')); roots.push(root)
  const directory = path.join(root, 'project'); await mkdir(directory)
  const storage = path.join(root, 'data')
  const calls: LaunchOptions[] = []
  const writes: string[] = []
  const sizes: number[][] = []
  const launcher = async (options: LaunchOptions) => {
    calls.push(options)
    return { write: (value: string) => writes.push(value), resize: (cols: number, rows: number) => sizes.push([cols, rows]), kill: () => { if (exitOnKill) options.exit(0) } }
  }
  const options = { launch: launcher, available: () => true }
  const manager = new Manager(storage, () => {}, options); managers.push(manager)
  const project = await manager.addProject(directory)
  return { manager, project, calls, writes, sizes, storage, options }
}
it('launches actual interactive CLI argument shape, not exec or print mode', () => {
  expect(interactiveInvocation('codex').args).toEqual(['--dangerously-bypass-approvals-and-sandbox', '--no-alt-screen'])
  expect(interactiveInvocation('codex', 'exact-id', true).args.slice(0, 2)).toEqual(['resume', 'exact-id'])
  expect(() => interactiveInvocation('codex', undefined, true)).toThrow('exact resume')
  const uuid = '4201f719-0eb4-44e4-98b7-1b9a136ef175'
  expect(interactiveInvocation('claude', uuid).args).toEqual(['--dangerously-skip-permissions', '--session-id', uuid])
  expect(interactiveInvocation('claude', uuid, true).args).toContain('--resume')
})
it('new agents keep one process and preserve ANSI across multiple inputs and resizing', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  expect(session.mode).toBe('interactive')
  expect(session.status).toBe('running')
  expect(f.calls).toHaveLength(1)
  expect(f.calls[0].mode).toBe('interactive')
  f.calls[0].data('\u001b[32mReady\u001b[0m\r\n')
  await f.manager.terminalWrite(session.id, 'first\r')
  await f.manager.terminalWrite(session.id, 'second\r')
  await f.manager.terminalResize(session.id, 132, 40)
  expect(f.calls).toHaveLength(1)
  expect(f.writes).toEqual(['first\r', 'second\r'])
  expect(f.sizes).toEqual([[132, 40]])
  expect((await f.manager.session(session.id)).events.some((event) => event.type === 'terminal' && event.text.includes('\u001b[32m'))).toBe(true)
  await expect(f.manager.send(session.id, 'voice')).rejects.toThrow('readiness')
  f.calls[0].exit(0)
  expect((await f.manager.session(session.id)).session.status).toBe('stopped')
  await expect(f.manager.restartInteractive(session.id)).rejects.toThrow('different conversation')
})
it('close stops and archives the CLI, explicit Claude restart preserves its assigned UUID', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'claude' })
  expect(session.providerSessionId).toMatch(/^[a-f0-9-]{36}$/)
  expect(f.calls[0].sessionId).toBe(session.providerSessionId)
  expect(f.calls[0].resume).toBe(false)
  await f.manager.closeSession(session.id)
  expect((await f.manager.session(session.id)).session).toMatchObject({ closed: true, status: 'stopped' })
  await expect(f.manager.send(session.id, 'late')).rejects.toThrow('closed')
  await f.manager.restartInteractive(session.id)
  expect(f.calls[1]).toMatchObject({ sessionId: session.providerSessionId, resume: true })
  expect((await f.manager.session(session.id)).session.closed).toBe(false)
})
it('preserves legacy structured history but lets empty legacy sessions start interactive explicitly', async () => {
  const f = await fixture()
  const full = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex', mode: 'structured' })
  const empty = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex', mode: 'structured' })
  await f.manager.send(full.id, 'structured turn')
  f.calls[0].data('{"type":"thread.started","thread_id":"retained-id"}\n{"type":"turn.completed"}\n')
  f.calls[0].exit(0)
  f.manager.dispose()
  const file = path.join(f.storage, 'manager.json')
  const stored = JSON.parse(await readFile(file, 'utf8'))
  for (const session of stored.sessions) delete session.mode
  await writeFile(file, JSON.stringify(stored))
  const restored = new Manager(f.storage, () => {}, f.options); managers.push(restored)
  expect((await restored.session(full.id)).session).toMatchObject({ mode: 'structured', providerSessionId: 'retained-id' })
  expect((await restored.session(empty.id)).session).toMatchObject({ mode: 'interactive', status: 'stopped' })
  await restored.restartInteractive(empty.id)
  expect(f.calls[1]).toMatchObject({ mode: 'interactive', resume: false })
})
it('accepts verified hooks, safely pastes follow-ups, and gates manual or busy input', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  const hook = f.calls[0].onHook!
  hook({ type: 'working' })
  await expect(f.manager.send(session.id, 'first')).rejects.toThrow('readiness')
  hook({ type: 'ready', sessionId: 'exact-thread' })
  expect((await f.manager.session(session.id)).session).toMatchObject({ status: 'idle', processActive: true, providerSessionId: 'exact-thread' })
  await f.manager.terminalWrite(session.id, '\u001b[1;1R')
  await expect(f.manager.send(session.id, 'bad\u001b[2J')).rejects.toThrow('control')
  await f.manager.send(session.id, 'first\r\nsecond')
  expect(f.writes.slice(-2)).toEqual(['\u001b[200~first\nsecond\u001b[201~', '\r'])
  await expect(f.manager.send(session.id, 'duplicate')).rejects.toThrow('readiness')
  hook({ type: 'working', sessionId: 'exact-thread' })
  hook({ type: 'completed', sessionId: 'exact-thread', summary: 'Done' })
  expect((await f.manager.session(session.id)).session).toMatchObject({ status: 'completed', processActive: true })
  await f.manager.terminalWrite(session.id, 'draft')
  await expect(f.manager.send(session.id, 'collision')).rejects.toThrow('readiness')
  await f.manager.terminalWrite(session.id, '\r')
  hook({ type: 'working', sessionId: 'exact-thread' })
  hook({ type: 'needs_input', sessionId: 'exact-thread' })
  await expect(f.manager.send(session.id, 'ambiguous answer')).rejects.toThrow('needs_manual_input')
  hook({ type: 'completed', sessionId: 'exact-thread' })
  await f.manager.send(session.id, 'followup')
  expect(f.calls).toHaveLength(1)
  await f.manager.closeSession(session.id)
  const before = (await f.manager.session(session.id)).events.length
  hook({ type: 'completed', sessionId: 'wrong-late', summary: 'Late event' })
  expect((await f.manager.session(session.id)).events).toHaveLength(before)
  await f.manager.restartInteractive(session.id)
  expect(f.calls[1]).toMatchObject({ resume: true, sessionId: 'exact-thread' })
})
it('rejects hook identity changes and resets an idle live session on restoration', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  f.calls[0].onHook!({ type: 'ready', sessionId: 'original' })
  const restored = new Manager(f.storage, () => {}, f.options); managers.push(restored)
  expect((await restored.session(session.id)).session).toMatchObject({ status: 'stopped', processActive: false })
  f.calls[0].onHook!({ type: 'ready', sessionId: 'different' })
  expect((await f.manager.session(session.id)).session).toMatchObject({ status: 'error', processActive: false, providerSessionId: 'original' })
})
it('launches the first deferred voice prompt as CLI input once, then requires hooks', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex', deferLaunch: true })
  expect(session).toMatchObject({ mode: 'interactive', processActive: false, interactiveStarted: false })
  expect(f.calls).toHaveLength(0)
  await expect(f.manager.send(session.id, 'bad\u001b[2J')).rejects.toThrow('control')
  await f.manager.send(session.id, 'voice initial\r\nprompt')
  expect(f.calls).toHaveLength(1)
  expect(f.calls[0]).toMatchObject({ mode: 'interactive', prompt: 'voice initial\nprompt', resume: false })
  expect(f.writes).toHaveLength(0)
  await expect(f.manager.send(session.id, 'second too early')).rejects.toThrow('readiness')
  f.calls[0].onHook!({ type: 'completed', sessionId: 'voice-thread' })
  await f.manager.send(session.id, 'voice followup')
  expect(f.calls).toHaveLength(1)
  expect(f.writes).toEqual(['\u001b[200~voice followup\u001b[201~', '\r'])
})

it('reserves paste delivery and withholds Enter when manual input or exit races the wait', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  const hook = f.calls[0].onHook!
  hook({ type: 'ready', sessionId: 'race-thread' })
  const delivery = f.manager.send(session.id, 'pending paste')
  expect(f.writes.at(-1)).toBe('\u001b[200~pending paste\u001b[201~')
  hook({ type: 'ready', sessionId: 'race-thread' })
  await expect(f.manager.send(session.id, 'duplicate')).rejects.toThrow('readiness')
  await f.manager.terminalWrite(session.id, 'manual edit\r')
  await expect(delivery).rejects.toThrow('not submitted')
  expect(f.writes).not.toContain('\r')
  hook({ type: 'completed', sessionId: 'race-thread' })
  const interrupted = f.manager.send(session.id, 'exit race')
  f.calls[0].exit(0)
  await expect(interrupted).rejects.toThrow('not submitted')
  expect(f.writes).not.toContain('\r')
})

it('names interactive tasks from the first prompt hook while preserving renamed titles', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  const hook = f.calls[0].onHook!
  hook({ type: 'working', sessionId: 'title-thread', prompt: 'Review terminal tabs' })
  expect((await f.manager.session(session.id)).session.title).toBe('Review terminal tabs')
  hook({ type: 'working', sessionId: 'title-thread', prompt: 'Follow-up' })
  expect((await f.manager.session(session.id)).session.title).toBe('Review terminal tabs')
  await f.manager.renameSession(session.id, 'My custom task')
  hook({ type: 'working', sessionId: 'title-thread', prompt: 'Another follow-up' })
  expect((await f.manager.session(session.id)).session.title).toBe('My custom task')
  const restored = new Manager(f.storage, () => {}, f.options); managers.push(restored)
  expect((await restored.session(session.id)).session.title).toBe('My custom task')
})

it('launches the first voice task as argv for an untouched Codex without initial hooks', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  await f.manager.terminalWrite(session.id, '\u001b[1;1R')
  await f.manager.send(session.id, 'check git diff')
  expect(f.calls).toHaveLength(2)
  expect(f.calls[1]).toMatchObject({ prompt: 'check git diff', cwd: f.project.path, resume: false })
  expect(f.writes).toEqual(['\u001b[1;1R'])
  f.calls[1].onHook!({ type: 'completed', sessionId: 'voice-thread' })
  await f.manager.send(session.id, 'explain the diff')
  expect(f.calls).toHaveLength(2)
  expect(f.writes.slice(-2)).toEqual(['\u001b[200~explain the diff\u001b[201~', '\r'])
})
it('never relaunches a fresh CLI after manual input or a question hook', async () => {
  const f = await fixture()
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  await f.manager.terminalWrite(session.id, 'draft')
  await expect(f.manager.send(session.id, 'check diff')).rejects.toThrow('draft_input')
  f.calls[0].onHook!({ type: 'needs_input' })
  await expect(f.manager.send(session.id, 'check diff')).rejects.toThrow('needs_manual_input')
  expect(f.calls).toHaveLength(1)
})

it('Stop cancels a first-prompt handoff before the replacement CLI starts', async () => {
  const f = await fixture(false)
  const session = await f.manager.createSession({ projectId: f.project.id, worktreePath: f.project.path, provider: 'codex' })
  const send = f.manager.send(session.id, 'check diff')
  const rejected = expect(send).rejects.toThrow('cancelled')
  await f.manager.stop(session.id)
  f.calls[0].exit(0)
  await rejected
  expect(f.calls).toHaveLength(1)
  expect((await f.manager.session(session.id)).events.filter(event => event.type === 'prompt')).toHaveLength(0)
})
