import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// Explicit opt-in only: this creates real CLI conversations using local accounts.
// It never runs as part of unit or mocked Electron smoke tests.
if (process.env.BUDDY_LIVE_PROVIDER_TEST !== '1') throw new Error('Set BUDDY_LIVE_PROVIDER_TEST=1 to run authenticated CLI checks')
const root = await mkdtemp(join(tmpdir(), 'buddy-live-provider-'))
const file = join(root, 'manager.cjs')
await build({ entryPoints: ['src/main/manager.ts'], outfile: file, bundle: true, platform: 'node', format: 'cjs', external: ['node-pty'] })
const { Manager } = createRequire(import.meta.url)(file)
const manager = new Manager(join(root, 'state'), () => {})
const waitForTurn = async (id) => {
  const deadline = Date.now() + 90000
  while (Date.now() < deadline) {
    const detail = await manager.session(id)
    if (detail.session.status !== 'running') return detail
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  await manager.stop(id)
  throw new Error('Live provider turn timed out')
}
try {
  const directory = join(root, 'project')
  await mkdir(directory)
  const project = await manager.addProject(directory)
  for (const provider of ['codex', 'claude']) {
    const session = await manager.createSession({ projectId: project.id, worktreePath: project.path, provider, mode: 'structured', title: 'Buddy transport validation' })
    const marker = 'BUDDY_' + provider.toUpperCase() + '_ACK'
    await manager.send(session.id, 'This is a transport test. Do not use tools, read files or change anything. Respond with exactly ' + marker)
    const first = await waitForTurn(session.id)
    if (first.session.status !== 'completed' || !first.session.providerSessionId || !first.events.some((event) => event.text.includes(marker)))
      throw new Error(provider + ' did not complete the initial transport check: ' + first.session.status)
    const cursor = first.events.at(-1)?.seq ?? 0
    await manager.send(session.id, 'Do not use tools or access files. Recall the exact marker you returned in the previous turn and reply with that marker followed by _RESUMED, nothing else.')
    const second = await waitForTurn(session.id)
    if (second.session.status !== 'completed' || second.session.providerSessionId !== first.session.providerSessionId || !second.events.some((event) => event.seq > cursor && event.text.includes(marker + '_RESUMED')))
      throw new Error(provider + ' did not preserve follow-up context: ' + second.session.status)
    console.log('PASS: ' + provider + ' authenticated completion, no-approval launch and exact-session follow-up')
  }
} finally {
  manager.dispose()
  await rm(root, { recursive: true, force: true })
}
