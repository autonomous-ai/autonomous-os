import { packager } from '@electron/packager'
import { execFileSync } from 'node:child_process'
import { accessSync, constants, cpSync, mkdirSync, readdirSync } from 'node:fs'
import { resolve, join } from 'node:path'

if (process.platform !== 'darwin') throw new Error('This packaging target requires macOS')
const iconDirectory = resolve('artifacts/icon')
execFileSync('swift', ['scripts/generate-icon.swift', iconDirectory], { stdio: 'inherit' })
const icon = join(iconDirectory, 'AutonomousBuddy.icns')
execFileSync('iconutil', ['-c', 'icns', join(iconDirectory, 'AutonomousBuddy.iconset'), '-o', icon], {
  stdio: 'inherit',
})
const swiftDirectory = resolve('../macos')
const swiftArgs = ['build', '-c', 'release', '--arch', process.arch === 'arm64' ? 'arm64' : 'x86_64']
execFileSync('swift', swiftArgs, { cwd: swiftDirectory, stdio: 'inherit' })
const swiftOutput = execFileSync('swift', [...swiftArgs, '--show-bin-path'], {
  cwd: swiftDirectory,
  encoding: 'utf8',
}).trim()
const helper = join(swiftOutput, 'AutonomousBuddy')
accessSync(helper, constants.X_OK)
const identity = process.env.DEV_ID_APP?.trim()
const paths = await packager({
  dir: '.',
  name: 'Autonomous Buddy',
  platform: 'darwin',
  arch: process.arch,
  appBundleId: 'network.autonomous.ai.buddy.manager',
  appCategoryType: 'public.app-category.developer-tools',
  icon,
  extendInfo: {
    NSAppleEventsUsageDescription:
      'Autonomous Buddy uses Apple Events to control applications when requested.',
    NSLocalNetworkUsageDescription:
      'Autonomous Buddy connects to your paired Autonomous device on the local network.',
    NSBonjourServices: ['_autonomous._tcp'],
  },
  out: 'artifacts/package',
  overwrite: true,
  prune: true,
  // Keep node-pty and its spawn-helper on disk; no ASAR path translation is needed.
  asar: false,
  ignore: [/^\/(artifacts|src|tests|scripts)(\/|$)/, /^\/\.(git|vite)(\/|$)/],
  // Embed before signing so the nested executable is covered by the app seal.
  afterCopy: [
    async ({ buildPath }) => {
      const nativeDirectory = resolve(buildPath, '..', 'native')
      mkdirSync(nativeDirectory, { recursive: true })
      cpSync(helper, join(nativeDirectory, 'AutonomousBuddy'))
      // SwiftPM dependencies currently link statically; copy resource bundles if present.
      for (const entry of readdirSync(swiftOutput)) {
        if (entry.endsWith('.bundle'))
          cpSync(join(swiftOutput, entry), join(nativeDirectory, entry), { recursive: true })
      }
      execFileSync(
        'codesign',
        [
          '--force',
          '--sign',
          identity || '-',
          ...(identity ? ['--options', 'runtime', '--timestamp'] : []),
          '--identifier',
          'network.autonomous.ai.buddy',
          join(nativeDirectory, 'AutonomousBuddy'),
        ],
        { stdio: 'inherit' },
      )
    },
  ],
  ...(identity ? { osxSign: { identity, optionsForFile: () => ({ hardenedRuntime: true }) } } : {}),
})
for (const directory of paths) {
  const bundle = `${directory}/Autonomous Buddy.app`
  accessSync(`${bundle}/Contents/Resources/native/AutonomousBuddy`, constants.X_OK)
  if (!identity) execFileSync('codesign', ['--force', '--deep', '--sign', '-', bundle], { stdio: 'inherit' })
  execFileSync('codesign', ['--verify', '--deep', '--strict', bundle], { stdio: 'inherit' })
  console.log(`Built unified ${identity ? 'Developer ID' : 'ad-hoc'} signed app: ${bundle}`)
}
