import assert from 'node:assert/strict'
import { chmodSync, lstatSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { makeUpdateOwnerWritable, verifyUpdateOwnerWritable } from './update-permissions.mjs'

test('makes copied resources writable without changing executable bits or linked external files', () => {
  const temp = mkdtempSync(join(tmpdir(), 'buddy-permissions-'))
  try {
    const app = join(temp, 'Buddy.app')
    const resources = join(app, 'Resources')
    mkdirSync(resources, { recursive: true })
    const manifest = join(resources, 'PrivacyInfo.xcprivacy')
    const binary = join(app, 'helper')
    const external = join(temp, 'external')
    for (const file of [manifest, binary, external]) writeFileSync(file, 'fixture')
    chmodSync(manifest, 0o444)
    chmodSync(binary, 0o555)
    chmodSync(external, 0o444)
    symlinkSync(external, join(resources, 'external-link'))
    symlinkSync('../Resources', join(resources, 'cycle'))
    chmodSync(resources, 0o555)
    assert.throws(() => verifyUpdateOwnerWritable(app), /not owner-writable.*Rebuild and sign/)
    makeUpdateOwnerWritable(app)
    assert.doesNotThrow(() => verifyUpdateOwnerWritable(app))
    assert.equal(lstatSync(manifest).mode & 0o777, 0o644)
    assert.equal(lstatSync(binary).mode & 0o777, 0o755)
    assert.equal(lstatSync(resources).mode & 0o777, 0o755)
    assert.equal(lstatSync(external).mode & 0o777, 0o444)
    assert.equal(lstatSync(join(resources, 'external-link')).isSymbolicLink(), true)
  } finally {
    rmSync(temp, { recursive: true, force: true })
  }
})
