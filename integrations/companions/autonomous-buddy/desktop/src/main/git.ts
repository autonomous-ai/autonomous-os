import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { realpath, stat, readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import type { FileEntry, GitSnapshot, Worktree } from '../shared/types.js'
const exec = promisify(execFile)
export async function gitCommand(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await exec('git', ['--no-pager', '-c', 'core.quotepath=false', ...args], {
    cwd,
    maxBuffer: 4 * 1024 * 1024,
    timeout: 15000,
    env: { ...process.env, GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0' },
  })
  return stdout
}
export async function listWorktrees(root: string): Promise<Worktree[]> {
  try {
    const raw = await gitCommand(root, ['worktree', 'list', '--porcelain', '-z'])
    return raw
      .split('\0\0')
      .filter(Boolean)
      .map((block, index) => {
        const fields = block.split('\0')
        const field = (name: string) =>
          fields.find((v) => v.startsWith(name + ' '))?.slice(name.length + 1) ?? ''
        return {
          path: field('worktree'),
          branch: field('branch').replace('refs/heads/', '') || '(detached)',
          head: field('HEAD'),
          primary: index === 0,
          locked: fields.some((v) => v === 'locked' || v.startsWith('locked ')),
        }
      })
      .filter((v) => v.path)
  } catch (error) {
    if ((error as { stderr?: string }).stderr?.includes('not a git repository'))
      return [{ path: root, branch: '', head: '', primary: true }]
    throw error
  }
}
export async function contained(root: string, relative: string): Promise<string> {
  if (typeof relative !== 'string' || path.isAbsolute(relative))
    throw new Error('Expected a relative file path')
  const base = await realpath(root)
  const candidate = path.resolve(base, relative)
  const within = (value: string) => value === base || value.startsWith(base + path.sep)
  if (!within(candidate)) throw new Error('Path escapes workspace')
  const actual = await realpath(candidate)
  if (!within(actual)) throw new Error('Path escapes workspace through a symbolic link')
  return actual
}
export async function fileText(root: string, relative: string): Promise<string> {
  const full = await contained(root, relative)
  const info = await stat(full)
  if (!info.isFile() || info.size > 1024 * 1024)
    throw new Error('Only text files up to 1 MiB can be previewed')
  const data = await readFile(full)
  if (data.includes(0)) throw new Error('Binary file cannot be previewed')
  return data.toString('utf8')
}
export async function fileEntries(root: string, relative: string): Promise<FileEntry[]> {
  const directory = await contained(root, relative)
  const entries = await readdir(directory, { withFileTypes: true })
  return entries
    .filter((v) => v.name !== '.git')
    .sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name))
    .map((v) => ({ name: v.name, path: path.posix.join(relative, v.name), directory: v.isDirectory() }))
}
export async function gitSnapshot(root: string): Promise<GitSnapshot> {
  const worktrees = await listWorktrees(root)
  if (!worktrees[0]?.head) return { branch: '', files: [], commits: [] }
  const raw = await gitCommand(root, ['status', '--porcelain=v1', '-z', '--untracked-files=all'])
  const parts = raw.split('\0')
  const files = []
  for (let i = 0; i < parts.length; i++) {
    const record = parts[i]
    if (!record) continue
    files.push({ path: record.slice(3), status: record.slice(0, 2) })
    if (/[RC]/.test(record.slice(0, 2))) i++
  }
  const branch = (
    await gitCommand(root, ['symbolic-ref', '--quiet', '--short', 'HEAD']).catch(() => 'HEAD')
  ).trim()
  const history = await gitCommand(root, ['log', '-30', '--format=%H%x00%s%x00%an%x00%aI']).catch(() => '')
  const commits = history
    .trim()
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [hash, subject, author, date] = line.split('\0')
      return { hash, subject, author, date }
    })
  return { branch, files, commits }
}
export async function fileDiff(root: string, relative: string): Promise<string> {
  if (!relative || path.isAbsolute(relative) || relative.split(/[\\/]/).includes('..'))
    throw new Error('Invalid diff path')
  const tracked = await gitCommand(root, ['ls-files', '-z', '--', relative])
  if (!tracked) return `Untracked file: ${relative}\n\n${await fileText(root, relative)}`
  // HEAD includes both staged and unstaged changes; unborn repositories need the index diff too.
  const args = ['diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--', relative]
  return (
    (await gitCommand(root, args).catch(
      async () =>
        (await gitCommand(root, ['diff', '--cached', '--no-ext-diff', '--no-textconv', '--', relative])) +
        (await gitCommand(root, ['diff', '--no-ext-diff', '--no-textconv', '--', relative])),
    )) || 'No changes for this file.'
  )
}
