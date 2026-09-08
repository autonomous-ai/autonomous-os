import { build } from 'esbuild'
await build({
  entryPoints: ['src/main/index.ts', 'src/main/preload.ts'],
  outdir: 'dist/main',
  outExtension: { '.js': '.cjs' },
  bundle: true,
  platform: 'node',
  format: 'cjs',
  target: 'node22',
  external: ['electron', 'node-pty'],
})
