import { existsSync, renameSync, rmSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

if (process.platform !== 'darwin') throw new Error('This installation target requires macOS')
const source = resolve(`artifacts/package/Autonomous Buddy-darwin-${process.arch}/Autonomous Buddy.app`)
const destination = '/Applications/Autonomous Buddy.app'
const staged = '/Applications/.Autonomous Buddy.installing.app'
const backup = '/Applications/Autonomous Buddy.previous.app'
if (!existsSync(source)) throw new Error('Run make build first')
if (existsSync(staged) || existsSync(backup))
  throw new Error('An earlier installation staging/backup exists; inspect it before retrying')
execFileSync('codesign', ['--verify', '--deep', '--strict', source], { stdio: 'inherit' })
try {
  execFileSync('ditto', [source, staged], { stdio: 'inherit' })
  execFileSync('codesign', ['--verify', '--deep', '--strict', staged], { stdio: 'inherit' })
  if (existsSync(destination)) renameSync(destination, backup)
  try {
    renameSync(staged, destination)
  } catch (error) {
    if (existsSync(backup)) renameSync(backup, destination)
    throw error
  }
  if (existsSync(backup)) rmSync(backup, { recursive: true })
} finally {
  if (existsSync(staged)) rmSync(staged, { recursive: true })
}
console.log(`Installed: ${destination}`)
