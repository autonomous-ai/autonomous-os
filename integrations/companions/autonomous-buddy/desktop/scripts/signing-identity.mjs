import { spawnSync } from 'node:child_process'

// -v lists usable identities. Anchor each complete line so a revoked/expired
// identity carrying an error suffix cannot be mistaken for a valid candidate.
export function parseDeveloperIdentities(output) {
  const identities = new Map()
  for (const line of output.split(/\r?\n/)) {
    const match = line.match(/^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"(Developer ID Application: [^"]+ \(([A-Z0-9]{10})\))"\s*$/)
    if (match) identities.set(match[1].toUpperCase(), { hash: match[1].toUpperCase(), name: match[2], team: match[3] })
  }
  return [...identities.values()]
}

export function parseInstalledTeam(output) {
  return output.match(/^TeamIdentifier=([A-Z0-9]{10})\s*$/m)?.[1]
}

export function selectSigningIdentity({ override, identities = [], installedTeam }) {
  const explicit = override?.trim()
  // A deliberate dash opts into ad-hoc; an empty variable still auto-detects.
  if (explicit) return { identity: explicit === '-' ? undefined : explicit, source: 'explicit' }
  if (!identities.length) return { identity: undefined, source: 'unavailable' }
  const candidates = installedTeam ? identities.filter((candidate) => candidate.team === installedTeam) : identities
  if (candidates.length === 1) return { identity: candidates[0].hash, source: 'detected', name: candidates[0].name }
  const reason = installedTeam && !candidates.length
    ? `No usable Developer ID Application identity matches installed Buddy team ${installedTeam}.`
    : 'Multiple usable Developer ID Application identities remain; automatic selection is ambiguous.'
  throw new Error(`${reason} Set DEV_ID_APP to the intended certificate name or SHA-1 fingerprint before packaging. No signing identity was changed.`)
}

export function discoverSigningIdentity({ override = process.env.DEV_ID_APP, run = spawnSync,
  installedApp = '/Applications/Autonomous Buddy.app', log = console.log } = {}) {
  if (override?.trim()) {
    const choice = selectSigningIdentity({ override })
    log(choice.identity ? 'Using explicit DEV_ID_APP signing identity.'
      : 'WARNING: Explicit ad-hoc signing selected; macOS Accessibility and Screen Recording permissions may need to be granted again after installation.')
    return choice.identity
  }
  const listed = run('security', ['find-identity', '-v', '-p', 'codesigning'], { encoding: 'utf8', timeout: 10000 })
  if (listed.error || listed.status !== 0) {
    throw new Error('Could not inspect usable signing identities. Unlock the signing keychain or set DEV_ID_APP explicitly; refusing an automatic ad-hoc downgrade.')
  }
  const installed = run('codesign', ['--display', '--verbose=4', installedApp], { encoding: 'utf8', timeout: 10000 })
  const installedTeam = installed.status === 0 ? parseInstalledTeam(`${installed.stdout ?? ''}\n${installed.stderr ?? ''}`) : undefined
  const choice = selectSigningIdentity({ identities: parseDeveloperIdentities(listed.stdout ?? ''), installedTeam })
  log(choice.identity ? `Using Developer ID identity: ${choice.name}`
    : 'WARNING: No usable Developer ID Application identity found; using ad-hoc signing. macOS Accessibility and Screen Recording permissions may need to be granted again after installation.')
  return choice.identity
}
