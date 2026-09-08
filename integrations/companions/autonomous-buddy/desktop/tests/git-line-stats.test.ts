import { afterEach, expect, it } from 'vitest'
import { mkdtemp, writeFile, rm, symlink } from 'node:fs/promises'
import path from 'node:path'
import { tmpdir } from 'node:os'
import { attachWorkingLineStats, parseLineStats } from '../src/main/git-line-stats'
import { gitCommand } from '../src/main/git'
const roots: string[] = []
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))) })
async function repo() {
  const root = await mkdtemp(path.join(tmpdir(), 'buddy-line-stats-')); roots.push(root)
  await gitCommand(root, ['init', '-b', 'main'])
  await gitCommand(root, ['config', 'user.name', 'Stats Test'])
  await gitCommand(root, ['config', 'user.email', 'stats@example.invalid'])
  await gitCommand(root, ['config', 'commit.gpgsign', 'false'])
  return root
}
type Entry = {path: string; status: string; lineStats?: { added: number; removed: number }}
it('parses NUL renames, arbitrary tab/newline paths, binary and literal magic names', () => {
  const stats = parseLineStats('2\t3\t\0old\tname\0new\nname\0-\t-\timage.png\0' + '0\t0\t:(glob)*\0')
  expect(stats.get('new\nname')).toEqual({ added: 2, removed: 3 })
  expect(stats.has('image.png')).toBe(true)
  expect(stats.get('image.png')).toBeUndefined()
  expect(stats.get(':(glob)*')).toEqual({ added: 0, removed: 0 })
})
it('reports net HEAD changes rather than summing cancelling staged and unstaged edits', async () => {
  const root = await repo()
  await writeFile(path.join(root, 'text'), 'base\n')
  await gitCommand(root, ['add', '--', 'text'])
  await gitCommand(root, ['commit', '-m', 'Base'])
  await writeFile(path.join(root, 'text'), 'staged\nextra\n')
  await gitCommand(root, ['add', '--', 'text'])
  await writeFile(path.join(root, 'text'), 'base\n')
  const entries: Entry[] = [{ path: 'text', status: 'MM' }]
  await attachWorkingLineStats(root, entries, gitCommand)
  expect(entries[0].lineStats).toEqual({ added: 0, removed: 0 })
  await rm(path.join(root, 'text'))
  await attachWorkingLineStats(root, entries, gitCommand)
  expect(entries[0].lineStats).toEqual({ added: 0, removed: 1 })
})
it('counts bounded untracked text without following symlinks and leaves binary/large files unknown', async () => {
  const root = await repo()
  await Promise.all([
    writeFile(path.join(root, 'empty'), ''), writeFile(path.join(root, 'text'), 'one\ntwo'),
    writeFile(path.join(root, 'binary'), Buffer.from([0, 1, 2])), writeFile(path.join(root, 'large'), 'a'.repeat(1024 * 1024 + 1)),
    symlink('/etc/hosts', path.join(root, 'link')),
  ])
  const entries: Entry[] = ['empty', 'text', 'binary', 'large', 'link'].map((name) => ({ path: name, status: '??' }))
  await attachWorkingLineStats(root, entries, gitCommand)
  expect(entries.map((entry) => entry.lineStats)).toEqual([{ added: 0, removed: 0 }, { added: 2, removed: 0 }, undefined, undefined, undefined])
})
it('keeps ambiguous unborn mixed changes and failed numstat unknown', async () => {
  const root = await repo()
  await writeFile(path.join(root, 'new'), 'one\n')
  await gitCommand(root, ['add', '--', 'new'])
  const staged: Entry[] = [{ path: 'new', status: 'A ' }]
  await attachWorkingLineStats(root, staged, gitCommand)
  expect(staged[0].lineStats).toEqual({ added: 1, removed: 0 })
  const mixed: Entry[] = [{ path: 'new', status: 'AM' }]
  await attachWorkingLineStats(root, mixed, gitCommand)
  expect(mixed[0].lineStats).toBeUndefined()
  const failure: Entry[] = [{ path: 'new', status: 'A ' }]
  await attachWorkingLineStats(root, failure, async () => { throw new Error('unavailable') })
  expect(failure[0].lineStats).toBeUndefined()
})

it('respects binary attributes for untracked text and isolates linked-worktree baselines', async () => {
  const root = await repo()
  await writeFile(path.join(root, '.gitattributes'), '*.dat -diff\n')
  await writeFile(path.join(root, 'binary.dat'), 'one\ntwo\n')
  const binary: Entry[] = [{ path: 'binary.dat', status: '??' }]
  await attachWorkingLineStats(root, binary, gitCommand)
  expect(binary[0].lineStats).toBeUndefined()
  await writeFile(path.join(root, 'text'), 'base\n')
  await gitCommand(root, ['add', '--', 'text'])
  await gitCommand(root, ['commit', '-m', 'Base'])
  const linked = path.join(root, 'linked')
  await gitCommand(root, ['worktree', 'add', '--detach', linked, 'HEAD'])
  await writeFile(path.join(root, 'text'), 'base\nprimary\n')
  await writeFile(path.join(linked, 'text'), 'base\nlinked\nextra\n')
  const a: Entry[] = [{ path: 'text', status: ' M' }], b: Entry[] = [{ path: 'text', status: ' M' }]
  await Promise.all([attachWorkingLineStats(root, a, gitCommand), attachWorkingLineStats(linked, b, gitCommand)])
  expect(a[0].lineStats).toEqual({ added: 1, removed: 0 })
  expect(b[0].lineStats).toEqual({ added: 2, removed: 0 })
})
