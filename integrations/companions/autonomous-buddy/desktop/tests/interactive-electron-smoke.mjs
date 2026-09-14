import { _electron, expect } from '@playwright/test'
import electronPath from 'electron'
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod, realpath } from 'node:fs/promises'
import path from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

// The app and node-pty are real. This deliberately labelled CLI fixture replaces
// Codex only; it does not contact providers, authenticate, or send paid prompts.
const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const root = await realpath(await mkdtemp(path.join(tmpdir(), 'buddy-interactive-electron-')))
const projectPath = path.join(root, 'interactive-project')
const bin = path.join(root, 'bin')
const profile = path.join(root, 'profile')
const log = path.join(root, 'interactive-calls.jsonl')
const artifacts = path.join(desktop, 'artifacts')
let application
let page
const pageErrors = []
const diagnostics = []
const fixture = `
import fs from 'node:fs';
const args = process.argv.slice(2);
const record = (event) => fs.appendFileSync(process.env.BUDDY_INTERACTIVE_LOG, JSON.stringify({pid:process.pid,...event})+'\\n');
if (args[0] === 'app-server') {
  const readline = await import('node:readline');
  const config = args.find(value => value.startsWith('hooks.SessionStart='));
  const command = JSON.parse(config.match(/command=("(?:[^"\\\\]|\\\\.)*")/)[1]);
  const lines = readline.createInterface({input:process.stdin});
  lines.on('line', line => {
    const request = JSON.parse(line);
    if (request.method === 'initialize') process.stdout.write(JSON.stringify({id:request.id,result:{}})+'\\n');
    if (request.method === 'hooks/list') process.stdout.write(JSON.stringify({id:request.id,result:{data:[{hooks:[{command,key:'<session-flags>:fixture',currentHash:'fixture-hash'}]}]}})+'\\n');
  });
  await new Promise(resolve => lines.on('close', resolve));
  process.exit(0);
}
record({type:'start',args,tty:!!process.stdin.isTTY,cwd:process.cwd()});
if (args.includes('exec') || args.includes('--print') || args.includes('--json')) process.exit(91);
process.stdin.setRawMode(true);
process.stdin.setEncoding('utf8');
let input = '';
let turns = 0;
process.stdout.write('\\u001b[32mInteractive CLI test fixture\\u001b[0m\\r\\nfixture> ');
process.stdout.on('resize',()=>record({type:'resize',cols:process.stdout.columns,rows:process.stdout.rows}));
process.stdin.on('data', chunk => {
  for (const char of chunk) {
    if (char === '\\r' || char === '\\n') {
      if (!input) continue;
      record({type:'prompt',text:input,turn:++turns});
      process.stdout.write('\\r\\n\\u001b[36mFixture response '+turns+': '+input+'\\u001b[0m\\r\\nfixture> ');
      input = '';
    } else if (char === '\\u0003') { input = ''; process.stdout.write('^C\\r\\nfixture> '); }
    else if (char === '\\u007f') { input = input.slice(0,-1); process.stdout.write('\\b \\b'); }
    else { input += char; process.stdout.write(char); }
  }
});
process.on('SIGTERM',()=>{record({type:'exit'});process.exit(0)});
`
async function records() {
  return (await readFile(log, 'utf8').catch(() => '')).trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))
}
function alive(pid) {
  try { process.kill(pid, 0); return true } catch { return false }
}
async function launch() {
  application = await _electron.launch({
    executablePath: process.env.BUDDY_APP_EXECUTABLE || electronPath,
    args: process.env.BUDDY_APP_EXECUTABLE ? [] : ['.'],
    cwd: desktop,
    env: {
      ...process.env,
      PATH: `${bin}${path.delimiter}${process.env.PATH ?? ''}`,
      SHELL: '/bin/sh',
      BUDDY_DATA_DIR: profile,
      BUDDY_INTERACTIVE_LOG: log,
      BUDDY_NATIVE_TEST_MODE: '1',
      BUDDY_TEST_DEVICE_URL: '',
    },
    timeout: 30000,
  })
  application.process().stderr?.on('data', (data) => diagnostics.push(String(data)))
  page = await application.firstWindow()
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.waitForFunction(() => !!window.buddy)
  await expect(page.locator('.app-shell')).toBeVisible()
}
const snapshot = () => page.evaluate(() => window.buddy.snapshot())
const detail = (id) => page.evaluate((id) => window.buddy.session(id), id)
try {
  await Promise.all([mkdir(projectPath), mkdir(bin), mkdir(profile), mkdir(artifacts, { recursive: true })])
  await writeFile(path.join(projectPath, 'README.md'), 'Temporary interactive CLI smoke project.\n')
  const script = path.join(bin, 'fixture.mjs')
  await writeFile(script, fixture)
  const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`
  await writeFile(path.join(bin, 'codex'), `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(script)} "$@"\n`)
  await chmod(path.join(bin, 'codex'), 0o755)
  await launch()
  await application.evaluate(({ dialog }, selected) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [selected] })
  }, projectPath)
  await page.getByRole('button', { name: 'Add project', exact: true }).click()
  await expect(page.locator('.project-heading')).toContainText('interactive-project')
  await page.locator('.new-tab').click()
  await page.locator('.provider-option').filter({ has: page.locator('strong', { hasText: 'Codex' }) }).click()
  await page.locator('#session-title').fill('Interactive fixture session')
  await page.getByRole('button', { name: 'Create session', exact: true }).click()
  await expect(page.locator('.modal')).toHaveCount(0)
  await expect(page.locator('.interactive-session')).toBeVisible()
  await expect(page.locator('.session-tab .agent-provider-name')).toHaveText('Codex')
  await expect(page.locator('.session-row .agent-provider-name')).toHaveText('Codex')
  await expect(page.locator('.session-pane-identity .agent-provider-name')).toHaveText('Codex')
  await expect(page.locator('.session-tab')).toHaveAttribute('title', /Codex.*Interactive fixture session/)
  await expect(page.locator('.session-tab .agent-task-title')).toHaveText('Interactive fixture session')
  await expect(page.locator('.xterm')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Message agent', exact: true })).toHaveCount(0)
  const session = (await snapshot()).sessions.find((item) => item.title === 'Interactive fixture session')
  expect(session).toMatchObject({ mode: 'interactive', provider: 'codex', status: 'running' })
  await expect.poll(async () => (await records()).filter((item) => item.type === 'start').length).toBe(1)
  const started = (await records()).find((item) => item.type === 'start')
  expect(started).toMatchObject({ tty: true, cwd: projectPath })
  expect(started.args).toContain('--dangerously-bypass-approvals-and-sandbox')
  expect(started.args).not.toContain('exec')
  expect(started.args).not.toContain('--json')
  await expect.poll(async () => (await detail(session.id)).events.some((event) => event.type === 'terminal' && event.text.includes('Interactive CLI test fixture'))).toBe(true)
  await page.locator('.xterm-helper-textarea').focus()
  await page.keyboard.type('first terminal prompt')
  await page.keyboard.press('Enter')
  await expect.poll(async () => (await records()).filter((item) => item.type === 'prompt').length).toBe(1)
  await page.keyboard.type('second terminal prompt')
  await page.keyboard.press('Enter')
  await expect.poll(async () => (await records()).filter((item) => item.type === 'prompt').length).toBe(2)
  const prompts = (await records()).filter((item) => item.type === 'prompt')
  expect(prompts.map((item) => item.text)).toEqual(['first terminal prompt', 'second terminal prompt'])
  expect(prompts.map((item) => item.pid)).toEqual([started.pid, started.pid])
  expect((await records()).filter((item) => item.type === 'start')).toHaveLength(1)
  await expect.poll(async () => (await detail(session.id)).events.some((event) => event.type === 'terminal' && event.text.includes('Fixture response 2'))).toBe(true)
  expect((await detail(session.id)).events.some((event) => event.type === 'terminal' && event.text.includes('\u001b[36m'))).toBe(true)
  const beforeResize = (await records()).filter((item) => item.type === 'resize').length
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1240, 780))
  await expect.poll(async () => (await records()).filter((item) => item.type === 'resize').length).toBeGreaterThan(beforeResize)
  const resized = (await records()).filter((item) => item.type === 'resize').at(-1)
  expect(resized.cols).toBeGreaterThan(2)
  expect(resized.rows).toBeGreaterThan(2)
  await page.screenshot({ path: path.join(artifacts, 'interactive-cli-session.png') })
  await page.getByRole('button', { name: 'Close tab Interactive fixture session', exact: true }).click()
  await expect.poll(async () => (await detail(session.id)).session.closed).toBe(true)
  await expect.poll(() => alive(started.pid)).toBe(false)
  await expect(page.locator('.session-row').filter({ hasText: 'Interactive fixture session' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Close tab Interactive fixture session', exact: true })).toHaveCount(0)
  expect((await detail(session.id)).events.some((event) => event.type === 'terminal' && event.text.includes('Fixture response 2'))).toBe(true)
  await application.close()
  application = undefined
  await launch()
  expect((await detail(session.id)).session).toMatchObject({ mode: 'interactive', status: 'stopped', closed: true })
  await expect(page.locator('.session-row').filter({ hasText: 'Interactive fixture session' })).toHaveCount(0)
  expect((await records()).filter((item) => item.type === 'start')).toHaveLength(1)
  const voiceSession = await page.evaluate(async () => {
    const state = await window.buddy.snapshot()
    const project = state.projects[0]
    return window.buddy.createSession({ projectId: project.id, worktreePath: project.path, provider: 'codex', title: 'Fresh voice task' })
  })
  await expect.poll(async () => (await records()).filter(item => item.type === 'start').length).toBe(2)
  const emptyCLI = (await records()).filter(item => item.type === 'start').at(-1)
  await page.evaluate(id => window.buddy.send(id, 'check git diff'), voiceSession.id)
  await expect.poll(async () => (await records()).filter(item => item.type === 'start').length).toBe(3)
  const voiceCLI = (await records()).filter(item => item.type === 'start').at(-1)
  expect(voiceCLI.args.slice(-2)).toEqual(['--', 'check git diff'])
  expect(voiceCLI.cwd).toBe(projectPath)
  await expect.poll(() => alive(emptyCLI.pid)).toBe(false)
  expect((await detail(voiceSession.id)).events.filter(event => event.type === 'prompt')).toHaveLength(1)
  await page.evaluate(id => window.buddy.closeSession(id), voiceSession.id)
  await expect.poll(() => alive(voiceCLI.pid)).toBe(false)
  expect(pageErrors).toEqual([])
  console.log('PASS: interactive CLI fixture in real packaged PTY, native xterm input, two prompts on one process, ANSI/resize, close stops and removes sidebar row, archived persistence.')
  console.log(`Screenshot: ${path.join(artifacts, 'interactive-cli-session.png')}`)
} catch (error) {
  if (page && !page.isClosed()) await page.screenshot({ path: path.join(artifacts, 'interactive-smoke-failure.png') }).catch(() => {})
  console.error('Electron diagnostics:', diagnostics.join('').slice(-5000))
  throw error
} finally {
  await application?.close().catch(() => {})
  await rm(root, { recursive: true, force: true })
}
