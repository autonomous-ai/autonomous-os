import { _electron, expect } from '@playwright/test'
import electronPath from 'electron'
import { WebSocketServer, WebSocket } from 'ws'
import { once } from 'node:events'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// This runs the compiled production app, real Git and a real PTY. Only the model
// executable is replaced, so no account, credential, or paid model is used.
const exec = promisify(execFile)
const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const temporary = await realpath(await mkdtemp(path.join(tmpdir(), 'buddy-electron-')))
const projectPath = path.join(temporary, 'buddy-demo')
const bin = path.join(temporary, 'bin')
const profile = path.join(temporary, 'profile')
const log = path.join(temporary, 'provider-calls.jsonl')
const artifacts = path.join(desktop, 'artifacts')
let application
let page
let lamp
let lampSocket
let connectionCount = 0
let commandSequence = 0
const notices = []
const pendingCommands = new Map()
const lampErrors = []
async function startLamp() {
  lamp = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  lamp.on('connection', (socket, request) => {
    if (request.headers.authorization !== 'Bearer test-token') {
      lampErrors.push('Missing test pairing bearer token')
      socket.close()
      return
    }
    lampSocket = socket
    connectionCount++
    socket.on('message', (data) => {
      const message = JSON.parse(data.toString())
      if (message.type === 'agent_event') notices.push(message)
      else {
        const pending = pendingCommands.get(message.id)
        if (pending) {
          pendingCommands.delete(message.id)
          clearTimeout(pending.timer)
          pending.resolve(message)
        }
      }
    })
  })
  await once(lamp, 'listening')
}
async function lampCommand(action, params = {}) {
  await expect.poll(() => lampSocket?.readyState, { timeout: 15000 }).toBe(WebSocket.OPEN)
  const id = `smoke-command-${++commandSequence}`
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pendingCommands.delete(id)
      reject(new Error(`Lamp command timed out: ${action}`))
    }, 15000)
    pendingCommands.set(id, { resolve, timer })
    lampSocket.send(JSON.stringify({ id, action: `agent.${action}`, params, timeout_ms: 10000 }))
  })
}
const pageErrors = []
const processErrors = []
const git = (args) => exec('git', args, { cwd: projectPath })
const fixtureProvider = `#!${process.execPath}
import fs from 'node:fs';
const args = process.argv.slice(2);
let prompt = '';
for await (const chunk of process.stdin) prompt += chunk;
const resumed = args.includes('resume');
const sessionId = resumed ? args[args.indexOf('--json') + 1] : 'buddy-fixture-thread-001';
fs.appendFileSync(process.env.BUDDY_SMOKE_LOG, JSON.stringify({ args, prompt, sessionId }) + '\\n');
const emit = event => process.stdout.write(JSON.stringify(event) + '\\n');
emit({ type: 'thread.started', thread_id: sessionId });
emit({ type: 'item.completed', item: { type: 'agent_message', text: 'Inspecting local project files…' } });
await new Promise(resolve => setTimeout(resolve, 1000));
emit({ type: 'item.completed', item: { type: 'agent_message', text: resumed
  ? 'Continuing the same session.\\n\\nThe session store keeps project, worktree and provider conversation IDs together. Follow-up messages resume this conversation without creating a new thread.\\n\\nValidation\\n  ✓ Session history survives an app restart\\n  ✓ Concurrent sessions keep their output separate\\n  ✓ Terminal output streams through a local PTY\\n\\nNext: connect voice routing to the selected session.'
  : 'I reviewed the workspace and the current changes.\\n\\nImplementation plan\\n  1. Keep projects and worktrees in the left navigation.\\n  2. Give each agent a persistent session with streaming output.\\n  3. Put file changes and Git history beside the conversation.\\n\\nThe native computer-use helper remains independent.' } });
emit({ type: 'turn.completed', usage: { input_tokens: 10, output_tokens: 20 } });
`
async function launchApp() {
  application = await _electron.launch({
    executablePath: process.env.BUDDY_APP_EXECUTABLE || electronPath,
    args: process.env.BUDDY_APP_EXECUTABLE ? [] : ['.'],
    cwd: desktop,
    env: {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
      SHELL: '/bin/sh',
      BUDDY_DATA_DIR: profile,
      BUDDY_SMOKE_LOG: log,
      BUDDY_NATIVE_TEST_MODE: '1',
      BUDDY_TEST_DEVICE_URL: `ws://127.0.0.1:${lamp.address().port}/api/buddy/ws`,
    },
    timeout: 30000,
  })
  application.process().stderr?.on('data', (data) => processErrors.push(String(data)))
  page = await application.firstWindow()
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.waitForFunction(() => !!window.buddy)
  await expect(page.locator('.app-shell')).toBeVisible()
  await expect.poll(async () => (await page.evaluate(() => window.buddy.nativeStatus())).available).toBe(true)
}
async function helperPids() {
  const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,ppid=,comm='])
  const parent = application.process().pid
  return stdout.split('\n').flatMap((line) => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/)
    return match && Number(match[2]) === parent && match[3].endsWith('/AutonomousBuddy')
      ? [Number(match[1])]
      : []
  })
}
function alive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}
const detail = (id) => page.evaluate((id) => window.buddy.session(id), id)
const snapshot = () => page.evaluate(() => window.buddy.snapshot())
async function createUISession(provider, name) {
  await page.locator('.new-tab').click()
  await page
    .locator('.provider-option')
    .filter({ has: page.locator('strong', { hasText: provider }) })
    .click()
  await page.locator('#session-title').fill(name)
  await page.getByRole('button', { name: 'Create session', exact: true }).click()
  await expect(page.locator('.modal')).toHaveCount(0)
  await expect(page.locator('.session-identity')).toContainText(name)
  return (await snapshot()).sessions.find((session) => session.title === name)
}
async function sendUI(prompt) {
  await page.getByRole('textbox', { name: 'Message agent' }).fill(prompt)
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
}
try {
  await Promise.all([mkdir(projectPath), mkdir(bin), mkdir(profile), mkdir(artifacts, { recursive: true })])
  await mkdir(path.join(projectPath, 'src'))
  await writeFile(
    path.join(projectPath, 'README.md'),
    '# Buddy agent workspace\n\nA local home for your projects and agents.\n',
  )
  await writeFile(
    path.join(projectPath, 'src', 'session-store.ts'),
    'export const sessions = new Map<string, unknown>()\n',
  )
  await writeFile(path.join(projectPath, 'package.json'), '{"name":"buddy-demo","private":true}\n')
  await git(['init', '-b', 'main'])
  await git(['config', 'user.name', 'Buddy Smoke Test'])
  await git(['config', 'user.email', 'buddy-test@example.invalid'])
  await git(['add', 'README.md', 'src/session-store.ts', 'package.json'])
  await git(['commit', '-m', 'Add the agent workspace foundation'])
  await writeFile(
    path.join(projectPath, 'README.md'),
    '# Buddy agent workspace\n\nA local home for your projects and agents.\n\nSessions preserve context across follow-up messages.\n',
  )
  await writeFile(
    path.join(projectPath, 'src', 'session-store.ts'),
    'export interface Session { id: string; projectId: string; providerSessionId?: string }\nexport const sessions = new Map<string, Session>()\n',
  )
  await writeFile(
    path.join(projectPath, 'notes.md'),
    'Connect voice commands to the active project and session.\n',
  )
  // .mjs avoids inheriting package type from a host directory.
  await writeFile(path.join(bin, 'codex.mjs'), fixtureProvider)
  await writeFile(
    path.join(bin, 'codex'),
    `#!/bin/sh\nexec '${process.execPath.replaceAll("'", "'\\''")}' '${path.join(bin, 'codex.mjs').replaceAll("'", "'\\''")}' "$@"\n`,
  )
  await chmod(path.join(bin, 'codex'), 0o755)
  await startLamp()
  await launchApp()
  await application.evaluate(({ dialog }, selected) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selected] })
  }, projectPath)
  await page.locator('.open-project').click()
  await expect(page.locator('.project-heading')).toContainText('buddy-demo')
  await expect(page.locator('.tree-select')).toContainText('main')
  const project = (await snapshot()).projects[0]
  expect(project.path).toBe(projectPath)
  await page.getByRole('button', { name: 'New worktree in buddy-demo', exact: true }).click()
  await page.locator('#branch-name').fill('feat/session-routing')
  await page.getByRole('button', { name: 'Create worktree', exact: true }).click()
  await expect(page.locator('.tree-select')).toHaveCount(2)
  await page.locator('.tree-select').filter({ hasText: 'main' }).click()
  const workspaceMenu = () => page.getByRole('button', { name: 'Workspace actions for main', exact: true }).click()
  await workspaceMenu()
  await expect(page.getByRole('menu', { name: 'Workspace actions' })).toBeVisible()
  await expect(page.getByRole('menuitem', { name: 'Delete worktree', exact: true })).toBeDisabled()
  await page.getByRole('menuitem', { name: 'Pin', exact: true }).click()
  await expect.poll(async () => (await snapshot()).workspaces.find((item) => item.worktreePath === projectPath)?.pinned).toBe(true)
  await workspaceMenu()
  await page.getByRole('menuitemradio', { name: 'In review', exact: true }).click()
  await expect.poll(async () => (await snapshot()).workspaces.find((item) => item.worktreePath === projectPath)?.status).toBe('review')
  await workspaceMenu()
  await page.screenshot({ path: path.join(artifacts, 'workspace-context-menu.png') })
  await page.keyboard.press('Escape')
  const terminal = await createUISession('Terminal', 'Workspace terminal')
  await page.evaluate((id) => window.buddy.terminalWrite(id, "printf 'BUDDY_%s\\n' 'PTY_OK'\r"), terminal.id)
  await expect
    .poll(async () =>
      (await detail(terminal.id)).events
        .filter((event) => event.type === 'terminal')
        .map((event) => event.text)
        .join(''),
    )
    .toContain('BUDDY_PTY_OK')
  expect((await detail(terminal.id)).session.status).toBe('running')
  await expect(page.locator('.xterm')).toBeVisible()
  await page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true })).toHaveCount(0)
  expect((await detail(terminal.id)).session.status).toBe('running')
  await page.locator('.session-row').filter({ hasText: 'Workspace terminal' }).click()
  await expect(page.locator('.xterm')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Close tab Workspace terminal', exact: true })).toBeVisible()
  const agent = await createUISession('Codex', 'Build session orchestration')
  await sendUI('Review the workspace and propose the session manager foundation.')
  await expect(page.locator('.session-status')).toContainText('Working')
  await expect(page.locator('.transcript')).toContainText('Inspecting local project files…')
  await expect.poll(async () => (await detail(agent.id)).session.status).toBe('completed')
  await expect(page.locator('.transcript')).toContainText('Implementation plan')
  await sendUI('Continue with session persistence and explain the validation.')
  await expect
    .poll(async () => (await detail(agent.id)).events.filter((event) => event.type === 'prompt').length)
    .toBe(2)
  await expect.poll(async () => (await detail(agent.id)).session.status).toBe('completed')
  await expect(page.locator('.transcript')).toContainText('Continuing the same session.')
  const calls = (await readFile(log, 'utf8'))
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line))
  expect(calls).toHaveLength(2)
  expect(calls[1].sessionId).toBe(calls[0].sessionId)
  expect(calls[1].args).toContain('resume')
  // A real local WebSocket impersonates only the lamp. Commands cross the
  // packaged Swift connection and private process relay before reaching Manager.
  const listed = await lampCommand('list')
  expect(listed.ok).toBe(true)
  expect(listed.result.projects).toContainEqual(project)
  const createParams = { project_id: project.id, provider: 'codex', title: 'Lamp voice session', request_id: 'smoke-create-voice-001' }
  const created = await lampCommand('create', createParams)
  expect(created.ok).toBe(true)
  const voice = created.result
  expect((await lampCommand('create', createParams)).result.id).toBe(voice.id)
  const firstVoice = { project_id: project.id, session_id: voice.id, request_id: 'smoke-send-voice-001', prompt: 'Review this project from the lamp voice command.' }
  expect(await lampCommand('send', firstVoice)).toMatchObject({ ok: true, result: { accepted: true, session_id: voice.id } })
  expect(await lampCommand('send', firstVoice)).toMatchObject({ ok: true, result: { accepted: true, session_id: voice.id } })
  await expect.poll(() => notices.some((event) => event.session_id === voice.id && event.status === 'completed')).toBe(true)
  const firstNotice = notices.find((event) => event.session_id === voice.id && event.status === 'completed')
  expect(firstNotice.project_id).toBe(project.id)
  expect(firstNotice.seq).toBeGreaterThan(0)
  const firstDetail = await lampCommand('session', { project_id: project.id, session_id: voice.id })
  expect(firstDetail.ok).toBe(true)
  expect(firstDetail.result.events.filter((event) => event.type === 'prompt')).toHaveLength(1)
  expect((await lampCommand('session', { project_id: project.id, session_id: voice.id, after_seq: firstDetail.result.next_seq })).result.events).toEqual([])
  const beforeReconnect = connectionCount
  lampSocket.close(1000, 'smoke reconnect')
  await expect.poll(() => connectionCount, { timeout: 15000 }).toBeGreaterThan(beforeReconnect)
  expect((await lampCommand('session', { project_id: project.id, session_id: voice.id })).result.session.providerSessionId).toBe(firstDetail.result.session.providerSessionId)
  const secondVoice = { ...firstVoice, request_id: 'smoke-send-voice-002', prompt: 'Continue the same lamp session and explain persistence.' }
  expect((await lampCommand('send', secondVoice)).ok).toBe(true)
  await expect.poll(() => notices.some((event) => event.session_id === voice.id && event.status === 'completed' && event.seq > firstNotice.seq)).toBe(true)
  const secondDetail = await lampCommand('session', { project_id: project.id, session_id: voice.id, after_seq: firstDetail.result.next_seq })
  expect(secondDetail.result.session.providerSessionId).toBe(firstDetail.result.session.providerSessionId)
  expect(secondDetail.result.events.filter((event) => event.type === 'prompt')).toHaveLength(1)
  const voiceCalls = (await readFile(log, 'utf8')).trim().split('\n').map((line) => JSON.parse(line))
  expect(voiceCalls).toHaveLength(4)
  expect(voiceCalls.slice(2).map((call) => call.prompt)).toEqual([firstVoice.prompt, secondVoice.prompt])
  expect(voiceCalls[3].args).toContain('resume')
  expect(voiceCalls[3].sessionId).toBe(voiceCalls[2].sessionId)
  await page.locator('.session-row').filter({ hasText: 'Workspace terminal' }).click()
  await expect(page.locator('.xterm')).toBeVisible()
  expect((await detail(terminal.id)).session.status).toBe('running')
  await page.locator('.session-row').filter({ hasText: 'Build session orchestration' }).click()
  await expect(page.locator('.transcript-block.prompt')).toHaveCount(2)
  await expect(page.locator('.changed-file')).toHaveCount(3)
  await page.locator('.changed-file').filter({ hasText: 'README.md' }).click()
  await expect(page.locator('.file-preview')).toContainText('+Sessions preserve context')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  await page.locator('.right-tabs').getByRole('button', { name: 'Files', exact: true }).click()
  await page.locator('.file-entry').filter({ hasText: 'src' }).click()
  await page.locator('.file-entry').filter({ hasText: 'session-store.ts' }).click()
  await expect(page.locator('.file-preview')).toContainText('providerSessionId')
  await page.getByRole('button', { name: 'Close file preview', exact: true }).click()
  await page
    .locator('.right-tabs')
    .getByRole('button', { name: /^Changes/ })
    .click()
  await expect(page.locator('.commit-list')).toContainText('Add the agent workspace foundation')
  await page.screenshot({ path: path.join(artifacts, 'manager-workspace.png') })
  await page.getByRole('button', { name: 'Computer & device', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Computer & device' })).toContainText('Ready')
  expect(await page.evaluate(() => window.buddy.computerCommand('ping'))).toMatchObject({
    ok: true,
    result: { pong: true },
  })
  await page.evaluate(() => window.buddy.nativeAction('pause', { paused: true }))
  await expect(page.locator('.native-state')).toContainText('Paused')
  expect((await page.evaluate(() => window.buddy.computerCommand('ping'))).ok).toBe(false)
  await page.evaluate(() => window.buddy.nativeAction('pause', { paused: false }))
  await expect(page.locator('.native-state')).toContainText('Ready')
  await page.screenshot({ path: path.join(artifacts, 'unified-buddy-computer.png') })
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click()
  const nativePids = await helperPids()
  expect(nativePids).toHaveLength(1)
  // Closing the workspace must preserve the native helper and running sessions.
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
  await expect.poll(() => application.windows().length).toBe(0)
  expect(await helperPids()).toEqual(nativePids)
  const reopened = application.waitForEvent('window')
  await application.evaluate(({ app }) => app.emit('activate'))
  page = await reopened
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await expect(page.locator('.app-shell')).toBeVisible()
  expect((await detail(terminal.id)).session.status).toBe('running')
  expect((await page.evaluate(() => window.buddy.nativeStatus())).available).toBe(true)
  const saved = await detail(agent.id)
  await application.close()
  application = undefined
  await expect.poll(() => nativePids.some(alive)).toBe(false)
  await launchApp()
  expect(await helperPids()).toHaveLength(1)
  await expect(page.locator('.session-identity')).toContainText('Build session orchestration')
  await expect(page.locator('.transcript-block.prompt')).toHaveCount(2)
  expect((await detail(agent.id)).session.providerSessionId).toBe(saved.session.providerSessionId)
  expect((await detail(agent.id)).events.filter((event) => event.type === 'prompt')).toEqual(
    saved.events.filter((event) => event.type === 'prompt'),
  )
  expect((await detail(terminal.id)).session.status).toBe('stopped')
  const afterRestart = (await readFile(log, 'utf8')).trim().split('\n')
  expect(afterRestart).toHaveLength(4)
  // Persistent receipts remain effective across both helper reconnect and app restart.
  expect((await lampCommand('send', secondVoice)).ok).toBe(true)
  expect((await lampCommand('session', { project_id: project.id, session_id: voice.id })).result.events.filter((event) => event.type === 'prompt')).toHaveLength(2)
  expect((await readFile(log, 'utf8')).trim().split('\n')).toHaveLength(4)
  expect(lampErrors).toEqual([])
  expect(pageErrors).toEqual([])
  console.log(
    'PASS: unified app, real Swift helper IPC/ping/pause/exit, production UI, Git/worktrees/diff, PTY, agent streaming, exact-ID follow-up, real Swift WebSocket lamp routing, reconnect and durable request dedup.',
  )
  console.log(`Screenshot: ${path.join(artifacts, 'manager-workspace.png')}`)
} catch (error) {
  if (page && !page.isClosed())
    await page.screenshot({ path: path.join(artifacts, 'manager-smoke-failure.png') }).catch(() => {})
  console.error('Electron process diagnostics:', processErrors.join('').slice(-6000))
  throw error
} finally {
  await application?.close().catch(() => {})
  for (const pending of pendingCommands.values()) clearTimeout(pending.timer)
  for (const socket of lamp?.clients ?? []) socket.terminate()
  if (lamp) await new Promise((resolve) => lamp.close(resolve))
  await rm(temporary, { recursive: true, force: true })
}
