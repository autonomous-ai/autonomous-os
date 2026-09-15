import { chmodSync, lstatSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

// Squirrel clears quarantine on every extracted resource. Owner-read-only files
// (including SwiftPM privacy manifests) make that installation step fail.
// Visit real entries only: Electron framework links and external links must not
// cause chmod to escape the staged bundle or revisit the same subtree.
function walk(path, visit) {
  const stat = lstatSync(path)
  if (!stat.isFile() && !stat.isDirectory()) return
  visit(path, stat)
  if (stat.isDirectory()) {
    for (const entry of readdirSync(path)) walk(join(path, entry), visit)
  }
}

export function makeUpdateOwnerWritable(root) {
  walk(root, (path, stat) => {
    if (!(stat.mode & 0o200)) chmodSync(path, stat.mode | 0o200)
  })
}

export function verifyUpdateOwnerWritable(root) {
  walk(root, (path, stat) => {
    if (!(stat.mode & 0o200))
      throw new Error(`Update resource is not owner-writable: ${path}. Rebuild and sign the app with writable resources before notarizing; do not reuse this DMG.`)
  })
}
