import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import type { AppSettings, AppearanceSettings } from '../shared/types'
import { DEFAULT_SETTINGS } from '../shared/settings'

type AppearanceContextValue = {
  settings: AppSettings
  appearance: AppearanceSettings
  resolvedTheme: 'dark' | 'light'
  loading: boolean
  error?: string
  update: (patch: Partial<AppearanceSettings>) => Promise<void>
}
const AppearanceContext = createContext<AppearanceContextValue | null>(null)
const subscribeSystemTheme = (notify: () => void) => {
  const query = window.matchMedia('(prefers-color-scheme: dark)')
  query.addEventListener('change', notify)
  return () => query.removeEventListener('change', notify)
}
const systemDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const revision = useRef(0)
  const mounted = useRef(false)
  const dark = useSyncExternalStore(subscribeSystemTheme, systemDark)
  const appearance = settings.appearance
  const resolvedTheme = appearance.theme === 'system' ? dark ? 'dark' : 'light' : appearance.theme
  useEffect(() => {
    mounted.current = true
    const unsubscribe = window.buddy.onSettings((value) => {
      revision.current += 1
      setSettings(value)
      setLoading(false)
      setError(undefined)
    })
    const request = ++revision.current
    void window.buddy.settings().then((value) => {
      if (mounted.current && revision.current === request) {
        setSettings(value)
        setLoading(false)
        setError(undefined)
      }
    }).catch(() => {
      if (mounted.current && revision.current === request) {
        setError('Could not read saved appearance settings.')
        setLoading(false)
      }
    })
    return () => {
      mounted.current = false
      revision.current += 1
      unsubscribe()
    }
  }, [])
  const update = useCallback(async (patch: Partial<AppearanceSettings>) => {
    const request = ++revision.current
    try {
      const value = await window.buddy.updateAppearance(patch)
      if (mounted.current && revision.current === request) {
        setSettings(value)
        setError(undefined)
      }
    } catch (cause) {
      if (mounted.current && revision.current === request) setError('Could not save appearance settings.')
      throw cause
    }
  }, [])
  useLayoutEffect(() => {
    const root = document.documentElement
    root.dataset.theme = resolvedTheme
    root.style.setProperty('--font-ui', appearance.uiFont === 'mono'
      ? '"SF Mono", Menlo, Monaco, monospace'
      : appearance.uiFont === 'sans' ? 'Arial, Helvetica, sans-serif'
      : '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif')
    root.style.setProperty('--font-mono', `"${appearance.terminalFontFamily}", Menlo, Monaco, monospace`)
  }, [resolvedTheme, appearance.uiFont, appearance.terminalFontFamily])
  return <AppearanceContext.Provider value={{ settings, appearance, resolvedTheme, loading, error, update }}>{children}</AppearanceContext.Provider>
}

export function useAppearance(): AppearanceContextValue {
  const value = useContext(AppearanceContext)
  if (!value) throw new Error('AppearanceProvider is missing')
  return value
}
