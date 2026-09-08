import { useCallback, useEffect, useState } from 'react'
import { Activity, Link, Monitor, Pause, Play, RefreshCw, ShieldCheck, Unlink } from 'lucide-react'
import type { NativeAction, NativeState } from '../shared/types'

export function ComputerPanel({ onError }: { onError: (error: unknown) => void }) {
  const [state, setState] = useState<NativeState | null>(null)
  const [busy, setBusy] = useState(false)
  const [host, setHost] = useState('')
  const refresh = useCallback(() => {
    void window.buddy.nativeStatus().then(setState).catch(onError)
  }, [onError])
  useEffect(() => {
    let active = true
    const unsubscribe = window.buddy.onNativeState((value) => {
      if (active) setState(value)
    })
    void window.buddy
      .nativeStatus()
      .then((value) => {
        if (active) setState(value)
      })
      .catch(onError)
    window.addEventListener('focus', refresh)
    return () => {
      active = false
      unsubscribe()
      window.removeEventListener('focus', refresh)
    }
  }, [onError, refresh])
  const action = async (name: NativeAction, params?: Record<string, unknown>) => {
    setBusy(true)
    try {
      await window.buddy.nativeAction(name, params)
      refresh()
    } catch (error) {
      onError(error)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <div className="dialog-icon">
        <Monitor size={24} />
      </div>
      <h2>Computer & device</h2>
      <p>Pair your device and manage computer control here in Autonomous Buddy.</p>
      <div className="provider-settings native-state">
        <div>
          <span>Computer control</span>
          <strong className={state?.available ? 'available' : 'muted'}>
            {!state ? 'Starting…' : state.available ? (state.paused ? 'Paused' : 'Ready') : 'Unavailable'}
          </strong>
        </div>
        <div>
          <span>Device</span>
          <span>{state?.paired ? state.deviceHost : 'Not paired'}</span>
        </div>
        <div>
          <span>Connection</span>
          <span>{state?.connection ?? 'Starting…'}</span>
        </div>
      </div>
      {(state?.error || state?.connectionError) && (
        <p className="panel-error" role="status">
          {state.error || state.connectionError}
        </p>
      )}
      {state && !state.available ? (
        <button
          className="secondary-button full-width"
          disabled={busy}
          onClick={() => void action('restart')}
        >
          <RefreshCw size={14} /> Restart computer control
        </button>
      ) : (
        <>
          {state?.paired ? (
            <div className="native-actions">
              <button
                className="secondary-button"
                disabled={busy}
                onClick={() => void action('pause', { paused: !state.paused })}
              >
                {state.paused ? <Play size={14} /> : <Pause size={14} />}
                {state.paused ? 'Resume control' : 'Pause control'}
              </button>
              <button className="secondary-button" disabled={busy} onClick={() => void action('unpair')}>
                <Unlink size={14} /> Unpair device
              </button>
            </div>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void action('pair', host.trim() ? { host: host.trim() } : {})
              }}
            >
              <label htmlFor="device-host">
                Device address <span className="muted">optional</span>
              </label>
              <input
                id="device-host"
                placeholder="lamp-xxxx.local"
                value={host}
                onChange={(event) => setHost(event.target.value)}
                list="nearby-devices"
              />
              <datalist id="nearby-devices">
                {state?.devices.map((device) => (
                  <option key={device.host} value={device.host}>
                    {device.name}
                  </option>
                ))}
              </datalist>
              <button className="primary-button full-width" disabled={busy || !state?.available}>
                <Link size={14} /> Pair a device
              </button>
            </form>
          )}
          <label>Mac permissions</label>
          <div className="provider-settings">
            <div>
              <span>Accessibility</span>
              <span className={state?.accessibility ? 'available' : 'muted'}>
                {state?.accessibility ? 'Granted' : 'Required for input control'}
              </span>
            </div>
            <div>
              <span>Screen Recording</span>
              <span className={state?.screenRecording ? 'available' : 'muted'}>
                {state?.screenRecording ? 'Granted' : 'Required for screenshots'}
              </span>
            </div>
          </div>
          <div className="native-actions">
            <button
              className="secondary-button"
              disabled={busy || !state?.available}
              onClick={() => void action('permissions')}
            >
              <ShieldCheck size={14} /> Manage permissions
            </button>
            <button
              className="secondary-button"
              disabled={busy || !state?.available}
              onClick={() => void action('activity')}
            >
              <Activity size={14} /> Activity
            </button>
          </div>
        </>
      )}
    </>
  )
}
