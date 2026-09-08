import { constants } from 'node:fs'
import { open, realpath } from 'node:fs/promises'
import path from 'node:path'

type LineStats = { added: number; removed: number }
type Entry = { path: string; status: string; originalPath?: string; lineStats?: LineStats }
type RunGit = (root: string, args: string[]) => Promise<string>
const MAX_TEXT_BYTES = 1024 * 1024

// Git -z keeps tabs/newlines literal in paths. Renames have an empty header
// path followed by two separate NUL-delimited source and destination paths.
export function parseLineStats(raw: string): Map<string, LineStats | undefined> {
  const result = new Map<string, LineStats | undefined>()
  const records = raw.split('\0')
  for (let i = 0; i < records.length; i++) {
    const record = records[i]
    if (!record) continue
    const match = /^(\d+|-)\t(\d+|-)\t([\s\S]*)$/.exec(record)
    if (!match) continue
    let name = match[3]
    if (!name) { i++; name = records[++i] }
    if (!name) continue
    if (match[1] === '-' || match[2] === '-') { result.set(name, undefined); continue }
    const added = Number(match[1]), removed = Number(match[2])
    if (Number.isSafeInteger(added) && Number.isSafeInteger(removed)) result.set(name, { added, removed })
  }
  return result
}

async function newFileStats(root: string, relative: string): Promise<LineStats | undefined> {
  // Only regular files below this worktree count; never follow a symlink to
  // another user's file or read through a linked parent directory.
  if (!relative || path.isAbsolute(relative) || relative.includes('\0') || relative.split('/').some((part) => part === '..' || part === '.git')) return
  let handle
  try {
    const base = await realpath(root)
    const file = path.resolve(base, relative)
    const parent = await realpath(path.dirname(file))
    if (parent !== base && !parent.startsWith(base + path.sep)) return
    handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const stat = await handle.stat()
    if (!stat.isFile() || stat.size > MAX_TEXT_BYTES) return
    const buffer = Buffer.alloc(MAX_TEXT_BYTES + 1)
    let length = 0
    while (length < buffer.length) {
      const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null)
      if (!bytesRead) break
      length += bytesRead
    }
    if (length > MAX_TEXT_BYTES) return
    const data = buffer.subarray(0, length)
    if (data.includes(0)) return
    try { new TextDecoder('utf-8', { fatal: true }).decode(data) } catch { return }
    let added = 0
    for (const byte of data) if (byte === 10) added++
    if (length && data[length - 1] !== 10) added++
    return { added, removed: 0 }
  } catch { return undefined }
  finally { await handle?.close() }
}

export async function attachWorkingLineStats(root: string, entries: Entry[], run: RunGit): Promise<void> {
  if (!entries.length) return
  for (const entry of entries) delete entry.lineStats
  // Each poll has bounded file I/O even for generated directories with thousands
  // of entries. Unknown is distinct from a known zero-line change.
  const head = await run(root, ['rev-parse', '--verify', 'HEAD']).catch(() => '')
  const tracked = entries.filter((file) => file.status !== '??')
  const stats = tracked.length ? await run(root, ['diff', '--numstat', '-z', '-M', '--no-ext-diff', '--no-textconv', '--no-color', ...(head ? ['HEAD'] : ['--cached'])]).then(parseLineStats).catch(() => null) : new Map<string, LineStats | undefined>()
  for (const entry of tracked) {
    // On an unborn branch the staged diff is authoritative only when no
    // additional working-tree edit is present; do not sum cancelling patches.
    if ((head || entry.status[1] === ' ') && stats && !entry.status.includes('U'))
      entry.lineStats = stats.has(entry.path) ? stats.get(entry.path) : { added: 0, removed: 0 }
  }
  const untracked = entries.filter((entry) => entry.status === '??').slice(0, 100)
  if (!untracked.length) return
  const attributes = await run(root, ['check-attr', '-z', 'diff', '--', ...untracked.map((entry) => entry.path)]).catch(() => null)
  if (attributes === null) return
  const binaryPaths = new Set<string>()
  const fields = attributes.split('\0')
  for (let i = 0; i + 2 < fields.length; i += 3) if (fields[i + 2] === 'unset') binaryPaths.add(fields[i])
  for (let i = 0; i < untracked.length; i += 4) {
    await Promise.all(untracked.slice(i, i + 4).map(async (entry) => {
      if (!binaryPaths.has(entry.path)) entry.lineStats = await newFileStats(root, entry.path)
    }))
  }
}
