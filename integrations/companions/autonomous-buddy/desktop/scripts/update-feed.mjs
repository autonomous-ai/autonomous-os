import { createHash } from 'node:crypto'
import { readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

export function releaseVersion(value) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(value))
    throw new Error(`Invalid Buddy release version: ${value}`)
  return value
}

export function createUpdateFeed(version, url, bytes, now = new Date()) {
  releaseVersion(version)
  const parsed = new URL(url)
  if (parsed.protocol !== 'https:' || !parsed.pathname.endsWith(`/${version}.zip`))
    throw new Error('Update URL must be HTTPS and end in /<version>.zip')
  if (!bytes.length) throw new Error('Update ZIP is empty')
  return {
    currentRelease: version,
    releases: [{ version, updateTo: {
      version, name: `Autonomous Buddy ${version}`, url,
      pub_date: now.toISOString(),
      sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length,
    } }],
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const [version, url, zip, output] = process.argv.slice(2)
  writeFileSync(output, `${JSON.stringify(createUpdateFeed(version, url, readFileSync(zip)), null, 2)}\n`)
}
