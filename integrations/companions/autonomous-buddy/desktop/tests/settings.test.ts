import { afterEach, expect, it } from 'vitest'
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { SettingsStore, validateAppearance } from '../src/main/settings'
import { DEFAULT_SETTINGS } from '../src/shared/settings'
const roots: string[] = []
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))) })
async function directory() { const root = await mkdtemp(join(tmpdir(), 'buddy-settings-')); roots.push(root); return root }
it('persists appearance changes without resetting other choices', async () => {
  const root = await directory()
  const store = new SettingsStore(root)
  expect(store.read()).toEqual(DEFAULT_SETTINGS)
  store.update({ theme: 'light', terminalFontSize: 18 })
  store.update({ zoom: 1.1 })
  expect(new SettingsStore(root).read().appearance).toMatchObject({ theme: 'light', terminalFontSize: 18, zoom: 1.1, cursorBlink: true })
  const detached = store.read()
  detached.appearance.zoom = 1.5
  expect(store.read().appearance.zoom).toBe(1.1)
})
it('rejects unsupported and invalid settings without modifying disk', async () => {
  const root = await directory()
  const store = new SettingsStore(root)
  store.update({ theme: 'dark' })
  const before = await readFile(join(root, 'settings.json'), 'utf8')
  for (const patch of [{ zoom: NaN }, { zoom: 0.1 }, { terminalFontSize: 12.5 }, { cursorBlink: 'yes' }, { theme: 'invalid' }, JSON.parse('{"__proto__":{}}')])
    expect(() => store.update(patch)).toThrow()
  expect(await readFile(join(root, 'settings.json'), 'utf8')).toBe(before)
  expect(() => validateAppearance([])).toThrow()
})
it('preserves corrupt stored settings instead of silently replacing them', async () => {
  const root = await directory()
  await writeFile(join(root, 'settings.json'), 'broken')
  expect(() => new SettingsStore(root)).toThrow('preserved')
  expect(await readFile(join(root, 'settings.json'), 'utf8')).toBe('broken')
})
