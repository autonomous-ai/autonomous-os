// Real Squirrel replacement on signed disposable copies; never modifies the installed Buddy.
import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdtemp, readFile, writeFile, mkdir, rm, appendFile, realpath } from 'node:fs/promises'
import { tmpdir, homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { build } from 'esbuild'
import { discoverSigningIdentity } from '../scripts/signing-identity.mjs'

if (process.platform !== 'darwin') throw new Error('macOS is required')
const identity = discoverSigningIdentity()
if (!identity) throw new Error('Developer ID is required for real update testing')
const source = resolve(process.env.BUDDY_UPDATE_TEST_APP || `artifacts/package/Autonomous Buddy-darwin-${process.arch}/Autonomous Buddy.app`)
const root = await realpath(await mkdtemp(join(tmpdir(), 'buddy-update-smoke-')))
const bundleID = `network.autonomous.ai.buddy.update-test.${randomUUID()}`
const installed = join(root, 'installed', 'Autonomous Buddy.app')
const candidate = join(root, 'candidate', 'Autonomous Buddy.app')
const profile = join(root, 'profile'), log = join(root, 'events.jsonl'), archive = join(root, 'update.zip')
const run = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
let child, server, succeeded = false
async function events() {
  try { return (await readFile(log, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) }
  catch { return [] }
}
try {
  await mkdir(profile)
  await writeFile(join(profile, 'preserved.txt'), 'user data survives an update')
  await writeFile(log, '')
  console.log(`Preparing isolated signed apps in ${root}`)
  const main = await build({
    stdin: { resolveDir: process.cwd(), loader: 'ts', contents: `
import { app, autoUpdater } from 'electron'
import { appendFileSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { AppUpdater } from './src/main/app-updater'
import { NativeHelper } from './src/main/native-helper'
const cfg = JSON.parse(readFileSync(join(__dirname, 'update-test.json'), 'utf8'))
const record = (event, extra = {}) => appendFileSync(cfg.log, JSON.stringify({event, version: app.getVersion(), pid: process.pid, ...extra}) + '\\n')
app.setPath('userData', cfg.profile)
process.env.BUDDY_NATIVE_TEST_MODE = '1'
let helper, controller, stopping = false, complete = false
app.on('window-all-closed', () => {})
app.on('before-quit', e => {
  if (complete) return
  e.preventDefault()
  if (stopping) return
  stopping = true
  controller?.dispose()
  void helper.stop().then(() => { complete = true; app.quit() })
})
app.whenReady().then(async () => {
  if (app.getVersion() === '99.0.1' && readFileSync(cfg.log, 'utf8').includes('helper-stopped-before-install')) throw new Error('Updater relaunched the old version')
  record('started')
  helper = new NativeHelper(join(process.resourcesPath, 'native/AutonomousBuddy'), () => {})
  helper.start()
  if (!(await helper.status()).available) throw new Error('Test helper did not start')
  record('helper-ready')
  if (app.getVersion() === '99.0.2') {
    if (readFileSync(join(cfg.profile, 'preserved.txt'), 'utf8') !== 'user data survives an update') throw new Error('User data changed')
    record('updated-and-data-preserved')
    app.quit()
    return
  }
  const setFeed = autoUpdater.setFeedURL.bind(autoUpdater)
  autoUpdater.setFeedURL = options => setFeed({...options, url: cfg.feed})
  autoUpdater.on('error', error => record('updater-error', {message: error.message}))
  controller = new AppUpdater({
    updater: autoUpdater, supported: true, arch: process.arch, version: app.getVersion(),
    showMessage: async options => { record('dialog', {message: options.message}); return {response: 0} },
    notifyReady: () => {}, publish: state => record('state', state),
    install: async () => {
      stopping = true
      controller.dispose()
      await helper.stop()
      record('helper-stopped-before-install')
      complete = true
      autoUpdater.quitAndInstall()
    },
  })
  controller.start()
  await controller.check()
}).catch(error => { record('fatal', {message: error.message}); app.exit(1) })
` }, bundle: true, platform: 'node', format: 'cjs', target: 'node22', external: ['electron'], write: false,
  })
  let zipBytes, archiveRequests = 0
  server = createServer((request, response) => {
    if (request.url?.startsWith('/latest.json')) {
      response.setHeader('Content-Type', 'application/json')
      response.end(JSON.stringify({ currentRelease: '99.0.2', releases: [{ version: '99.0.2', updateTo: {
        version: '99.0.2', name: 'Buddy update smoke', url: `http://127.0.0.1:${server.address().port}/update.zip`,
        sha256: createHash('sha256').update(zipBytes).digest('hex'), size: zipBytes.length,
      } }] }))
    } else if (request.url === '/update.zip') {
      archiveRequests++
      response.setHeader('Content-Type', 'application/zip')
      response.setHeader('Content-Length', zipBytes.length)
      response.end(zipBytes)
    } else { response.statusCode = 404; response.end() }
  })
  await new Promise(done => server.listen(0, '127.0.0.1', done))
  const feed = `http://127.0.0.1:${server.address().port}/latest.json`
  for (const [appPath, version] of [[installed, '99.0.1'], [candidate, '99.0.2']]) {
    run('/usr/bin/ditto', [source, appPath])
    const resources = join(appPath, 'Contents/Resources/app')
    const manifest = JSON.parse(await readFile(join(resources, 'package.json'), 'utf8'))
    await writeFile(join(resources, 'package.json'), JSON.stringify({ ...manifest, version, main: 'update-test.cjs' }))
    await writeFile(join(resources, 'update-test.cjs'), main.outputFiles[0].contents)
    await writeFile(join(resources, 'update-test.json'), JSON.stringify({ profile, log, feed }))
    const plist = join(appPath, 'Contents/Info.plist')
    for (const [key, value] of [['CFBundleIdentifier', bundleID], ['CFBundleVersion', version], ['CFBundleShortVersionString', version]])
      run('/usr/libexec/PlistBuddy', ['-c', `Set :${key} ${value}`, plist])
    run('/usr/bin/codesign', ['--force', '--sign', identity, '--timestamp', '--preserve-metadata=entitlements,flags,runtime', appPath])
    run('/usr/bin/codesign', ['--verify', '--deep', '--strict', appPath])
  }
  run('/usr/bin/ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', candidate, archive])
  zipBytes = await readFile(archive)
  console.log('Launching 99.0.1 and installing 99.0.2 through Squirrel')
  child = spawn(join(installed, 'Contents/MacOS/Autonomous Buddy'), [], { stdio: ['ignore', 'ignore', 'pipe'] })
  child.stderr.on('data', data => { void appendFile(join(root, 'stderr.log'), data) })
  const deadline = Date.now() + 180_000
  let history = []
  while (Date.now() < deadline) {
    history = await events()
    const failure = history.find(item => ['fatal', 'updater-error'].includes(item.event))
    assert.equal(failure, undefined, JSON.stringify(failure))
    try {
      const installerLog = await readFile(join(homedir(), 'Library/Caches', `${bundleID}.ShipIt`, 'ShipIt_stderr.log'), 'utf8')
      assert(!installerLog.includes('Installation error:'), installerLog.slice(-3000))
    } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (history.some(item => item.event === 'updated-and-data-preserved')) break
    await new Promise(done => setTimeout(done, 500))
  }
  assert(history.some(item => item.event === 'helper-stopped-before-install'), 'Helper must stop before installing')
  assert(history.some(item => item.event === 'updated-and-data-preserved'), `No successful relaunch: ${JSON.stringify(history)}`)
  assert.equal(archiveRequests, 1, 'Only one update download')
  assert.equal(run('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString', join(installed, 'Contents/Info.plist')]).trim(), '99.0.2')
  run('/usr/bin/codesign', ['--verify', '--deep', '--strict', installed])
  console.log('PASS: signed replacement, relaunch, helper shutdown, preserved user data, one download')
  succeeded = true
} finally {
  // Remove only this test's ShipIt job, including a pending relaunch after failure.
  try { run('/bin/launchctl', ['remove', `${bundleID}.ShipIt`]) } catch { /* No job remains. */ }
  const processes = run('/bin/ps', ['-axo', 'pid=,comm=']).split('\n')
  for (const line of processes) {
    const match = line.trim().match(/^(\d+)\s+(.+)$/)
    if (match && match[2].startsWith(root + '/')) {
      try { process.kill(Number(match[1]), 'SIGTERM') } catch { /* Already exited. */ }
    }
  }
  if (child && child.exitCode === null) child.kill('SIGTERM')
  await new Promise(done => server ? server.close(done) : done())
  if (succeeded) {
    await rm(root, { recursive: true, force: true })
    await rm(join(homedir(), 'Library/Caches', `${bundleID}.ShipIt`), { recursive: true, force: true })
  } else console.error(`Test artifacts retained for diagnosis: ${root}`)
}
