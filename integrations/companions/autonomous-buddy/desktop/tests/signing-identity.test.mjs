import test from 'node:test'
import assert from 'node:assert/strict'
import { parseDeveloperIdentities, parseInstalledTeam, selectSigningIdentity, discoverSigningIdentity } from '../scripts/signing-identity.mjs'

const hashA = 'A'.repeat(40)
const hashB = 'B'.repeat(40)
const line = (hash, team = 'TEAM000001') => `  1) ${hash} "Developer ID Application: Example Org (${team})"`

test('usable Developer ID parser excludes development, revoked, expired and malformed lines', () => {
  const output = [line(hashA), line(hashA), line(hashB) + ' (CSSMERR_TP_CERT_REVOKED)',
    line(hashB) + ' (CSSMERR_TP_CERT_EXPIRED)',
    `2) ${hashB} "Apple Development: Person (TEAM000001)"`,
    '  1 valid identities found', 'Developer ID Application: incomplete'].join('\n')
  assert.deepEqual(parseDeveloperIdentities(output), [{ hash: hashA, name: 'Developer ID Application: Example Org (TEAM000001)', team: 'TEAM000001' }])
})

test('explicit identity precedes discovery; blank still detects; dash explicitly selects ad-hoc', () => {
  assert.equal(selectSigningIdentity({ override: ' certificate fingerprint ' }).identity, 'certificate fingerprint')
  assert.equal(selectSigningIdentity({ override: '-', identities: parseDeveloperIdentities(line(hashA)) }).identity, undefined)
  assert.equal(selectSigningIdentity({ override: ' ', identities: parseDeveloperIdentities(line(hashA)) }).identity, hashA)
})

test('multiple teams preserve installed team or fail without a silent team switch', () => {
  const identities = parseDeveloperIdentities(line(hashA) + '\n' + line(hashB, 'TEAM000002'))
  assert.equal(selectSigningIdentity({ identities, installedTeam: 'TEAM000002' }).identity, hashB)
  assert.throws(() => selectSigningIdentity({ identities }), /ambiguous.*Set DEV_ID_APP/s)
  assert.throws(() => selectSigningIdentity({ identities, installedTeam: 'TEAM000003' }), /matches installed Buddy team/)
  assert.throws(() => selectSigningIdentity({ identities: [identities[0]], installedTeam: 'TEAM000002' }), /matches installed Buddy team/)
})

test('same-team duplicate certificates require an explicit certificate choice', () => {
  assert.throws(() => selectSigningIdentity({ identities: parseDeveloperIdentities(line(hashA) + '\n' + line(hashB)), installedTeam: 'TEAM000001' }), /ambiguous/)
})

test('installed team parser does not treat ad-hoc cdhash as a team', () => {
  assert.equal(parseInstalledTeam('Executable=app\nTeamIdentifier=TEAM000001\n'), 'TEAM000001')
  assert.equal(parseInstalledTeam('TeamIdentifier=not set\nCDHash=12345'), undefined)
})

test('discovery uses only read-only commands and selects matching installed team', () => {
  const calls = []
  const identity = discoverSigningIdentity({ override: '', log: () => {}, run: (command, args) => {
    calls.push([command, args])
    return command === 'security'
      ? { status: 0, stdout: line(hashA) + '\n' + line(hashB, 'TEAM000002') }
      : { status: 0, stderr: 'TeamIdentifier=TEAM000002\n' }
  } })
  assert.equal(identity, hashB)
  assert.deepEqual(calls[0], ['security', ['find-identity', '-v', '-p', 'codesigning']])
  assert.deepEqual(calls[1], ['codesign', ['--display', '--verbose=4', '/Applications/Autonomous Buddy.app']])
})

test('unavailable identity warns about permissions, lookup failure never silently downgrades', () => {
  const messages = []
  assert.equal(discoverSigningIdentity({ override: '', log: (message) => messages.push(message), run: (command) =>
    command === 'security' ? { status: 0, stdout: '0 valid identities found' } : { status: 1 } }), undefined)
  assert.match(messages[0], /permissions may need to be granted again/)
  assert.throws(() => discoverSigningIdentity({ override: '', log: () => {}, run: () => ({ status: 1 }) }), /refusing an automatic ad-hoc downgrade/)
  assert.equal(discoverSigningIdentity({ override: 'chosen', log: () => {}, run: () => { throw new Error('should not inspect') } }), 'chosen')
})
