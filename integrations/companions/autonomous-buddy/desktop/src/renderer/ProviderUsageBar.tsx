import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, RefreshCw } from 'lucide-react'
import type { ProviderUsage } from '../shared/types'

function resetLabel(resetsAt: number | undefined, now: number) {
  if (!resetsAt) return ''
  const minutes = Math.ceil((resetsAt - now) / 60_000)
  if (minutes <= 0) return 'Reset due'
  if (minutes < 60) return `Resets in ${minutes}m`
  const hours = Math.floor(minutes / 60)
  return hours < 24
    ? `Resets in ${hours}h ${minutes % 60}m`
    : `Resets in ${Math.floor(hours / 24)}d ${hours % 24}h`
}

export function ProviderUsageBar() {
  const [usage, setUsage] = useState<ProviderUsage[]>([])
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => Date.now())
  const mounted = useRef(false)
  const inflight = useRef(false)
  const refresh = useCallback((force = false) => {
    if (inflight.current) return
    inflight.current = true
    return window.buddy
      .providerUsage(force)
      .then((result) => {
        if (mounted.current) {
          setUsage(result)
          setError('')
          setNow(Date.now())
        }
      })
      .catch((failure: unknown) => {
        if (mounted.current) setError(failure instanceof Error ? failure.message : String(failure))
      })
      .finally(() => {
        inflight.current = false
        if (mounted.current) setBusy(false)
      })
  }, [])
  useEffect(() => {
    mounted.current = true
    void refresh()
    const timer = window.setInterval(() => {
      setNow(Date.now())
      if (!inflight.current) setBusy(true)
      void refresh()
    }, 60_000)
    return () => {
      mounted.current = false
      window.clearInterval(timer)
    }
  }, [refresh])
  return (
    <section className="provider-usage-bar" aria-label="Agent usage limits">
      <span className="usage-heading">Usage</span>
      {(['claude', 'codex'] as const).map((provider) => {
        const value = usage.find((item) => item.provider === provider)
        const windows = value?.state === 'ready' && !error ? value.windows : []
        const message = error || value?.message || (!value ? 'Loading usage…' : 'Not available')
        const details = windows.length
          ? windows
              .map(
                (window) =>
                  `${window.label}: ${window.usedPercent}% used${window.resetsAt ? `; resets ${new Date(window.resetsAt).toLocaleString()}` : ''}`,
              )
              .join('\n')
          : message
        return (
          <div
            className={`provider-usage ${provider}`}
            key={provider}
            title={details}
            tabIndex={0}
            aria-label={`${provider === 'claude' ? 'Claude' : 'Codex'} usage: ${details}`}
          >
            <Bot size={12} />
            <strong>{provider === 'claude' ? 'Claude' : 'Codex'}</strong>
            {windows.length ? (
              windows.map((window) => (
                <span className="usage-window" key={window.label}>
                  <span className="usage-window-label">{window.label}</span>
                  <span
                    className="usage-meter"
                    role="meter"
                    aria-label={`${provider} ${window.label} used`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={window.usedPercent}
                  >
                    <i style={{ width: `${Math.max(0, Math.min(100, window.usedPercent))}%` }} />
                  </span>
                  <span className="usage-percent">{Math.round(window.usedPercent)}%</span>
                  {window.resetsAt && <small>{resetLabel(window.resetsAt, now)}</small>}
                </span>
              ))
            ) : (
              <span className="usage-unavailable">
                {!value && !error
                  ? 'Loading…'
                  : /sign.in|log.in|auth/i.test(message)
                    ? 'Sign in'
                    : 'Not available'}
              </span>
            )}
          </div>
        )
      })}
      <button
        className="icon-button usage-refresh"
        aria-label="Refresh agent usage"
        title="Refresh agent usage limits"
        disabled={busy}
        onClick={() => {
          setBusy(true)
          void refresh(true)
        }}
      >
        <RefreshCw size={12} className={busy ? 'usage-loading' : undefined} />
      </button>
    </section>
  )
}
