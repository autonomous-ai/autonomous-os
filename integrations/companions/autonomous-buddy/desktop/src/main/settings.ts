import { mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { DEFAULT_SETTINGS, type AppSettings, type AppearanceSettings } from '../shared/settings'

export function validateAppearance(value: unknown): Partial<AppearanceSettings> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid appearance settings')
  const patch = value as Record<string, unknown>
  const enums: Record<string, string[]> = {
    theme: ['system', 'dark', 'light'], uiFont: ['system', 'sans', 'mono'],
    terminalFontFamily: ['SF Mono', 'Menlo', 'Monaco', 'monospace'], cursorStyle: ['block', 'bar', 'underline'],
  }
  const ranges: Record<string, [number, number]> = { zoom: [0.75, 1.5], terminalFontSize: [10, 24], terminalLineHeight: [1, 2] }
  for (const [key, setting] of Object.entries(patch)) {
    if (Object.hasOwn(enums, key)) {
      if (typeof setting !== 'string' || !enums[key].includes(setting)) throw new Error('Invalid appearance option: ' + key)
    } else if (Object.hasOwn(ranges, key)) {
      const [min, max] = ranges[key]
      if (typeof setting !== 'number' || !Number.isFinite(setting) || setting < min || setting > max || (key === 'terminalFontSize' && !Number.isInteger(setting)))
        throw new Error('Invalid appearance size: ' + key)
    } else if (['cursorBlink', 'showUsage', 'showStatusBar'].includes(key)) {
      if (typeof setting !== 'boolean') throw new Error('Invalid appearance toggle: ' + key)
    } else throw new Error('Unsupported appearance setting: ' + key)
  }
  return structuredClone(patch) as Partial<AppearanceSettings>
}
export class SettingsStore {
  private value: AppSettings = structuredClone(DEFAULT_SETTINGS)
  private readonly file: string
  constructor(directory: string) {
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    this.file = join(directory, 'settings.json')
    try {
      const stored = JSON.parse(readFileSync(this.file, 'utf8'))
      if (!stored || typeof stored !== 'object' || stored.version !== 1) throw new Error('Unsupported settings format')
      this.value.appearance = { ...this.value.appearance, ...validateAppearance(stored.appearance) }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error('Could not load Buddy settings; existing file was preserved')
    }
  }
  read(): AppSettings { return structuredClone(this.value) }
  update(patch: unknown): AppSettings {
    const appearance = { ...this.value.appearance, ...validateAppearance(patch) }
    const next = { version: 1, appearance }
    writeFileSync(this.file + '.tmp', JSON.stringify(next), { mode: 0o600 })
    renameSync(this.file + '.tmp', this.file)
    this.value = { appearance }
    return this.read()
  }
}
