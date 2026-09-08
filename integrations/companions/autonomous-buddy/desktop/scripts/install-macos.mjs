import { accessSync, constants, existsSync, mkdirSync, renameSync, rmSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

if (process.platform !== 'darwin') throw new Error('This installation target requires macOS')
const source = resolve(`artifacts/package/Autonomous Buddy-darwin-${process.arch}/Autonomous Buddy.app`)
const destination = '/Applications/Autonomous Buddy.app'
const legacy = '/Applications/AutonomousBuddy.app'
const suffix = `${Date.now()}-${randomUUID()}`
const staged = `/Applications/.Autonomous Buddy-${suffix}.installing.app`
const backupRoot = join(homedir(), 'Library/Application Support/AutonomousBuddy/LegacyBackups')
const backup = join(backupRoot, `Autonomous Buddy-${suffix}.disabled`)
const legacyBackup = join(backupRoot, `AutonomousBuddy-${suffix}.disabled`)
const managerID = 'network.autonomous.ai.buddy.manager'
const nativeID = 'network.autonomous.ai.buddy'

function verifyBundle(bundle, expectedID, requireHelper = false) {
  const identifier = execFileSync(
    '/usr/libexec/PlistBuddy',
    ['-c', 'Print :CFBundleIdentifier', `${bundle}/Contents/Info.plist`],
    { encoding: 'utf8' },
  ).trim()
  if (identifier !== expectedID)
    throw new Error(`Refusing to replace unexpected application: ${bundle} (${identifier})`)
  if (requireHelper) accessSync(`${bundle}/Contents/Resources/native/AutonomousBuddy`, constants.X_OK)
  execFileSync('codesign', ['--verify', '--deep', '--strict', bundle], { stdio: 'inherit' })
}

// Match executable paths, not a process name shared by unrelated builds or sessions.
function processesIn(bundle) {
  return execFileSync('ps', ['-axo', 'pid=,comm='], { encoding: 'utf8' })
    .split('\n')
    .flatMap((line) => {
      const match = line.trim().match(/^(\d+)\s+(.+)$/)
      return match && match[2].startsWith(`${bundle}/Contents/`) ? [Number(match[1])] : []
    })
}
const delay = (ms) => new Promise((done) => setTimeout(done, ms))
async function quitBundle(bundle) {
  if (!processesIn(bundle).length) return
  // Address the verified bundle by path so older app versions can run quit hooks.
  execFileSync(
    'osascript',
    [
      '-e',
      'on run argv\nwith timeout of 10 seconds\ntell application (item 1 of argv) to quit\nend timeout\nend run',
      bundle,
    ],
    { timeout: 12_000, stdio: 'inherit' },
  )
  await delay(500)
  // Only remaining processes inside this exact bundle can be orphaned helpers.
  for (const pid of processesIn(bundle)) {
    try {
      process.kill(pid, 'SIGTERM')
    } catch (error) {
      if (error.code !== 'ESRCH') throw error
    }
  }
  const deadline = Date.now() + 10_000
  while (processesIn(bundle).length && Date.now() < deadline) await delay(200)
  if (processesIn(bundle).length)
    throw new Error(`Application did not stop cleanly; installation cancelled: ${bundle}`)
}

if (!existsSync(source)) throw new Error('Run make build first')
verifyBundle(source, managerID, true)
// Check both destinations before any process is stopped or application moved.
if (existsSync(destination)) verifyBundle(destination, managerID)
if (existsSync(legacy)) verifyBundle(legacy, nativeID)
let replaced = false
let archivedLegacy = false
let installed = false
try {
  execFileSync('ditto', [source, staged], { stdio: 'inherit' })
  verifyBundle(staged, managerID, true)
  if (existsSync(destination)) await quitBundle(destination)
  if (existsSync(legacy)) await quitBundle(legacy)
  mkdirSync(backupRoot, { recursive: true, mode: 0o700 })
  if (existsSync(destination)) {
    renameSync(destination, backup)
    replaced = true
  }
  if (existsSync(legacy)) {
    renameSync(legacy, legacyBackup)
    archivedLegacy = true
  }
  renameSync(staged, destination)
  installed = true
  verifyBundle(destination, managerID, true)
} catch (error) {
  if (installed) rmSync(destination, { recursive: true })
  if (replaced) renameSync(backup, destination)
  if (archivedLegacy) renameSync(legacyBackup, legacy)
  throw error
} finally {
  if (existsSync(staged)) rmSync(staged, { recursive: true })
}
console.log(`Installed one application (agent manager + native helper): ${destination}`)
if (replaced) console.log(`Previous application preserved: ${backup}`)
if (archivedLegacy) console.log(`Legacy native application preserved: ${legacyBackup}`)
console.log('Project/session data and native pairing credentials were preserved.')
