import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createUpdateFeed, releaseVersion } from './update-feed.mjs'

test('release versions reject stale or malformed version inputs', () => {
  assert.equal(releaseVersion('0.0.22'), '0.0.22')
  for (const value of ['0.00.22', '0.0', 'v0.0.22', '0.0.22-dev', ''])
    assert.throws(() => releaseVersion(value))
})

test('static feed selects the exact current release and hashes the ZIP', () => {
  const feed = createUpdateFeed('0.0.22', 'https://example.test/arm64/0.0.22.zip', Buffer.from('abc'), new Date('2026-01-01T00:00:00Z'))
  assert.equal(feed.currentRelease, feed.releases[0].version)
  assert.deepEqual(feed.releases[0].updateTo, {
    version: '0.0.22', name: 'Autonomous Buddy 0.0.22',
    url: 'https://example.test/arm64/0.0.22.zip', pub_date: '2026-01-01T00:00:00.000Z',
    size: 3, sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
  })
})

test('feed rejects insecure URLs, mismatched versions and empty archives', () => {
  for (const url of ['http://example.test/0.0.22.zip', 'https://example.test/0.0.21.zip', 'https://example.test/0.0.22.dmg'])
    assert.throws(() => createUpdateFeed('0.0.22', url, Buffer.from('zip')))
  assert.throws(() => createUpdateFeed('0.0.22', 'https://example.test/0.0.22.zip', Buffer.alloc(0)))
})
