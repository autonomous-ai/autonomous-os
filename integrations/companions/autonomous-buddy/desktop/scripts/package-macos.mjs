import { packager } from '@electron/packager'
import { execFileSync } from 'node:child_process'

if (process.platform !== 'darwin') throw new Error('This packaging target requires macOS')
const paths = await packager({
  dir: '.',
  name: 'Autonomous Buddy',
  platform: 'darwin',
  arch: process.arch,
  appBundleId: 'network.autonomous.ai.buddy.manager',
  appCategoryType: 'public.app-category.developer-tools',
  out: 'artifacts/package',
  overwrite: true,
  prune: true,
  // Keep node-pty and its spawn-helper on disk; no ASAR path translation is needed.
  asar: false,
  ignore: [/^\/(artifacts|src|tests|scripts)(\/|$)/, /^\/\.(git|vite)(\/|$)/],
})
for (const directory of paths) {
  const bundle = `${directory}/Autonomous Buddy.app`
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', bundle], { stdio: 'inherit' })
  execFileSync('codesign', ['--verify', '--deep', '--strict', bundle], { stdio: 'inherit' })
  console.log(`Built local ad-hoc signed app: ${bundle}`)
}
