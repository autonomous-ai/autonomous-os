export interface AppearanceSettings {
  theme: 'system' | 'dark' | 'light'
  zoom: number
  uiFont: 'system' | 'sans' | 'mono'
  terminalFontFamily: 'SF Mono' | 'Menlo' | 'Monaco' | 'monospace'
  terminalFontSize: number
  terminalLineHeight: number
  cursorBlink: boolean
  cursorStyle: 'block' | 'bar' | 'underline'
  showUsage: boolean
  showStatusBar: boolean
}
export interface AppSettings { appearance: AppearanceSettings }
export const DEFAULT_APPEARANCE: AppearanceSettings = {
  theme: 'system', zoom: 1, uiFont: 'system', terminalFontFamily: 'SF Mono',
  terminalFontSize: 13, terminalLineHeight: 1.4, cursorBlink: true, cursorStyle: 'block',
  showUsage: true, showStatusBar: true,
}
export const DEFAULT_SETTINGS: AppSettings = { appearance: DEFAULT_APPEARANCE }
