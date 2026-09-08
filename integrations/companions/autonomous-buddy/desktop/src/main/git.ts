import { execFile } from 'node:child_process'
import { attachWorkingLineStats } from './git-line-stats'
import { promisify } from 'node:util'
import { realpath, stat, readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import type { FileEntry, GitFile, GitSnapshot, Worktree } from '../shared/types.js'
const exec = promisify(execFile)
export async function gitCommand(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await exec('git', ['--literal-pathspecs', '--no-pager', '-c', 'core.quotepath=false', ...args], {
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
export async function gitSnapshot(root: string, includeLineStats = true): Promise<GitSnapshot> {
  const repository = await gitCommand(root, ['rev-parse', '--is-inside-work-tree']).catch(() => '')
  if (repository.trim() !== 'true') return { branch: '', files: [], commits: [] }
  const raw = await gitCommand(root, ['status', '--porcelain=v1', '-z', '--untracked-files=all'])
  const parts = raw.split('\0')
  const files = []
  for (let i = 0; i < parts.length; i++) {
    const record = parts[i]
    if (!record) continue
    const file: GitFile = { path: record.slice(3), status: record.slice(0, 2) }
    if (/[RC]/.test(file.status)) file.originalPath = parts[++i]
    files.push(file)
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
  if (includeLineStats) await attachWorkingLineStats(root, files, gitCommand)
  return { branch, files, commits }
}
function validFile(value: string): void {
  if (typeof value !== 'string' || !value || value.includes('\0') || path.isAbsolute(value) || value.split(/[\\/]/).some((part) => part === '..' || part === '.git'))
    throw new Error('Expected an explicit workspace file path')
}
export const isStaged = (file: GitFile) => file.status[0] !== ' ' && file.status[0] !== '?'
export const isUnstaged = (file: GitFile) => file.status[1] !== ' '

async function selectedChanges(root: string, files: string[]): Promise<string[]> {
  if (!Array.isArray(files) || files.length === 0 || files.length > 1000) throw new Error('Select 1–1000 changed files')
  const snapshot = await gitSnapshot(root, false)
  const paths = new Set<string>()
  for (const value of files) {
    validFile(value)
    const file = snapshot.files.find((item) => item.path === value)
    if (!file) throw new Error('Selected file is no longer changed; refresh Git status')
    paths.add(file.path)
    if (file.originalPath) { validFile(file.originalPath); paths.add(file.originalPath) }
  }
  return [...paths]
}

export async function stageFiles(root: string, files: string[]): Promise<void> {
  const paths = await selectedChanges(root, files)
  await gitCommand(root, ['add', '--all', '--', ...paths])
}
export async function unstageFiles(root: string, files: string[]): Promise<void> {
  const paths = await selectedChanges(root, files)
  const head = await gitCommand(root, ['rev-parse', '--verify', 'HEAD']).catch(() => '')
  if (head) await gitCommand(root, ['reset', '--quiet', 'HEAD', '--', ...paths])
  else await gitCommand(root, ['rm', '--cached', '--force', '--ignore-unmatch', '--', ...paths])
}
export async function commitStaged(root: string, message: string): Promise<string> {
  if (typeof message !== 'string' || !message.trim() || message.length > 10000 || message.includes('\0'))
    throw new Error('Commit message must contain 1–10000 characters')
  const staged = await gitCommand(root, ['diff', '--cached', '--name-only', '-z'])
  if (!staged) throw new Error('No staged changes to commit')
  await gitCommand(root, ['commit', '-m', message.trim()])
  return (await gitCommand(root, ['rev-parse', 'HEAD'])).trim()
}

export async function fileDiff(root: string, relative: string): Promise<string> {
  validFile(relative)
  const file = (await gitSnapshot(root, false)).files.find((item) => item.path === relative)
  if (!file) return 'No changes for this file.'
  if (file.status === '??') return `Untracked file: ${relative}\n\n${await fileText(root, relative)}`
  const paths = [relative, ...(file.originalPath ? [file.originalPath] : [])]
  const head = await gitCommand(root, ['rev-parse', '--verify', 'HEAD']).catch(() => '')
  const flags = ['--no-ext-diff', '--no-textconv', '--no-color']
  return (head ? await gitCommand(root, ['diff', ...flags, 'HEAD', '--', ...paths]) :
    (await gitCommand(root, ['diff', '--cached', ...flags, '--', ...paths])) +
    (await gitCommand(root, ['diff', ...flags, '--', ...paths]))) || 'No changes for this file.'
}

async function commitRange(root: string, hash: string): Promise<string[]> {
  if (typeof hash !== 'string' || !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(hash)) throw new Error('Expected a full commit hash')
  const line = (await gitCommand(root, ['rev-list', '--parents', '-n', '1', hash])).trim().split(' ')
  if (line[0] !== hash) throw new Error('Unknown commit')
  return line[1] ? [line[1], hash] : ['--root', hash]
}
export async function commitFiles(root: string, hash: string): Promise<GitFile[]> {
  const raw = await gitCommand(root, ['diff-tree', '--no-commit-id', '-r', '-M', '--name-status', '-z', ...await commitRange(root, hash)])
  const parts = raw.split('\0')
  const files: GitFile[] = []
  for (let i = 0; i < parts.length && parts[i];) {
    const status = parts[i++]
    const first = parts[i++]
    if (/^[RC]/.test(status)) files.push({ status, originalPath: first, path: parts[i++] })
    else files.push({ status, path: first })
  }
  return files
}
export async function commitDiff(root: string, hash: string, file: string): Promise<string> {
  validFile(file)
  const entry = (await commitFiles(root, hash)).find((item) => item.path === file)
  if (!entry) throw new Error('File is not part of this commit')
  return gitCommand(root, ['diff-tree', '--no-commit-id', '-r', '-p', '-M', '--no-ext-diff', '--no-textconv', '--no-color', ...await commitRange(root, hash), '--', file, ...(entry.originalPath ? [entry.originalPath] : [])])
}
